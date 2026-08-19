"""Unit tests for the judgment record and its payload store.

Three halves, not two. The first is the storage posture `grade_schema.py`
already established and this module copies: an unknown field on a stored line
is dropped rather than fatal, a damaged line is COUNTED rather than raised, and
a file that does not exist yet is `([], 0)` rather than an error.

The second is the null semantics that only the judge has. A gate-decided
pairwise made no model call, so it carries no payload, no position assignment
and no vote index -- and `None` there has to survive the round trip, because a
`0` vote index on a comparison nobody voted on is a fabricated observation.

The third is the payload store, which is where the interesting failures live.
The secret scan reads the payload's STRINGS rather than its serialization,
because JSON escaping disarms the quote-anchored patterns -- pinned below
against the real scanner, both directions.
`write_payload` promises a sha OF THE UNCOMPRESSED canonical JSON and a
byte-stable `.gz`, and both are pinned here: the sha is recomputed
independently, and the same payload written under two judgment ids must produce
identical bytes. The second assertion is what catches gzip's two nondeterminism
sources -- the mtime in the header and the FNAME the header picks up off the
file handle -- neither of which shows up as an error, only as two artifacts of
one payload that no longer compare equal.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from bakeoff.judge_schema import (
    JUDGE_SCHEMA_VERSION,
    JudgeRecord,
    PayloadSecretsFound,
    append_judgment,
    load_judgments,
    read_payload,
    write_payload,
)
from bakeoff.scanners import scan_secrets

# The canonical AWS documentation example. `scanners._SECRET_PATTERNS` matches
# `AKIA` + 16 uppercase alphanumerics, so this is a string the scanner really
# flags rather than one this test hopes it flags.
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

# A double-quoted assignment, which is the commonest shape in JS, JSON and
# Python source and the one JSON escaping hides. `generic_api_key` anchors on
# the quote immediately after `=`, and serialising puts a backslash there.
DOUBLE_QUOTED_SECRET_LINE = '+api_key = "sk-live-abcdefghijklmnopqrstuv"\n'


def _common(**kw) -> dict:
    base = dict(
        judged_at="2026-08-18T00:00:00Z",
        judge_model_id="openai.gpt-5.6-sol",
        judge_prompt_version=3,
        judge_prompt_sha="b" * 64,
        judge_sampling={"temperature": 0.0, "max_completion_tokens": 4096},
        rubric_version="1.0.0",
        task_id="trucking-pilot-v2/PR-1",
        graded_against_task_set_commit="0f1e2d3",
    )
    base.update(kw)
    return base


def _rubric(judgment_id: str = "j-rubric", **kw) -> JudgeRecord:
    base = _common(
        judgment_id=judgment_id,
        kind="rubric",
        run_id="run-a",
        dimension_scores={
            "functional_equivalence": 2,
            "completeness": 1,
            "cross_file_consistency": 2,
            "scope_discipline": 0,
            "convention_adherence": 2,
        },
        flags={
            "introduced_stub": False,
            "left_debug_artifacts": False,
            "wrote_tests": True,
        },
        full_reasoning_text="The candidate reimplements the reference behaviour.",
        input_payload_path="payloads/j-rubric.json.gz",
        input_payload_sha="c" * 64,
        grade_version_seen={"run-a": "1"},
    )
    base.update(kw)
    return JudgeRecord(**base)


def _pairwise(judgment_id: str = "j-pair", **kw) -> JudgeRecord:
    base = _common(
        judgment_id=judgment_id,
        kind="pairwise",
        run_id_a="run-a",
        run_id_b="run-b",
        sample_index=2,
        position_assignment="b_first",
        verdict="a",
        vote_index=1,
        full_reasoning_text="A updates both callers; B leaves one stale.",
        input_payload_path="payloads/j-pair.json.gz",
        input_payload_sha="d" * 64,
        grade_version_seen={"run-a": "1", "run-b": "1"},
    )
    base.update(kw)
    return JudgeRecord(**base)


def _gate_decided(judgment_id: str = "j-gate") -> JudgeRecord:
    """The pairwise nobody voted on: one side failed the ladder, so the
    objective result settles it and no model was called."""
    return _pairwise(
        judgment_id,
        verdict="gate_decided",
        gate_decided_by="a",
        position_assignment=None,
        vote_index=None,
        input_payload_path=None,
        input_payload_sha=None,
        full_reasoning_text="",
    )


def test_a_record_round_trips_with_none_fields_preserved():
    """Both kinds survive `to_dict`/`from_dict`, and every `None` stays `None`.

    The rubric half of the record is absent on a pairwise and the pairwise half
    is absent on a rubric, so a round trip that coerced either to a default
    would invent a dimension score of 0 or a vote index of 0 -- both of which
    read downstream as observations rather than as absences.
    """
    for record in (_rubric(), _pairwise(), _gate_decided()):
        back = JudgeRecord.from_dict(json.loads(json.dumps(record.to_dict())))
        assert back == record

    rubric = JudgeRecord.from_dict(_rubric().to_dict())
    assert rubric.run_id_a is None
    assert rubric.run_id_b is None
    assert rubric.verdict is None
    assert rubric.vote_index is None
    assert rubric.judge_schema_version == JUDGE_SCHEMA_VERSION

    gate = JudgeRecord.from_dict(_gate_decided().to_dict())
    assert gate.position_assignment is None
    assert gate.vote_index is None
    assert gate.input_payload_path is None
    assert gate.input_payload_sha is None
    assert gate.dimension_scores is None
    assert gate.flags is None


def test_an_unknown_field_in_a_stored_line_is_dropped_not_fatal():
    """A judgment written by a LATER judge must not be lost to a `TypeError`
    over one added field. The reader still sees `judge_schema_version` and can
    decide it is holding a newer line; raising would make that decision for it,
    permanently, in the direction of losing the data."""
    data = _pairwise().to_dict()
    data["from_the_future"] = {"confidence": 0.9}
    assert JudgeRecord.from_dict(data).judgment_id == "j-pair"


def test_load_judgments_counts_malformed_lines_and_keeps_good_ones(tmp_path: Path):
    path = tmp_path / "judgments" / "judgments.jsonl"
    append_judgment(path, _rubric("j1"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("not json\n")
    append_judgment(path, _pairwise("j2"))

    records, malformed = load_judgments(path)
    assert [r.judgment_id for r in records] == ["j1", "j2"]
    assert malformed == 1


def test_a_blank_line_is_damage_not_padding(tmp_path: Path):
    """Iterating a file that ends in a newline yields no empty final element,
    so a line that strips to nothing means something wrote into the judgment
    file that was not a judgment. Counting it is what keeps the driver's resume
    gate honest -- a silent skip turns a torn batch into a smaller-looking
    collection that resumes cleanly and re-judges nothing."""
    path = tmp_path / "judgments.jsonl"
    path.write_text(
        json.dumps(_rubric("j1").to_dict()) + "\n\n"
        + json.dumps(_rubric("j2").to_dict()) + "\n"
    )
    records, malformed = load_judgments(path)
    assert [r.judgment_id for r in records] == ["j1", "j2"]
    assert malformed == 1


def test_missing_file_loads_as_empty(tmp_path: Path):
    """Nothing has been judged yet is the first state every collection is in."""
    assert load_judgments(tmp_path / "nope.jsonl") == ([], 0)


def test_payload_sha_is_stable_across_two_writes_of_the_same_payload(tmp_path: Path):
    """Same payload, two judgment ids: one sha and two byte-identical files.

    The bytes are asserted, not just the sha, because gzip has two headers that
    drift on their own -- the mtime, and the FNAME copied off the file handle,
    which would embed `<judgment_id>.json.gz.tmp` and make every artifact
    unique. Neither surfaces as an error; both surface as two archives of one
    payload that no longer compare equal, months later, when someone diffs the
    inputs of a re-judge against the inputs of the judge.
    """
    payloads = tmp_path / "payloads"
    payload = {"task_prompt": "fix add", "candidate_diff": "@@\n+x\n", "b": [3, 1]}

    path_one, sha_one = write_payload(payloads, "j-one", payload)
    path_two, sha_two = write_payload(payloads, "j-two", payload)

    assert sha_one == sha_two
    assert Path(path_one).read_bytes() == Path(path_two).read_bytes()
    assert Path(path_one).name == "j-one.json.gz"
    assert list(payloads.glob("*.tmp")) == []


def test_payload_sha_is_of_the_uncompressed_canonical_json(tmp_path: Path):
    """The sha names the payload, not the archive.

    Recomputed here from `json.dumps(..., sort_keys=True)` with no gzip in
    sight: a sha taken over the compressed bytes would move when the zlib level
    or the gzip header moved, and a verdict whose recorded input digest no
    longer matches its own input is indistinguishable from a tampered one.
    """
    payload = {"z": 1, "a": {"nested": [1, 2, 3]}}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()

    path, sha = write_payload(tmp_path / "payloads", "j-sha", payload)

    assert sha == expected
    with gzip.open(path, "rb") as handle:
        assert handle.read() == json.dumps(payload, sort_keys=True).encode()


def test_a_secret_in_the_payload_refuses_the_write_and_leaves_no_file(tmp_path: Path):
    """A scanner hit stops the write BEFORE anything lands, tmp included.

    The payload carries the repository source the model was shown, so it is the
    same exposure the wire log is scanned for (§6.2). Refusing after the tmp
    file exists would leave the secret on disk under a name nothing later reads
    or cleans up, which is the worst of both outcomes: the judgment is lost and
    the secret is kept.
    """
    payloads = tmp_path / "payloads"
    payloads.mkdir()
    payload = {"candidate_diff": f"+AWS_ACCESS_KEY_ID={FAKE_AWS_KEY}\n"}

    with pytest.raises(PayloadSecretsFound) as excinfo:
        write_payload(payloads, "j-secret", payload)

    assert "aws_access_key_id" in str(excinfo.value)
    assert list(payloads.iterdir()) == []


def test_a_double_quoted_secret_is_refused_though_json_escaping_hides_it(
    tmp_path: Path,
):
    """The scan reads the payload's strings, not its serialization.

    The first two assertions are the whole reason `write_payload` scans twice,
    and they are asserted against the real scanner rather than described: the
    same diff line is CLEAN as canonical JSON and DIRTY as the string it came
    from, because `generic_api_key` anchors on the quote that `json.dumps`
    escapes. A payload scanned only after serialization would write
    `+api_key = "sk-live-..."` to disk unflagged while the identical bytes in a
    wire log were caught (spec section 6.2) -- the scanner disarmed by the
    storage format, which is silent in the direction of keeping the secret.

    Nested one list and one dict deep, because the payload builder assembles
    per-run structures and a walk that only looked at top-level values would
    pass this test for the wrong reason.
    """
    payloads = tmp_path / "payloads"
    payloads.mkdir()
    payload = {"runs": [{"candidate_diff": DOUBLE_QUOTED_SECRET_LINE}]}

    assert scan_secrets(DOUBLE_QUOTED_SECRET_LINE) == ["generic_api_key"]
    assert scan_secrets(json.dumps(payload, sort_keys=True)) == []

    with pytest.raises(PayloadSecretsFound) as excinfo:
        write_payload(payloads, "j-quoted", payload)

    assert "generic_api_key" in str(excinfo.value)
    assert list(payloads.iterdir()) == []


def test_read_payload_round_trips_write_payload(tmp_path: Path):
    payload = {
        "task_prompt": "fix add",
        "reference_diff": "@@\n-a\n+b\n",
        "checks": [{"name": "f2p", "status": "pass"}],
        "similarity": {"changed_line_ratio": 1.5},
    }
    path, _ = write_payload(tmp_path / "payloads", "j-round", payload)
    assert read_payload(path) == payload
    assert read_payload(Path(path)) == payload
