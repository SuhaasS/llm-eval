"""Spec section 6.6 gate. Twelve fault cases, each of which must produce a
complete, correctly classified record with nothing lost.

Two rules shape this file.

FIRST: a case is asserted as close to production as it can be. A 429
asserted directly at classify_exclusion passes today while the path that
would carry it in a real run does not exist -- execute_run never populates
api_error_status. Unit-level assertions on a classifier prove the classifier;
they prove nothing about whether the harness ever calls it. Where a case can
go through execute_run or assemble_record, it does.

SECOND: cases with no assertion available are stated as such rather than
skipped or faked. tool_calls.malformed is the example -- it is deliberately
deferred to the offline wire-log analysis, and a test that pins it at 0
keeps a future reader from misreading that 0 as "no malformations found".
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from bakeoff.claude_runner import ClaudeCodeConfig, RunnerResult
from bakeoff.classify import RunSignals, classify_exclusion
from bakeoff.eventlog import EventLog
from bakeoff.runner import TaskSpec, assemble_record, execute_run
from bakeoff.scanners import scan_destructive
from bakeoff.schema import (
    Checkpoint,
    ExclusionClass,
    FailureClass,
    Outcome,
    Severity,
    TerminationReason,
)
from bakeoff.trajectory import parse_trajectory
from bakeoff.wire import WireLogger


@pytest.fixture
def task():
    return TaskSpec(
        task_id="t-fault",
        task_version=1,
        repo="pindrop/example",
        base_sha="abc123",
        container_image_digest="python@sha256:" + "0" * 64,
        prompt="Fix it",
        test_paths=["tests/test_a.py"],
    )


def _assistant(
    index: int,
    stop_reason: str | None = None,
    tool: str | None = None,
    tool_input: dict | None = None,
    model: str = "gemma-4-31b",
    cache_read: int = 0,
) -> str:
    content = []
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": tool_input or {}})
    usage = {"input_tokens": 100, "output_tokens": 50}
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": f"2026-08-04T00:00:{index:02d}.000Z",
            "version": "2.1.220",
            "requestId": f"req-{index}",
            "message": {
                "model": model,
                "stop_reason": stop_reason,
                "usage": usage,
                "content": content,
            },
        }
    )


def _write(tmp_path: Path, lines: list[str], name: str = "t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


# --- a fake in-process run, so orchestration faults need no Docker -----------
#
# execute_run's failure behaviour is the subject of five of the twelve cases,
# and every one of them is about what survives when something breaks PART WAY
# THROUGH. That cannot be observed by mocking execute_run itself, and running
# real containers would make these cases integration-only -- which is exactly
# when a gate stops being run. So the container and the agent are faked and
# the orchestrator is real.


class FakeContainer:
    """Stands in for RunContainer. `fail_snapshot_after` models a disk
    filling up mid-run: the first N snapshots succeed, the next raises."""

    def __init__(self, fail_snapshot_after: int | None = None) -> None:
        self.fail_snapshot_after = fail_snapshot_after
        self.snapshots = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def exec(self, *_args, **_kwargs):
        return None

    def snapshot_diff(self, base_sha: str) -> tuple[str, list[str]]:
        self.snapshots += 1
        if (
            self.fail_snapshot_after is not None
            and self.snapshots > self.fail_snapshot_after
        ):
            raise OSError("No space left on device")
        return (f"diff-{self.snapshots}", [f"file{self.snapshots}.py"])


class FakeRunner:
    """Stands in for ClaudeCodeRunner. Fires `turns` boundaries, then either
    returns or raises -- an agent killed mid-stream."""

    def __init__(
        self,
        turns: int = 2,
        raise_after: int | None = None,
        transcript_path: Path | None = None,
    ) -> None:
        self.turns = turns
        self.raise_after = raise_after
        self.transcript_path = transcript_path

    def run(self, prompt, cwd, on_turn=None):
        for turn in range(1, self.turns + 1):
            if self.raise_after is not None and turn > self.raise_after:
                raise RuntimeError("container died mid-stream")
            if on_turn:
                on_turn(turn, turn * 1000)
        return RunnerResult(
            exit_code=0,
            transcript_path=self.transcript_path,
            stdout="",
            stderr="",
            wall_clock_ms=self.turns * 1000,
            timed_out=False,
            turns_streamed=self.turns,
        )


def _fake_run(
    monkeypatch,
    task,
    tmp_path,
    container=None,
    runner=None,
    container_raises=False,
    event_log=None,
    sample_index=0,
    artifacts_name="artifacts",
):
    import bakeoff.runner as runner_module

    box = container or FakeContainer()

    def make_container(**_kwargs):
        if container_raises:
            raise RuntimeError("docker daemon is not reachable")
        return box

    monkeypatch.setattr(runner_module, "RunContainer", make_container)
    monkeypatch.setattr(runner_module, "ContainerBackend", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_module, "ClaudeCodeRunner", lambda *a, **k: runner or FakeRunner()
    )

    config = ClaudeCodeConfig(
        model="gemma-4-31b",
        base_url="http://litellm:4000",
        auth_token="unused",
        settings_path="/eval/eval_settings.json",
        config_dir="/eval/claude-config",
        max_turns=60,
        wall_clock_timeout_s=300,
    )
    return execute_run(
        task=task,
        model="gemma-4-31b",
        sample_index=sample_index,
        config=config,
        event_log=event_log or EventLog(tmp_path / "log"),
        repo_path=str(tmp_path / "repo"),
        artifacts_root=tmp_path / artifacts_name,
    )


# --- the wiring, not a fault: cache_state must survive the whole orchestrator -


def test_execute_run_records_cache_state_and_the_run_that_warmed_it(
    task, tmp_path, monkeypatch
):
    """The assertion this file's FIRST rule exists for.

    Through schema 2.0.0 `cache_state` was a parameter nobody passed, so every
    record asserted `warm: false` -- a false claim, not an empty field, on the
    axis Sonnet's 2.9x cost spread turns on. A unit test on assemble_record
    would have passed the whole time, exactly as it did for api_error_status.
    So this goes through execute_run, and it checks the lookup as well as the
    measurement: the second run of a task must point at the first.

    The transcripts are the two real shapes. Run one reads 0 on turn 1 and
    30506 on turn 2 -- the cache warming WITHIN the run, which is why `warm`
    cannot be taken from the run total. Run two reads on turn 1, which it
    could only have got from run one.
    """
    log = EventLog(tmp_path / "log")

    cold = _write(
        tmp_path,
        [_assistant(1, cache_read=0), _assistant(2, "end_turn", cache_read=30506)],
        name="cold.jsonl",
    )
    warm = _write(
        tmp_path,
        [_assistant(1, cache_read=30506), _assistant(2, "end_turn", cache_read=41693)],
        name="warm.jsonl",
    )

    first = _fake_run(
        monkeypatch,
        task,
        tmp_path,
        runner=FakeRunner(transcript_path=cold),
        event_log=log,
        sample_index=0,
        artifacts_name="a0",
    )
    second = _fake_run(
        monkeypatch,
        task,
        tmp_path,
        runner=FakeRunner(transcript_path=warm),
        event_log=log,
        sample_index=1,
        artifacts_name="a1",
    )

    assert first.cache_state.warm is False
    assert first.tokens.cache_read > 0, "the run total would have said warm"
    assert first.cache_state.prior_same_task_run_id is None

    assert second.cache_state.warm is True
    assert second.cache_state.prior_same_task_run_id == first.run_id


# --- case 3: deliberately malformed tool call --------------------------------


def test_malformed_tool_call_does_not_lose_the_turn(tmp_path):
    """A tool_use block whose input is a string rather than an object is the
    shape LiteLLM's tool translation produces when it mis-serializes. The
    turn, its tokens and its cost must all survive: this is the evidence that
    decides adapter-failure versus model weakness (section 6.4), and it is
    unrecoverable if the parser drops the turn."""
    path = _write(
        tmp_path,
        [
            _assistant(1, stop_reason="tool_use", tool="Edit", tool_input={}),
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-04T00:00:02.000Z",
                    "message": {
                        "model": "gemma-4-31b",
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                        "content": [
                            {"type": "tool_use", "name": "Edit", "input": "not-an-object"}
                        ],
                    },
                }
            ),
        ],
    )
    parsed = parse_trajectory(path, model="gemma-4-31b")

    assert len(parsed.turns) == 2
    assert parsed.total_tokens.output == 100
    assert parsed.tool_calls.total == 2


def test_malformed_count_is_deliberately_deferred_not_measured(task, tmp_path):
    """tool_calls.malformed is 0 on every harness-written record BY DESIGN.

    Deciding a tool call was malformed means inspecting raw completions,
    which is the wire-log analysis in the scoring plan (section 6.4). This
    test exists so that 0 is never read as "the harness looked and found
    none" -- if malformation detection is ever moved into the harness, this
    test fails and forces the reader to be told.
    """
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=_write(tmp_path, [_assistant(1, tool="Edit")]),
        runner_result=None, checkpoints=[], destructive_events=[],
        artifacts_root=tmp_path,
    )
    assert record.tool_calls.total == 1
    assert record.tool_calls.malformed == 0
    assert record.failure_class is not FailureClass.TOOL_MALFORMATION


# --- case 4: token budget exhausted mid-edit ---------------------------------


def test_token_budget_exhausted_mid_edit_keeps_the_partial_work(task, tmp_path):
    """Truncated mid-edit is the case where the diff matters most: the agent
    was working, and the checkpoint holds however far it got. Recording the
    outcome while discarding the partial diff would lose the only evidence
    of what the token ceiling actually cost."""
    path = _write(
        tmp_path,
        [
            _assistant(1, stop_reason="tool_use", tool="Edit"),
            _assistant(2, stop_reason="max_tokens", tool="Edit", model="kimi-k2-5"),
        ],
    )
    partial = Checkpoint(
        turn=1, diff_vs_base="--- a/src/a.py\n+++ b/src/a.py\n@@\n+half a fix",
        files_touched=["src/a.py"], elapsed_ms=1000,
    )
    record = assemble_record(
        task=task, model="kimi-k2-5", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=path, runner_result=None, checkpoints=[partial],
        destructive_events=[], artifacts_root=tmp_path,
    )
    assert record.outcome == Outcome.BUDGET_EXHAUSTED
    assert record.terminated_by == TerminationReason.TOKENS
    assert record.failure_class is FailureClass.TRUNCATION
    assert record.checkpoints[0].diff_vs_base == partial.diff_vs_base
    assert record.artifacts.final_diff == partial.diff_vs_base


# --- case 5: turn budget exhausted -------------------------------------------


def test_turn_budget_exhausted_is_distinct_from_token_exhaustion(task, tmp_path):
    """--max-turns stops the agent between turns; a token ceiling stops it
    mid-generation. Section 5.4 sizes the two budgets separately, so
    collapsing them would make an under-sized turn cap look like model
    verbosity. A turn cap is also NOT an infra failure -- the model got every
    response it asked for."""
    path = _write(
        tmp_path,
        [_assistant(i, stop_reason="tool_use", tool="Bash") for i in range(1, 7)],
    )
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:30:00Z",
        trajectory_path=path, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )
    assert record.outcome == Outcome.FAILED
    assert record.terminated_by == TerminationReason.TURNS
    assert record.turns_used == 6
    assert record.exclusion is None
    assert record.failure_class is not FailureClass.TRUNCATION


# --- case 6: agent issues a destructive command ------------------------------


def test_destructive_command_survives_parse_scan_and_record(task, tmp_path):
    """The full chain, not scan_destructive alone. The scanner reports turn
    numbers from the trajectory and resolve_reverts joins them against
    checkpoints -- an off-by-one anywhere in that join silently files a
    deletion under the wrong turn, and the join is only exercised end to
    end."""
    path = _write(
        tmp_path,
        [
            _assistant(1, stop_reason="tool_use", tool="Edit"),
            _assistant(
                2, stop_reason="tool_use", tool="Bash",
                tool_input={"command": "rm -rf tests/test_a.py"},
            ),
        ],
    )
    parsed = parse_trajectory(path, model="gemma-4-31b")
    events = scan_destructive(parsed.bash_commands, task.test_paths)
    assert events and events[0].turn == 2

    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=path, runner_result=None,
        checkpoints=[
            Checkpoint(turn=2, diff_vs_base="d", files_touched=["tests/test_a.py"],
                       elapsed_ms=2000),
        ],
        destructive_events=events, artifacts_root=tmp_path,
    )
    event = record.destructive_events[0]
    assert event.turn == 2
    assert event.severity is Severity.HIGH  # still gone at the end
    assert event.reverted_by_agent is False


def test_a_destructive_command_survives_an_unpriceable_model(task, tmp_path):
    """Section 7's safety record must not depend on the price book.

    execute_run scans destructive commands off the parsed trajectory and
    empties the list on any parse exception. A cache-token pricing failure
    used to be such an exception, so an unpriceable arm reported NO
    destructive commands -- which reads as a positive safety claim rather
    than as missing data. The scan runs on gemma-4-31b usage carrying
    cache_read, which is exactly what used to raise.
    """
    path = _write(
        tmp_path,
        [
            _assistant(1, stop_reason="tool_use", tool="Edit", cache_read=4096),
            _assistant(
                2, stop_reason="tool_use", tool="Bash",
                tool_input={"command": "rm -rf tests/test_a.py"},
                cache_read=4096,
            ),
        ],
    )
    parsed = parse_trajectory(path, model="gemma-4-31b")

    # The pricing failed and the parse did not.
    assert parsed.total_cost_usd is None
    assert "cache support unconfirmed" in parsed.pricing_error

    events = scan_destructive(parsed.bash_commands, task.test_paths)
    assert events and events[0].turn == 2

    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=path, runner_result=None, checkpoints=[],
        destructive_events=events, artifacts_root=tmp_path,
    )
    assert record.destructive_events[0].turn == 2
    assert record.turns_used == 2
    assert record.tokens.cache_read == 8192
    assert record.cost_usd is None
    assert record.trajectory_parse_error == ""


def test_per_turn_costs_carry_their_price_basis_when_the_run_total_cannot(
    task, tmp_path
):
    """A partial pricing failure leaves real dollars in `per_turn` while
    `cost_usd` goes None. Keying the basis on the run total blanked it there,
    so those figures sat in the record with no book attached -- and they are
    exactly what an offline repricer sums.

    Turn 1 carries no cache token and prices; turn 2 carries one and raises.
    """
    path = _write(
        tmp_path,
        [
            _assistant(1, stop_reason="tool_use", tool="Read"),
            _assistant(2, stop_reason="end_turn", cache_read=4096),
        ],
    )
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=path, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )

    assert record.cost_usd is None, "one turn could not be priced"
    assert record.per_turn[0].cost_usd is not None, "the other one could"
    assert record.versions.pricing_basis != ""


def test_a_record_that_priced_nothing_claims_no_price_basis(task, tmp_path):
    """The mirror. A crashed run with no transcript has cost 0.0 by default,
    and stamping a basis on it would dress an absent measurement as a priced
    one."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path, container_crashed=True,
    )
    assert record.turns_used == 0
    assert record.versions.pricing_basis == ""


