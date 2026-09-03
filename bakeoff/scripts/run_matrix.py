#!/usr/bin/env python3
"""Run a task set against the eval arms and write one record per cell.

This is the collection driver. `smoke_test.py` is the Phase 0c go/no-go gate
and stays that; this is what Gate 1 and everything after it actually run.

The order of operations is the point, and it is not negotiable: every task is
validated OFFLINE -- images built, start state materialized, red-before and
green-after proven inside the pinned image -- before the proxy starts and
before one token is spent. Six of six "model failures" so far have been
harness defects, and by the time a record exists the tokens are paid for.

Usage:
    python scripts/run_matrix.py --preflight-only
    python scripts/run_matrix.py --mode offline
    python scripts/run_matrix.py --mode live --repeats 1
    python scripts/run_matrix.py --mode live --tasks click-3360-... --models gemma-4-31b

Exit codes:
    0  every cell in the plan has a record
    1  something is wrong -- preflight refused, cells stranded, or records that
       cannot be interpreted
    2  stopped early and cleanly, work remains, re-invoking resumes it
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

# Module scope on purpose. `bakeoff.container` imports `docker` and NOT
# litellm, so it does not undo the reason `bakeoff.runner`,
# `bakeoff.claude_runner` and `bakeoff.proxy_callback` are deferred into
# `run_cell`: `--preflight-only` has to stay off litellm.
from bakeoff.container import fresh_tree  # noqa: E402
from bakeoff.images import (  # noqa: E402
    ImageError,
    build_base_images,
    build_proxy_image,
    build_task_image,
    image_entrypoint,
)
from bakeoff.matrix import (  # noqa: E402
    INFRA_ABORT_STREAK,
    INFRA_ABORT_STREAK_PER_ARM,
    StreakTracker,
    adjacent_repeats,
    infra_problems,
    matrix_order,
    plan_resume,
    to_task_spec,
    write_json,
)
from bakeoff.preflight import (  # noqa: E402
    preflight,
    preflight_cache_key,
    verdict_matches_key,
)
from bakeoff.proxy import (  # noqa: E402
    EVAL_ARMS,
    SSO_LOGIN_HINT,
    CredentialWindow,
    Proxy,
    credential_stop,
    credential_window,
    proxy_environment,
)
from bakeoff.session import effective_config  # noqa: E402
from bakeoff.tasks import (  # noqa: E402
    TaskError,
    load_task_set_with_refusals,
    materialize,
    refusal_warnings,
    task_runtime,
)

# Under $HOME, never /var/folders: the Docker VM on macOS mounts $HOME only,
# and a repo bind-mounted from elsewhere appears inside the container as a
# silently EMPTY directory -- the agent then edits nothing and it is recorded
# as the model's failure.
CACHE = Path.home() / ".cache" / "bakeoff"
DEFAULT_TASK_SET = REPO / "taskset"

# Everything a cell costs that is NOT inside the agent's wall_clock_timeout_s:
# materializing a fresh tree, starting the container, the final force_capture,
# the record write. Added to the timeout when asking whether a cell fits inside
# the credential window, because a margin of the timeout alone would clear a
# cell that then dies in its tail with the tokens already spent.
#
# NOT measured -- no stored record carries it, since `time.wall_clock_total_ms`
# covers the agent only. 120 s is a deliberate over-estimate. `cell_wall_s` is
# now recorded per row precisely so the next version of this number can be a
# measurement instead of a guess.
CELL_OVERHEAD_S = 120


def _base_label(key: tuple[str, str]) -> str:
    """One base, named the way an operator would say it: `python 3.12`.

    A tuple's repr in a refusal message (`('node', '22')`) is readable but
    reads as a data structure rather than as the thing that failed, and these
    messages are the whole product of a gate that refuses an invocation.
    """
    return " ".join(key)


def _short_base_label(key) -> str:
    """The build banner's form: `py3.12`, `node22`.

    Kept distinct from `_base_label` so the python line reads exactly as it did
    before node existed -- `base py3.12  sha256:...` is what every stored
    preflight log and every runbook shows.
    """
    runtime, version = key
    return f"py{version}" if runtime == "python" else f"{runtime}{version}"


def base_claude_version(image: str) -> str:
    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "claude --version"],
        capture_output=True, text=True,
    )
    return probe.stdout.strip().split()[0] if probe.returncode == 0 else ""


def assert_one_agent(bases: dict[tuple[str, str], str]) -> str:
    """Every base ships the same Claude Code, or nothing runs.

    What this replaces. `preflight`'s `expected_claude_version` refusal says
    "two tasks would run different agents and the comparison across them is
    not one". Handing each task its OWN base's version keeps the useful half
    of that -- it still catches a task image whose `image.pip`/`image.build`
    clobbered `claude` -- and loses exactly one thing: cross-BASE agreement.
    Nothing downstream restores it. `Versions.claude_code` is read from the
    transcript, per run, and no reader compares it across tasks, so two bases
    on two agents would pass every gate and be invisible in the log.

    Probing ONE base and passing that string to everyone would also restore
    it, and is rejected: it makes one base arbitrarily canonical, and it
    surfaces a build-level defect as a per-task preflight failure late in the
    run rather than before the first task image is built.

    EMPTY IS A REFUSAL, and it is checked before agreement.
    `base_claude_version` returns "" when its `docker run` exits non-zero, so
    "every base failed to answer" collapses to the single value "" -- which
    reads as agreement, and is falsy, so preflight's
    `elif expected_claude_version and ...` guard never fires and the
    agent-version check is off for every task in the matrix. A gate that
    disables another gate by returning its own failure is worse than no gate.

    It used to be true that they always agree, because there was one
    `ARG CLAUDE_CODE_VERSION` in one file. Broadening 7 added a SECOND base
    Dockerfile with its own copy of that line, which is where this check earns
    the most -- and `tests/test_images.py` asserts the two pins are equal as a
    cheap offline first line, because this one needs a daemon and two builds.

    Nothing here DECIDES on the key -- it is carried into the message and
    nowhere else. Broadening 5 keyed `bases` by python version; this takes
    `(runtime, version)`, and the widening is a type annotation plus how a key
    is spelled in a refusal.
    """
    seen = {key: base_claude_version(image)
            for key, image in sorted(bases.items())}
    rendered = ", ".join(
        f"{_base_label(k)} -> {c or '<no answer>'}" for k, c in seen.items()
    )
    blank = sorted(_base_label(k) for k, c in seen.items() if not c)
    if blank:
        raise ImageError(
            f"`claude --version` answered nothing in the base image(s) for "
            f"{', '.join(blank)}: {rendered}. An empty version is not "
            "agreement -- it is falsy, so preflight's expected_claude_version "
            "check would be silently disabled for every task in this matrix. "
            "Rebuild the bases."
        )
    distinct = set(seen.values())
    if len(distinct) > 1:
        raise ImageError(
            f"the base images do not all ship the same Claude Code: {rendered}. "
            "Section 5.4 holds the agent identical across everything being "
            "compared, and no per-task check can see this -- each task is "
            "gated against its own base. Rebuild them all, and check that "
            "docker/eval-agent.Dockerfile and docker/eval-agent-node.Dockerfile "
            "still carry the same ARG CLAUDE_CODE_VERSION."
        )
    return distinct.pop()


def prepare_bases(tasks) -> tuple[dict[tuple[str, str], str], str]:
    """The base images this task set needs, built and checked.

    Called by `main` BEFORE `resolve_tasks` and before the proxy image is
    built, because both of the refusals below are free and offline while
    everything after them costs an image build or a token.

    The set comes from the TASKS, never from `tasks._PYTHON_VERSIONS` /
    `_NODE_VERSIONS`: building every allowed runtime on every invocation is
    several image builds for a task set that uses one, in the half of the run
    documented as free.

    `task_runtime`, never `task.image.python` -- EVERY task carries a python
    version, node ones included (it is a dataclass default nobody read), so
    indexing on that key hands a vitest task the python base. That image builds
    and that container starts; the runner is simply absent, and it reaches the
    model as exit 127 on every arm.
    """
    keys = sorted({task_runtime(task) for task in tasks})
    print(f"\nbuilding {len(keys)} base image(s): "
          f"{', '.join(_base_label(k) for k in keys)} ...", flush=True)
    bases = build_base_images(REPO, keys)
    expected = assert_one_agent(bases)
    for key in keys:
        print(f"base {_short_base_label(key)}  {bases[key][:19]}...  "
              f"claude {expected}")
    return bases, expected


def suite_time_line(verdict: dict) -> str:
    """One line: what the gate's bounded runs cost, against the bound they carried.

    The bound comes from `evidence["suite_timeout_s"]`, which
    `_Runner.last_timeout_s` read off the ARGV, and never from
    `task.budget.suite_timeout_s`, which is in scope at both call sites.
    They can disagree, and printing the configured number beside a measured
    duration is configuration reported as observation -- in the one line
    whose entire purpose is to let an author compare the two.

    The denominator is the SCHEMA's size, not this task's worst case, and
    the wording says so: a node task can make at most eight of these nine
    (the bare-runner probe is pytest-only) and a task with an explicit
    `tests.p2p` never makes the scoped one. "2 of the schema's 9" is a
    statement about the key set; the ceiling for a given task is not
    derivable from a verdict and is not claimed here.

    Three answers, because the absences differ. A verdict written before
    the bump in this commit has no durations at all and says so with the
    version that wrote it; a verdict whose runs are all null is a gate that
    started nothing; anything else is a measurement.
    """
    evidence = verdict.get("evidence") or {}
    durations = evidence.get("bounded_run_durations_s")
    if not isinstance(durations, dict):
        return ("suite time  not recorded: written by preflight_version "
                f"{verdict.get('preflight_version') or '?'}")
    slowest = evidence.get("bounded_run_duration_max_s")
    if slowest is None:
        return "suite time  no bounded command ran"
    measured = [v for v in durations.values() if v is not None]
    bound = evidence.get("suite_timeout_s")
    return (
        f"suite time  slowest {slowest:.1f}s of the "
        + (f"{bound}s bound" if bound is not None else "unrecorded bound")
        + f"; {sum(measured):.1f}s over {len(measured)} of the schema's "
        + f"{len(durations)} bounded runs"
    )


def _measured_total(verdict: dict) -> float:
    """The bounded seconds a verdict recorded, 0.0 when it recorded none.

    Reads the same key `suite_time_line` reads, with the same guard --
    `isinstance(durations, dict)`, not a bare `.get` fallback, because
    `... or {}` only rescues a FALSY non-dict and a hand-edited blob whose
    `bounded_run_durations_s` is a list or string would otherwise raise out
    of `resolve_tasks` on the very line after `suite_time_line` printed its
    "not recorded" sentence for the same blob. A reporting affordance may
    not be the thing that stops a matrix.
    """
    evidence = verdict.get("evidence") or {}
    durations = evidence.get("bounded_run_durations_s")
    if not isinstance(durations, dict):
        return 0.0
    return sum(v for v in durations.values() if v is not None)


def cached_verdict(cache: Path, task_id: str, key: str) -> dict | None:
    """The stored verdict blob for THIS key, or `None` -- never a raise.

    `<cache>/preflight/<task_id>.json` is filed under the task id alone and
    is written BEFORE the `ok` test, while `preflight.json` -- the cache
    that decides a PASS -- is written only on PASS. So a --force-preflight
    run that NO-GOes leaves a stale PASS key in the one file and its own
    NO-GO blob in the other, and a later warm invocation would read seconds
    from a verdict that refused the task. `preflight.verdict_matches_key`
    plus `ok is True` is what catches it.

    A miss on anything -- absent, unreadable, not JSON, wrong key, not a
    PASS -- is `None`, and the caller prints nothing.
    """
    path = Path(cache) / "preflight" / f"{task_id}.json"
    try:
        blob = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(blob, dict) or blob.get("ok") is not True:
        return None
    return blob if verdict_matches_key(blob, key) else None


def resolve_tasks(tasks, bases, expected_version, cache, force):
    """Build each task's image, materialize its start state, and preflight.

    Returns (resolved, failures). `resolved` maps task_id to a dict carrying
    the image id and the start sha -- everything a run needs that is not in
    the manifest.

    `bases` maps a `(runtime, version)` pair to a base image id, and the base
    is chosen PER TASK by `task_runtime`. Handing every task the first entry
    still builds and still runs -- under an interpreter, or a runtime, the task
    was not cut for, with only preflight's read-back saying so.

    The preflight cache is keyed by `preflight.preflight_cache_key` --
    (manifest digest, image id, start sha, PREFLIGHT_VERSION).
    Re-validating 80 tasks on every resume would turn a 30-second restart
    into half an hour, and a gate that is expensive to run is a gate that
    gets skipped -- the same argument verify_logger.py makes for staying
    free.

    The preflight tree is allocated by `container.fresh_tree` and removed when
    the resolution ends. The shape it replaces -- one stable path per task,
    `rmtree`'d at the TOP of the next invocation -- is the stale bind mount
    measured 2026-09-02: the Docker VM serves the directory it cached for a
    mount source, so the second container on that path saw an EMPTY `/repo`
    (`[files], [], [files], []` over four cycles). On a vitest task that is
    `No test files found` at exit 1, the exit a genuinely red suite gives, so
    every second `--preflight-only` invocation gated on nothing at all.
    """
    cache_path = cache / "preflight.json"
    cached = {}
    if cache_path.exists() and not force:
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            cached = {}

    resolved: dict[str, dict] = {}
    failures: list[str] = []
    gate_seconds = 0.0
    gate_tasks = 0
    gate_cached = 0
    for task in tasks:
        print(f"\n=== {task.task_id} ===", flush=True)
        image = build_task_image(task, bases[task_runtime(task)],
                                 cache / "build", cache)
        entrypoint = image_entrypoint(image)
        if entrypoint:
            failures.append(
                f"{task.task_id}: the task image declares ENTRYPOINT "
                f"{entrypoint!r}; RunContainer's `sleep infinity` would become "
                "an argument to it and the container would exit immediately"
            )
            continue

        work = fresh_tree(cache / "preflight-tree" / task.task_id)
        try:
            start_sha = materialize(task, work / "repo", cache)
            print(f"image     {image[:19]}...")
            print(f"start_sha {start_sha}  (base {task.base_sha[:12]} + test half)")
            if not task.declared_start_sha:
                print(f"          pin it: repo.start_sha: {start_sha}")

            key = preflight_cache_key(task, image, start_sha)
            if cached.get(task.task_id, {}).get("key") == key:
                print("preflight cached PASS (--force-preflight to re-run)")
                blob = cached_verdict(cache, task.task_id, key)
                if blob is not None:
                    print(f"          {suite_time_line(blob)}")
                    gate_seconds += _measured_total(blob)
                    gate_cached += 1
                    gate_tasks += 1
                resolved[task.task_id] = {"image": image,
                                          "start_sha": start_sha}
                continue

            result = preflight(
                task, image=image, repo_path=work / "repo", start_sha=start_sha,
                expected_claude_version=expected_version,
            )
            blob = result.to_dict()
            write_json(cache / "preflight" / f"{task.task_id}.json", blob)
            # Totalled BEFORE the verdict branch, and the NO-GO branch prints
            # its line too. A task refused because a bounded run hit `timeout`
            # burned the FULL suite_timeout_s, up to nine times -- the largest
            # single contributor to the number this total exists to check the
            # one-hour SSO window against, and the exact task whose bound an
            # author is about to resize. A total that quietly meant "the
            # tasks that passed" is the defect the caveat line below warns
            # about, one line up.
            gate_seconds += _measured_total(blob)
            gate_tasks += 1
            if not result.ok:
                print("preflight NO-GO")
                for problem in result.problems:
                    print(f"  - {problem}")
                print(f"          {suite_time_line(blob)}")
                failures.append(
                    f"{task.task_id}: {len(result.problems)} problem(s)")
                continue
            print(
                "preflight PASS  f2p red at start, green after the reference; "
                "p2p green both ways; tree clean"
            )
            print(f"          {suite_time_line(blob)}")
            cached[task.task_id] = {"key": key}
            resolved[task.task_id] = {"image": image, "start_sha": start_sha}
        finally:
            # The tree does not outlive the resolution, and both `continue`
            # paths above pass through here. Removing it is safe only because
            # `fresh_tree` will never reissue the name -- which is the whole
            # difference from the shape this replaces.
            shutil.rmtree(work, ignore_errors=True)

    if gate_tasks:
        print(
            f"\ngate suite time  {gate_seconds:.1f}s across {gate_tasks} task(s)"
            + (f", {gate_cached} from cached verdicts" if gate_cached else "")
        )
        print(
            "                 bounded runs only -- image build, materialization "
            "and container start are NOT in this number"
        )

    write_json(cache_path, cached)
    return resolved, failures


def artifacts_root(cache: Path, stamp: str) -> Path:
    """Per-invocation, like the wire directory beside it.

    `run_cell` rmtree'd a cell's directory before every attempt and the path was
    a pure function of the cell, so a later invocation deleted an earlier run's
    artifacts and left that record's `artifacts.*` pointing at the replacement.
    The record survived intact; what it pointed at did not, and WireLogger's
    "x" cannot catch it because the delete precedes execute_run's mkdir.

    It also saves `record.unwritten.json`, which `_write_or_strand` writes into
    artifacts_root -- the one artifact the "a run always produces a record"
    invariant exists to preserve, and the next attempt on that cell deleted it.

    Records store absolute paths, so a matrix resumed under a new stamp reads
    back correctly across both.
    """
    return cache / "artifacts" / stamp


def run_cell(cell, task, resolved, args, event_log, wire_dir, network, artifacts,
             collection_id, invocation_stamp):
    from bakeoff.claude_runner import ClaudeCodeConfig
    from bakeoff.proxy_callback import unattributed_count
    from bakeoff.runner import execute_run

    run_root = artifacts / f"{cell.task_id}-{cell.model}-{cell.sample_index}"
    repo = fresh_tree(run_root / "tree") / "repo"
    # No rmtree. The artifacts root is per-invocation AND the tree leaf is per
    # materialization, so this path is new every time and there is nothing to
    # clear -- and a delete keyed on a path with no attempt_number in it
    # becomes the artifacts-collision bug the moment run-level retry lands
    # (TASKS.md P1): two attempts of one cell would share a stamp AND a
    # directory, and the second would delete the first's artifacts and its
    # record.unwritten.json while both records live in the log. The leaf is
    # also what keeps a retry off a path a container has already mounted: the
    # Docker VM serves a replaced host inode from its cache, EMPTY (measured
    # 2026-09-02, `[files], [], [files], []` over four cycles).
    # A fresh tree per run. Sharing one would let a later sample start from
    # an earlier sample's dirty state and report a diff its own agent never
    # made (spec section 5.1: fresh container per sample, no state bleed).
    start_sha = materialize(task, repo, CACHE)
    if start_sha != resolved["start_sha"]:
        raise TaskError(
            f"{cell.label}: materialization produced {start_sha}, preflight "
            f"validated {resolved['start_sha']}"
        )

    config = ClaudeCodeConfig(
        model=cell.model,
        base_url="http://litellm:4000",
        # The proxy has no master_key, so any value passes. Sent anyway:
        # Claude Code requires one, and an empty token changes the request
        # shape rather than the authorization result.
        auth_token="sk-eval-local",
        settings_path="/eval/eval_settings.json",
        config_dir="/eval/claude-config",
        max_turns=task.budget.max_turns,
        wall_clock_timeout_s=task.budget.wall_clock_timeout_s,
    )
    before = unattributed_count(wire_dir)
    record = execute_run(
        task=to_task_spec(task, resolved["image"], start_sha),
        model=cell.model,
        sample_index=cell.sample_index,
        config=config,
        event_log=event_log,
        repo_path=str(repo),
        artifacts_root=run_root / "artifacts",
        collection_id=collection_id,
        invocation_stamp=invocation_stamp,
        network=network,
        proxy_wire_dir=wire_dir,
        # Section 5.2's highest-risk contamination source. Claude Code does
        # not fail on a --settings path that is not there: it starts with none
        # of the pinned settings, the agent cannot edit anything, every arm
        # lands no diff, and a one-line omission here reads as four capability
        # findings.
        settings_host_path=REPO / "config" / "eval_settings.json",
    )

    config_dump = {}
    if record.artifacts.container_stdout:
        config_dump = effective_config(Path(record.artifacts.container_stdout))
    write_json(run_root / "artifacts" / "effective_config.json", config_dump)

    unattributed = unattributed_count(wire_dir) - before
    kept = None
    if args.keep:
        kept = str(repo)
        print(f"  kept {repo}")
    else:
        # The whole leaf, not just `repo`: the leaf is the allocation, and
        # leaving it behind is the husk `fresh_tree` cannot collect under a
        # key nothing allocates under again.
        shutil.rmtree(repo.parent, ignore_errors=True)
    return record, config_dump, unattributed, kept


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], default="live")
    parser.add_argument("--task-set", default=str(DEFAULT_TASK_SET))
    parser.add_argument("--tasks", help="comma-separated task_ids (default: all)")
    parser.add_argument("--models", help="comma-separated arms (default: the four)")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260812)
    parser.add_argument("--event-log", default=str(CACHE / "eventlog"))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--force-preflight", action="store_true")
    parser.add_argument(
        "--allow-mixed-images",
        action="store_true",
        help="resume even though stored records ran on a different image",
    )
    parser.add_argument("--keep", action="store_true")
    # Thresholds, because they are a real operational tradeoff and not a
    # constant anyone should have to edit code to change. Stored records already
    # carry api_throttle exclusions, and TASKS.md P1 says throttling becomes
    # routine at scale -- so an unlucky burst of three unrecovered throttles on
    # one arm would end a multi-day matrix. The defaults are tuned for the
    # credential case; a heavily throttled run should raise them, not lose the
    # gate.
    parser.add_argument(
        "--abort-streak", type=int, default=INFRA_ABORT_STREAK,
        help="consecutive uninterpretable cells, any arm, before stopping",
    )
    parser.add_argument(
        "--abort-streak-per-arm", type=int, default=INFRA_ABORT_STREAK_PER_ARM,
        help="consecutive uninterpretable cells on ONE arm before stopping; "
             "this is the one that fires, because section 5.7 interleaves arms",
    )
    args = parser.parse_args()

    from bakeoff.eventlog import EventLog

    selected = args.tasks.split(",") if args.tasks else None
    try:
        tasks, refusals = load_task_set_with_refusals(
            Path(args.task_set), only=selected
        )
    except TaskError as exc:
        print(f"task set: {exc}")
        return 1

    by_id = {task.task_id: task for task in tasks}
    models = args.models.split(",") if args.models else list(EVAL_ARMS)
    stamp = subprocess.run(
        ["date", "-u", "+%Y%m%dT%H%M%SZ"], check=True, capture_output=True, text=True
    ).stdout.strip()

    print(f"task set  {args.task_set}  ({len(tasks)} task(s), commit "
          f"{tasks[0].task_set_commit or 'not a git repo'})")
    print(f"arms      {', '.join(models)}")
    print(f"repeats   {args.repeats}   seed {args.seed}")
    print(f"event log {args.event_log}")

    for line in refusal_warnings(refusals, root=Path(args.task_set),
                                 selected=set(selected or ())):
        print(line)

    try:
        bases, expected_version = prepare_bases(tasks)
    except ImageError as exc:
        # Same shape as the preflight NO-GO below: nothing was run, nothing
        # was spent, and the operator gets the reason rather than a traceback.
        print(f"\nBASE IMAGE NO-GO -- nothing was run and nothing was spent\n  {exc}")
        return 1

    resolved, failures = resolve_tasks(
        tasks, bases, expected_version, CACHE, args.force_preflight
    )
    if failures:
        print("\nPREFLIGHT NO-GO -- nothing was run and nothing was spent")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nA task that cannot be shown to be red before the reference fix "
            "and green after it scores every arm on evidence that does not "
            "distinguish a solved run from an idle one."
        )
        return 1
    print(f"\npreflight PASS on all {len(resolved)} task(s)")
    if args.preflight_only:
        return 0

    order = matrix_order(list(resolved), models, args.repeats, args.seed)
    event_log = EventLog(Path(args.event_log))
    resume = plan_resume(
        order,
        event_log,
        versions={t.task_id: t.task_version for t in tasks},
        images={tid: r["image"] for tid, r in resolved.items()},
    )

    print(f"\norder     {len(order)} cell(s), round-major, shuffled per round")
    adjacent = adjacent_repeats(order)
    print(
        "          no (task, arm) runs back-to-back"
        if not adjacent
        else f"          WARNING: {len(adjacent)} back-to-back repeat(s): {adjacent}"
    )
    if resume.done:
        print(f"resume    {len(resume.done)} cell(s) already written, skipping")
    if resume.excluded:
        print(f"\n{len(resume.excluded)} already-written cell(s) hold an exclusion:")
        for line in resume.excluded[:20]:
            print(f"  - {line}")
        if len(resume.excluded) > 20:
            print(f"  ... and {len(resume.excluded) - 20} more")
        print(
            "These are holes in the matrix, not work to redo: the records are "
            "valid and the log is append-only, run_id is deterministic, and "
            "nothing increments attempt_number. Not a blocker -- stated so a "
            "reader of the finished matrix knows which cells are missing and "
            "why (TASKS.md P1)."
        )
    if resume.stale_partials:
        print("\nSTALE PARTIAL WRITES -- these cells can never be written again:")
        for line in resume.stale_partials:
            print(f"  - {line}")
        print(
            "A process was killed mid-write. `run_id` is deterministic and "
            "nothing increments attempt_number, so the cell is poisoned until "
            "the .partial is removed by hand. Removing it here would delete "
            "another process's in-flight write."
        )
        return 1
    if resume.version_conflicts:
        print("\nTASK EDITED SINCE THOSE RECORDS WERE WRITTEN:")
        for line in resume.version_conflicts:
            print(f"  - {line}")
        print(
            "task_version is not part of run_id, so resuming would mix records "
            "of two different tasks under one task_id. Bump task_version and "
            "use a fresh event log, or restore the manifest."
        )
        return 1
    if resume.image_conflicts and not args.allow_mixed_images:
        print("\nSTORED RECORDS RAN ON A DIFFERENT IMAGE:")
        for line in resume.image_conflicts[:10]:
            print(f"  - {line}")
        print(
            f"({len(resume.image_conflicts)} cell(s) total.) Section 5.1 pins "
            "the image per run and the records already say which one they used; "
            "this matrix would span two. Pass --allow-mixed-images to proceed."
        )
        return 1
    if not resume.todo:
        print("\nnothing to run: every cell already has a record")
        return 0

    environment = proxy_environment(args.mode)

    # AFTER proxy_environment: that is what resolves the project-local AWS
    # config, so calling this first would read a different session from the one
    # the proxy is about to be handed.
    window = (
        credential_window(os.environ.get("AWS_REGION_NAME") or "us-east-1")
        if args.mode == "live"
        else CredentialWindow(None, "offline")
    )
    if window.expires_at is not None:
        print(f"creds     usable until {window.expires_at.isoformat()} ({window.source})")
        if window.error:
            print(f"          {window.error}")
    elif window.source == "offline":
        # No credentials exist to expire -- the stub answers. Said plainly
        # rather than reported as an unreadable expiry, which would read as a
        # degraded live run.
        print("creds     none needed (offline: the stub answers, nothing is spent)")
    else:
        print(f"creds     expiry unreadable ({window.source}): {window.error}")
        print("          the abort streak is the only backstop on this run")

    # Before the proxy is built, not inside the loop: an already-dead session
    # must not cost an image build and a container start to discover.
    longest_cell = max(
        by_id[cell.task_id].budget.wall_clock_timeout_s + CELL_OVERHEAD_S
        for cell in resume.todo
    )
    blocked = credential_stop(window, datetime.now(timezone.utc), longest_cell)
    if blocked:
        print(f"\nNOT STARTING: {blocked}")
        print(
            "Nothing was spent and no run_id was touched.\n"
            f"{SSO_LOGIN_HINT}\n"
            "then re-invoke this command."
        )
        return 2

    config_name = (
        "litellm_config.yaml" if args.mode == "live" else "litellm_smoke_offline.yaml"
    )
    build_proxy_image(REPO)
    artifacts = artifacts_root(CACHE, stamp)
    wire_dir = CACHE / "wire" / stamp

    rows: list[dict] = []
    stranded: list[str] = []
    infra: dict[str, list[str]] = {}
    tracker = StreakTracker(
        overall=args.abort_streak, per_arm=args.abort_streak_per_arm
    )
    credentials_expired = False
    unrun = 0

    with Proxy(
        config_name, wire_dir, environment, stamp,
        image="bakeoff-litellm-proxy:matrix", repo_root=REPO,
        with_stub=args.mode == "offline",
    ) as proxy:
        for index, cell in enumerate(resume.todo, start=1):
            task = by_id[cell.task_id]
            stop = credential_stop(
                window,
                now=datetime.now(timezone.utc),
                # The agent's cap is NOT the whole cell. Materializing a fresh
                # tree, starting the container, the final force_capture and the
                # record write all sit outside it -- so a margin of
                # wall_clock_timeout_s alone would clear a cell that then
                # outlives the credentials in its tail, spending the tokens and
                # writing a poisoned record anyway.
                needed_s=task.budget.wall_clock_timeout_s + CELL_OVERHEAD_S,
            )
            if stop:
                print(f"\nSTOPPING BEFORE {cell.label}: {stop}")
                print(
                    "Nothing was spent on this cell and its run_id is untouched.\n"
                    f"{SSO_LOGIN_HINT}\n"
                    "then re-invoke this command: plan_resume skips every cell "
                    "already written."
                )
                credentials_expired = True
                unrun = len(resume.todo) - index + 1
                break
            print(f"\n[{index}/{len(resume.todo)}] {cell.label}", flush=True)
            try:
                cell_started = time.monotonic()
                record, config_dump, unattributed, kept = run_cell(
                    cell, task, resolved[cell.task_id], args,
                    event_log, wire_dir, proxy.internal_name, artifacts,
                    # Both are this invocation's stamp today, which is exactly
                    # the 3.7.0 behaviour `collection_id` is documented as
                    # having. Minting the collection id from the event log's
                    # last index line -- so ~160 invocations of one matrix stop
                    # reading as 160 collections -- is a separate change; the
                    # record now has somewhere honest to put each of them.
                    stamp, stamp,
                )
                cell_wall_s = time.monotonic() - cell_started
            except Exception as exc:  # noqa: BLE001 - one cell, not the matrix
                # Loud and counted, never swallowed: a caller that believed the
                # log was complete would compute means over a matrix with a
                # hole in it.
                stranded.append(f"{cell.label}: {type(exc).__name__}: {exc}")
                print(f"  STRANDED {type(exc).__name__}: {exc}")
                traceback.print_exc()
                abort = tracker.record(cell.model, bad=True)
                if abort:
                    print(f"\nABORTING: {abort}")
                    break
                continue

            problems = infra_problems(
                record,
                wire_entries=record.wire_entries_seen,
                unattributed=unattributed,
                config=config_dump,
            )
            if problems:
                infra[cell.label] = problems
            abort = tracker.record(cell.model, bad=bool(problems))
            row = {
                "cell": cell.label,
                "outcome": record.outcome.value,
                "turns": record.turns_used,
                "tools": record.tool_calls.total,
                "wire": record.wire_entries_seen,
                "cost": record.cost_usd,
                "wall_s": record.time.wall_clock_total_ms / 1000,
                # The whole cell, not just the agent. The two differ by
                # exactly CELL_OVERHEAD_S's true value, which is the
                # measurement that constant is standing in for.
                "cell_wall_s": round(cell_wall_s, 1),
                "diff_b": len(record.artifacts.final_diff or ""),
                "problems": problems,
            }
            # Never a path that is not this run's -- and absent, not null,
            # because a row without `--keep` has no path to name. Under
            # `--keep` the run tree outlives the cell and a reader wants the
            # path.
            if kept:
                row["kept_repo"] = kept
            rows.append(row)
            cost = "unpriced" if record.cost_usd is None else f"{record.cost_usd:.5f}"
            print(
                f"  {record.outcome.value:18} turns={record.turns_used:<3} "
                f"tools={record.tool_calls.total:<3} wire={record.wire_entries_seen:<3} "
                f"cost={cost:<9} wall={record.time.wall_clock_total_ms / 1000:.1f}s "
                f"diff={len(record.artifacts.final_diff or '')}b"
            )
            # Printed beside the row rather than folded into `outcome`. Section
            # 5.4 makes budget-truncation rate a finding rather than something
            # to hide, and `failed / turns` is a different statement about a
            # model from `failed / agent_finish` -- one ran out of budget, the
            # other decided it was done.
            # `Exclusion` is a dataclass, not an enum: `.value` raised
            # AttributeError here, outside the try, so the first excluded cell
            # would have killed the whole matrix with a traceback. Never
            # exercised because no cell had ever been excluded -- and after the
            # auth classification landed, every credential failure reaches it.
            excluded = (
                f"  EXCLUDED={record.exclusion.cls.value}/"
                f"{record.exclusion.reason_code}"
                if record.exclusion
                else ""
            )
            print(
                f"  terminated_by={record.terminated_by.value}"
                + excluded
                + (f"  checkpoint_error={record.checkpoint_error}"
                   if record.checkpoint_error else "")
            )
            for problem in problems:
                print(f"  INFRA: {problem}")
            if abort:
                print(f"\nABORTING: {abort}")
                break

        write_json(
            wire_dir.parent / f"matrix-{stamp}.json",
            {
                "seed": args.seed,
                "mode": args.mode,
                "order": [cell.label for cell in order],
                "skipped": [cell.label for cell in resume.done],
                "rows": rows,
                "stranded": stranded,
                "infra": infra,
                "proxy_requests": proxy.request_count(),
                "stopped_early": credentials_expired,
                "credential_window": {
                    "expires_at": (
                        window.expires_at.isoformat() if window.expires_at else None
                    ),
                    "source": window.source,
                },
            },
        )

    print(f"\n{len(rows)} record(s) written to {args.event_log}")
    if stranded:
        print(f"\n{len(stranded)} cell(s) produced NO record:")
        for line in stranded:
            print(f"  - {line}")
    if infra:
        print(f"\n{len(infra)} record(s) are not interpretable:")
        for label, problems in infra.items():
            print(f"  {label}")
            for problem in problems:
                print(f"    - {problem}")
    if credentials_expired:
        print(
            f"\nSTOPPED EARLY with {unrun} cell(s) unrun. This is not a failure: "
            "nothing was spent on them and every one keeps its run_id, so "
            "re-invoking after `aws sso login` resumes exactly here."
        )
    if stranded or infra:
        print(
            "\nRead the wire log before concluding a model is at fault: "
            "section 6.4 requires telling adapter failure from model failure."
        )
        return 1
    return 2 if credentials_expired else 0


if __name__ == "__main__":
    sys.exit(main())
