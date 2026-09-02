"""tests/test_grade_schema.py"""
import json
from pathlib import Path

import pytest

from bakeoff.grade_schema import (
    CHECK_ORDER, GRADE_SCHEMA_VERSION, CheckResult, GradeFailure, GradeRecord,
    NotGradedReason, append_grade, load_grades, schema_at_least,
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


def test_the_grade_schema_version_moved_with_what_the_record_means():
    """A reader that cannot tell grade schema versions apart reads an absent
    field as a positive negative claim -- the same reason `SCHEMA_VERSION`
    moves for additive bumps.

    Pinned to a literal so a bump is a DELIBERATE edit rather than a side
    effect: 1.0.0 -> 1.1.0 adds `GradeRecord.suite_timeout_s`, because with
    the bound now per task, `timed_out: True` alone cannot say what the
    check actually blew; 1.1.0 -> 1.2.0 adds
    `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE`, which is a value a reader
    of the `not_graded_reason` field can now meet and could not before."""
    assert GRADE_SCHEMA_VERSION == "1.2.0"


def test_the_gitlink_refusal_is_a_not_graded_reason_and_not_a_failure():
    """It belongs to the FIRST group -- the run never produced a gradable
    submission -- and the enum it is NOT in is the point: a `GradeFailure`
    would put the row in the denominator as a model failure, which is the one
    claim this refusal exists to avoid making.
    """
    assert (NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE.value
            == "submodule_gitlink_ungradable")
    assert "submodule_gitlink_ungradable" not in {
        m.value for m in GradeFailure
    }


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


def test_a_1_0_0_line_loads_with_no_bound_rather_than_a_fabricated_one(
    tmp_path: Path,
):
    """`suite_timeout_s` arrived in 1.1.0, and every line written before it
    predates the field. `_build` filters to the dataclass's own fields, so
    such a line loads and the field defaults to `None` -- which is the same
    thing `None` means on a fresh line: no bounded command ran, nobody
    counted. Defaulting to 600 instead would turn "this writer had no such
    field" into the claim that this grade was measured against 600 s, which
    is exactly the read a version bump exists to prevent."""
    data = _record().to_dict()
    data["grade_schema_version"] = "1.0.0"
    del data["suite_timeout_s"]
    p = tmp_path / "grades.jsonl"
    p.write_text(json.dumps(data) + "\n", encoding="utf-8")

    records, malformed = load_grades(p)
    assert malformed == 0
    assert records[0].suite_timeout_s is None


def test_a_1_1_0_line_loads_unchanged_under_1_2_0(tmp_path: Path):
    """1.2.0 adds an enum MEMBER and no field, so a 1.1.0 line is
    field-identical and must load with nothing defaulted and nothing lost.
    The version still moves: a reader that meets
    `submodule_gitlink_ungradable` on a line claiming 1.1.0 would be reading
    a value that writer could not have produced.
    """
    data = _record().to_dict()
    data["grade_schema_version"] = "1.1.0"
    p = tmp_path / "grades.jsonl"
    p.write_text(json.dumps(data) + "\n", encoding="utf-8")

    records, malformed = load_grades(p)
    assert malformed == 0
    assert records[0].grade_schema_version == "1.1.0"
    assert records[0].suite_timeout_s == data["suite_timeout_s"]


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


def test_an_unparsable_version_raises_rather_than_reading_as_old():
    """A version that is not dotted integers is not evidence about age.

    Returning False would file RECORD_SCHEMA_TOO_OLD against a record whose
    version is merely unfamiliar -- a confident wrong reason, which is the
    defect `not_graded_detail` exists to prevent one layer up. The caller
    decides what to do with the raise.
    """
    with pytest.raises(ValueError):
        schema_at_least("3.8.0-rc1", "3.0.0")


def test_a_blank_line_is_damage_not_padding(tmp_path: Path):
    """A trailing newline does not produce a blank line; a bare one is damage.

    Iterating a file that ends in "\\n" yields no empty final element, so a
    line that strips to nothing means something wrote into the grade file that
    was not a grade. Counting it is what keeps the driver's resume gate honest:
    a silent `continue` here turns a torn batch into a smaller-looking
    collection that resumes cleanly and re-grades nothing.
    """
    p = tmp_path / "grades.jsonl"
    p.write_text(json.dumps(_record("a").to_dict()) + "\n\n"
                 + json.dumps(_record("b").to_dict()) + "\n")
    records, malformed = load_grades(p)
    assert [g.run_id for g in records] == ["a", "b"]
    assert malformed == 1


def test_a_json_line_that_is_not_an_object_is_counted(tmp_path: Path):
    """`json.loads` succeeds on a scalar and on an array, so the decode-error
    branch never sees them.

    The `isinstance(data, dict)` guard is belt-and-braces and this test says so
    rather than overclaiming: measured, `dict(123)` and `dict([1, 2])` raise
    TypeError and `dict("a string")` raises ValueError, all of which the last
    branch already catches, so the COUNT is the same with the guard removed.
    What the guard buys is rejecting a shape error where the shape is known
    instead of three exception types deep inside `from_dict`. The count is what
    the driver's resume gate reads, and the count is what is pinned here.
    """
    p = tmp_path / "grades.jsonl"
    p.write_text("123\n[1, 2]\n\"a string\"\n"
                 + json.dumps(_record("a").to_dict()) + "\n")
    records, malformed = load_grades(p)
    assert [g.run_id for g in records] == ["a"]
    assert malformed == 3


def test_an_object_missing_required_fields_is_counted(tmp_path: Path):
    """A hand-edited or half-written object parses as JSON and still cannot
    become a GradeRecord. Same treatment as unparsable bytes: countable damage,
    never a reason to lose the good grades around it."""
    p = tmp_path / "grades.jsonl"
    p.write_text(json.dumps({"run_id": "orphan"}) + "\n"
                 + json.dumps(_record("a").to_dict()) + "\n")
    records, malformed = load_grades(p)
    assert [g.run_id for g in records] == ["a"]
    assert malformed == 1


def test_check_order_is_the_ladder():
    """Pinned because the order is load-bearing twice: a later check's result
    is meaningless once an earlier one failed, and `grade_failure` names the
    FIRST rung that failed -- so reordering silently changes what every stored
    grade means."""
    assert CHECK_ORDER == (
        "patch_non_empty", "test_restore", "build", "typecheck", "f2p",
        "p2p", "lint", "secret_scan", "destructive_scan",
    )


def test_an_enum_survives_the_round_trip_as_its_plain_value():
    """`grade_failure` is typed `str | None`, and a caller holding the enum is
    the expected case. `to_dict` must emit the VALUE, or a reader without this
    module sees "GradeFailure.F2P_FAILED" where the schema promises
    "f2p_failed"."""
    rec = _record(resolved=False, grade_failure=GradeFailure.F2P_FAILED)
    data = rec.to_dict()
    assert data["grade_failure"] == "f2p_failed"
    assert json.loads(json.dumps(data))["grade_failure"] == "f2p_failed"
    assert GradeRecord.from_dict(data).grade_failure == "f2p_failed"