# --- case 7: the test harness itself crashes ---------------------------------


def test_harness_crash_before_the_agent_starts_still_writes_a_record(
    task, tmp_path, monkeypatch
):
    """An unreachable Docker daemon, a bad image reference, a bug in the
    orchestrator. None of them may cost the record: without one there is no
    evidence the run was attempted, and a silently missing run_id looks
    identical to a run that was never scheduled."""
    record = _fake_run(monkeypatch, task, tmp_path, container_raises=True)

    assert record.outcome is Outcome.CRASHED
    assert record.exclusion is not None
    assert record.exclusion.cls is ExclusionClass.INFRA_FAILURE
    assert record.exclusion.reason_code == "container_crashed"
    assert EventLog(tmp_path / "log").read_run(record.run_id) == record


# --- case 8: disk full during checkpoint write -------------------------------


def test_checkpoint_write_failure_keeps_the_checkpoints_already_captured(
    task, tmp_path, monkeypatch
):
    """A snapshot that raises on turn 2 must not discard turn 1.

    Those checkpoints were captured successfully and are the only record of
    what the agent had built by then. Throwing them away because a later
    snapshot failed is the exact 'nothing lost' violation section 6.6 is
    written to catch -- and it is invisible from the outside, because the
    record still looks well-formed with an empty checkpoint list.
    """
    record = _fake_run(
        monkeypatch, task, tmp_path,
        container=FakeContainer(fail_snapshot_after=1),
        runner=FakeRunner(turns=3),
    )

    assert record.outcome is Outcome.CRASHED
    assert len(record.checkpoints) == 1
    assert record.checkpoints[0].diff_vs_base == "diff-1"


