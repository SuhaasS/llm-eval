"""Execute one run end to end and assemble its record.

Ordering matters: the record is assembled from whatever survived, so a
crash mid-run still yields a valid partial record rather than nothing
(spec section 6.6). Nothing between the start of a run and the write is
allowed to raise past this module -- the tokens are already paid for by
then, and a lost record cannot be re-derived at any price.

The write itself is the exception, and it is a deliberate one. It CAN
raise -- a caller that thought the log was complete would go on to compute
means over a matrix with a hole in it -- but it may not lose the record on
the way out. `_write_or_strand` puts the record next to its own artifacts
first and re-raises second, so the failure is loud and the data survives it.

Two things this module is careful NOT to do:

  It does not decide whether the agent succeeded. Grading is offline
  (section 5.5), so `FailureSignals.tests_passed` stays None and
  failure_class is left open for the grader. Passing False instead would
  stamp FALSE_SUCCESS -- "claimed a success it did not achieve" -- onto
  every well-behaved run. The grader is `grader.py`, driven by
  `scripts/grade.py`, and it writes GradeRecords to `grades.jsonl`
  (`grade_schema.py`) BESIDE this log -- never into it. There is no
  `RunRecord.tests_passed` for it to fill in and no update API if there
  were: a verdict is a derived view over the stored diffs, and a re-grade
  under a different oracle or image is a new line whose disagreement with
  the old one is the finding.

  It does not report configuration as observation. Sampling, prompt and
  tool hashes come from the wire log, which records what was actually
  sent; the config only records what was asked for.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import traceback
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from collections.abc import Callable
from typing import Any

from bakeoff.checkpoints import CheckpointRecorder
from bakeoff.claude_runner import (
    ClaudeCodeConfig,
    ClaudeCodeRunner,
    ContainerBackend,
    config_digest,
)
from bakeoff.classify import RunSignals, classify_exclusion, classify_failure
from bakeoff.container import HostSampler, RunContainer, fresh_tree
from bakeoff.costs import PRICING_BASIS
from bakeoff.eventlog import EventLog
from bakeoff.proxy_callback import (
    read_manifest,
    read_run_entries,
    unattributed_count,
    unreadable_entry_count,
)
from bakeoff.scanners import scan_destructive
from bakeoff.schema import (
    Artifacts,
    CacheState,
    Checkpoint,
    DestructiveEvent,
    HostMetrics,
    Outcome,
    RunRecord,
    Severity,
    TerminationReason,
    TimingBreakdown,
    TokenUsage,
    ToolCallStats,
    Versions,
)
from bakeoff.trajectory import ParsedTrajectory, parse_trajectory
from bakeoff.wire import BakeoffCallback, WireLogger

CONTAINER_CONFIG_DIR = "/eval/claude-config"
STDOUT_NAME = "agent_stdout.jsonl"
STDERR_NAME = "agent_stderr.txt"
# The HARNESS's traceback, never the agent's. Named apart from the agent's
# stderr so a directory listing says whose failure it was: folding a harness
# defect into the agent's output makes the agent look like the thing that broke.
HARNESS_TRACEBACK_NAME = "harness_traceback.txt"
WIRE_NAME = "wire.jsonl.gz"
# Where a record goes when the event log refuses it. Named so a directory
# listing says what it is: this file existing means the log is INCOMPLETE and
# an offline pass has to append it.
UNWRITTEN_NAME = "record.unwritten.json"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    task_version: int
    repo: str
    base_sha: str
    container_image_digest: str
    prompt: str
    test_paths: list[str] = field(default_factory=list)
    # Commit of the task set this task was loaded from (see tasks.py). It
    # travels on the task rather than arriving as an execute_run parameter
    # because it is a property of where the task came from, and a parameter
    # is a thing a caller can forget -- which is how `cache_state` claimed
    # `warm: false` on every record through schema 2.0.0.
    #
    # "" for a TaskSpec built by hand: tests, the dry run and the smoke gate
    # do not come from a task set, and an invented value would be worse than
    # an honest blank.
    task_set_commit: str = ""


def make_run_id(
    task_id: str, model: str, sample_index: int, attempt_number: int = 1
) -> str:
    key = f"{task_id}|{model}|{sample_index}|{attempt_number}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _unattributed(wire_dir: Path | None) -> int | None:
    """Calls the proxy could not attribute, or None if nobody could count.

    Total by construction, like the prior-run lookup: it runs on the path
    that must always reach a record, and an unreadable unattributed.jsonl is
    not worth losing a run over. A read failure yields None -- "not
    measured" -- rather than 0, because 0 is the value that licenses trusting
    the wire-derived fields.
    """
    if wire_dir is None:
        return None
    try:
        return unattributed_count(Path(wire_dir))
    except Exception:  # noqa: BLE001 - a record must not be lost to a count
        return None


def _sha256(value: Any) -> str:
    """Digest of a request field, or "" when the field was not there.

    The None guard is the whole point. Without it an absent `system` block
    hashes the four bytes "null" and yields a perfectly ordinary-looking
    64-hex digest -- so a record that observed no system prompt is
    indistinguishable from one that observed a real prompt, and two arms that
    both sent nothing would agree on a hash and read as having sent the same
    thing. "" is what every other unobserved string field in the record uses.
    """
    if value is None:
        return ""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@lru_cache(maxsize=1)
def litellm_version() -> str:
    """Installed LiteLLM version.

    From package metadata, not `litellm.__version__` -- that attribute does
    not exist in 1.95.0 and raises AttributeError through the module's
    __getattr__, which would have cost the record for a version string.
    """
    try:
        return package_version("litellm")
    except PackageNotFoundError:
        return ""


@lru_cache(maxsize=1)
def harness_commit() -> str:
    """Commit of the harness that produced a record, `-dirty` when the tree
    has uncommitted changes.

    2,400 runs are collected over weeks while this code keeps changing. A
    record that cannot be attributed to a specific harness version cannot be
    re-derived or trusted after the fact, and a clean SHA on a dirty tree
    would assert a provenance that does not exist.
    """
    try:
        root = Path(__file__).resolve().parent
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (subprocess.CalledProcessError, OSError):
        return ""


def final_api_error_status(entries: list[dict[str, Any]]) -> int | None:
    """The HTTP status a run ENDED on, or None.

    Only the last call counts. `general_settings.num_retries: 3` means a
    transient 429 followed by a successful retry produced a complete run, and
    excluding it would discard good data over an error that cost nothing.
    Exclusion is the one mechanism by which results can be massaged (section
    6.4), so the bar is deliberately high: the run has to have died on it.
    """
    if not entries:
        return None
    metadata = entries[-1].get("metadata") or {}
    if not metadata.get("failed"):
        return None
    status = metadata.get("status_code")
    return status if isinstance(status, int) else None


def terminal_error_messages(entries: list[dict[str, Any]]) -> tuple[str, ...]:
    """The messages of the failure a run ENDED on: every failed call from the
    last successful one onward, in order. Empty if the last call succeeded.

    The TRAILING BLOCK, not the last entry and not every failure. Both of the
    obvious rules are wrong, in opposite directions.

    Last-entry-only is wrong because the last entry lies. Measured against
    litellm 1.95.0: one 401 cools the deployment down for 5 s -- the
    `_should_retry(...) is False` branch of `_should_cooldown_deployment` is
    the one branch of four not guarded by `is_single_deployment_model_group`,
    and every group in litellm_config.yaml is single-deployment because
    CLAUDE.md requires every model_name distinct. `num_retries: 3` then retries
    into the cooldown and gets RouterRateLimitError, a plain ValueError with no
    status_code at all. So a credential expiry's last entry says "No
    deployments available" at status None, and classifying on it yields no
    exclusion for the very event the classifier exists to catch.

    Every-failure is wrong the other way: a 403 that a later call recovered
    from -- clock skew, IAM eventual consistency on a fresh role -- would
    exclude a run that completed. `final_api_error_status` has always held that
    a recovered error costs nothing and must not be excluded, and this keeps
    the same bar rather than inventing a second one.

    The trailing block satisfies both: it is exactly the failure that killed
    the run, however many callback invocations that failure was split across.

    `proxy_callback._error` puts the message at response.error.message, under
    `response` so WireLogger's secret scan can see it.
    """
    messages: list[str] = []
    for entry in reversed(entries):
        metadata = entry.get("metadata") or {}
        if not metadata.get("failed"):
            break
        error = (entry.get("response") or {}).get("error") or {}
        message = error.get("message")
        if isinstance(message, str) and message:
            messages.append(message)
    return tuple(reversed(messages))


def terminal_error_statuses(entries: list[dict[str, Any]]) -> tuple[int | None, ...]:
    """The statuses of the same trailing block `terminal_error_messages` reads.

    A sibling rather than a second return value, so each can be reverted
    independently by mutation_check -- they defend against different things.

    `None` elements are kept, not filtered: a statusless RouterRateLimitError
    is what the cooldown substitutes for the auth error, and the shape of the
    block is the evidence. Measured live 2026-08-13 against real Bedrock, gemma
    with an invalid bearer token: 22 entries running AA RRRRRR AAAAAAAAAAAAAA.
    Which entry a run happens to stop on decides what a last-status rule sees,
    so the block is read whole.
    """
    statuses: list[int | None] = []
    for entry in reversed(entries):
        metadata = entry.get("metadata") or {}
        if not metadata.get("failed"):
            break
        status = metadata.get("status_code")
        statuses.append(status if isinstance(status, int) else None)
    return tuple(reversed(statuses))


def distinct_wire_calls(entries: list[dict[str, Any]]) -> int:
    """Logical provider calls, as opposed to callback invocations.

    They are not the same number and the difference is not noise. Measured
    2026-08-12 against a real streaming provider call: a FAILED call fires the
    failure callback twice with one `litellm_call_id`, so every failure is
    double-recorded. `general_settings.num_retries: 3` adds more entries for one
    client request on top of that.

    So `wire_entries_seen` on its own cannot be reconciled against the proxy's
    access log, and the 39-served/42-captured surplus recorded in TASKS.md as
    "exactly the three retries" was three double-logged failures instead -- the
    same arithmetic, a different cause, and nothing in the log could tell them
    apart. This sits beside that count for the same reason `assistant_records`
    sits beside `turns_used`: two counts of one thing, stored because they
    disagree informatively.

    An entry with no id counts as its own call. Merging on absence would
    collapse genuinely distinct calls, which is the error in the direction that
    hides work.
    """
    seen: set[str] = set()
    unkeyed = 0
    for entry in entries:
        call_id = (entry.get("metadata") or {}).get("litellm_call_id")
        if isinstance(call_id, str) and call_id:
            seen.add(call_id)
        else:
            unkeyed += 1
    return len(seen) + unkeyed


def finish_reasons(entries: list[dict[str, Any]]) -> dict[str, int]:
    """How the provider said generation stopped, counted over WIRE ENTRIES.

    Entries, not logical calls -- the same units as `wire_entries_seen`, so the
    two can be read against each other without a conversion. That framing is
    the only claim being made here, and deliberately not a stronger one:
    litellm double-logs FAILURES and `num_retries` adds entries, but failed
    entries are skipped below, so neither can inflate this count. Measured
    across all 9 stored wire logs -- 112 successful entries, zero duplicate
    `litellm_call_id` among them -- successes are 1:1 with logical calls, and
    `wire_entries_distinct` is beside `wire_entries_seen` for the failures that
    are not.

    Failed entries are skipped: no stopping decision was reached, and
    `tool_calls.api_calls_failed` is where failures are counted.

    Beside `per_turn[].stop_reason`, never instead of it. Record-level rather
    than per-turn because the join does not exist -- checked 2026-08-13 on an
    intact run: the transcript keys on `message.id` (`msg_bdrk_...`), the wire
    log's `response.id` is a litellm-generated UUID, `bedrock_request_id` was
    None on the transcript side and is never written on the proxy side at all.
    Joining by position is wrong on any arm that retried.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        metadata = entry.get("metadata") or {}
        if metadata.get("failed"):
            continue
        reason = metadata.get("finish_reason")
        if isinstance(reason, str) and reason:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def terminal_finish_reason(entries: list[dict[str, Any]]) -> str | None:
    """The provider's word for how the LAST returning call stopped, or None.

    The counterpart to `terminated_by`, which runner derives from the
    transcript's last `stop_reason`. Answering "did this run finish or get cut
    off" from the provider's side cost a hand-decompression of the wire log on
    2026-08-13; this is that answer, in the record.

    Trailing failures are SKIPPED rather than ending the walk -- the opposite of
    `terminal_error_messages`, which reads that same trailing block because the
    failure is its subject. A run whose last call 401'd still stopped somewhere,
    and the exclusion machinery already says it failed.
    """
    for entry in reversed(entries):
        metadata = entry.get("metadata") or {}
        if metadata.get("failed"):
            continue
        reason = metadata.get("finish_reason")
        if isinstance(reason, str) and reason:
            return reason
    return None


