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
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from bakeoff.images import (  # noqa: E402
    build_base_image,
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
from bakeoff.preflight import preflight  # noqa: E402
from bakeoff.proxy import EVAL_ARMS, Proxy, proxy_environment  # noqa: E402
from bakeoff.session import effective_config  # noqa: E402
from bakeoff.tasks import TaskError, load_task_set, materialize  # noqa: E402

# Under $HOME, never /var/folders: the Docker VM on macOS mounts $HOME only,
# and a repo bind-mounted from elsewhere appears inside the container as a
# silently EMPTY directory -- the agent then edits nothing and it is recorded
# as the model's failure.
CACHE = Path.home() / ".cache" / "bakeoff"
DEFAULT_TASK_SET = REPO / "taskset"


def base_claude_version(image: str) -> str:
    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "claude --version"],
        capture_output=True, text=True,
    )
    return probe.stdout.strip().split()[0] if probe.returncode == 0 else ""


def resolve_tasks(tasks, base_image, expected_version, cache, force):
    """Build each task's image, materialize its start state, and preflight.

    Returns (resolved, failures). `resolved` maps task_id to a dict carrying
    the image id and the start sha -- everything a run needs that is not in
    the manifest.

    The preflight cache is keyed on (manifest digest, image id, start sha).
    Re-validating 80 tasks on every resume would turn a 30-second restart
    into half an hour, and a gate that is expensive to run is a gate that
    gets skipped -- the same argument verify_logger.py makes for staying
    free.
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
    for task in tasks:
        print(f"\n=== {task.task_id} ===", flush=True)
        image = build_task_image(task, base_image, cache / "build", cache)
        entrypoint = image_entrypoint(image)
        if entrypoint:
            failures.append(
                f"{task.task_id}: the task image declares ENTRYPOINT "
                f"{entrypoint!r}; RunContainer's `sleep infinity` would become "
                "an argument to it and the container would exit immediately"
            )
            continue

        work = cache / "preflight-tree" / task.task_id
        shutil.rmtree(work, ignore_errors=True)
        start_sha = materialize(task, work / "repo", cache)
        print(f"image     {image[:19]}...")
        print(f"start_sha {start_sha}  (base {task.base_sha[:12]} + test half)")
        if not task.declared_start_sha:
            print(f"          pin it: repo.start_sha: {start_sha}")

        key = f"{task.manifest_digest}|{image}|{start_sha}"
        if cached.get(task.task_id, {}).get("key") == key:
            print("preflight cached PASS (--force-preflight to re-run)")
            resolved[task.task_id] = {"image": image, "start_sha": start_sha,
                                      "repo": work / "repo"}
            continue

        result = preflight(
            task, image=image, repo_path=work / "repo", start_sha=start_sha,
            expected_claude_version=expected_version,
        )
        write_json(cache / "preflight" / f"{task.task_id}.json", result.to_dict())
        if not result.ok:
            print("preflight NO-GO")
            for problem in result.problems:
                print(f"  - {problem}")
            failures.append(f"{task.task_id}: {len(result.problems)} problem(s)")
            continue
        print(
            "preflight PASS  f2p red at start, green after the reference; "
            "p2p green both ways; tree clean"
        )
        cached[task.task_id] = {"key": key}
        resolved[task.task_id] = {"image": image, "start_sha": start_sha,
                                  "repo": work / "repo"}

    write_json(cache_path, cached)
    return resolved, failures


def run_cell(cell, task, resolved, args, event_log, wire_dir, network, artifacts):
    from bakeoff.claude_runner import ClaudeCodeConfig
    from bakeoff.proxy_callback import unattributed_count
    from bakeoff.runner import execute_run

    run_root = artifacts / f"{cell.task_id}-{cell.model}-{cell.sample_index}"
    repo = run_root / "repo"
    shutil.rmtree(run_root, ignore_errors=True)
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
    if not args.keep:
        shutil.rmtree(repo, ignore_errors=True)
    return record, config_dump, unattributed


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

    try:
        tasks = load_task_set(Path(args.task_set), only=(
            args.tasks.split(",") if args.tasks else None
        ))
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

    print("\nbuilding base image ...", flush=True)
    base_image = build_base_image(REPO)
    expected_version = base_claude_version(base_image)
    print(f"base      {base_image[:19]}...  claude {expected_version}")

    resolved, failures = resolve_tasks(
        tasks, base_image, expected_version, CACHE, args.force_preflight
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
    config_name = (
        "litellm_config.yaml" if args.mode == "live" else "litellm_smoke_offline.yaml"
    )
    build_proxy_image(REPO)
    artifacts = CACHE / "artifacts"
    wire_dir = CACHE / "wire" / stamp

    rows: list[dict] = []
    stranded: list[str] = []
    infra: dict[str, list[str]] = {}
    tracker = StreakTracker(
        overall=args.abort_streak, per_arm=args.abort_streak_per_arm
    )

    with Proxy(
        config_name, wire_dir, environment, stamp,
        image="bakeoff-litellm-proxy:matrix", repo_root=REPO,
        with_stub=args.mode == "offline",
    ) as proxy:
        for index, cell in enumerate(resume.todo, start=1):
            print(f"\n[{index}/{len(resume.todo)}] {cell.label}", flush=True)
            try:
                record, config_dump, unattributed = run_cell(
                    cell, by_id[cell.task_id], resolved[cell.task_id], args,
                    event_log, wire_dir, proxy.internal_name, artifacts,
                )
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
            rows.append(
                {
                    "cell": cell.label,
                    "outcome": record.outcome.value,
                    "turns": record.turns_used,
                    "tools": record.tool_calls.total,
                    "wire": record.wire_entries_seen,
                    "cost": record.cost_usd,
                    "wall_s": record.time.wall_clock_total_ms / 1000,
                    "diff_b": len(record.artifacts.final_diff or ""),
                    "problems": problems,
                }
            )
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
    if stranded or infra:
        print(
            "\nRead the wire log before concluding a model is at fault: "
            "section 6.4 requires telling adapter failure from model failure."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
