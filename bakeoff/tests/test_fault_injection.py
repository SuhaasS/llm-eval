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
from dataclasses import replace
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

    def host_sampler(self):
        # A sampler with no container and no client: start() spawns a thread
        # that returns immediately, and metrics() reports samples=0 with
        # contention_flag None -- "nobody measured", which is the truth for a
        # run whose container is a stub. Without this the AttributeError is
        # swallowed by execute_run's catch-all and every test here fails on an
        # unrelated assertion.
        from bakeoff.container import HostSampler

        return HostSampler(container=None, client=None)

    def exec(self, *_args, **_kwargs):
        return None

    def checked_exec(self, *_args, **_kwargs):
        return None

    def network_isolation(self) -> tuple[bool | None, str]:
        # None, not True. This double has no Docker network to inspect, so the
        # honest answer is that nobody measured -- and a double that returned
        # True would let a record assert section 5.1 held on a run that never
        # touched a container.
        return None, "fake container: not measured"

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
    collection_id="",
    invocation_stamp="",
    proxy_wire_dir=None,
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
        collection_id=collection_id,
        invocation_stamp=invocation_stamp,
        proxy_wire_dir=proxy_wire_dir,
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

    The run is still CRASHED here, and since 3.5.0 that is due to the FINAL
    snapshot rather than the mid-run ones: `force_capture` runs after the
    agent has exited and its diff is the submission (section 5.6), so it is
    deliberately still allowed to fail loudly. The mid-run failures are
    contained -- see the two cases below.
    """
    record = _fake_run(
        monkeypatch, task, tmp_path,
        container=FakeContainer(fail_snapshot_after=1),
        runner=FakeRunner(turns=3),
    )

    assert record.outcome is Outcome.CRASHED
    assert len(record.checkpoints) == 1
    assert record.checkpoints[0].diff_vs_base == "diff-1"


def test_a_mid_run_snapshot_failure_does_not_crash_a_working_run(
    task, tmp_path, monkeypatch
):
    """Measured 2026-08-12, once in seven offline arms.

    `maybe_capture` is called from inside the agent's stdout loop, so a raise
    there did not merely lose a checkpoint -- it unwound out of
    `ClaudeCodeRunner.run`, past the container context manager, into
    `execute_run`'s catch-all, and the record said CRASHED with zero turns,
    zero tokens, no diff and no cost. For a run that was working: `git add
    -A` had lost a race against Claude Code's atomic Write.

    Checkpoints are supplementary evidence (section 5.5's cost/quality
    curve); the trajectory and the final diff are the run's product. The
    cheap thing must never cost the expensive one, and by the time this is
    called the tokens are spent.
    """
    container = FakeContainer(fail_snapshot_after=1)
    # Succeeds on the FINAL capture, so the only failures are mid-run.
    original = container.snapshot_diff

    def snapshot(base_sha):
        if container.snapshots >= 3:
            container.fail_snapshot_after = None
        return original(base_sha)

    container.snapshot_diff = snapshot

    record = _fake_run(
        monkeypatch, task, tmp_path,
        container=container,
        runner=FakeRunner(turns=3),
    )

    assert record.outcome is not Outcome.CRASHED
    assert record.crash_error == ""
    # The submission survived. This is the field the run exists to produce,
    # and under the old behaviour it was None on a record marked CRASHED.
    assert record.artifacts.final_diff == "diff-4"
    assert record.checkpoint_error, "the gap still has to be named"


def test_a_checkpoint_gap_is_named_rather_than_left_looking_like_an_idle_turn(
    task, tmp_path, monkeypatch
):
    """Containment alone would swap a loud wrong record for a quiet one.

    The section 5.5 curve -- the pass rate at any budget K -- is computed
    post-hoc over `checkpoints`. A list that is short because two snapshots
    failed is byte-identical to one that is short because the agent changed
    nothing for two turns, so the curve would be understated with nothing
    anywhere saying why.
    """
    record = _fake_run(
        monkeypatch, task, tmp_path,
        container=FakeContainer(fail_snapshot_after=1),
        runner=FakeRunner(turns=3),
    )

    assert "No space left on device" in record.checkpoint_error
    assert "turn 2" in record.checkpoint_error


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


def test_the_providers_finish_reason_reaches_the_record(task, tmp_path):
    """The chain on the shape today's arms produce: tool_calls through the run,
    `stop` on the last call. Before 3.7.0 the record said only
    `terminated_by: agent_finish` -- Claude Code's reading of it."""
    record = assemble_record(
        task=task, model="kimi-k2-5", sample_index=0,
        started_at="2026-08-13T00:00:00Z", finished_at="2026-08-13T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[
            {"request": {}, "response": {},
             "metadata": {"failed": False, "finish_reason": "tool_calls"}},
            {"request": {}, "response": {},
             "metadata": {"failed": False, "finish_reason": "stop"}},
        ],
    )
    assert record.finish_reasons == {"tool_calls": 1, "stop": 1}
    assert record.terminal_finish_reason == "stop"


