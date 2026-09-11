#!/usr/bin/env python3
"""Spec Phase 0c -- the end-to-end go/no-go gate.

One trivial task, one run, each arm, through the real Claude Code binary in
the pinned container, through a real LiteLLM proxy container, to a real
model. A model that cannot complete a loop, emit tool calls and land a diff
here will not work at scale, and that has to surface on day one rather than
in week three after the dataset is built.

Two modes, because the two halves have different prerequisites:

    --mode offline   mock deployment, no credentials, no spend. Proves the
                     topology, the loop, header attribution, wire capture
                     and the section 5.2 config dump. Cannot prove tool
                     translation or a diff -- and says so rather than
                     quietly passing a weaker gate under the same name.

    --mode live      real Bedrock, real spend. Full criteria.

Run scripts/verify_logger.py first. A smoke run that spends money on a
broken logger wastes both.

Usage:
    python scripts/smoke_test.py --mode offline
    python scripts/smoke_test.py --mode live
    python scripts/smoke_test.py --mode live --models claude-sonnet-5
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

# Re-exported, not redefined. These moved into the package so the matrix
# driver could run the eval's real topology instead of a second copy of it
# (see bakeoff/proxy.py); the names stay importable from here because this
# script is the Phase 0c gate's operator-facing entry point and its tests
# address it by that name.
from bakeoff.proxy import (  # noqa: E402
    DEFAULT_PROVIDER,
    EVAL_ARMS_BY_PROVIDER,
    PROVIDERS,
    SSO_LOGIN_HINT,
    Proxy as _Proxy,
    freeze_sigv4_credentials,
    proxy_environment,
)
from bakeoff.session import (  # noqa: E402
    config_differences,
    config_problems,
    effective_config,
)

# Not duplicated: two copies of "which config a (mode, provider) pair runs
# on" is exactly how the offline gate and the paid driver would come to
# certify different things. No cycle -- run_matrix.py never imports this
# script, only names it in a docstring.
from scripts.run_matrix import arms_missing_from_config, config_name_for  # noqa: E402

AGENT_TAG = "bakeoff-eval-agent:smoke"
PROXY_TAG = "bakeoff-litellm-proxy:smoke"

# Phase 0c's exit criterion. One run per arm structurally cannot see a failure
# RATE, only a failure -- and the two 2026-08-07 runs proved the point: Kimi
# failed both times and in two different ways, so either run alone would have
# read as one deterministic bug. GO requires every repeat of every arm to pass;
# the per-arm rates are reported either way, because "which arm, how often, in
# what way" is the output Phase 3 sizing actually needs.
LIVE_REPEATS = 3

# The offline gate stays at one. The stub replies from a fixed script, so
# repeats are byte-identical and would only triple verify_logger's runtime.
OFFLINE_REPEATS = 1

# Bedrock's prompt-cache TTL for the arms that have one, in seconds. Claude
# Code requests no `ttl` on its cache_control blocks, so the default applies;
# every write observed on the wire came back as `ephemeral_5m_input_tokens`.
# Used only to say whether the harness's own run spacing sits inside it -- the
# answer, measured across six matrices, is 3-5 seconds against 300.
CACHE_TTL_S = 300.0

PROMPT = (
    "The test in tests/test_calc.py fails. Fix the bug in calc.py so that it "
    "passes. Do not modify the test."
)


# --- what the run has to demonstrate ----------------------------------------


@dataclass(frozen=True)
class Expectations:
    """Which criteria apply, and which are honestly out of reach.

    `not_applicable` is printed, never silently skipped. A gate that drops
    checks without saying which ones reads exactly like the full gate to
    anyone scanning the output, which is how a weaker check becomes a
    claimed guarantee.
    """

    name: str
    not_applicable: frozenset[str] = field(default_factory=frozenset)
    why: str = ""


LIVE = Expectations(name="live")

# Nothing is dropped offline. That is a property of the stub, not a claim
# about the gate: fixtures/anthropic_stub.py speaks real streaming SSE and
# answers with a tool call, so tool translation and the diff are both
# reachable. A `mock_response` deployment could reach neither, and would
# have needed them marked not-applicable here.
#
# What offline still cannot show is anything about a MODEL: the stub's reply
# is a fixed script. Only --mode live covers that, and no criterion here
# pretends otherwise.
OFFLINE = Expectations(name="offline")


def run_problems(
    record: Any,
    wire_entries: int,
    unattributed: int,
    config: dict,
    expectations: Expectations,
) -> list[str]:
    """Every reason this run is not evidence. Empty means GO."""
    problems: list[str] = []

    # First, because it zeroes everything downstream: the derived fields are
    # empty and must not be read as "the agent did nothing".
    if record.trajectory_parse_error:
        problems.append(f"trajectory did not parse: {record.trajectory_parse_error}")

    # An unpriced run is NOT a failure. The three candidates raise on any
    # observed cache token (their Bedrock cache pricing is unconfirmed), and
    # the loop, the tool calls and the diff are all still evidence -- only the
    # price is missing. This used to arrive as trajectory_parse_error, which
    # zeroed the record; now the tokens survive and the run is repriceable
    # offline, so it is reported rather than gated.
    #
    # It becomes a real problem only when the tokens are ALSO empty, because
    # then nothing can reprice it and the cost is lost permanently.
    if record.cost_usd is None:
        repriceable = record.tokens.input or record.tokens.output or (
            record.tokens.cache_read or record.tokens.cache_write
        )
        if not repriceable:
            problems.append(
                f"cost unknown AND no tokens recorded: {record.pricing_error} "
                "-- this run can never be repriced"
            )

    if record.turns_used <= 0:
        problems.append("no turns: the agent never completed a loop")

    if "tool_calls" not in expectations.not_applicable and record.tool_calls.total <= 0:
        problems.append("no tool calls: transport works, tool translation unproven")

    # Spec section 6.2. An isolated run with an empty wire log is the
    # signature of capture registered in the wrong process, which shipped
    # once already.
    if wire_entries <= 0:
        problems.append("wire log is empty: section 6.2 capture is dead")
    if unattributed:
        problems.append(
            f"{unattributed} call(s) landed in unattributed.jsonl: attribution lost"
        )

    if "final_diff" not in expectations.not_applicable and not (
        record.artifacts.final_diff or ""
    ):
        problems.append("no diff: nothing was submitted")

    # Tri-state, matching matrix.infra_problems. `not record.isolated` reported
    # a run whose container never started -- where nobody measured -- as a
    # section 5.1 violation, a claim invented from an absence.
    if record.isolated is False:
        problems.append("isolated=False: section 5.1 did not hold")
    elif record.isolated is None:
        problems.append(
            "isolation could not be measured: the container never started, so "
            "section 5.1 is unverified rather than violated"
        )
    if record.exclusion is not None:
        problems.append("run was excluded as an infra or adapter failure")

    problems.extend(config_problems(config))
    return problems


# --- the fixture -------------------------------------------------------------


def build_smoke_repo(workdir: Path) -> tuple[Path, str]:
    """Copy the fixture template and make it a real repo.

    base_sha is derived from the commit made here rather than passed in: a
    hand-recorded SHA goes stale the first time the fixture changes, and
    `git checkout --detach <stale sha>` is a plain exec whose failure the
    orchestrator does not raise on. The run would proceed on whatever state
    the tree happened to be in.
    """
    repo = workdir / "repo"
    shutil.copytree(REPO / "fixtures" / "smoke_task", repo)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "eval@pindrop.test"],
        ["config", "user.name", "eval"],
        ["add", "-A"],
        ["commit", "-q", "-m", "base: add() has a sign bug"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, sha


# --- images ------------------------------------------------------------------


def build_images() -> str:
    """Build both images; return the agent image ID.

    An ID, not a tag: RunContainer refuses tags (section 5.1) and a locally
    built image has no registry digest until it is pushed.
    """
    for tag, dockerfile in (
        (AGENT_TAG, "docker/eval-agent.Dockerfile"),
        (PROXY_TAG, "docker/litellm-proxy.Dockerfile"),
    ):
        subprocess.run(
            ["docker", "build", "-q", "-f", str(REPO / dockerfile), "-t", tag, str(REPO)],
            check=True,
            capture_output=True,
        )
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", AGENT_TAG],
        check=True,
        capture_output=True,
        text=True,
    )
    image = out.stdout.strip()
    assert_agent_can_verify_its_work(image)
    return image


def assert_agent_can_verify_its_work(image: str) -> None:
    """Refuse to spend money on a matrix the agent cannot check itself in.

    Spec section 3.3 measures a loop that ends in "runs tests, sees failures,
    self-corrects". Through all of Phase 0c the image shipped no pytest, so
    the loop ended at "edits" and every arm was scored on one unverified
    guess -- Gemma burned 30 of 30 turns re-running a command that raised
    ModuleNotFoundError with the bug fixed and with it unfixed alike, and the
    9/9 was read as capability.

    A precondition rather than a run criterion, deliberately: by the time a
    record exists the tokens are paid for, and nothing in the record can
    distinguish "the model never verified" from "the model could not".
    """
    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image,
         "-c", "command -v pytest && python -m pytest --version"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            "the eval image ships no test runner, so the agent cannot verify "
            "its own work and no arm's failure would be attributable to the "
            f"model:\n{probe.stdout}{probe.stderr}"
        )


# --- the proxy ---------------------------------------------------------------


class Proxy(_Proxy):
    """`bakeoff.proxy.Proxy` bound to this script's image tag and repo root.

    The class moved into the package so the matrix driver runs the same
    topology rather than a second copy of it. This subclass exists only so
    the call site below, and the operator-facing name, stay unchanged.
    """

    def __init__(self, config_name, wire_dir, env, tag, with_stub=False):
        super().__init__(
            config_name=config_name,
            wire_dir=wire_dir,
            env=env,
            tag=tag,
            image=PROXY_TAG,
            repo_root=REPO,
            with_stub=with_stub,
        )


# --- the run -----------------------------------------------------------------


def run_arm(
    arm: str,
    image: str,
    workdir: Path,
    wire_dir: Path,
    event_log,
    network: str,
    max_turns: int,
    timeout_s: int,
    repeat: int = 0,
) -> tuple[Any, dict, int]:
    """One run of one arm. `repeat` indexes runs of the same arm.

    It has to reach two places, and both are load-bearing:

      the workdir     each repeat builds its own repo. Sharing one would let
                      repeat 2 start from repeat 1's dirty tree and report a
                      diff its own agent never made.

      sample_index    run_id is a hash of (task, model, sample, attempt) and
                      write_run opens mode "x", so a second repeat at
                      sample_index=0 raises ImmutabilityError out of
                      execute_run rather than overwriting -- correct, and
                      fatal to the matrix if the index does not move.
    """
    from bakeoff.claude_runner import ClaudeCodeConfig
    from bakeoff.proxy_callback import unattributed_count
    from bakeoff.runner import TaskSpec, execute_run

    arm_dir = workdir / f"{arm}-{repeat}"
    repo, base_sha = build_smoke_repo(arm_dir)

    task = TaskSpec(
        task_id="smoke-001",
        task_version=1,
        repo="fixtures/smoke_task",
        base_sha=base_sha,
        container_image_digest=image,
        prompt=PROMPT,
        test_paths=["tests/test_calc.py"],
    )
    config = ClaudeCodeConfig(
        model=arm,
        base_url="http://litellm:4000",
        # The proxy has no master_key, so any value passes. Sent anyway:
        # Claude Code requires one, and an empty token changes the request
        # shape rather than the authorization result.
        auth_token="sk-eval-local",
        settings_path="/eval/eval_settings.json",
        config_dir="/eval/claude-config",
        max_turns=max_turns,
        wall_clock_timeout_s=timeout_s,
    )

    before = unattributed_count(wire_dir)
    record = execute_run(
        task=task,
        model=arm,
        sample_index=repeat,
        config=config,
        event_log=event_log,
        repo_path=str(repo),
        artifacts_root=arm_dir / "artifacts",
        # The smoke gate mints the same deterministic run_ids the matrix does,
        # so two invocations are indistinguishable in a merged view without it.
        collection_id=workdir.name,
        invocation_stamp=workdir.name,
        network=network,
        proxy_wire_dir=wire_dir,
        settings_host_path=REPO / "config" / "eval_settings.json",
    )

    config_dump = {}
    if record.artifacts.container_stdout:
        config_dump = effective_config(Path(record.artifacts.container_stdout))
    (arm_dir / "artifacts" / "effective_config.json").write_text(
        json.dumps(config_dump, indent=2, sort_keys=True)
    )

    # Delta, not absolute: unattributed.jsonl is shared across arms, so a
    # later arm would inherit an earlier arm's failure and every arm after
    # the first would fail for someone else's reason.
    return record, config_dump, unattributed_count(wire_dir) - before


def wire_entry_count(record) -> int:
    import gzip

    # None since 3.1.0, when the artifact does not exist -- the record no
    # longer publishes a path to a file that was never opened.
    if not record.artifacts.wire_log_gz:
        return 0
    path = Path(record.artifacts.wire_log_gz)
    if not path.exists():
        return 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def wire_failed_count(record) -> int:
    """Entries recording a provider attempt that failed.

    This is what licenses a captured count larger than a served one. LiteLLM
    retries a failed call up to `num_retries` times and each attempt fires the
    callback separately, so surplus attempts must each follow a failure -- a
    surplus larger than the failure count is unexplained and worth a gate.
    """
    import gzip

    if not record.artifacts.wire_log_gz:
        return 0
    path = Path(record.artifacts.wire_log_gz)
    if not path.exists():
        return 0
    failed = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (entry.get("metadata") or {}).get("failed"):
                failed += 1
    return failed


# --- output ------------------------------------------------------------------


def print_table(rows: list[dict]) -> None:
    header = (
        f"{'run':30} {'outcome':18} {'turns':>5} {'tools':>6} {'wire':>5} "
        f"{'out_tok':>8} {'cache_r':>8} {'cache_w':>8} {'cost$':>9} "
        f"{'wall_s':>7} {'diff_b':>7}  verdict"
    )
    print("\n" + header)
    print("-" * len(header))
    for row in rows:
        # cost is None when the arm's tokens could not be priced. A float
        # format spec raises TypeError on that, and this table prints AFTER
        # the whole matrix has been paid for -- the arm that triggers it would
        # take the entire report down with it.
        cost = "  unpriced" if row["cost"] is None else f"{row['cost']:>9.5f}"
        print(
            f"{row['label']:30} {row['outcome']:18} {row['turns']:>5} "
            f"{row['tools']:>6} {row['wire']:>5} {row['out_tok']:>8} "
            f"{row['cache_read']:>8} {row['cache_write']:>8} "
            f"{cost} {row['wall_s']:>7.1f} {row['diff_b']:>7}  "
            f"{row['verdict']}"
        )


def print_rates(rows: list[dict], repeats: int) -> None:
    """Per-arm pass rate and the distinct ways each arm failed.

    The reason N=3 exists. A summed failure count would have reported Kimi's
    two 2026-08-07 runs as "2 failures" when they were two *different*
    failures -- one mid-stream death, one clean exit that never called the
    edit tool -- and those need different fixes. Grouping by run_problems'
    returned reasons keeps them apart without inventing a second taxonomy.
    """
    if repeats <= 1:
        return
    print(f"\nper-arm rates over {repeats} run(s)")
    for arm in dict.fromkeys(row["arm"] for row in rows):
        arm_rows = [row for row in rows if row["arm"] == arm]
        passed = sum(1 for row in arm_rows if not row["problems"])
        print(f"  {arm:26} passed {passed}/{len(arm_rows)}")
        modes: dict[str, int] = {}
        for row in arm_rows:
            if row["problems"]:
                modes["; ".join(row["problems"])] = (
                    modes.get("; ".join(row["problems"]), 0) + 1
                )
        for mode, count in sorted(modes.items(), key=lambda item: -item[1]):
            print(f"    {count}x  {mode}")


def print_cache_tokens(rows: list[dict]) -> None:
    """Whether an arm returns cache tokens at all, stated rather than assumed.

    costs.py prices cache tokens for Sonnet and raises for the three
    candidates, whose Bedrock cache pricing is unconfirmed. That trip used to
    zero the whole record; parse_trajectory now keeps the turns and tokens and
    records the cost as unknown, so the remaining job of this line is to say
    which arms are affected -- and therefore which arms need a rate before any
    cost figure is published.

    Measured on 2026-08-07: the cache warms on turn 2 of a single run (not at
    N=10, as TASKS.md assumed), and Kimi does return cache tokens once it runs
    a real multi-turn loop. The earlier reading that the candidates returned
    none came from runs that died at turn 2.

    `warm` is therefore printed PER REPEAT rather than summed. The totals here
    say whether an arm caches at all; only the per-repeat column says which
    repeat paid the cache write, and on the 2026-08-11 N=3 runs that column
    alone separated the $0.547 run from the $0.176 one.
    """
    print("\ncache tokens (costs.py has no confirmed rate for any candidate)")
    for arm in dict.fromkeys(row["arm"] for row in rows):
        arm_rows = [row for row in rows if row["arm"] == arm]
        reads = sum(row["cache_read"] for row in arm_rows)
        writes = sum(row["cache_write"] for row in arm_rows)
        priced = arm.startswith("claude-sonnet-5")
        if reads or writes:
            note = (
                "priced"
                if priced
                else "UNPRICED -- cost recorded as null, tokens intact"
            )
        else:
            note = "none returned"
        # None is undetermined, not cold -- do not collapse it into "cold".
        warm = ",".join(
            {True: "warm", False: "cold", None: "?"}[row["cache_warm"]]
            for row in arm_rows
        )
        print(f"  {arm:26} read={reads:<9} write={writes:<9} at_start={warm:<14} {note}")


def run_order(arms: list[str], repeats: int, interleave: bool) -> list[tuple[str, int]]:
    """The (arm, repeat) sequence to execute, in order.

    ARM-MAJOR IS THE DEFAULT, and it is the ordering every cost figure in
    TASKS.md was measured under. It keeps a NO-GO arm's runs adjacent in the
    output and keeps one arm's repeats close in time, so a pass rate is not
    confounded by drift in Bedrock-side load across the whole matrix.

    It also walks straight into section 5.8's confound: back-to-back repeats of
    the same arm warm that arm's prompt cache, and the second and third repeats
    then read at 0.1x what the first paid to write. On the 2026-08-11 set the
    three Sonnet runs started 15 and 14 seconds apart and the first cost 2.9x
    the others. That is a fact about the ordering, not about the model.

    Interleaving is the section 5.8 control, and it is worth being precise
    about what it does not do. Measured: one interleaved round of the full
    matrix takes 1.4-3.9 minutes, inside the 5-minute TTL, so every arm after
    round 1 still finds its own prefix warm. Interleaving REDISTRIBUTES the
    write cost across repeats; it does not produce cold runs. Nothing short of
    idling past the TTL does, and the gap that decides it is now recorded per
    run as `cache_state.seconds_since_prior_run`.
    """
    if interleave:
        return [(arm, repeat) for repeat in range(repeats) for arm in arms]
    return [(arm, repeat) for arm in arms for repeat in range(repeats)]


def adjacent_repeats(order: list[tuple[str, int]]) -> list[str]:
    """Arms that appear in two consecutive positions of the run order."""
    return [a for (a, _), (b, _) in zip(order, order[1:]) if a == b]


def print_run_order(order: list[tuple[str, int]], rows: list[dict], repeats: int) -> None:
    """Spec section 5.8: the interleaving "must be verified, not assumed".

    Reports the ORDERING and the GAP, because only the second one decides
    anything. Interleaving a matrix that completes a round in under 5 minutes
    reorders which repeat pays the cache write; it does not make any repeat
    cold. The gaps are read from the records rather than from the schedule --
    what the harness intended is not evidence of what the cache did.
    """
    if repeats <= 1:
        return
    print("\nrun ordering (spec section 5.8)")

    arms = {arm for arm, _ in order}
    adjacent = adjacent_repeats(order)
    if len(arms) == 1:
        print("  one arm: interleaving cannot separate its repeats from each other")
    elif not adjacent:
        print("  interleaved: no arm ran twice in a row")
    else:
        counts: dict[str, int] = {}
        for arm in adjacent:
            counts[arm] = counts.get(arm, 0) + 1
        print(
            "  NOT INTERLEAVED: an arm ran back-to-back, so its later repeats "
            "read a cache its own earlier repeat paid to write."
        )
        for arm, count in sorted(counts.items(), key=lambda item: -item[1]):
            print(f"    {arm:26} {count} back-to-back transition(s)")
        print("  Re-run with --interleave to redistribute that write cost.")

    gaps = [
        row["gap_s"]
        for row in rows
        if isinstance(row.get("gap_s"), (int, float))
    ]
    if not gaps:
        print("  no run followed an earlier run of its own arm")
        return
    print(
        f"  gap to the prior run of the same arm: "
        f"{min(gaps):.1f}s to {max(gaps):.1f}s over {len(gaps)} run(s)"
    )
    if max(gaps) < CACHE_TTL_S:
        print(
            f"  ALL INSIDE the {CACHE_TTL_S:.0f}s prompt-cache TTL. Every warm "
            "run below was warmed by this harness, not by a deployment pattern."
        )


def print_cost(rows: list[dict], repeats: int) -> None:
    """Spec section 5.8: cost per run, by cache scenario, naming both.

    NEITHER COLUMN IS A CORRECTION OF THE OTHER, which is why neither is called
    normalized. They are two real deployment situations:

      first_task     turn 1 found the cache cold. A developer opening a fresh
                     task pays the tool-schema write, ~42k tokens at 1.25x.
      warm_followup  turn 1 read a prefix someone else wrote. A developer
                     starting another task inside the TTL.

    Calling the warm number "normalized" asserted it was the corrected one. It
    is not. In this harness the warm runs exist because the scheduler ran
    repeats 3-5 seconds apart inside a 300-second TTL -- see print_run_order --
    and what carries over is 30,506 tokens of task-independent tool schemas, not
    any part of the work. Averaging the two would price a scenario nobody is in.

    Runs whose cost is None are excluded and counted, because a mean over "the
    runs that happened to miss the cache" is not a price of anything -- and for
    the candidate arms that is not a coincidence: `warm is True` requires cache
    tokens, cache tokens raise on a model with no confirmed rate, so their
    warm_followup column is unreachable by construction rather than unobserved.
    """
    if repeats <= 1:
        return
    print("\ncost per run by cache scenario (spec section 5.8)")
    for arm in dict.fromkeys(row["arm"] for row in rows):
        arm_rows = [row for row in rows if row["arm"] == arm]
        priced = [row for row in arm_rows if row["cost"] is not None]
        if not priced:
            print(f"  {arm:26} unpriced on all {len(arm_rows)} run(s)")
            continue

        def mean(subset: list[dict]) -> str:
            if not subset:
                return "     --      "
            return f"{sum(row['cost'] for row in subset) / len(subset):<9.5f} n={len(subset)}"

        if all(row["cache_warm"] is None for row in arm_rows):
            # Neither scenario applies: nothing reported cache accounting, so
            # every run is undetermined rather than cold. Printing two empty
            # columns would read as "measured, and both were zero".
            total = sum(row["cost"] for row in priced) / len(priced)
            print(
                f"  {arm:26} cost={total:<9.5f} n={len(priced)}   "
                "cache state undetermined on every run -- no scenario applies"
            )
            continue

        cold = mean([row for row in priced if row["cache_warm"] is False])
        warm = mean([row for row in priced if row["cache_warm"] is True])
        unpriced = len(arm_rows) - len(priced)
        note = ""
        if unpriced and any(row["cache_warm"] is True for row in arm_rows):
            note = f"  [{unpriced} unpriced, incl. every warm run -- no cache rate]"
        elif unpriced:
            note = f"  [{unpriced} unpriced]"
        print(f"  {arm:26} first_task={cold}   warm_followup={warm}{note}")


def print_sampling(rows: list[dict]) -> None:
    """Task 11 carried this here, and it is a finding either way.

    Section 5.3 puts sampling entirely in proxy config because Claude Code
    has no temperature flag. If the configured value does not appear on the
    wire, three arms ran at unspecified sampling with nothing in the record
    saying so -- and config/litellm_config.yaml's per-arm values are
    decorative. Reported, never gated: a dropped temperature is a finding,
    not a reason to call the run invalid.
    """
    print("\nsampling as it appears on the wire (spec section 5.3)")
    # One line per ARM, not per run: sampling comes from proxy config, which
    # does not vary between repeats of the same arm.
    seen: set[str] = set()
    for row in rows:
        if row["arm"] in seen:
            continue
        seen.add(row["arm"])
        sampling = row["sampling"] or {}
        temperature = sampling.get("temperature")
        shown = "absent" if temperature is None else repr(temperature)
        expected = "none by API constraint" if row["arm"].startswith(
            "claude-sonnet-5"
        ) else "1.0 configured"
        print(
            f"  {row['arm']:26} temperature={shown:<8} "
            f"max_output_tokens={sampling.get('max_output_tokens')}   "
            f"<- {expected}"
        )


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], required=True)
    parser.add_argument(
        "--provider", choices=list(PROVIDERS), default=DEFAULT_PROVIDER,
        help="which route the proxy serves: openrouter (default) or bedrock",
    )
    parser.add_argument(
        "--models", help="comma-separated subset of the arms (live mode)"
    )
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument(
        "--repeats",
        type=int,
        default=None,
        help=f"runs per arm (default {LIVE_REPEATS} live, {OFFLINE_REPEATS} offline). "
        "GO requires every repeat of every arm to pass.",
    )
    parser.add_argument(
        "--interleave",
        action="store_true",
        help="round-robin the arms so repeats of one arm never run back-to-back "
        "(spec section 5.8). Off by default; the ordering actually used is "
        "reported either way.",
    )
    parser.add_argument("--keep", action="store_true", help="keep artifacts on disk")
    args = parser.parse_args()

    from bakeoff.eventlog import EventLog

    expectations = LIVE if args.mode == "live" else OFFLINE
    config_name = config_name_for(args.mode, args.provider)
    if args.mode == "live":
        arms = args.models.split(",") if args.models else list(EVAL_ARMS_BY_PROVIDER[args.provider])
    else:
        # Two arms, not one. The second configures a temperature, which is
        # how the section 5.3 question gets answered offline: if a
        # deployment-level temperature does not reach the wire, three arms
        # of the real eval run at unspecified sampling with nothing in the
        # record saying so. It also gives the section 5.2 cross-arm diff
        # something to compare.
        # Three arms. The third routes through `openai/` and is the only one
        # that reaches the openai->anthropic adapter -- the two `anthropic/`
        # arms bypass it, which is why the tool-id defect was invisible here
        # and had to be found live. See config/litellm_smoke_offline.yaml.
        arms = (
            args.models.split(",")
            if args.models
            else ["claude-sonnet-5", "gemma-4-31b", "kimi-k2-5"]
        )

    # Before any image is built or the proxy started: an arm absent from the
    # chosen config is an operator error (a typo, or a config/provider
    # mismatch), not a model failure -- see arms_missing_from_config.
    missing = arms_missing_from_config(arms, REPO / "config" / config_name)
    if missing:
        # Exit 2, not 1: run_matrix.py returns 2 for this same refusal and
        # reserves 2 for "stopped before spending anything, re-invoke to
        # retry" (credential_stop uses it the same way). `raise
        # SystemExit(str)` prints the string but exits 1 -- verified via
        # `python3 -c 'raise SystemExit("boom")'; echo $?` -> 1 -- so the
        # message is printed explicitly and 2 is returned instead.
        print(f"arm(s) not in {config_name}: {', '.join(missing)}", file=sys.stderr)
        return 2

    # Under $HOME, never /var/folders: the Docker VM on macOS mounts $HOME
    # only, and a repo bind-mounted from elsewhere appears inside the
    # container as a silently EMPTY directory -- the run then edits nothing
    # and reports it as the model's failure.
    stamp = subprocess.run(
        ["date", "-u", "+%Y%m%dT%H%M%SZ"], check=True, capture_output=True, text=True
    ).stdout.strip()
    workdir = Path.home() / ".cache" / "bakeoff-smoke" / stamp
    workdir.mkdir(parents=True)
    wire_dir = workdir / "wire"

    repeats = args.repeats
    if repeats is None:
        repeats = LIVE_REPEATS if args.mode == "live" else OFFLINE_REPEATS
    if repeats < 1:
        raise SystemExit("--repeats must be at least 1")

    print(f"mode      {args.mode}  ({config_name})")
    print(f"arms      {', '.join(arms)}")
    print(f"repeats   {repeats} per arm ({len(arms) * repeats} runs)")
    order = run_order(arms, repeats, args.interleave)
    print(f"order     {'interleaved' if args.interleave else 'arm-major'}")
    print(f"workdir   {workdir}")
    if expectations.not_applicable:
        print(
            f"NOT CHECKED in this mode: {', '.join(sorted(expectations.not_applicable))}"
            f" -- {expectations.why}"
        )

    environment = proxy_environment(args.mode, args.provider)
    print("building images ...")
    image = build_images()
    print(f"image     {image[:19]}...")

    # One event log for the invocation, under a fresh timestamped root:
    # run_id is a hash of (task, model, sample, attempt), and write_run
    # opens "x", so re-running into a previous root raises ImmutabilityError
    # out of execute_run.
    event_log = EventLog(workdir / "eventlog")

    rows: list[dict] = []
    configs: dict[str, dict] = {}
    failures: dict[str, list[str]] = {}

    with Proxy(
        config_name, wire_dir, environment, stamp, with_stub=args.mode == "offline"
    ) as proxy:
        # See run_order: arm-major by default, round-robin under --interleave.
        # Either way the ordering used is reported rather than assumed, which
        # is what section 5.8 asks for.
        for arm, repeat in order:
            label = arm if repeats == 1 else f"{arm}/{repeat + 1}"
            print(f"\nrunning {label} ...")
            record, config_dump, unattributed = run_arm(
                arm,
                image,
                workdir,
                wire_dir,
                event_log,
                proxy.internal_name,
                args.max_turns,
                args.timeout_s,
                repeat=repeat,
            )
            entries = wire_entry_count(record)
            problems = run_problems(
                record,
                wire_entries=entries,
                unattributed=unattributed,
                config=config_dump,
                expectations=expectations,
            )
            if problems:
                failures[label] = problems
            # One representative per arm: section 5.2 compares arms
            # against each other, and repeats of one arm are configured
            # identically by construction.
            configs.setdefault(arm, config_dump)
            rows.append(
                {
                    "arm": arm,
                    "label": label,
                    "outcome": record.outcome.value,
                    "turns": record.turns_used,
                    "tools": record.tool_calls.total,
                    "wire": entries,
                    "wire_distinct": record.wire_entries_distinct,
                    "wire_failed": wire_failed_count(record),
                    "out_tok": record.tokens.output,
                    "cache_read": record.tokens.cache_read,
                    "cache_write": record.tokens.cache_write,
                    "cache_warm": record.cache_state.warm,
                    "gap_s": record.cache_state.seconds_since_prior_run,
                    "cost": record.cost_usd,
                    "wall_s": record.time.wall_clock_total_ms / 1000,
                    "diff_b": len(record.artifacts.final_diff or ""),
                    "unattributed": unattributed,
                    "sampling": record.sampling,
                    "problems": problems,
                    "verdict": "NO-GO" if problems else "go",
                    "wire_log": record.artifacts.wire_log_gz,
                }
            )

        if not rows:
            print("\nno arms ran")
            return 1

        # Cross-check capture against the proxy's own access log. Counting
        # only the entries we captured cannot detect a call we failed to
        # capture -- the wire log would simply be short, and short is
        # indistinguishable from quiet.
        #
        # The two counts are DIFFERENT QUANTITIES and equality was the wrong
        # test. `served` is inbound client requests; `captured` is callback
        # invocations, and one request can produce several.
        #
        # TWO mechanisms, and only one of them was known. `num_retries: 3` adds
        # an invocation per retry -- that is the one this check was written for.
        # The other was measured 2026-08-12 against a real provider call: a
        # FAILED call fires the failure callback TWICE under a single
        # litellm_call_id, so every failure is logged twice whether or not it
        # was retried. The served=39/captured=42 surplus recorded in TASKS.md as
        # "exactly the three retries of gemma's failing call" was three
        # double-logged failures instead: same arithmetic, different cause, and
        # nothing in the log could tell them apart until `wire_entries_distinct`
        # existed.
        #
        # `distinct` is the number to reconcile against, because it counts
        # logical calls. It is reported beside the raw count rather than
        # replacing it -- a gate that silently corrected one number into the
        # other would hide the day the duplication stops or doubles again.
        served = proxy.request_count()
        captured = sum(row["wire"] for row in rows) + sum(
            row["unattributed"] for row in rows
        )
        distinct = sum(row["wire_distinct"] for row in rows) + sum(
            row["unattributed"] for row in rows
        )
        failed = sum(row["wire_failed"] for row in rows)
        if captured < served:
            failures.setdefault("wire capture", []).append(
                f"the proxy served {served} POST /v1/messages but only "
                f"{captured} provider attempt(s) were recorded: "
                f"{served - captured} call(s) were captured nowhere"
            )
        elif captured - served > 2 * failed:
            # Every surplus invocation traces to a failure: a retry follows one,
            # and the duplicate IS one. A failure can therefore account for at
            # most two extra invocations -- its own duplicate and one retry
            # invocation -- so more surplus than that is something else, and
            # nothing in the record would say what.
            failures.setdefault("wire capture", []).append(
                f"the proxy served {served} POST /v1/messages and recorded "
                f"{captured} callback invocation(s), a surplus of "
                f"{captured - served}, against {failed} failed attempt(s): "
                "retries and duplicate failure logging cannot account for the "
                "difference"
            )
        elif captured > served:
            print(
                f"\nwire capture  {served} request(s) -> {distinct} logical "
                f"call(s) -> {captured} callback invocation(s); "
                f"{captured - served} surplus after {failed} failure(s) "
                "(retries and the duplicate the failure path logs)"
            )

        print_table(rows)
        print_rates(rows, repeats)
        print_cache_tokens(rows)
        print_run_order(order, rows, repeats)
        print_cost(rows, repeats)
        print_sampling(rows)

        differences = config_differences(configs) if len(configs) > 1 else []
        print("\neffective config across arms (spec section 5.2)")
        if differences:
            for difference in differences:
                print(f"  DIFFERS: {difference}")
        else:
            print("  identical on permissionMode, mcp_servers, tools")

        if failures or differences:
            print("\nNO-GO")
            for label, problems in failures.items():
                print(f"  {label}")
                for problem in problems:
                    print(f"    - {problem}")
                wire = next(
                    (r["wire_log"] for r in rows if r.get("label") == label), None
                )
                if wire:
                    print(f"    wire log: {wire}")
            if differences:
                print("  arms were not configured identically; see above")
            print(
                "\nRead the wire log before concluding a model is at fault: "
                "spec section 6.4 requires telling adapter failure from model "
                "failure, and that distinction is what this gate exists to "
                "surface."
            )
            print("\nproxy logs (tail):")
            print(proxy.logs())
            return 1

    print(
        f"\nGO ({expectations.name} criteria): every arm completed a loop "
        f"on all {repeats} of {repeats} run(s)."
    )
    if repeats < LIVE_REPEATS and args.mode == "live":
        # Said out loud, because a GO at N=1 reads exactly like a GO at N=3
        # to anyone scanning the output -- and the whole reason this knob
        # exists is that N=1 cannot see a rate.
        print(
            f"WEAKER THAN THE PHASE 0c CRITERION: {repeats} run(s) per arm, "
            f"not {LIVE_REPEATS}. A failure rate below roughly "
            f"1-in-{repeats + 1} is invisible at this N."
        )
    if expectations.not_applicable:
        print(
            f"NOT checked in this mode: {', '.join(sorted(expectations.not_applicable))}"
        )
    if args.mode == "offline":
        print(
            "This is the OFFLINE gate: the topology, the loop, tool "
            "translation, capture and the section 5.2 dump are all real, but "
            "the replies came from a fixed script. It says nothing about any "
            "model. Only --mode live does."
        )
    else:
        print("Use wall_s and out_tok above to size parallelism for OPEN-3.")
    if args.keep:
        print(f"artifacts kept at {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