# --- case 8b: disk full during the record write ------------------------------


def test_failed_record_write_leaves_no_phantom_index_entry(task, tmp_path, monkeypatch):
    """Every run_id in the index must have a record file behind it.

    Asserting only that the index is empty passes vacuously -- the index is
    never created when the very first write fails. The load-bearing version
    writes one good record first, so there is a populated index that a
    phantom entry could be appended to.
    """
    log = EventLog(tmp_path / "log")

    def build(task_id: str):
        return assemble_record(
            task=TaskSpec(
                task_id=task_id, task_version=1, repo="r", base_sha="s",
                container_image_digest="python@sha256:" + "0" * 64, prompt="p",
            ),
            model="gemma-4-31b", sample_index=0,
            started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
            trajectory_path=None, runner_result=None, checkpoints=[],
            destructive_events=[], artifacts_root=tmp_path,
        )

    good = build("t-good")
    log.write_run(good)

    def boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr("bakeoff.eventlog.json.dump", boom)
    with pytest.raises(OSError):
        log.write_run(build("t-doomed"))
    monkeypatch.undo()

    lines = (tmp_path / "log" / "index.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    for line in lines:
        run_id = json.loads(line)["run_id"]
        assert (tmp_path / "log" / "runs" / f"{run_id}.json").exists()
    assert not list((tmp_path / "log" / "runs").glob("*.partial"))


# --- case 8b: the log refuses the record, and the record survives anyway -----


def test_a_refused_write_strands_the_record_beside_its_own_artifacts(
    task, tmp_path, monkeypatch
):
    """The last statement of a run used to be unguarded.

    By the time `write_run` is called the tokens are spent, and the write has
    real ways to fail that say nothing about the run: a `<run_id>.json.partial`
    left by a process killed mid-write raises ImmutabilityError forever after
    -- and `run_id` is deterministic, so the retry raises too -- while a full
    disk raises OSError. Either way the record was gone, which is the one loss
    the module docstring says cannot be re-derived at any price.

    Both halves are asserted. The strand file has to be there AND the
    exception has to escape: a caller that believed the log was complete would
    go on to compute means over a matrix with a hole in it.
    """
    from bakeoff.runner import UNWRITTEN_NAME

    def boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr("bakeoff.eventlog.json.dump", boom)

    with pytest.raises(OSError):
        _fake_run(monkeypatch, task, tmp_path)

    stranded = tmp_path / "artifacts" / UNWRITTEN_NAME
    assert stranded.exists(), "the record was lost with the write"

    # A whole record, not a stub: it has to be appendable offline as-is.
    recovered = json.loads(stranded.read_text())
    assert recovered["task_id"] == task.task_id
    assert recovered["schema_version"]
    assert recovered["run_id"]


def test_a_stale_partial_does_not_silently_swallow_the_rerun(task, tmp_path, monkeypatch):
    """The concrete way A-2's guard gets exercised in production.

    A process killed between `open(tmp, "x")` and the rename leaves the
    .partial behind, and nothing cleans it up. Because run_id is derived from
    (task, model, sample, attempt), the natural fix -- run it again -- hits
    the same file and raises again.
    """
    from bakeoff.runner import UNWRITTEN_NAME, make_run_id

    log = EventLog(tmp_path / "log")
    run_id = make_run_id(task.task_id, "gemma-4-31b", 0, 1)
    runs = tmp_path / "log" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json.partial").write_text("{}")

    with pytest.raises(Exception):
        _fake_run(monkeypatch, task, tmp_path, event_log=log)

    assert (tmp_path / "artifacts" / UNWRITTEN_NAME).exists()


# --- case 8c: no transcript is loss, and must not read as a quiet run --------


def test_a_missing_transcript_is_recorded_as_loss_not_as_silence(
    task, tmp_path, monkeypatch
):
    """Zeroes are only readable as observation when the error field is empty.

    A run whose transcript never appeared sends turns_used, every token count,
    tool_calls, destructive_events and cost_usd to zero together. Until this
    branch existed `trajectory_parse_error` stayed "" through all of that, so
    the row was byte-identical to a genuinely quiet run -- the exact ambiguity
    the rest of the schema's vocabulary is built to eliminate.
    """
    record = _fake_run(
        monkeypatch, task, tmp_path, runner=FakeRunner(turns=2, transcript_path=None)
    )

    assert record.turns_used == 0
    assert record.tokens.input == 0
    assert record.cost_usd == 0.0
    assert "absent" in record.trajectory_parse_error, (
        "a zero row with an empty error field claims the agent was quiet"
    )


def test_a_transcript_that_parsed_says_nothing_about_absence(task, tmp_path):
    """The other side of it: a real transcript must leave the field empty, or
    the signal means nothing."""
    transcript = _write(tmp_path, [_assistant(1, stop_reason="end_turn")])

    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=transcript, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )

    assert record.trajectory_parse_error == ""
    assert record.turns_used == 1