def test_an_expired_credential_reaches_the_record_as_an_auth_failure(task, tmp_path):
    """The whole chain, on the shape a real expiry produces: the verbatim
    litellm 1.95.0 message for a bedrock/ ExpiredTokenException, and the 500
    that mapping invents. Before 3.6.0 this record said `api_5xx` -- the
    operator's lapsed SSO session, written into an append-only log as an AWS
    outage, on the reference arm."""
    record = assemble_record(
        task=task, model="claude-sonnet-5-runtime", sample_index=0,
        started_at="2026-08-12T00:00:00Z", finished_at="2026-08-12T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[
            {
                "request": {"model": "claude-sonnet-5-runtime"},
                "response": {"error": {
                    "type": "APIConnectionError",
                    "message": (
                        "bedrock - An error occurred (ExpiredTokenException) when "
                        "calling the InvokeModel operation: The security token "
                        "included in the request is expired"
                    ),
                }},
                "metadata": {"failed": True, "status_code": 500},
            },
        ],
    )
    assert record.exclusion is not None
    assert record.exclusion.reason_code == "api_auth"
    # The other half of what makes this row unreadable, and the half
    # infra_problems keys on.
    assert record.turns_used == 0


def test_an_auth_failure_the_router_cooldown_masked_still_reaches_the_record(
    task, tmp_path
):
    """The sequence a real expiry actually produces, end to end.

    One 401 cools the deployment down for 5 s, `num_retries: 3` retries into
    the cooldown, and RouterRateLimitError carries no status_code -- so the
    LAST entry is `failed: true` with `status_code: null` and a message that
    names no credential. Classifying on it alone yields no exclusion at all,
    which is the failure this whole change exists to prevent."""
    from bakeoff.runner import final_api_error_status

    refusal = {
        "request": {"model": "claude-sonnet-5-runtime"},
        "response": {"error": {
            "type": "RouterRateLimitError",
            "message": (
                "No deployments available for selected model, Try again in 5.0 "
                "seconds. Passed model=claude-sonnet-5-runtime."
            ),
        }},
        "metadata": {"failed": True, "status_code": None},
    }
    entries = [
        {
            "request": {"model": "claude-sonnet-5-runtime"},
            "response": {"error": {
                "type": "AuthenticationError",
                "message": (
                    "BedrockException Invalid Authentication - The security "
                    "token included in the request is invalid"
                ),
            }},
            "metadata": {"failed": True, "status_code": 403},
        },
        refusal, refusal, refusal,
    ]
    # The status the old rule would have classified on: the cooldown's refusal
    # carries none, so there was nothing to classify and the run was excluded
    # for nothing at all.
    assert final_api_error_status(entries) is None

    record = assemble_record(
        task=task, model="claude-sonnet-5-runtime", sample_index=0,
        started_at="2026-08-12T00:00:00Z", finished_at="2026-08-12T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=entries,
    )
    assert record.exclusion is not None
    assert record.exclusion.reason_code == "api_auth"


def test_a_recovered_failure_leaves_no_error_message_behind(task, tmp_path):
    """terminal_error_messages is empty when the last call SUCCEEDED, the same
    bar final_api_error_status has always held. A throttle the retry recovered
    produced a complete run and must not be excluded by its own error text --
    and neither must a 403 that a later call recovered from."""
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-12T00:00:00Z", finished_at="2026-08-12T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        wire_entries=[
            {"request": {}, "response": {"error": {"message": "ExpiredTokenException"}},
             "metadata": {"failed": True, "status_code": 500}},
            {"request": {}, "response": {}, "metadata": {"failed": False}},
        ],
    )
    assert record.exclusion is None


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


