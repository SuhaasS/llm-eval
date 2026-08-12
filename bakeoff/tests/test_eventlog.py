import json

import pytest

from bakeoff.eventlog import EventLog, ImmutabilityError
from bakeoff.schema import Outcome, RunRecord, TerminationReason


def make_record(
    run_id: str = "r-001",
    task_id: str = "t-001",
    model: str = "claude-sonnet-5",
    outcome: Outcome = Outcome.RESOLVED,
    turns_used: int = 7,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        task_id=task_id,
        task_version=1,
        model=model,
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-04T00:00:00Z",
        finished_at="2026-08-04T00:05:00Z",
        outcome=outcome,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=turns_used,
    )


def test_write_then_read_round_trips(tmp_path):
    log = EventLog(tmp_path)
    log.write_run(make_record())
    assert log.read_run("r-001") == make_record()


def test_writing_same_run_id_twice_raises(tmp_path):
    log = EventLog(tmp_path)
    log.write_run(make_record())
    with pytest.raises(ImmutabilityError):
        log.write_run(make_record())


def test_index_appends_one_line_per_run(tmp_path):
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))
    log.write_run(make_record("r-002"))

    lines = (tmp_path / "index.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(x)["run_id"] for x in lines] == ["r-001", "r-002"]


def test_list_runs_returns_all_ids(tmp_path):
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))
    log.write_run(make_record("r-002"))
    assert sorted(log.list_runs()) == ["r-001", "r-002"]


def test_the_prior_run_lookup_returns_the_most_recently_written_match(tmp_path):
    """Last line, not max started_at: the index is appended after the record
    is durable, so line order is completion order -- and the run that finished
    most recently is the one that most recently touched the cache."""
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))
    log.write_run(make_record("r-002"))
    assert log.last_run_id_for("t-001", "claude-sonnet-5") == "r-002"


def test_a_run_of_another_model_on_the_same_task_is_not_a_prior_run(tmp_path):
    """Bedrock's prompt cache is per-model. A Sonnet run cannot warm
    Nemotron's, so keying on task alone would store an id that cannot explain
    the `warm` recorded beside it."""
    log = EventLog(tmp_path)
    log.write_run(make_record("r-sonnet", model="claude-sonnet-5"))
    log.write_run(make_record("r-kimi", model="kimi-k2-5"))

    assert log.last_run_id_for("t-001", "kimi-k2-5") == "r-kimi"
    assert log.last_run_id_for("t-001", "claude-sonnet-5") == "r-sonnet"
    assert log.last_run_id_for("t-002", "claude-sonnet-5") is None


def test_a_run_that_made_no_model_call_is_not_named_as_the_warmer(tmp_path):
    """A run that died before its first call touched no cache, and naming it
    puts a plausible-looking id beside a `warm` it cannot explain."""
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))
    log.write_run(make_record("r-died", outcome=Outcome.CRASHED, turns_used=0))

    assert log.last_run_id_for("t-001", "claude-sonnet-5") == "r-001"


def test_a_crash_after_the_model_calls_still_counts_as_a_warmer(tmp_path):
    """Why this filters on turns_used and not on outcome.

    `execute_run` sets container_crashed from an `except Exception` around its
    whole body, so a run that made twenty calls and then failed in checkpoint
    capture or container teardown is CRASHED. It warmed the cache. Skipping it
    on outcome loses the only run that explains the next one's `warm` -- the
    same unexplained-warm failure the filter exists to prevent, reintroduced by
    the filter itself.
    """
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))
    log.write_run(make_record("r-late-crash", outcome=Outcome.CRASHED, turns_used=20))

    assert log.last_run_id_for("t-001", "claude-sonnet-5") == "r-late-crash"


def test_an_index_line_written_before_turns_used_existed_is_kept(tmp_path):
    """Absent evidence is not evidence of a quiet run. Pre-3.0.0 index lines
    carry no turns_used, and dropping them would silently orphan every warm
    run that a record written yesterday explains."""
    log = EventLog(tmp_path)
    log.index_path.write_text(
        json.dumps(
            {
                "run_id": "r-old",
                "task_id": "t-001",
                "model": "claude-sonnet-5",
                "started_at": "2026-08-04T00:00:00Z",
            }
        )
        + "\n"
    )
    assert log.last_run_id_for("t-001", "claude-sonnet-5") == "r-old"


def test_a_corrupt_index_line_cannot_cost_a_run_its_record(tmp_path):
    """execute_run calls this OUTSIDE its try. Anything raised here escapes as
    an exception from execute_run itself, so the run produces no record at all
    -- and because the bad line stays in the index, so does every later run.

    Catching OSError and JSONDecodeError was not enough: `null` is valid JSON
    and raises AttributeError on `.get`, and a truncation splitting a
    multi-byte character raises UnicodeDecodeError from the iterator.
    """
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))
    with open(log.index_path, "a", encoding="utf-8") as index:
        index.write("null\n[]\n123\n")
    with open(log.index_path, "ab") as index:
        index.write(b'{"run_id": "r-t\xff\xfe')

    assert log.last_run_id_for("t-001", "claude-sonnet-5") == "r-001"


def test_the_prior_run_lookup_returns_its_start_time_for_the_ttl_gap(tmp_path):
    """CacheState.seconds_since_prior_run is computed from this. It rides on
    the same scan because opening the prior run's record would turn a total
    lookup into one that can fail, in a call site that must not fail."""
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))

    assert log.last_run_for("t-001", "claude-sonnet-5") == (
        "r-001",
        "2026-08-04T00:00:00Z",
    )
    assert log.last_run_for("t-999", "claude-sonnet-5") is None


def test_an_absent_index_reports_no_prior_run_rather_than_raising(tmp_path):
    """execute_run calls this outside its try, before the container starts.
    Raising here would cost the record entirely -- the one loss this harness
    cannot recover at any price."""
    log = EventLog(tmp_path)
    assert not log.index_path.exists()
    assert log.last_run_id_for("t-001", "claude-sonnet-5") is None


def test_a_torn_trailing_index_line_does_not_break_the_lookup(tmp_path):
    """Nothing serializes index appends, so a reader can catch a partial
    final line. Skipping it costs one candidate; raising costs a run."""
    log = EventLog(tmp_path)
    log.write_run(make_record("r-001"))
    with open(log.index_path, "a", encoding="utf-8") as index:
        index.write('{"run_id": "r-tor')

    assert log.last_run_id_for("t-001", "claude-sonnet-5") == "r-001"


def test_partial_write_does_not_corrupt_index(tmp_path, monkeypatch):
    """A crash mid-write must leave no index entry claiming a run exists."""
    log = EventLog(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("bakeoff.eventlog.json.dump", boom)
    with pytest.raises(OSError):
        log.write_run(make_record("r-bad"))

    index = tmp_path / "index.jsonl"
    assert not index.exists() or "r-bad" not in index.read_text()