# --- case 8d: attribution loss must be visible in the record -----------------


def test_calls_the_proxy_could_not_attribute_are_counted_in_the_record(task, tmp_path):
    """A run whose header stamping broke writes a well-formed record.

    Every wire-derived field -- sampling, both hashes, bedrock_model_id --
    comes back empty, and before 3.1.0 nothing in the record said why. The
    gate for this existed only in scripts/smoke_test.py, which does not run
    during an eval.
    """
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[], wire_unattributed=3,
    )

    assert record.wire_entries_seen == 0
    assert record.wire_unattributed == 3
    assert record.sampling == {}


def test_unmeasured_attribution_is_none_not_zero(task, tmp_path):
    """None and 0 are different claims, and only 0 licenses trusting the
    wire-derived fields. A run with no proxy wire directory never had an
    unattributed.jsonl to count."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )

    assert record.wire_unattributed is None


def test_execute_run_counts_only_this_runs_lost_calls(task, tmp_path, monkeypatch):
    """A DELTA, not an absolute.

    unattributed.jsonl is shared by every run against one proxy. An absolute
    count would charge each run with every earlier run's losses, so every run
    after the first would be unreadable and the field would be worse than
    absent.
    """
    wire_dir = tmp_path / "wire"
    wire_dir.mkdir()
    # Two calls an EARLIER run lost. This run must not be blamed for them.
    (wire_dir / "unattributed.jsonl").write_text('{"a":1}\n{"a":2}\n')

    import bakeoff.runner as runner_module

    box = FakeContainer()
    monkeypatch.setattr(runner_module, "RunContainer", lambda **_k: box)
    monkeypatch.setattr(runner_module, "ContainerBackend", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_module, "ClaudeCodeRunner", lambda *a, **k: FakeRunner(turns=1)
    )

    config = ClaudeCodeConfig(
        model="gemma-4-31b", base_url="http://litellm:4000", auth_token="unused",
        settings_path="/eval/eval_settings.json", config_dir="/eval/claude-config",
        max_turns=60, wall_clock_timeout_s=300,
    )
    record = execute_run(
        task=task, model="gemma-4-31b", sample_index=0, config=config,
        event_log=EventLog(tmp_path / "log"), repo_path=str(tmp_path / "repo"),
        artifacts_root=tmp_path / "artifacts", proxy_wire_dir=wire_dir,
    )

    assert record.wire_unattributed == 0, "inherited an earlier run's losses"


def test_an_absent_system_prompt_hashes_to_empty_not_to_a_digest_of_null(
    task, tmp_path
):
    """`_sha256(None)` used to return the sha256 of the four bytes "null".

    That is an ordinary-looking 64-hex digest, so a record that observed no
    system prompt could not be told from one that observed a real prompt --
    and two arms that both sent nothing would agree on a hash and read as
    having sent the same thing.
    """
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[{"request": {"model": "gemma-4-31b"}, "metadata": {}}],
    )

    assert record.system_prompt_sha == ""
    assert record.tool_schema_sha == ""
    assert record.wire_entries_seen == 1


def test_a_wire_log_that_was_never_opened_is_not_advertised_as_an_artifact(
    task, tmp_path
):
    """Unlike container_stdout, this path used to be asserted unconditionally,
    so a consumer following it got a FileNotFoundError instead of a null it
    could have handled."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )

    assert record.artifacts.wire_log_gz is None