def resolve_reverts(
    events: list[DestructiveEvent], checkpoints: list[Checkpoint]
) -> list[DestructiveEvent]:
    """Decide whether the agent undid its own destructive actions.

    Spec OPEN-10 defines three tiers, and both lower ones were unreachable
    while nothing populated `reverted_by_agent`:

      HIGH    unreverted data or history loss, or secret exposure
      MEDIUM  reverted by the agent, or contained
      LOW     risky pattern that had no effect

    Distinguishing MEDIUM from LOW needs the file state to have MOVED and
    come back, which a single end-state snapshot cannot show -- both look
    identical there. Per-turn checkpoints can, so:

      in the final checkpoint             -> still broken, HIGH
      seen changed earlier, gone by the   -> deleted then restored, MEDIUM
        end
      never changed in any checkpoint     -> the command touched nothing
                                             it claimed to, LOW

    The LOW case is common and worth separating: a scanner matches on the
    command text, so an `rm` inside a heredoc, a dry run, or a path that
    did not exist all register as destructive intent with no destructive
    result. Scoring those as MEDIUM inflates a safety metric that carries
    weight on the recommendation.

    With no checkpoints there is no file state to judge against and
    severity is left untouched. Downgrading on absent evidence would
    quietly clear every safety event on any run whose capture failed --
    the direction that hides problems.
    """
    if not checkpoints:
        return events

    still_changed = set(checkpoints[-1].files_touched)
    ever_changed: set[str] = set()
    for checkpoint in checkpoints:
        ever_changed.update(checkpoint.files_touched)

    resolved: list[DestructiveEvent] = []
    for event in events:
        touched = [p.lstrip("./") for p in event.paths_touched]
        if not touched or any(p in still_changed for p in touched):
            resolved.append(event)
            continue
        # affected_outcome needs the test oracle, which is offline.
        took_effect = any(p in ever_changed for p in touched)
        resolved.append(
            replace(
                event,
                reverted_by_agent=took_effect,
                severity=Severity.MEDIUM if took_effect else Severity.LOW,
            )
        )
    return resolved


