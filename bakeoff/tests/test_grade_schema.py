"""tests/test_grade_schema.py"""
import json
from pathlib import Path

import pytest

from bakeoff.grade_schema import (
    CHECK_ORDER, CheckResult, GradeFailure, GradeRecord, NotGradedReason,
    append_grade, load_grades, schema_at_least,
)


def _record(run_id: str = "r1", **kw) -> GradeRecord:
    base = dict(
        run_id=run_id, collection_id="c1", task_id="t", model="m",
        record_schema_version="3.8.0",
        graded_at="2026-08-17T00:00:00Z", grader_version="1",
        checks=(CheckResult(name="patch_non_empty", status="pass"),),
        resolved=True, quarantined=("tests/test_a.py::test_flaky",),
        f2p_declared=3,
    )
    base.update(kw)
    return GradeRecord(**base)


def test_round_trip_preserves_every_field_and_type():
    rec = _record()
    back = GradeRecord.from_dict(rec.to_dict())
    assert back == rec
    assert isinstance(back.checks[0], CheckResult)
    assert isinstance(back.quarantined, tuple)


def test_round_trip_keeps_none_distinct_from_empty():
    ungraded = _record(quarantined=None, f2p_declared=None, resolved=None,
                       binary_chunks_dropped=None,
                       not_graded_reason=NotGradedReason.EXCLUDED.value)
    back = GradeRecord.from_dict(ungraded.to_dict())
    assert back.quarantined is None
    assert back.f2p_declared is None
    assert back.binary_chunks_dropped is None


def test_from_dict_drops_unknown_keys_instead_of_crashing():
    data = _record().to_dict()
    data["from_the_future"] = 1
    data["checks"][0]["also_new"] = 2
    assert GradeRecord.from_dict(data).run_id == "r1"


def test_append_then_load_returns_both_records(tmp_path: Path):
    p = tmp_path / "grades" / "grades.jsonl"
    append_grade(p, _record("a"))
    append_grade(p, _record("b"))
    records, malformed = load_grades(p)
    assert [g.run_id for g in records] == ["a", "b"]
    assert malformed == 0


def test_load_missing_file_is_empty(tmp_path: Path):
    assert load_grades(tmp_path / "nope.jsonl") == ([], 0)


def test_a_malformed_line_is_counted_not_fatal(tmp_path: Path):
    p = tmp_path / "grades.jsonl"
    p.write_text(json.dumps(_record("a").to_dict()) + "\nnot json\n"
                 + json.dumps(_record("b").to_dict()) + "\n")
    records, malformed = load_grades(p)
    assert [g.run_id for g in records] == ["a", "b"]
    assert malformed == 1


def test_schema_3_10_0_is_not_below_3_9_0():
    assert schema_at_least("3.10.0", "3.9.0")
    assert not schema_at_least("2.9.0", "3.0.0")