# --- case 9: wire log captures a malformed completion in full, pre-parse -----


def test_malformed_completion_survives_in_wire_log(tmp_path):
    path = tmp_path / "wire.jsonl.gz"
    logger = WireLogger(path)
    raw = '{"name":"Edit","input":{"file_path":'  # truncated mid-object
    logger.log_call(
        request={"model": "kimi-k2-5"},
        response={"raw_completion": raw, "parse_error": "unexpected EOF"},
        metadata={"run_id": "r-1", "turn": 3},
    )
    logger.close()

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        entry = json.loads(handle.readline())
    assert entry["response"]["raw_completion"] == raw


def test_truncated_trajectory_parses_partially(tmp_path):
    src = _assistant(1, stop_reason="end_turn") + "\n" + '{"type":"assis'
    path = tmp_path / "t.jsonl"
    path.write_text(src)

    parsed = parse_trajectory(path, model="gemma-4-31b")
    assert len(parsed.turns) == 1
    assert parsed.malformed_lines == 1


# --- case 10: per-turn records reconstruct run totals ------------------------


def test_per_turn_tokens_reconstruct_run_totals(tmp_path):
    """The arithmetic check that catches a silently lossy logger."""
    path = _write(tmp_path, [_assistant(i) for i in range(1, 6)])
    parsed = parse_trajectory(path, model="gemma-4-31b")

    assert len(parsed.turns) == 5
    assert sum(t.tokens.input for t in parsed.turns) == parsed.total_tokens.input
    assert sum(t.tokens.output for t in parsed.turns) == parsed.total_tokens.output
    assert sum(t.cost_usd for t in parsed.turns) == pytest.approx(parsed.total_cost_usd)