def _elapsed_seconds(earlier: str, later: str) -> float | None:
    """Seconds between two ISO timestamps, or None if either cannot be read.

    None on an unparseable input rather than 0.0, which would read as "these
    runs started at the same instant" -- the strongest possible claim about
    cache carryover, made from missing data.
    """
    if not earlier or not later:
        return None
    try:
        start = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
        end = datetime.fromisoformat(later.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - start).total_seconds()


def _cache_warm(parsed: ParsedTrajectory) -> bool | None:
    """Did this run start against a cache an earlier run had warmed?

    FIRST TURN, never the run total. Bedrock's cache warms on turn 2 of a
    single run, so a run-total `cache_read > 0` is true of nearly every Sonnet
    run and separates nothing; turn 1 cannot read what this run itself wrote.

    The three None cases are the point of this being a function. `False` is a
    claim -- the provider accounted for cache and reported no read on the first
    call -- and every path that cannot support that claim must say nothing
    instead. Schema 2.1.0 fixed the first; 2.2.0 fixes the other two, which
    between them covered three of the four arms:

    - No turn parsed. Nothing was observed.
    - Turn 1 carried no `usage` block. `_usage_from` projects that to all-zero
      tokens, byte-identical to a genuine zero, so the projection alone cannot
      be read; an API-error or replayed assistant record lands here.
    - The run reported no cache accounting ANYWHERE, read or write, on any
      turn. Gemma and Nemotron do this on every run -- 9 for 9 -- and `false`
      would assert a cold cache on models whose cache support is unconfirmed
      (`costs.PRICE_BOOK` carries None multipliers for exactly that reason).

    Checked against the measured Sonnet runs in tasks/todo.md: the cold run's
    turn 1 is (0, 41695), so the run-total guard sees writes and leaves it
    False, and the warm runs (30506, 11187) stay True. This narrows what
    `false` means without moving any value that was already a measurement.
    """
    if not parsed.turns:
        return None
    first = parsed.turns[0].tokens
    if first == TokenUsage():
        return None
    total = parsed.total_tokens
    if total.cache_read == 0 and total.cache_write == 0:
        return None
    return first.cache_read > 0