def test_task_set_commit_is_blank_for_a_task_that_came_from_no_task_set(
    task, tmp_path
):
    """A hand-built TaskSpec carries no provenance, and says so.

    The dry run, the smoke gate and this suite all construct TaskSpec
    directly. None of them came from a task set, so the honest value is
    blank -- inventing one would attach a dataset commit to a fixture.

    Paired with the test below. Together they pin the 3.4.0 contract: the
    blank is a property of the TASK now, not of the project. Before 3.4.0 it
    was blank on every record because no dataset existed, and a reader
    handed the two cannot tell them apart without the schema version, which
    is why the version moved for a change that added no field.
    """
    record = assemble_record(
        task=task, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )
    assert record.versions.task_set_commit == ""


def test_task_set_commit_is_carried_from_the_task_that_declared_it(task, tmp_path):
    """...and is NOT re-derived, guessed, or read from the harness's own git.

    It is the one thing that makes a stored record re-derivable against the
    task set it ran: task_id plus task_version name a manifest, and only this
    field says which revision of the set that manifest came from. A record
    that named a task nobody can reconstruct is a record that cannot be
    re-scored, which is the entire premise of an append-only log."""
    from dataclasses import replace

    dated = replace(task, task_set_commit="deadbeef" * 5 + "-dirty")
    record = assemble_record(
        task=dated, model="gemma-4-31b", sample_index=0,
        started_at="2026-08-04T00:00:00Z", finished_at="2026-08-04T00:01:00Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )
    assert record.versions.task_set_commit == "deadbeef" * 5 + "-dirty"


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

    assert record.isolated is True, record.isolation_evidence
    assert record.tool_calls.api_calls_failed == 0

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
    # api_calls_failed, not errored. The number is a property of the
    # transport, and it sat under a name that reads as a property of the
    # agent's tool use.
    assert record.tool_calls.api_calls_failed == 1
    assert record.tool_calls.errored == 0


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


# --- Gate 0: a record must say what it could not see --------------------------
#
# Every assertion below covers an observation that was made and then discarded,
# so the record showed a well-formed zero instead. None of them is recoverable
# after the fact: the transcript and the wire log survive a run, a traceback and
# an exit code do not.


def test_a_crashed_run_records_the_cause_not_just_the_fact(task, tmp_path, monkeypatch):
    """`except Exception: crashed = True` kept no type, no message, no
    traceback, so a harness defect and a genuine infra failure produced the
    same record -- and exclusion is, by runner.py's own comment, the one
    mechanism by which results can be massaged. A CRASHED row nobody can
    attribute is a row nobody can defend excluding."""
    record = _fake_run(monkeypatch, task, tmp_path, container_raises=True)

    assert record.outcome is Outcome.CRASHED
    assert "docker daemon is not reachable" in record.crash_error
    assert record.crash_error.startswith("RuntimeError:")
    assert record.artifacts.harness_traceback, "the traceback was not persisted"
    traceback_text = Path(record.artifacts.harness_traceback).read_text()
    assert "docker daemon is not reachable" in traceback_text
    assert "Traceback" in traceback_text


def test_a_clean_run_makes_no_crash_claim(task, tmp_path, monkeypatch):
    """The empty case has to stay empty, or `crash_error` becomes noise that
    readers learn to skip."""
    record = _fake_run(monkeypatch, task, tmp_path)
    assert record.crash_error == ""
    assert record.artifacts.harness_traceback is None


def test_the_agents_stderr_is_persisted(task, tmp_path, monkeypatch):
    """It was captured all along and thrown away here, so
    `Artifacts.container_stderr` was a field no code ever set. stderr is where
    the agent says why it could not start, which is exactly the run whose
    stdout is empty."""
    runner = FakeRunner()
    original = runner.run

    def run_with_stderr(prompt, cwd, on_turn=None):
        result = original(prompt, cwd, on_turn)
        return replace(result, stderr="claude: cannot open config dir\n")

    runner.run = run_with_stderr
    record = _fake_run(monkeypatch, task, tmp_path, runner=runner)

    assert record.artifacts.container_stderr, "stderr was captured and dropped"
    assert "cannot open config dir" in Path(record.artifacts.container_stderr).read_text()


def test_the_agents_exit_code_is_recorded(task, tmp_path, monkeypatch):
    """A CLI that exited non-zero but still wrote a transcript was
    byte-identical in the record to a clean finish."""
    runner = FakeRunner()
    original = runner.run
    runner.run = lambda p, cwd, on_turn=None: replace(
        original(p, cwd, on_turn), exit_code=137
    )

    record = _fake_run(monkeypatch, task, tmp_path, runner=runner)
    assert record.agent_exit_code == 137


def test_an_exit_code_that_was_never_observed_is_null_not_zero(
    task, tmp_path, monkeypatch
):
    """0 is the code of a clean exit. A run whose agent never ran must not
    claim one."""
    record = _fake_run(monkeypatch, task, tmp_path, container_raises=True)
    assert record.agent_exit_code is None


def test_unreadable_stdout_lines_are_counted_not_silently_dropped(
    task, tmp_path, monkeypatch
):
    """The stream-json reader treated "not an assistant event" and "not JSON"
    alike, so unparseable stdout vanished with no counter -- and it undercounts
    `turns_streamed`, one of the three counts that exist to cross-check each
    other. A miscount in the count that catches miscounts cannot be caught."""
    runner = FakeRunner()
    original = runner.run
    runner.run = lambda p, cwd, on_turn=None: replace(
        original(p, cwd, on_turn), stdout_malformed_lines=4
    )

    record = _fake_run(monkeypatch, task, tmp_path, runner=runner)
    assert record.stdout_malformed_lines == 4


def test_unreadable_transcript_lines_reach_the_record(task, tmp_path, monkeypatch):
    """`ParsedTrajectory.malformed_lines` was counted and had no consumer, so a
    transcript with 40 unreadable lines and a clean one produced identical
    records."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-12T00:00:00Z",
                "message": {"id": "m1", "usage": {"input_tokens": 1, "output_tokens": 1},
                            "content": [], "stop_reason": "end_turn"},
            }
        )
        + "\n{ truncated\nnot json at all\n"
    )
    record = _fake_run(
        monkeypatch, task, tmp_path, runner=FakeRunner(transcript_path=transcript)
    )
    assert record.transcript_malformed_lines == 2
    # The readable part still parsed: this is a count of damage, not a verdict
    # on the transcript.
    assert record.turns_used == 1
    assert record.trajectory_parse_error == ""


def test_a_failing_destructive_scan_does_not_read_as_a_clean_run(
    task, tmp_path, monkeypatch
):
    """The scanner used to share a `try` with the trajectory parse. When the
    SCANNER raised, assemble_record's independent re-parse still succeeded --
    so `trajectory_parse_error` stayed empty and `destructive_events` was `[]`,
    which is a positive safety claim (spec section 7) manufactured by a
    failure. Exactly what the comment above it said it was preventing."""
    import bakeoff.runner as runner_module

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-12T00:00:00Z",
                "message": {
                    "id": "m1",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "rm -rf tests/"}}
                    ],
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )

    def exploding_scan(*_args, **_kwargs):
        raise ValueError("scanner regex blew up")

    monkeypatch.setattr(runner_module, "scan_destructive", exploding_scan)
    record = _fake_run(
        monkeypatch, task, tmp_path, runner=FakeRunner(transcript_path=transcript)
    )

    assert record.destructive_events == []
    assert "scanner regex blew up" in record.scanner_error
    # The parse itself was fine, and the record must not blame it.
    assert record.trajectory_parse_error == ""
    assert record.turns_used == 1


def test_a_wire_log_collision_costs_the_wire_log_and_not_the_run(
    task, tmp_path, monkeypatch
):
    """What the code claimed and did not do. `gzip.open(path, "xt")` raising
    inside the run body became `crashed = True`, so the record read CRASHED +
    container_crashed on a run whose container had not started -- a harness
    bookkeeping collision recorded as an infrastructure failure, and an
    excludable one."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "wire.jsonl.gz").write_text("a wire log from an earlier attempt")

    record = _fake_run(monkeypatch, task, tmp_path)

    assert record.outcome is not Outcome.CRASHED
    assert record.terminated_by is not TerminationReason.CRASH
    assert record.crash_error == "", "a bookkeeping collision is not a crash"
    assert "FileExistsError" in record.wire_log_error
    # No artifact is published for a file this run did not write.
    assert record.artifacts.wire_log_gz is None


@integration
def test_a_routable_network_is_recorded_as_not_isolated(agent_image, tmp_path):
    """The one case that separates the measurement from the argument it
    replaced.

    `isolated = bool(network)` answered True for any network at all, so a
    container attached to a ROUTABLE one -- with a live path off the host, and
    therefore to a model the wire log never saw -- would have carried
    `isolated: true` into every comparison built on it. Section 5.1 is the
    guarantee that makes `container_image_digest` mean anything about the
    process under test, and it was the one field runner.py asserted rather than
    observed.

    Asserted through execute_run and not against RunContainer, because
    `bool(network)` lives here. A container-level test passes with the argument
    restored.
    """
    import docker

    from bakeoff.runner import TaskSpec, execute_run

    client = docker.from_env()
    network = client.networks.create("bakeoff-fault-routable", driver="bridge")
    repo, sha = _git_repo(tmp_path)
    try:
        record = execute_run(
            task=TaskSpec(
                task_id="t-routable", task_version=1, repo="r", base_sha=sha,
                container_image_digest=agent_image, prompt="go", test_paths=[],
            ),
            model="mock-ok", sample_index=0,
            config=ClaudeCodeConfig(
                model="mock-ok", base_url="http://litellm:4000", auth_token="x",
                settings_path="/eval/eval_settings.json",
                config_dir="/eval/claude-config", max_turns=2,
                wall_clock_timeout_s=30,
            ),
            event_log=EventLog(tmp_path / "log"), repo_path=str(repo),
            artifacts_root=tmp_path / "artifacts",
            network=network.name,
        )
    finally:
        network.remove()

    assert record.isolated is False, record.isolation_evidence
    assert "ROUTABLE" in record.isolation_evidence
    assert network.name in record.isolation_evidence


def test_an_unsampled_run_does_not_claim_the_host_was_quiet(task, tmp_path):
    """assemble_record with no host argument is every path that could not
    sample -- a crash before the container started, a dry run, a test. Through
    3.6.0 all of them said `contention_flag: false`."""
    record = assemble_record(
        task=task, model="kimi-k2-5", sample_index=0,
        started_at="2026-08-13T00:00:00Z", finished_at="2026-08-13T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )
    assert record.host.contention_flag is None
    assert record.host.samples == 0


def test_a_record_says_which_collection_produced_it(task, tmp_path):
    """run_id is sha256(task|model|sample|attempt) and names no episode, so two
    collections over the same cells produce identical ids. An offline reader
    merging event logs would treat different runs as one -- and 6 such ids
    already exist, one of them in seven logs. Recorded rather than repaired:
    the log is append-only."""
    record = assemble_record(
        task=task, model="kimi-k2-5", sample_index=0,
        started_at="2026-08-13T00:00:00Z", finished_at="2026-08-13T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
        collection_id="20260813T075544Z",
    )
    assert record.collection_id == "20260813T075544Z"


def test_a_record_with_no_collection_states_that_rather_than_guessing(task, tmp_path):
    """"" is every record written before 3.7.0 and every non-matrix path --
    honest, and distinguishable from a named episode."""
    record = assemble_record(
        task=task, model="kimi-k2-5", sample_index=0,
        started_at="2026-08-13T00:00:00Z", finished_at="2026-08-13T00:00:30Z",
        trajectory_path=None, runner_result=None, checkpoints=[],
        destructive_events=[], artifacts_root=tmp_path,
    )
    assert record.collection_id == ""


def test_collection_id_survives_the_whole_orchestrator(task, tmp_path, monkeypatch):
    """The assertion this file's first rule exists for, and it earned its place
    immediately: `collection_id` was added to assemble_record and NOT to
    execute_run, so every caller raised TypeError and no unit test noticed --
    they all drive assemble_record directly. The section 6.6 gate caught it on
    the offline smoke, one layer from a paid run.

    Exactly the shape of the cache_state defect above: a parameter nobody
    passed, invisible to a unit test on the function that receives it."""
    record = _fake_run(
        monkeypatch, task, tmp_path, collection_id="20260813T184012Z"
    )
    assert record.collection_id == "20260813T184012Z"


def test_a_run_with_no_collection_named_says_so_end_to_end(task, tmp_path, monkeypatch):
    """"" is honest and distinguishable from a named episode. It is also every
    record written before 3.7.0."""
    assert _fake_run(monkeypatch, task, tmp_path).collection_id == ""


# --- schema 3.8.0: the finalize phase, and the record that survives it -------
#
# Every case here used to produce NO RECORD AT ALL. `execute_run`'s `finally`
# and its whole assembly stretch sat outside every `try`, so a raise in either
# escaped past `_write_or_strand` -- not even `record.unwritten.json`. The
# tokens for these runs are already spent by the time the failure happens.


class _TalkativeRunner(FakeRunner):
    """A FakeRunner that emits stdout, so the stdout finalize step has work to
    do. The default double returns "" and the step is then a no-op, which
    would make a test of its failure pass for the wrong reason."""

    def run(self, prompt, cwd, on_turn=None):
        result = super().run(prompt, cwd, on_turn)
        return replace(result, stdout='{"type":"system","subtype":"init"}\n')


def _wire_dir_with(tmp_path, run_id, lines):
    wire_dir = tmp_path / "wire"
    wire_dir.mkdir(parents=True, exist_ok=True)
    (wire_dir / f"{run_id}.jsonl").write_bytes(lines)
    return wire_dir


def _entry(index, *, failed=False, status=None, message=""):
    return {
        "logged_at": "2026-08-13T00:00:0%dZ" % index,
        "request": {"model": "gemma-4-31b", "max_tokens": 8},
        "resolved": None,
        "response": ({"error": {"message": message}} if failed else {"id": f"m{index}"}),
        "metadata": {
            "run_id": "r",
            "call_index": index,
            "failed": failed,
            "status_code": status,
            "litellm_call_id": f"c{index}",
        },
    }


def test_a_finalize_step_that_raises_still_produces_a_record(
    task, tmp_path, monkeypatch
):
    """The whole point of the phase. A failing step is named, not fatal."""
    import bakeoff.runner as runner_module

    def boom(*_a, **_k):
        raise OSError("[Errno 28] No space left on device")

    monkeypatch.setattr(runner_module.Path, "write_text", boom)
    record = _fake_run(
        monkeypatch, task, tmp_path, runner=_TalkativeRunner()
    )

    assert record.finalize_error, "a failed finalize step must name itself"
    assert "stdout" in record.finalize_error
    assert record.run_id


def test_one_failing_finalize_step_does_not_skip_the_others(
    task, tmp_path, monkeypatch
):
    """Per step, not one try around the block.

    A single guard would let a stdout failure cost the checkpoints too -- and
    the checkpoints are the only copy of the submission diff.
    """
    import bakeoff.runner as runner_module

    real_write = runner_module.Path.write_text

    def selective(self, *args, **kwargs):
        if self.name == runner_module.STDOUT_NAME:
            raise OSError("[Errno 28] No space left on device")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(runner_module.Path, "write_text", selective)
    record = _fake_run(
        monkeypatch, task, tmp_path, runner=_TalkativeRunner()
    )

    assert "stdout" in record.finalize_error
    # The steps after the failing one still ran.
    assert "checkpoints" not in record.finalize_error
    assert "wire" not in record.finalize_error


def test_assembly_that_raises_yields_a_minimal_record_not_a_lost_one(
    task, tmp_path, monkeypatch
):
    import bakeoff.runner as runner_module

    def boom(**_kwargs):
        raise ValueError("assembly blew up")

    monkeypatch.setattr(runner_module, "assemble_record", boom)
    log = EventLog(tmp_path / "log")
    record = _fake_run(monkeypatch, task, tmp_path, event_log=log)

    assert record.assembly_error.startswith("ValueError: assembly blew up")
    # Read back: the record is IN THE LOG, not merely returned.
    assert log.read_run(record.run_id).assembly_error == record.assembly_error


def test_the_minimal_record_does_not_fabricate_claims(task, tmp_path, monkeypatch):
    """Every field it defaults is a claim it makes.

    `cost_usd=0.0` is a genuine zero, `isolated=False` says section 5.1 did
    not hold, and FAILED with zero turns is filed GAVE_UP -- a permanent
    behavioural claim about the model, produced by a defect in the harness.
    """
    import bakeoff.runner as runner_module

    monkeypatch.setattr(
        runner_module, "assemble_record", lambda **_k: (_ for _ in ()).throw(
            ValueError("nope")
        )
    )
    record = _fake_run(monkeypatch, task, tmp_path)

    assert record.cost_usd is None, "0.0 is a measured zero cost"
    assert record.isolated is None, "False is a section 5.1 violation claim"
    assert record.outcome is Outcome.CRASHED
    assert record.terminated_by is TerminationReason.CRASH
    assert record.exclusion is None, "an invented exclusion removes the row"


def test_the_minimal_record_carries_the_image_digest(task, tmp_path, monkeypatch):
    """Otherwise plan_resume refuses to resume the whole matrix, forever.

    `Versions()` defaults `container_image_digest` to "", and plan_resume
    compares a stored record's digest against the image about to be used --
    so a defaulted minimal record is a permanent image conflict, and
    run_matrix exits 1 on any of them unless --allow-mixed-images is passed.
    """
    import bakeoff.runner as runner_module

    monkeypatch.setattr(
        runner_module, "assemble_record", lambda **_k: (_ for _ in ()).throw(
            ValueError("nope")
        )
    )
    record = _fake_run(monkeypatch, task, tmp_path)

    assert record.versions.container_image_digest == task.container_image_digest


def test_the_minimal_record_keeps_the_submission_diff(task, tmp_path, monkeypatch):
    """The payload, not just the claim fields.

    The per-turn diffs live only in memory and only in the record, and the
    materialized repo is deleted after the run -- so a minimal record without
    them throws away the submission of the very cell it is rescuing.
    """
    import bakeoff.runner as runner_module

    monkeypatch.setattr(
        runner_module, "assemble_record", lambda **_k: (_ for _ in ()).throw(
            ValueError("nope")
        )
    )
    box = FakeContainer()
    record = _fake_run(monkeypatch, task, tmp_path, container=box)

    assert record.checkpoints, "the per-turn diffs exist nowhere else"
    assert record.artifacts.final_diff is not None


def test_the_minimal_record_does_not_manufacture_a_safety_claim(
    task, tmp_path, monkeypatch
):
    """`destructive_events: []` beside `scanner_error: ""` is a positive spec
    section 7 claim -- "the scan ran and found nothing" -- and `scanner_error`
    exists precisely so a failure cannot produce it."""
    import bakeoff.runner as runner_module
    from bakeoff.schema import DestructiveCategory, DestructiveEvent

    event = DestructiveEvent(
        turn=1,
        command="rm -rf tests/",
        paths_touched=["tests/"],
        category=DestructiveCategory.TEST_DELETION,
        reverted_by_agent=False,
        affected_outcome=False,
        severity=Severity.HIGH,
    )
    monkeypatch.setattr(runner_module, "scan_destructive", lambda *_a, **_k: [event])
    monkeypatch.setattr(
        runner_module, "assemble_record", lambda **_k: (_ for _ in ()).throw(
            ValueError("nope")
        )
    )
    transcript = _write(
        tmp_path,
        [_assistant(1, tool="Bash", tool_input={"command": "rm -rf tests/"}),
         _assistant(2, stop_reason="end_turn")],
    )
    record = _fake_run(
        monkeypatch, task, tmp_path, runner=FakeRunner(transcript_path=transcript)
    )

    assert [e.command for e in record.destructive_events] == ["rm -rf tests/"]


def test_a_torn_multibyte_wire_line_does_not_cost_the_record(
    task, tmp_path, monkeypatch
):
    """A truncation splitting a multi-byte character raises UnicodeDecodeError
    -- a ValueError, so the JSONDecodeError catch never saw it -- before a
    single line had been examined, inside the finalize phase."""
    from bakeoff.runner import make_run_id

    run_id = make_run_id(task.task_id, "gemma-4-31b", 0, 1)
    payload = (json.dumps(_entry(1)) + "\n").encode("utf-8")
    payload += '{"metadata": {"run_id": "r"}, "x": "café'.encode("utf-8")[:-1]
    wire_dir = _wire_dir_with(tmp_path, run_id, payload)

    record = _fake_run(monkeypatch, task, tmp_path, proxy_wire_dir=wire_dir)

    assert record.wire_entries_seen == 1, "the readable entry must survive"
    assert record.wire_malformed_lines == 1, "and the loss must be counted"


def test_a_non_object_wire_line_does_not_cost_the_record(task, tmp_path, monkeypatch):
    """`null` is valid JSON and raises AttributeError on `.get`."""
    from bakeoff.runner import make_run_id

    run_id = make_run_id(task.task_id, "gemma-4-31b", 0, 1)
    payload = (json.dumps(_entry(1)) + "\nnull\n123\n[]\n").encode("utf-8")
    wire_dir = _wire_dir_with(tmp_path, run_id, payload)

    record = _fake_run(monkeypatch, task, tmp_path, proxy_wire_dir=wire_dir)

    assert record.wire_entries_seen == 1
    assert record.wire_malformed_lines == 3


def test_wire_malformed_lines_is_none_when_nobody_counted(task, tmp_path, monkeypatch):
    """None is "no wire directory", 0 is a measurement."""
    record = _fake_run(monkeypatch, task, tmp_path)
    assert record.wire_malformed_lines is None


def test_a_wire_log_collision_does_not_zero_the_wire_derived_fields(
    task, tmp_path, monkeypatch
):
    """The 3.6.0 defect's second door.

    A WireLogger that cannot open used to skip the replay entirely, so the
    record got empty `sampling`, empty hashes, `wire_entries_seen: 0` and --
    the expensive part -- NO EXCLUSION. An auth-failed run whose wire log
    collided was recorded as a model that made zero turns.
    """
    import bakeoff.runner as runner_module
    from bakeoff.runner import make_run_id

    run_id = make_run_id(task.task_id, "gemma-4-31b", 0, 1)
    payload = b""
    for i in (1, 2):
        payload += (
            json.dumps(
                _entry(i, failed=True, status=401, message="Invalid API key")
            )
            + "\n"
        ).encode("utf-8")
    wire_dir = _wire_dir_with(tmp_path, run_id, payload)

    def refuse(_path):
        raise OSError("File exists")

    monkeypatch.setattr(runner_module, "WireLogger", refuse)
    record = _fake_run(monkeypatch, task, tmp_path, proxy_wire_dir=wire_dir)

    assert record.wire_log_error, "the gz is genuinely missing and must say so"
    assert record.wire_entries_seen == 2, "the proxy's own file is still readable"
    assert record.exclusion is not None, "an auth failure must still exclude"
    assert record.exclusion.reason_code == "api_auth"
    assert record.artifacts.wire_log_gz is None


def test_a_replay_failure_does_not_publish_a_truncated_wire_log(
    task, tmp_path, monkeypatch
):
    """Containment alone would ship a lie.

    `artifacts.wire_log_gz` is published on `not wire_log_error and exists()`.
    Contain a mid-replay failure into `finalize_error` only and the gz exists,
    truncated, and is published as the section 6.2 artifact -- worse than the
    loud record loss it replaced.
    """
    import bakeoff.runner as runner_module
    from bakeoff.runner import make_run_id

    run_id = make_run_id(task.task_id, "gemma-4-31b", 0, 1)
    payload = b"".join(
        (json.dumps(_entry(i)) + "\n").encode("utf-8") for i in (1, 2, 3)
    )
    wire_dir = _wire_dir_with(tmp_path, run_id, payload)

    real_logger = runner_module.WireLogger

    class HalfBrokenLogger(real_logger):
        _calls = 0

        def log_call(self, **kwargs):
            HalfBrokenLogger._calls += 1
            if HalfBrokenLogger._calls == 2:
                raise OSError("[Errno 28] No space left on device")
            return super().log_call(**kwargs)

    monkeypatch.setattr(runner_module, "WireLogger", HalfBrokenLogger)
    record = _fake_run(monkeypatch, task, tmp_path, proxy_wire_dir=wire_dir)

    assert record.wire_log_error, "a truncated gz must not pass as written"
    assert record.artifacts.wire_log_gz is None
    # And the derived fields still read the WHOLE trailing block, from the
    # proxy's file -- not the 1 entry the WireLogger managed to write.
    assert record.wire_entries_seen == 3


def test_invocation_stamp_is_threaded_through_execute_run(task, tmp_path, monkeypatch):
    """lessons.md entry 2: a parameter nobody passes is invisible to a unit
    test on the function that receives it. This drives the OUTERMOST entry
    point, which is the only thing that exercises the wiring."""
    record = _fake_run(
        monkeypatch, task, tmp_path, collection_id="coll-1", invocation_stamp="inv-9"
    )
    assert record.collection_id == "coll-1"
    assert record.invocation_stamp == "inv-9"