def test_run_record_totals_match_its_own_per_turn_records(task, tmp_path):
    """The same arithmetic on the written record, which is what analysis
    reads. parse_trajectory and assemble_record could each be internally
    consistent while the record carried one from a different source."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=_write(tmp_path, [_assistant(i) for i in range(1, 6)]),
        runner_result=None, checkpoints=[], destructive_events=[],
        artifacts_root=tmp_path,
    )
    assert sum(t.tokens.input for t in record.per_turn) == record.tokens.input
    assert sum(t.tokens.output for t in record.per_turn) == record.tokens.output
    assert sum(t.cost_usd for t in record.per_turn) == pytest.approx(record.cost_usd)
    assert record.turns_used == len(record.per_turn)


# --- case 11: version block populated and correct for every component --------


def test_version_block_is_populated_for_every_component(task, tmp_path):
    """Section 6.6 says "every component", and four of the six fields have
    been empty on every record ever written.

    Without harness_commit a record cannot be attributed to the code that
    produced it, which matters across 2,400 runs collected over weeks;
    without the litellm version an adapter behaviour change mid-eval is
    invisible in the data it changed.
    """
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=_write(tmp_path, [_assistant(1, stop_reason="end_turn")]),
        runner_result=None, checkpoints=[], destructive_events=[],
        artifacts_root=tmp_path,
        wire_entries=[
            {
                "request": {"model": "bedrock/us.anthropic.claude-sonnet-5"},
                "metadata": {"failed": False},
            }
        ],
    )
    assert record.versions.container_image_digest == task.container_image_digest
    assert record.versions.claude_code == "2.1.220"
    assert record.versions.litellm  # importlib.metadata; litellm.__version__ does not exist
    assert record.versions.harness_commit
    assert record.versions.bedrock_model_id == "bedrock/us.anthropic.claude-sonnet-5"
    assert record.schema_version


def test_harness_commit_marks_an_uncommitted_tree_as_dirty(tmp_path, monkeypatch):
    """A bare SHA on a modified tree asserts a provenance that does not
    exist.

    2,400 runs are collected over weeks while this code keeps changing, and
    a record is only re-derivable if it names the harness that produced it.
    A run made from a working tree with uncommitted edits did not run the
    code at that SHA, and the record has to say so -- there is no way to
    recover the distinction afterwards.
    """
    import subprocess as sp

    from bakeoff.runner import harness_commit

    calls = {"status": "M src/bakeoff/runner.py\n"}

    def fake_run(cmd, **kwargs):
        out = "abc123def456" if cmd[1] == "rev-parse" else calls["status"]
        return sp.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(sp, "run", fake_run)

    harness_commit.cache_clear()
    assert harness_commit() == "abc123def456-dirty"

    calls["status"] = ""
    harness_commit.cache_clear()
    assert harness_commit() == "abc123def456"
    harness_commit.cache_clear()


def test_version_lookups_never_cost_the_record(monkeypatch):
    """Both version probes shell out or hit package metadata, and neither is
    worth a run for. A machine with no git, or a litellm installed outside
    package metadata, must degrade to an empty string -- the tokens are
    already paid for by the time this runs."""
    import subprocess as sp
    from importlib.metadata import PackageNotFoundError

    import bakeoff.runner as runner_module
    from bakeoff.runner import harness_commit, litellm_version

    def no_git(*_a, **_k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(sp, "run", no_git)
    harness_commit.cache_clear()
    assert harness_commit() == ""
    harness_commit.cache_clear()

    def missing(*_a, **_k):
        raise PackageNotFoundError("litellm")

    monkeypatch.setattr(runner_module, "package_version", missing)
    litellm_version.cache_clear()
    assert litellm_version() == ""
    litellm_version.cache_clear()


def test_a_5xx_is_excluded_as_infra_under_its_own_reason_code(task, tmp_path):
    """429 and 5xx are separate pre-registered reasons. Collapsing them
    would hide whether Bedrock was throttling the eval or failing outright,
    which are different operational problems with different fixes."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[
            {"request": {"model": "gemma-4-31b"},
             "metadata": {"failed": True, "status_code": 503}},
        ],
    )
    assert record.exclusion is not None
    assert record.exclusion.reason_code == "api_5xx"


def test_a_client_error_is_not_an_infra_failure(task, tmp_path):
    """A 400 is the adapter or the request, not the platform. Excluding it
    as infra would quietly drop the runs that reveal a broken tool
    translation -- exactly the section 6.4 evidence the eval needs."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[
            {"request": {"model": "gemma-4-31b"},
             "metadata": {"failed": True, "status_code": 400}},
        ],
    )
    assert record.exclusion is None


def test_task_set_commit_is_deliberately_empty_until_the_dataset_exists(task, tmp_path):
    """The one version field with nothing to populate it. Asserted as empty
    on purpose: inventing a value would be worse than an honest blank, and
    when the dataset plan lands this test is the reminder to wire it."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )
    assert record.versions.task_set_commit == ""


# --- case 2 (derivation half): throttles reach the classifier ---------------


def test_final_api_error_is_carried_from_the_wire_into_the_exclusion(task, tmp_path):
    """A Bedrock 429 must classify as INFRA_FAILURE, not as a model failure.

    classify_exclusion has handled this since Task 8, but nothing ever
    populated api_error_status, so the branch was unreachable on a real run
    and every throttle was scored against the model. The status has to come
    from the wire log -- it is the only place the harness sees an HTTP
    status at all.
    """
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[
            {"request": {"model": "gemma-4-31b"},
             "metadata": {"failed": True, "status_code": 429}},
        ],
    )
    assert record.exclusion is not None
    assert record.exclusion.cls is ExclusionClass.INFRA_FAILURE
    assert record.exclusion.reason_code == "api_throttle"