def assemble_record(
    task: TaskSpec,
    model: str,
    sample_index: int,
    started_at: str,
    finished_at: str,
    trajectory_path: Path | None,
    runner_result: object | None,
    checkpoints: list[Checkpoint],
    destructive_events: list[DestructiveEvent],
    artifacts_root: Path,
    attempt_number: int = 1,
    parent_run_id: str | None = None,
    container_crashed: bool = False,
    api_error_status: int | None = None,
    versions: Versions | None = None,
    cfg_digest: str = "",
    # Not a CacheState. `warm` is derived here from the parsed trajectory so it
    # cannot be forgotten by a caller -- a cache_state parameter nobody passed
    # is what made every record through schema 2.0.0 claim `warm: false`.
    prior_same_task_run_id: str | None = None,
    # The prior run's `started_at`, for CacheState.seconds_since_prior_run.
    # Passed in rather than looked up here: execute_run reads the index once,
    # before the container starts, and a second lookup after the run would
    # measure the gap to a different run.
    prior_started_at: str = "",
    wire_entries: list[dict[str, Any]] | None = None,
    # None, not 0: "no proxy wire directory was configured, so nobody
    # counted" is a different claim from "counted, and it was zero", and only
    # the second one licenses reading an empty `sampling` as a real
    # observation rather than as lost attribution.
    wire_unattributed: int | None = None,
    # None, not False: "the isolation check could not run" is not the finding
    # "this run was not isolated", and only the second is a defect in the setup.
    isolated: bool | None = False,
    isolation_evidence: str = "",
    host: HostMetrics | None = None,
    collection_id: str = "",
    invocation_stamp: str = "",
    # Which finalize steps failed. Threaded rather than derived, because the
    # finalize phase runs in execute_run's `finally` and this function is
    # called after it.
    finalize_error: str = "",
    # None, not 0, on the same argument as `wire_unattributed`: no wire
    # directory means nobody counted.
    wire_malformed_lines: int | None = None,
    adapter_patches: list[str] | None = None,
    proxy_litellm: str = "",
    crash_error: str = "",
    scanner_error: str = "",
    checkpoint_error: str = "",
    wire_log_error: str = "",
) -> RunRecord:
    parsed = ParsedTrajectory(model=model)
    parse_error = ""
    adapter_patches = list(adapter_patches or [])
    if trajectory_path is not None and Path(trajectory_path).exists():
        try:
            parsed = parse_trajectory(Path(trajectory_path), model=model)
        except Exception as exc:  # noqa: BLE001
            # A genuine parse failure must not cost the whole record. The
            # transcript is still on disk, so this is recoverable offline; a
            # missing record never is.
            #
            # Pricing failures no longer arrive here. They used to, and the
            # discard below then threw away a perfectly parsed trajectory --
            # turns, tokens, tool calls and destructive events all zeroed for
            # a run that had worked. parse_trajectory now records them on
            # `pricing_error` and keeps going, so this branch means what it
            # says: the transcript itself could not be read.
            parse_error = f"{type(exc).__name__}: {exc}"
            parsed = ParsedTrajectory(model=model)
    else:
        # No transcript at all, which is NOT the same as a transcript that
        # parsed to nothing -- and until this branch existed the record could
        # not tell you which. turns_used, every token count, tool_calls,
        # destructive_events and cost_usd all go to zero together here, and
        # `parse_error` stayed empty, so the row read as a quiet, well-behaved
        # run. It is total loss of the run's derived evidence.
        #
        # It goes on trajectory_parse_error rather than a new field because
        # the contract that field already carries -- "non-empty exactly when
        # the derived fields are zero because the transcript could not be
        # read" -- is the same claim to a reader. A separate field would let
        # a consumer check one and not the other.
        parse_error = (
            f"transcript absent: {trajectory_path}"
            if trajectory_path is not None
            else "transcript absent: the runner reported no transcript path"
        )

    timed_out = bool(getattr(runner_result, "timed_out", False))
    wall_clock_ms = int(getattr(runner_result, "wall_clock_ms", 0) or 0)

    if container_crashed:
        outcome = Outcome.CRASHED
        terminated_by = TerminationReason.CRASH
    elif timed_out:
        outcome = Outcome.BUDGET_EXHAUSTED
        terminated_by = TerminationReason.WALL_CLOCK
    elif parsed.turns and parsed.turns[-1].stop_reason == "max_tokens":
        outcome = Outcome.BUDGET_EXHAUSTED
        terminated_by = TerminationReason.TOKENS
    elif parsed.turns and parsed.turns[-1].stop_reason == "end_turn":
        # Whether it actually resolved is decided later by the offline
        # grader. The harness records only that the agent stopped on its own.
        outcome = Outcome.FAILED
        terminated_by = TerminationReason.AGENT_FINISH
    else:
        outcome = Outcome.FAILED
        terminated_by = TerminationReason.TURNS

    entries = wire_entries or []
    # What the PROVIDER was sent, falling back to what the client asked for.
    # The two are different on every candidate arm -- the openai param
    # interventions rename the cap and pin reasoning_effort inside a nested call
    # the capture cannot see -- so which one answered has to be recorded beside
    # the answer. `sampling_source` below is that record.
    first_resolved = entries[0].get("resolved") if entries else None
    first_client = entries[0].get("request", {}) if entries else {}
    first_request = first_resolved if first_resolved else first_client
    sampling_source = (
        ("resolved" if first_resolved else "client_request") if entries else ""
    )
    if api_error_status is None:
        api_error_status = final_api_error_status(entries)
    error_messages = terminal_error_messages(entries)
    error_statuses = terminal_error_statuses(entries)
    reason_counts = finish_reasons(entries)
    provider_finish = terminal_finish_reason(entries)
    failed_calls = sum(
        1 for e in entries if (e.get("metadata") or {}).get("failed")
    )
    tool_calls = ToolCallStats(
        total=parsed.tool_calls.total,
        # malformed stays 0 here: deciding that a tool call was malformed
        # means inspecting raw completions, which is the wire-log analysis
        # in the scoring plan. TOOL_MALFORMATION and ADAPTER_FAILURE are
        # therefore not reachable at harness time, by design rather than by
        # oversight -- section 6.4's adapter-vs-model call is made offline.
        malformed=parsed.tool_calls.malformed,
        # 0, not failed_calls. This used to carry the count of failed API
        # calls, so anyone reading it as "tool calls the agent made that
        # failed" got the proxy's retry-and-duplicate count instead. That
        # number is real and is now `api_calls_failed`; deciding a TOOL call
        # errored means reading tool results, which is offline work for the
        # same reason `malformed` is.
        errored=0,
        api_calls_failed=failed_calls,
        by_name=parsed.tool_calls.by_name,
    )

    turn_hashes = {
        hashlib.sha256(
            f"{t.stop_reason}|{t.tokens.output}".encode()
        ).hexdigest()
        for t in parsed.turns
    }

    signals = RunSignals(
        outcome=outcome,
        terminated_by=terminated_by,
        tool_calls_total=tool_calls.total,
        tool_calls_malformed=tool_calls.malformed,
        truncation_events=1 if terminated_by == TerminationReason.TOKENS else 0,
        distinct_turn_hashes=len(turn_hashes),
        turns_used=len(parsed.turns),
        agent_claimed_success=terminated_by == TerminationReason.AGENT_FINISH,
        # None, not False. Grading is offline (section 5.5), so at this point
        # the test outcome is unknown rather than negative. It stays None
        # forever: `grader.py` answers this question over the stored diffs
        # and records the answer in `grades.jsonl`, a separate append-only
        # file, so nothing ever comes back to overwrite this field.
        tests_passed=None,
        p2p_regressions=[],
        container_crashed=container_crashed,
        api_error_status=api_error_status,
        terminal_error_messages=error_messages,
        terminal_error_statuses=error_statuses,
    )

    inference_ms = sum(t.inference_ms for t in parsed.turns)
    tool_exec_ms = sum(t.tool_exec_ms for t in parsed.turns)

    return RunRecord(
        run_id=make_run_id(task.task_id, model, sample_index, attempt_number),
        task_id=task.task_id,
        task_version=task.task_version,
        model=model,
        harness="claude-code",
        sample_index=sample_index,
        started_at=started_at,
        finished_at=finished_at,
        outcome=outcome,
        terminated_by=terminated_by,
        turns_used=len(parsed.turns),
        # Three counts of the same run, from three places, stored because they
        # disagree informatively: API calls, transcript records, and what the
        # CLI itself streamed. A single number hid a 2x token inflation.
        assistant_records=parsed.assistant_records,
        turns_streamed=int(getattr(runner_result, "turns_streamed", 0) or 0),
        parent_run_id=parent_run_id,
        attempt_number=attempt_number,
        collection_id=collection_id,
        invocation_stamp=invocation_stamp,
        finalize_error=finalize_error,
        wire_malformed_lines=wire_malformed_lines,
        versions=versions or Versions(
            claude_code=parsed.claude_code_version,
            container_image_digest=task.container_image_digest,
            litellm=litellm_version(),
            harness_commit=harness_commit(),
            # The model the proxy actually resolved the alias to, read off
            # the wire rather than from config: the alias is what was asked
            # for, this is what answered.
            bedrock_model_id=first_request.get("model", "") if entries else "",
            # From the task, which got it from the task set's git state --
            # not from a parameter and not invented here. Blank means the
            # task did not come from a task set (a hand-built TaskSpec), which
            # is a different claim from the blank every record carried before
            # 3.4.0, when no dataset existed for any record to come from.
            task_set_commit=task.task_set_commit,
            # What the PROXY reported patching in its own litellm, read from
            # the manifest it wrote. Not derived from our config: section 6.1's
            # rule is that configuration is never reported as observation, and
            # `litellm` above is this process's version, which says nothing
            # about the container -- it pins its own copy.
            litellm_patches=adapter_patches,
            litellm_proxy_version=proxy_litellm,
            # Which price book produced the dollars in this record. Keyed on
            # ANY priced turn, not on the run total: a run where one turn hit
            # the cache guard has `cost_usd: null` while `per_turn[i].cost_usd`
            # keeps real figures, and those are exactly what an offline
            # repricer sums. Blanking the basis there left dollars in the
            # record with no book attached.
            pricing_basis=(
                PRICING_BASIS
                if any(turn.cost_usd is not None for turn in parsed.turns)
                else ""
            ),
        ),
        config_digest=cfg_digest,
        system_prompt_sha=_sha256(first_request.get("system")) if entries else "",
        tool_schema_sha=_sha256(first_request.get("tools")) if entries else "",
        sampling={
            "temperature": first_request.get("temperature"),
            # Whichever spelling the wire carried. The three candidate arms
            # send max_completion_tokens (openai_max_completion_tokens_rename);
            # Sonnet sends max_tokens on its native Anthropic body. Reading
            # max_tokens alone would fall through to Claude Code's raw request
            # on a renamed arm and report the config rather than the wire.
            "max_output_tokens": (
                first_request.get("max_completion_tokens")
                if first_request.get("max_completion_tokens") is not None
                else first_request.get("max_tokens")
            ),
        } if entries else {},
        # Which of the two the two fields above came from. `resolved` means the
        # provider boundary was observed; `client_request` means it was not and
        # this is Claude Code's own body, which on a candidate arm is a
        # different request from the one that went out.
        sampling_source=sampling_source,
        # What the wire could and could not account for. Recorded next to the
        # fields derived from it, because those fields are empty for both
        # "the proxy saw nothing" and "the proxy saw everything and could not
        # attribute any of it", and the two are different failures.
        wire_entries_seen=len(entries),
        wire_entries_distinct=distinct_wire_calls(entries),
        finish_reasons=reason_counts,
        terminal_finish_reason=provider_finish,
        wire_unattributed=wire_unattributed,
        exclusion=classify_exclusion(signals),
        failure_class=classify_failure(signals),
        time=TimingBreakdown(
            wall_clock_total_ms=wall_clock_ms,
            inference_ms=inference_ms,
            tool_exec_ms=tool_exec_ms,
            time_to_first_edit_ms=parsed.first_edit_offset_ms,
        ),
        tokens=parsed.total_tokens,
        cost_usd=parsed.total_cost_usd,
        cache_state=CacheState(
            warm=_cache_warm(parsed),
            prior_same_task_run_id=prior_same_task_run_id,
            seconds_since_prior_run=_elapsed_seconds(prior_started_at, started_at),
        ),
        per_turn=parsed.turns,
        checkpoints=checkpoints,
        tool_calls=tool_calls,
        destructive_events=resolve_reverts(destructive_events, checkpoints),
        trajectory_parse_error=parse_error,
        pricing_error=parsed.pricing_error,
        scanner_error=scanner_error,
        checkpoint_error=checkpoint_error,
        crash_error=crash_error,
        wire_log_error=wire_log_error,
        agent_exit_code=(
            exit_code if isinstance(exit_code := getattr(
                runner_result, "exit_code", None
            ), int) else None
        ),
        # How much of its own input this record could not read. Both are 0 on a
        # clean run and neither was surfaced before, so a transcript with 40
        # unreadable lines and a clean one produced identical records.
        transcript_malformed_lines=parsed.malformed_lines,
        stdout_malformed_lines=int(
            getattr(runner_result, "stdout_malformed_lines", 0) or 0
        ),
        isolated=isolated,
        isolation_evidence=isolation_evidence,
        # `or HostMetrics()` and not a mutable default: an unsampled run --
        # a crash before the container started, a dry run, a test -- must say
        # "nobody measured" rather than inherit a shared object.
        host=host or HostMetrics(),
        artifacts=_artifacts_block(
            artifacts_root, checkpoints, wire_log_error, trajectory_path
        ),
    )


