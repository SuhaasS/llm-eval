import json

import pytest

from bakeoff.eventlog import EventLog, ImmutabilityError
from bakeoff.schema import Outcome, RunRecord, TerminationReason


def make_record(run_id: str = "r-001") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        task_id="t-001",
        task_version=1,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-04T00:00:00Z",
        finished_at="2026-08-04T00:05:00Z",
        outcome=Outcome.RESOLVED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=7,
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