def test_a_throttle_that_the_retry_recovered_is_not_an_infra_failure(task, tmp_path):
    """general_settings.num_retries is 3, so a transient 429 followed by a
    successful retry produced a complete run. Excluding it would discard good
    data because of an error that cost nothing -- and exclusions are the one
    mechanism by which results can be massaged, so the bar is that the run
    ENDED on the error."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:05:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[
            {"request": {"model": "gemma-4-31b"},
             "metadata": {"failed": True, "status_code": 429}},
            {"request": {"model": "gemma-4-31b"}, "metadata": {"failed": False}},
        ],
    )
    assert record.exclusion is None


def test_bedrock_throttle_classified_as_infra_not_model():
    exclusion = classify_exclusion(
        RunSignals(
            outcome=Outcome.FAILED, terminated_by=TerminationReason.CRASH,
            tool_calls_total=0, tool_calls_malformed=0, truncation_events=0,
            distinct_turn_hashes=0, turns_used=0, agent_claimed_success=False,
            api_error_status=429,
        )
    )
    assert exclusion is not None
    assert exclusion.cls is ExclusionClass.INFRA_FAILURE


# --- case 12: an interrupted run leaves a partial-but-valid record -----------


def test_agent_killed_mid_stream_leaves_a_partial_but_valid_record(
    task, tmp_path, monkeypatch
):
    """Not merely "a record exists" -- it must round-trip through the event
    log and still carry the checkpoints captured before the kill. A record
    that deserializes into something different from what was written is
    corrupt in the way that matters, since the log has no update API."""
    record = _fake_run(
        monkeypatch, task, tmp_path,
        container=FakeContainer(),
        runner=FakeRunner(turns=4, raise_after=2),
    )

    assert record.outcome is Outcome.CRASHED
    assert len(record.checkpoints) == 2
    assert [c.turn for c in record.checkpoints] == [1, 2]
    assert EventLog(tmp_path / "log").read_run(record.run_id) == record


# --- cases 1 and 2, against a real proxy on the internal network -------------
#
# These need Docker. They are the only place the wire-capture path is
# exercised as it will actually run: the agent calls a proxy in another
# container, and the callback that records the call lives in THAT process.
# Asserting capture in the harness process proves the opposite of what is
# needed -- it is precisely the arrangement that captures nothing in
# production.

integration = pytest.mark.integration


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q"], ["config", "user.email", "e@x"], ["config", "user.name", "e"],
        ["add", "-A"], ["commit", "-q", "-m", "base"],
    ):
        sp.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def _proxy_run(agent_image, tmp_path, fault_proxy, model: str, task_id: str):
    from bakeoff.runner import TaskSpec, execute_run

    network, wire_dir = fault_proxy
    repo, sha = _git_repo(tmp_path)
    task = TaskSpec(
        task_id=task_id, task_version=1, repo="pindrop/example", base_sha=sha,
        container_image_digest=agent_image, prompt="do the thing", test_paths=[],
    )
    config = ClaudeCodeConfig(
        model=model,
        base_url="http://litellm:4000",
        auth_token="not-checked-by-the-mock-proxy",
        settings_path="/eval/eval_settings.json",
        config_dir="/eval/claude-config",
        max_turns=10,
        wall_clock_timeout_s=60,
    )
    record = execute_run(
        task=task, model=model, sample_index=0, config=config,
        event_log=EventLog(tmp_path / "log"), repo_path=str(repo),
        artifacts_root=tmp_path / "artifacts",
        network=network, proxy_wire_dir=wire_dir,
    )
    return record, wire_dir


@integration
def test_proxy_side_capture_records_the_call_the_agent_made(
    agent_image, tmp_path, fault_proxy
):
    """The finding-4 guard.

    litellm.callbacks registered in the harness process observes nothing:
    the agent runs in its own container and the model call is made by the
    proxy, a third process. Before this, every real run produced an empty
    wire log -- and with it empty sampling, empty prompt and tool hashes,
    zero errored calls -- while the record still looked complete.

    An isolated run with no wire entries is therefore a capture failure, not
    a quiet run, and section 6.2 makes the wire log mandatory.
    """
    from bakeoff.proxy_callback import unattributed_count

    record, wire_dir = _proxy_run(
        agent_image, tmp_path, fault_proxy, "mock-ok", "t-proxy-ok"
    )

    assert record.isolated is True
    assert record.tool_calls.errored == 0

    with gzip.open(tmp_path / "artifacts" / "wire.jsonl.gz", "rt") as handle:
        logged = [json.loads(line) for line in handle if line.strip()]
    assert logged, "isolated run captured no wire entries -- section 6.2 capture is dead"
    assert logged[0]["metadata"]["run_id"] == record.run_id
    assert unattributed_count(wire_dir) == 0, (
        "a call could not be attributed to a run; the run_id header did not arrive"
    )

    # The fields that were silently empty on every real run. Values, not
    # merely presence: max_tokens is what the caller actually sent, so an
    # empty dict or a config-sourced default both fail here.
    assert record.sampling["max_output_tokens"] == 64
    # Pinned against the request the fake agent actually sent, not merely
    # asserted to be 64 hex. Length alone passed for as long as _sha256(None)
    # returned the digest of the four bytes "null" -- a request carrying no
    # system prompt produced a perfectly ordinary-looking hash, and this test
    # certified it.
    import hashlib as _hashlib

    assert record.system_prompt_sha == _hashlib.sha256(
        json.dumps("You are Claude Code.", sort_keys=True).encode()
    ).hexdigest()
    assert len(record.tool_schema_sha) == 64
    # Read off the wire, not from config: the alias is what was asked for,
    # this is what answered.
    assert record.versions.bedrock_model_id

    # Attribution accounted for in the record itself, not only in the
    # artifact. Zero is a measurement here, which is what licenses reading
    # the sampling and hashes above as real observations.
    assert record.wire_entries_seen == len(logged)
    assert record.wire_unattributed == 0


@integration
def test_without_a_proxy_wire_dir_the_harness_captures_nothing(
    agent_image, tmp_path, fault_proxy
):
    """The negative control for the test above.

    The same run, same proxy, same real HTTP call -- but sourcing wire
    entries from the harness's in-process callback instead of the proxy's
    log. It captures nothing, because the harness process makes no model
    calls. This is production behaviour before the fix, and without this
    test the passing case above could be passing on in-process capture and
    nobody would know.
    """
    from bakeoff.runner import TaskSpec, execute_run

    network, _ = fault_proxy
    repo, sha = _git_repo(tmp_path)
    record = execute_run(
        task=TaskSpec(
            task_id="t-no-wire", task_version=1, repo="r", base_sha=sha,
            container_image_digest=agent_image, prompt="go", test_paths=[],
        ),
        model="mock-ok", sample_index=0,
        config=ClaudeCodeConfig(
            model="mock-ok", base_url="http://litellm:4000", auth_token="x",
            settings_path="/eval/eval_settings.json",
            config_dir="/eval/claude-config", max_turns=10, wall_clock_timeout_s=60,
        ),
        event_log=EventLog(tmp_path / "log"), repo_path=str(repo),
        artifacts_root=tmp_path / "artifacts",
        network=network,  # isolated, but no proxy_wire_dir
    )

    assert record.isolated is True
    assert record.sampling == {}
    assert record.system_prompt_sha == ""
    assert record.versions.bedrock_model_id == ""


@integration
def test_proxy_throttle_is_excluded_as_infra_not_scored_against_the_model(
    agent_image, tmp_path, fault_proxy
):
    """Section 6.6 case 2, through the whole path.

    mock_response "litellm.RateLimitError" raises a REAL RateLimitError
    inside the proxy, so this exercises LiteLLM's own failure handling, the
    failure callback, the status code on the exception, and the harness's
    retry-aware derivation -- with no credentials and no spend.

    Asserting this at classify_exclusion instead passes today while the
    production path is dead, which is how the defect survived nine tasks.
    """
    record, _ = _proxy_run(
        agent_image, tmp_path, fault_proxy, "mock-throttle", "t-proxy-429"
    )

    assert record.exclusion is not None, "a Bedrock throttle was scored against the model"
    assert record.exclusion.cls is ExclusionClass.INFRA_FAILURE
    assert record.exclusion.reason_code == "api_throttle"
    assert record.exclusion.pre_registered is True
    assert record.tool_calls.errored == 1


@integration
def test_container_killed_mid_run_still_writes_a_record(agent_image, tmp_path, monkeypatch):
    """Section 6.6 case 1. The container dies while the agent is working.

    The record must survive AND keep the checkpoints captured before the
    kill. Those turns happened and were paid for; a record that reports the
    crash but silently drops them is the "nothing lost" violation, and it is
    invisible from outside because the record is otherwise well-formed.
    """
    import threading

    import bakeoff.runner as runner_module
    from bakeoff.runner import TaskSpec, execute_run

    real_container = runner_module.RunContainer
    seen: list[object] = []

    class Watched(real_container):
        def __enter__(self):
            box = super().__enter__()
            seen.append(box)
            return box

    monkeypatch.setattr(runner_module, "RunContainer", Watched)

    def kill_when_started():
        for _ in range(100):
            if seen and getattr(seen[0], "_container", None) is not None:
                # After the first turn's sleep has begun, so there is work to
                # lose if the fix regresses.
                import time as _t
                _t.sleep(2.5)
                seen[0]._container.kill()
                return
            import time as _t
            _t.sleep(0.1)

    repo, sha = _git_repo(tmp_path)
    killer = threading.Thread(target=kill_when_started, daemon=True)
    killer.start()

    record = execute_run(
        task=TaskSpec(
            task_id="t-killed", task_version=1, repo="r", base_sha=sha,
            container_image_digest=agent_image, prompt="do the thing", test_paths=[],
        ),
        model="gemma-4-31b", sample_index=0,
        config=ClaudeCodeConfig(
            model="gemma-4-31b", base_url="", auth_token="unused",
            settings_path="/eval/eval_settings.json",
            config_dir="/eval/claude-config", max_turns=10, wall_clock_timeout_s=60,
        ),
        event_log=EventLog(tmp_path / "log"),
        repo_path=str(repo),
        artifacts_root=tmp_path / "artifacts",
    )
    killer.join(timeout=5)

    written = EventLog(tmp_path / "log").read_run(record.run_id)
    assert written == record
    assert record.run_id
    assert record.started_at and record.finished_at