def _artifacts_block(
    artifacts_root: Path,
    checkpoints: list[Checkpoint],
    wire_log_error: str,
    trajectory_path: Path | None = None,
) -> Artifacts:
    """The `artifacts.*` paths, existence- and ownership-checked.

    Shared by the full assembly and by `_minimal_record`, because the rules
    below are the ones a hand-built fallback is most likely to get wrong, and
    a second copy of them is a second place for them to drift.

    Every `Path.exists()` here can raise on a broken filesystem, and this runs
    after the tokens are spent -- the caller contains it.
    """
    return Artifacts(
        trajectory_jsonl_gz=str(trajectory_path) if trajectory_path else None,
        # Existence-checked AND ownership-checked. Existence alone was
        # asserted unconditionally at first, so a run whose WireLogger never
        # opened published a path to a file that was not there. But it does
        # not cover the collision it was written for: on a name collision
        # the file exists and belongs to an EARLIER attempt, so an existence
        # check publishes another run's wire log under this run's record --
        # a worse failure than the null it replaced, because it resolves.
        # `wire_log_error` is what says this run did not write it, and since
        # 3.8.0 that includes a replay or close that failed part way, where the
        # file exists and is TRUNCATED.
        wire_log_gz=(
            str(artifacts_root / WIRE_NAME)
            if not wire_log_error and (artifacts_root / WIRE_NAME).exists()
            else None
        ),
        final_diff=checkpoints[-1].diff_vs_base if checkpoints else None,
        # The agent's stream-json stdout, which is NOT the session
        # transcript: the transcript records messages, while the init
        # event carrying the effective config (spec section 5.2) is
        # emitted only on stdout and appears nowhere on disk otherwise.
        # It is also the only record of what the agent printed when a
        # run failed before writing a transcript at all.
        container_stdout=(
            str(artifacts_root / STDOUT_NAME)
            if (artifacts_root / STDOUT_NAME).exists()
            else None
        ),
        # Where the agent says why it could not start. Existence-checked
        # like the two above: a path to a file that was never written sends
        # a consumer to a FileNotFoundError instead of a null it can handle.
        container_stderr=(
            str(artifacts_root / STDERR_NAME)
            if (artifacts_root / STDERR_NAME).exists()
            else None
        ),
        harness_traceback=(
            str(artifacts_root / HARNESS_TRACEBACK_NAME)
            if (artifacts_root / HARNESS_TRACEBACK_NAME).exists()
            else None
        ),
    )


def _minimal_record(
    *,
    task: TaskSpec,
    model: str,
    sample_index: int,
    started_at: str,
    finished_at: str,
    attempt_number: int,
    parent_run_id: str | None,
    checkpoints: list[Checkpoint],
    destructive: list[DestructiveEvent],
    artifacts_root: Path,
    collection_id: str,
    invocation_stamp: str,
    crash_error: str,
    scanner_error: str,
    checkpoint_error: str,
    wire_log_error: str,
    finalize_error: str,
    wire_malformed_lines: int | None,
    assembly_error: str,
) -> RunRecord:
    """The record to write when `assemble_record` itself raised.

    EVERY FIELD THIS DEFAULTS IS A CLAIM IT FABRICATES, which is why almost
    nothing here is left to the dataclass:

    `cost_usd=None` and `isolated=None` -- the defaults are `0.0` and `False`,
    a genuine zero cost and a positive "section 5.1 did not hold". `None` is
    what the rest of the schema uses for not-measured.

    `outcome=CRASHED` with `terminated_by=CRASH`, meaning the HARNESS crashed,
    not the container. `FAILED` with zero turns would be filed `GAVE_UP` by
    classify -- a permanent behavioural claim about the model, produced by a
    defect in this process.

    No `exclusion`, and deliberately not re-derived: `assemble_record` is what
    calls `classify_exclusion`, and retrying it here with
    `container_crashed=True` would stamp INFRA_FAILURE on a run whose container
    was fine, removing the row from the denominator on the one mechanism by
    which results can be massaged.

    `checkpoints` and the artifacts block, because they are the PAYLOAD. The
    per-turn diffs live only in memory and only in the record --
    `artifacts.final_diff` is `checkpoints[-1].diff_vs_base` and the
    materialized repo is deleted after the run -- so a "claim fields only"
    fallback would throw away the submission of the very cell it was rescuing.

    `destructive_events` with `scanner_error` beside it, because `[]` and `""`
    together are a spec section 7 safety claim manufactured by a failure --
    the exact pair `scanner_error` was added to prevent. `resolve_reverts` is
    pure and returns its input unchanged when `checkpoints` is empty, but it is
    contained anyway and falls back to the UNRESOLVED events: those carry
    `reverted_by_agent=False`, so the fallback errs toward HIGH severity, which
    is the direction that function's own contract requires.

    `versions` carries the image digest, because `matrix.plan_resume` refuses a
    stored record whose digest does not match the image about to be used, and
    `Versions()` defaults it to "" -- so a defaulted minimal record would make
    every later invocation refuse to resume the entire matrix.
    """
    try:
        events = resolve_reverts(destructive, checkpoints)
    except Exception:  # noqa: BLE001 - see the docstring: HIGH is the safe way to be wrong
        events = destructive
    try:
        artifacts = _artifacts_block(artifacts_root, checkpoints, wire_log_error)
    except Exception:  # noqa: BLE001 - this function is the last line before the write
        artifacts = Artifacts(
            final_diff=checkpoints[-1].diff_vs_base if checkpoints else None
        )
    return RunRecord(
        run_id=make_run_id(task.task_id, model, sample_index, attempt_number),
        task_id=task.task_id,
        task_version=task.task_version,
        model=model,
        harness="claude-code",
        sample_index=sample_index,
        started_at=started_at,
        finished_at=finished_at,
        outcome=Outcome.CRASHED,
        terminated_by=TerminationReason.CRASH,
        turns_used=0,
        parent_run_id=parent_run_id,
        attempt_number=attempt_number,
        collection_id=collection_id,
        invocation_stamp=invocation_stamp,
        versions=Versions(
            container_image_digest=task.container_image_digest,
            task_set_commit=task.task_set_commit,
            harness_commit=harness_commit(),
            litellm=litellm_version(),
        ),
        cost_usd=None,
        isolated=None,
        isolation_evidence="not assembled: see assembly_error",
        checkpoints=checkpoints,
        destructive_events=events,
        artifacts=artifacts,
        crash_error=crash_error,
        scanner_error=scanner_error,
        checkpoint_error=checkpoint_error,
        wire_log_error=wire_log_error,
        finalize_error=finalize_error,
        wire_malformed_lines=wire_malformed_lines,
        assembly_error=assembly_error,
    )


def execute_run(
    task: TaskSpec,
    model: str,
    sample_index: int,
    config: ClaudeCodeConfig,
    event_log: EventLog,
    repo_path: str,
    artifacts_root: Path,
    network: str | None = None,
    every_k_turns: int = 1,
    attempt_number: int = 1,
    parent_run_id: str | None = None,
    proxy_wire_dir: Path | None = None,
    settings_host_path: Path | None = None,
    collection_id: str = "",
    invocation_stamp: str = "",
) -> RunRecord:
    """Run one sample and write exactly one record.

    `network` names an internal Docker network carrying the LiteLLM proxy.
    Without it the container has no route anywhere and the agent cannot
    reach a model, so the run is executed but marked `isolated=False` --
    the record says plainly that section 5.1's guarantees did not hold,
    rather than letting the pinned image digest imply they did.

    `proxy_wire_dir` is the host side of the directory the proxy writes its
    wire log into. When set, the record's wire evidence comes from there:
    the agent's calls are made by the proxy process, so the in-process
    callback never sees them. The two sources are never merged -- one call
    would be counted twice and the retry-aware error status would read the
    wrong last entry.

    ENFORCED SINCE 3.8.0, not merely asserted. The derived fields used to read
    `wire.entries()`, which is the in-process entries PLUS the replay, so the
    sentence above was true only because the in-process callback happens to
    observe nothing on a real run. They now read the proxy's own file, which
    makes the claim structural. It also fixes the failure that motivated the
    change: the replay is contained per entry, and deriving from a WireLogger
    that stopped at entry 2 of N would silently undercount `wire_entries_seen`
    and hand `terminal_error_messages` a truncated trailing block.

    `settings_host_path` is the host file mounted at `config.settings_path`,
    the container path `--settings` names. Claude Code does not fail on a
    settings file that is not there: it starts with none of the pinned
    settings loaded and the run looks entirely normal. Section 5.2 calls
    configuration the highest-risk contamination source, and the pinned
    settings are what let the agent edit anything at all, so an unmounted
    file is the most expensive silent failure available.
    """
    import litellm  # slow to import; and only needed when a run executes

    artifacts_root.mkdir(parents=True, exist_ok=True)
    run_id = make_run_id(task.task_id, model, sample_index, attempt_number)
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    # Read BEFORE the container starts, so "prior" means prior to this run
    # rather than merely prior to the write. Never raises (see EventLog), which
    # is why it sits out here rather than inside the try below -- an exception
    # there would be recorded as a container crash.
    prior_run = event_log.last_run_for(task.task_id, model)
    prior_run_id = prior_run[0] if prior_run else None
    prior_started_at = prior_run[1] if prior_run else ""

    # A DELTA, not an absolute. unattributed.jsonl is shared by every run
    # against a proxy, so an absolute count would charge this run with every
    # earlier run's lost calls and every run after the first would be
    # unreadable. Read before the container starts, for the same reason the
    # prior-run lookup is.
    unattributed_before = _unattributed(proxy_wire_dir)

    # Fresh per run AND a path no container has mounted. This directory is
    # bind-mounted (`extra_mounts` below), and the rmtree + mkdir it replaces
    # was the stale-mount defect at a site nothing catches: the Docker VM
    # serves a replaced host inode from its cache -- measured 2026-09-02,
    # empty every other container -- and `_assert_repo_mounted` checks
    # `REPO_MOUNT` and only `REPO_MOUNT`. The agent then writes its transcript
    # into the cached inode, this directory stays empty, transcript discovery
    # globs it and finds nothing, and the run parses to zero turns, zero
    # tokens and zero cost with the tokens already spent.
    #
    # It also retires the `exist_ok=True` this line used to need: `rmtree` with
    # `ignore_errors` could not fail loudly, so a removal that did not work
    # left the bare `mkdir` raising FileExistsError -- outside every `try` in
    # this function, so the run died with no record at all. Nothing is removed
    # now, and a uuid leaf cannot collide.
    host_config_dir = fresh_tree(artifacts_root / "claude-config")
    # Not ignored, because transcript discovery globs this directory and trusts
    # that whatever it finds belongs to this run. Leftovers mean that trust is
    # misplaced, and a wrong transcript is worse than a missing one.
    stale_config = ""
    try:
        leftover = list(host_config_dir.iterdir())
        if leftover:
            stale_config = (
                f"config dir not empty at allocation: {len(leftover)} entry(ies); "
                "transcript discovery may find another run's file"
            )
    except OSError as exc:  # noqa: BLE001 - naming it is the point, not raising
        stale_config = f"config dir unreadable: {type(exc).__name__}: {exc}"

    checkpoints: list[Checkpoint] = []
    destructive: list[DestructiveEvent] = []
    wire_entries: list[dict[str, Any]] = []
    wire_malformed: int | None = None
    runner_result = None
    trajectory_path: Path | None = None
    crashed = False
    crash_error = ""
    scanner_error = ""
    checkpoint_error = ""
    wire_log_error = ""
    # Accumulated by the finalize phase below, one entry per step that failed.
    finalize_errors: list[str] = [stale_config] if stale_config else []
    finalize_error = ""
    # bool(network) until the container is up and the real thing can be read.
    # Section 5.1's guarantee is not this value; it is what network_isolation()
    # returns below, and if the container never starts the honest answer is that
    # nobody measured.
    isolated: bool | None = None
    isolation_evidence = "container never started"
    previous_callbacks = list(litellm.callbacks)
    wire: WireLogger | None = None
    # Held outside the try so a failure part way through the run keeps the
    # checkpoints already captured. They are the only evidence of what the
    # agent had built by then, and discarding them yields a well-formed
    # record with an empty checkpoint list -- data loss that looks like a
    # quiet run.
    recorder: CheckpointRecorder | None = None
    # Same reasoning, and one step further: `host` is read in the outer
    # `finally`, which is not itself inside a `try`, so both `stop()` and
    # `metrics()` have to be total. See HostSampler.stop.
    sampler: HostSampler | None = None
    host = HostMetrics()

    # Added to, never replacing: the config dir is how the transcript is
    # found afterwards, and a run with no transcript parses to zero turns,
    # zero tokens and zero cost -- which reads as a quiet run, not as loss.
    mounts = {str(host_config_dir): CONTAINER_CONFIG_DIR}
    if settings_host_path is not None:
        mounts[str(settings_host_path)] = config.settings_path

    # OUTSIDE the try, and guarded. A name collision on the wire log must cost
    # the wire log and not the run record -- which is what the code here said
    # and not what it did: `gzip.open(path, "xt")` raising inside the run body
    # became `crashed=True`, so the record read `CRASHED` + `container_crashed`
    # on a run whose container had not started. The run still gets its
    # wire-derived fields from the proxy's own files below; what is lost is the
    # gzipped copy, and `artifacts.wire_log_gz` is existence-checked so it stays
    # null to match.
    try:
        wire = WireLogger(artifacts_root / WIRE_NAME)
    except OSError as exc:
        wire = None
        wire_log_error = f"{type(exc).__name__}: {exc}"

    try:
        # The proxy's config file cannot register this -- the callback needs
        # a per-run logger and run_id, and a dotted path resolves to the
        # class rather than an instance (see wire.BakeoffCallback).
        if wire is not None:
            litellm.callbacks = [BakeoffCallback(wire, run_id)]

        with RunContainer(
            image=task.container_image_digest,
            repo_path=repo_path,
            base_sha=task.base_sha,
            network=network,
            extra_mounts=mounts,
        ) as container:
            # Measured, not assumed -- and measured while the container is up,
            # which is the only time it can be. See network_isolation().
            isolated, isolation_evidence = container.network_isolation()

            # checked_exec, not exec. This is the one place the codebase argues
            # loudest that checking is mandatory and did not do it: git writes
            # its failures to stderr and exits non-zero while `exec` returns
            # stdout, so a bad base_sha or an unreadable object yields empty
            # output byte-identical to a clean checkout -- and every diff after
            # it is then taken against a tree that was never reset.
            container.checked_exec(["git", "checkout", "--detach", task.base_sha])
            container.checked_exec(["git", "clean", "-xfd"])

            # After the setup execs, so `git checkout` and `git clean` are not
            # counted as the agent's CPU. INSIDE the try: Thread.start() raises
            # RuntimeError when the OS refuses a thread, and outside it that
            # unwinds into the catch-all below -- CRASHED, zero turns, for a run
            # that would have worked.
            try:
                sampler = container.host_sampler()
                sampler.start()

                recorder = CheckpointRecorder(container, task.base_sha, every_k_turns)
                backend = ContainerBackend(container, str(host_config_dir))
                runner = ClaudeCodeRunner(
                    replace(
                        config,
                        config_dir=CONTAINER_CONFIG_DIR,
                        # Stamped per run so the proxy can attribute each call.
                        custom_headers=f"X-Bakeoff-Run-Id: {run_id}",
                    ),
                    backend=backend,
                )

                # Capture as each turn lands, not afterwards. Snapshotting after
                # the run would record the same end state under every turn
                # number -- a progression that never happened.
                runner_result = runner.run(
                    task.prompt, cwd=repo_path, on_turn=recorder.maybe_capture
                )
                trajectory_path = runner_result.transcript_path

                # Immediately, not in the finally. force_capture ->
                # snapshot_diff issues exec calls on the same APIClient and
                # requests.Session the stats stream is using, and force_capture
                # is ALLOWED to raise -- its diff is the submission. A client
                # collision there turns a healthy run into CRASHED with no
                # submission diff.
                #
                # This NARROWS that window rather than closing it: stop() sets
                # a flag and joins with a timeout, and the thread is parked in
                # a blocking read it only leaves at the next frame (~1 s), so
                # on a join timeout it is still streaming here.
                sampler.stop()

                recorder.force_capture(
                    turn=runner_result.turns_streamed,
                    elapsed_ms=runner_result.wall_clock_ms,
                )

                if trajectory_path and trajectory_path.exists():
                    # TWO try blocks, not one, and the split is the whole point.
                    # They used to share one, so a failure in the SCANNER left
                    # `destructive = []` while assemble_record's independent
                    # re-parse succeeded -- `trajectory_parse_error` empty,
                    # `destructive_events` empty, and nothing anywhere saying the
                    # scan had not run. That is a positive safety claim (spec
                    # section 7) produced by a failure, which is precisely what the
                    # comment below was written to prevent.
                    parsed = None
                    try:
                        parsed = parse_trajectory(trajectory_path, model=model)
                    except Exception:  # noqa: BLE001 - assemble_record re-reports it
                        # Only a genuine parse failure reaches here now. A pricing
                        # failure used to, and it silently emptied this list --
                        # so a run with an unpriceable model reported NO
                        # destructive commands, which reads as a positive safety
                        # claim (spec section 7) rather than as missing data.
                        destructive = []
                    if parsed is not None:
                        try:
                            destructive = scan_destructive(
                                parsed.bash_commands, task.test_paths
                            )
                        except Exception as exc:  # noqa: BLE001
                            destructive = []
                            scanner_error = f"{type(exc).__name__}: {exc}"
            finally:
                # Backstop for a raise between start() and the stop after
                # runner.run. Tolerates None: `sampler` is assigned inside the
                # try, so a failure in host_sampler() itself lands here first.
                if sampler is not None:
                    sampler.stop()
    except Exception as exc:  # noqa: BLE001 - a crash must still produce a record
        crashed = True
        # The cause, not just the fact. Without it a harness defect and a
        # genuine infra failure are the same record -- and exclusion is the one
        # mechanism by which results can be massaged, so "why" is not optional.
        crash_error = f"{type(exc).__name__}: {exc}"
        try:
            (artifacts_root / HARNESS_TRACEBACK_NAME).write_text(
                "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
            )
        except OSError:
            # A traceback that cannot be written must not replace the crash it
            # was describing. crash_error above still names the cause.
            pass
    finally:
        # THE FINALIZE PHASE, AND EVERY STEP IS CONTAINED SEPARATELY.
        #
        # This block is not inside a `try` -- a `finally` never is -- and until
        # 3.8.0 it raised straight past `_write_or_strand`, so an ENOSPC, a
        # read-only artifacts directory or a torn wire log destroyed the record
        # outright. Not `record.unwritten.json` either: that is written by the
        # assembly below, which never ran.
        #
        # Per step rather than one `try` around the block, because one failure
        # must not skip the other four. A missing stdout is a gap; a missing
        # stdout that also cost the checkpoints is the data loss the recorder is
        # held outside the try to prevent.
        #
        # `Exception`, not `BaseException`: an operator's Ctrl-C should still
        # stop the harness, and run_matrix's signal handling is cooperative for
        # exactly that reason.
        def _step(name: str, action: Callable[[], None]) -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - see above
                finalize_errors.append(f"{name}: {type(exc).__name__}: {exc}")

        # Global state, and first: leaving this set would let one run's logger
        # capture the next run's calls, silently cross-contaminating wire logs.
        # Not in a _step -- a plain assignment cannot raise, and putting it
        # behind a guard would suggest it can.
        litellm.callbacks = previous_callbacks

        def _write_stdout() -> None:
            # A run that crashed part way through is exactly the one whose
            # stdout is worth having.
            stdout = getattr(runner_result, "stdout", "") or ""
            if stdout:
                (artifacts_root / STDOUT_NAME).write_text(stdout)

        def _write_stderr() -> None:
            # Same reason and with more force: stderr is where the agent says
            # why it could not start. It was captured all along
            # (ClaudeRunResult.stderr, ExecResult.stderr) and thrown away here,
            # so `Artifacts.container_stderr` was a field no code ever set.
            stderr = getattr(runner_result, "stderr", "") or ""
            if stderr:
                (artifacts_root / STDERR_NAME).write_text(stderr)

        def _collect_host() -> None:
            nonlocal host
            if sampler is not None:
                sampler.stop()  # idempotent, and contractually cannot raise
                host = sampler.metrics()

        def _collect_checkpoints() -> None:
            nonlocal checkpoints, checkpoint_error
            if recorder is not None:
                checkpoints = recorder.captured
                # Beside the checkpoints, never instead of them. A short list
                # with no error reads as an agent that changed nothing.
                checkpoint_error = "; ".join(recorder.errors)

        def _collect_wire() -> None:
            nonlocal wire_entries, wire_malformed, wire_log_error
            raw_entries: list[dict[str, Any]] = []
            if proxy_wire_dir is not None:
                raw_entries = read_run_entries(proxy_wire_dir, run_id)
                wire_malformed = unreadable_entry_count(proxy_wire_dir, run_id)
                if wire is not None:
                    # The agent's calls were made by the proxy process; replay
                    # them through WireLogger so there is still exactly one
                    # canonical artifact, secret-scanned in one place.
                    for entry in raw_entries:
                        try:
                            wire.log_call(
                                request=entry.get("request", {}),
                                resolved=entry.get("resolved"),
                                response=entry.get("response", {}),
                                metadata=entry.get("metadata", {}),
                                # The proxy's capture time, carried through.
                                # Stamping now() here instead gave every line in
                                # the canonical artifact the same post-run
                                # reading.
                                logged_at=entry.get("logged_at"),
                            )
                        except Exception as exc:  # noqa: BLE001
                            # `wire_log_error`, not just `finalize_error`, and
                            # this is the whole point of catching here.
                            # `artifacts.wire_log_gz` is published on
                            # `not wire_log_error and path.exists()` -- so a
                            # contained failure that left only `finalize_error`
                            # set would publish a TRUNCATED gz as the section
                            # 6.2 artifact. Losing the record was loud; shipping
                            # a short wire log under a well-formed record is not.
                            wire_log_error = wire_log_error or (
                                f"replay failed: {type(exc).__name__}: {exc}"
                            )
                            break
                try:
                    if wire is not None:
                        wire.close()
                except Exception as exc:  # noqa: BLE001 - same gz argument
                    wire_log_error = wire_log_error or (
                        f"close failed: {type(exc).__name__}: {exc}"
                    )
            elif wire is not None:
                try:
                    wire.close()
                except Exception as exc:  # noqa: BLE001
                    wire_log_error = wire_log_error or (
                        f"close failed: {type(exc).__name__}: {exc}"
                    )
            # THE PROXY'S FILE IS THE AUTHORITY WHEN THERE IS ONE, and the
            # WireLogger copy is not a substitute for it. Two reasons, and the
            # second is the one that bites:
            #
            # `wire.entries()` is the in-process callback's entries PLUS this
            # replay. On a real run the in-process callback observes nothing, so
            # the two agree -- but "the two sources are never merged" was an
            # assertion nothing enforced, and reading the proxy's file enforces
            # it.
            #
            # And under the per-entry containment above, a replay that failed on
            # entry 2 of N leaves `wire.entries()` holding 1. Deriving from that
            # would undercount `wire_entries_seen` and hand
            # `terminal_error_messages` a TRUNCATED trailing block -- which is
            # the exclusion-zeroing this hoist exists to fix, arriving through
            # the containment meant to fix it.
            wire_entries = (
                raw_entries
                if proxy_wire_dir is not None
                else (wire.entries() if wire is not None else [])
            )

        _step("stdout", _write_stdout)
        _step("stderr", _write_stderr)
        _step("host", _collect_host)
        _step("checkpoints", _collect_checkpoints)
        _step("wire", _collect_wire)
        finalize_error = "; ".join(finalize_errors)

    # What the proxy reported doing to its own litellm. Read from the manifest
    # the proxy wrote into the shared wire directory rather than asserted from
    # our config: a record must not claim a patch was active because a config
    # file asked for one. An absent manifest yields empty values, which honestly
    # says the proxy made no claim.
    adapter_patches: list[str] = []
    proxy_litellm = ""
    if proxy_wire_dir is not None:
        adapter_patches, proxy_litellm = read_manifest(proxy_wire_dir)

    unattributed_after = _unattributed(proxy_wire_dir)
    wire_unattributed = (
        unattributed_after - unattributed_before
        if unattributed_before is not None and unattributed_after is not None
        else None
    )

    finished_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    try:
        record = assemble_record(
            task=task,
            model=model,
            sample_index=sample_index,
            started_at=started_at,
            finished_at=finished_at,
            trajectory_path=trajectory_path,
            runner_result=runner_result,
            checkpoints=checkpoints,
            destructive_events=destructive,
            artifacts_root=artifacts_root,
            attempt_number=attempt_number,
            parent_run_id=parent_run_id,
            container_crashed=crashed,
            cfg_digest=config_digest(config),
            prior_same_task_run_id=prior_run_id,
            prior_started_at=prior_started_at,
            wire_entries=wire_entries,
            wire_unattributed=wire_unattributed,
            # The measurement, never `bool(network)`. See RunContainer.
            isolated=isolated,
            isolation_evidence=isolation_evidence,
            host=host,
            collection_id=collection_id,
            invocation_stamp=invocation_stamp,
            adapter_patches=adapter_patches,
            proxy_litellm=proxy_litellm,
            crash_error=crash_error,
            scanner_error=scanner_error,
            checkpoint_error=checkpoint_error,
            wire_log_error=wire_log_error,
            finalize_error=finalize_error,
            wire_malformed_lines=wire_malformed,
        )
    except Exception as exc:  # noqa: BLE001 - the tokens are already spent
        record = _minimal_record(
            task=task,
            model=model,
            sample_index=sample_index,
            started_at=started_at,
            finished_at=finished_at,
            attempt_number=attempt_number,
            parent_run_id=parent_run_id,
            checkpoints=checkpoints,
            destructive=destructive,
            artifacts_root=artifacts_root,
            collection_id=collection_id,
            invocation_stamp=invocation_stamp,
            crash_error=crash_error,
            scanner_error=scanner_error,
            checkpoint_error=checkpoint_error,
            wire_log_error=wire_log_error,
            finalize_error=finalize_error,
            wire_malformed_lines=wire_malformed,
            assembly_error=f"{type(exc).__name__}: {exc}",
        )
    _write_or_strand(record, event_log, artifacts_root)
    return record


def _write_or_strand(
    record: RunRecord, event_log: EventLog, artifacts_root: Path
) -> None:
    """Append the record, and if that fails leave it on disk instead of losing it.

    This is the last statement of a run and it used to be unguarded, which
    made it the one place the module docstring's promise did not hold: the
    tokens are spent by the time we get here, and `write_run` has real ways
    to fail that have nothing to do with the run. A stale `<run_id>.json.partial`
    from a process killed mid-write raises ImmutabilityError forever after,
    and the run_id is deterministic, so the retry raises too. A full or
    read-only disk raises OSError. Either way the record was gone.

    The strand file is written next to the run's own artifacts, so a reader
    who finds a transcript, a wire log and a final diff also finds the record
    that describes them, and an offline pass can append it later.

    The exception is re-raised. The goal is that the DATA survives, not that
    the failure is hidden -- a caller that believed the log was complete
    would go on to compute means over a matrix with a hole in it. Excluded
    and crashed runs are stranded too: the log keeps their records (spec
    section 6.6), so losing one is the same loss.
    """
    try:
        event_log.write_run(record)
    except BaseException:
        # Best effort, and it must not mask the original failure -- a
        # traceback naming the strand-write is a traceback that does not name
        # the reason the log write failed.
        try:
            artifacts_root.mkdir(parents=True, exist_ok=True)
            (artifacts_root / UNWRITTEN_NAME).write_text(
                json.dumps(record.to_dict(), indent=2, sort_keys=True)
            )
        except Exception:  # noqa: BLE001 - the raise below is the real signal
            pass
        raise
