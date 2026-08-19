"""The judge's own record: what one LLM verdict concluded, and from exactly
what. See `docs/superpowers/specs/2026-08-18-judge-design.md`, section
"`judge_schema.py` -- `JudgeRecord`", and spec section 6.3.

The judge RANKS; the deterministic ladder GATES (spec section 4.2.3), and the
two are never averaged. So this file is a sibling of `grades/grades.jsonl`, not
a child of it: a `JudgeRecord` carries no `resolved`, contributes nothing to
pass@1, and cannot promote a `GradeFailure` into a pass. A reader that folds a
rubric score into a resolve rate has undone the separation the whole design is
built on.

A verdict is a DERIVED view, worth nothing without the inputs it was derived
from, and the judge has more of those than the grader does. `judge_model_id`,
`judge_prompt_version`, `judge_prompt_sha`, `judge_sampling`, `rubric_version`,
`grade_version_seen` and `graded_against_task_set_commit` name each one:

* `judge_model_id` is PINNED, never an alias. `luna`, `sol` and `terra` are
  three models rather than three names for one, so a floating alias is a moving
  oracle -- the same submission scores differently on two passes and the record
  cannot say which model answered.
* `judge_prompt_version` is DECLARED and `judge_prompt_sha` is MEASURED, on the
  `grader_version`/`grader_commit` precedent. The version is what a resume gate
  reads; the sha over the exact rendered prompt is the evidence that the gate
  was honest. A bumped version beside an unchanged sha, or the reverse, is the
  drift worth seeing, and only storing both makes it visible.
* `judge_sampling` is recorded AS SENT, cap included under whatever key it went
  out with -- `max_tokens` on one body, `max_completion_tokens` on another
  (`runner.py` renames it per provider). Normalising the key here would file a
  request that was never made.
* `grade_version_seen` maps `run_id -> grader_version`, because a re-grade
  under a new grader can flip `resolved`, which changes which pairs are
  gate-decided. A verdict is only interpretable against the grade generation it
  actually saw.

Null semantics, in the order they are easiest to get wrong:

* The rubric half and the pairwise half are both `| None`, and `kind` says
  which half is populated. `None` is NOT ASKED, never a neutral answer -- a
  `dimension_scores` defaulted to zeros on a pairwise is five failing scores
  nobody gave, and a `vote_index` defaulted to `0` on a gate-decided pair is a
  vote nobody cast.
* `verdict == "gate_decided"` is the one verdict no model produced. One side
  failed the deterministic gate and the objective result settles it, so there
  is no call to record: `position_assignment`, `vote_index`,
  `input_payload_path` and `input_payload_sha` are all `None` together, and
  `gate_decided_by` names the winner. Filling any of them would assert a model
  call that never happened, which is precisely what the judge must not do -- it
  never rescues a failed gate, and it never claims to have been asked.
* `run_id_a`/`run_id_b` are CANONICAL, the two run ids lexicographically
  sorted, and `position_assignment` records which of them was shown first.
  Storing the presentation order in the identity would make `(x, y)` and
  `(y, x)` two different comparisons, and the position-swap probe the design
  asks for could no longer find its own pairs.

The payload is REQUIRED, not optional, and section 4.3 says why: *because full
judge inputs are logged, a re-judge is a re-score, not a re-run*. The tokens for
the original run are already spent, so a verdict whose input cannot be
reconstructed forces a re-collection to re-score. It is stored gzipped beside
the jsonl rather than inline because payloads carry two full diffs and would
otherwise dominate the line file.

Judgments are APPEND-ONLY. There is no update and no delete, for the reason the
event log and the grade file have none: a re-judge under a different model,
prompt version or rubric is a NEW line, and the disagreement between the two
lines is the finding. An update API would delete the only evidence that the
judge is not deterministic.

`human_grader_record` (spec section 6.3, second half) is deliberately absent.
The Cohen's kappa calibration it feeds is blocked on people rather than on code
(OPEN-5), and a stub written now would fix `shown_payload` and `time_spent_s`
semantics before anyone has graded a gold subset. Do not add it here without
that design.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from bakeoff.scanners import scan_secrets

# The judgment file's own schema, independent of the run record's and of the
# grade file's. A reader that cannot tell judgment schema versions apart reads
# an absent field as a positive negative claim -- the same reason
# `SCHEMA_VERSION` and `GRADE_SCHEMA_VERSION` move for additive bumps.
JUDGE_SCHEMA_VERSION = "1.0.0"


def _build(klass: type, data: dict[str, Any]) -> Any:
    """Construct a record class, ignoring fields it does not know.

    Copied from `grade_schema._build` rather than imported, on purpose: the
    judge is a separate, offline reader with its own schema, and importing
    another view's private helper would couple the judgment file's read path to
    a grade-record refactor. The reasoning is the same one -- a judgment written
    by a LATER judge must not be lost to a `TypeError` over one added field. The
    reader still sees `judge_schema_version`, so it can tell it is holding a
    newer judgment and decide; raising would make that decision for it,
    permanently, in the direction of losing the data.
    """
    known = {f.name for f in fields(klass)}
    return klass(**{k: v for k, v in data.items() if k in known})


class PayloadSecretsFound(RuntimeError):
    """A judge payload matched a secret pattern and was not written.

    Raised BEFORE any file exists, tmp included. The payload carries the
    repository source the model was shown, so it is the same exposure spec
    section 6.2 has wire logs scanned for; refusing after the temporary file
    landed would leave the secret on disk under a name nothing later reads or
    cleans up, losing the judgment and keeping the secret.
    """


@dataclass(frozen=True)
class JudgeRecord:
    """One judge verdict: one rubric profile, or one vote in one comparison.

    Identity is `judgment_id`, minted by the caller. Not derived from the
    comparison, because three votes over the same pair are three records that
    must not collide, and a re-judge under a new prompt version is a fourth.
    """

    judgment_id: str
    judged_at: str
    # PINNED, never an alias. See the module docstring.
    judge_model_id: str
    # Moves on ANY change to the prompt text, including one that reads as
    # cosmetic: whitespace and ordering move model output, so a prompt edit
    # under an unchanged version silently mixes two generations of verdict.
    judge_prompt_version: int
    # sha256 of the exact RENDERED prompt, task text and all -- the evidence
    # that the declared version was honest.
    judge_prompt_sha: str
    # As sent, cap under the name it went out with. See the module docstring.
    judge_sampling: dict[str, Any]
    rubric_version: str
    # "rubric" | "pairwise". Says which half of the record below is populated.
    kind: str
    task_id: str
    judge_schema_version: str = JUDGE_SCHEMA_VERSION

    # --- rubric ---
    # The single run being profiled. `None` on a pairwise, where the two runs
    # are `run_id_a`/`run_id_b` and neither is "the" run.
    run_id: str | None = None
    # Exactly the five orthogonal dimensions, each in {0, 1, 2}. A 3-point
    # anchored scale on purpose: 1-10 scales show poor inter-rater agreement,
    # and the profile is reported per model rather than summed into a rank.
    dimension_scores: dict[str, int] | None = None
    # Exactly the three unscored binary flags. Diagnostic, never a score.
    flags: dict[str, bool] | None = None

    # --- pairwise ---
    # Canonical: the two run ids lexicographically sorted, so `(x, y)` and
    # `(y, x)` are one comparison. Presentation order lives in
    # `position_assignment` instead.
    run_id_a: str | None = None
    run_id_b: str | None = None
    # Sample i of one model against sample i of the other, on the same task.
    # Both are random draws, which is what makes the comparison unbiased --
    # pairing a draw against a best or a median compares a draw to a statistic.
    sample_index: int | None = None
    # "a_first" | "b_first". STORED, not derived, so a position-swap probe can
    # be run over a subset after the fact without re-judging everything.
    # `None` on a gate-decided pair: nothing was shown to anybody.
    position_assignment: str | None = None
    # "a" | "b" | "tie" | "gate_decided", in canonical terms rather than in
    # presentation terms. A verdict recorded as "the one shown first" would be
    # unreadable without replaying the position assignment.
    verdict: str | None = None
    # "a" | "b", the winner the deterministic ladder already picked. `None`
    # unless `verdict == "gate_decided"`.
    gate_decided_by: str | None = None
    # 0..2 -- three votes per comparison, three independent calls. `None` on a
    # gate-decided pair, where no call was made.
    vote_index: int | None = None

    # --- both ---
    # Kept verbatim. It is the only thing that makes a verdict auditable by a
    # human, and it is what a disputed kappa is re-examined against.
    full_reasoning_text: str = ""
    # The gzipped payload beside the jsonl, and the sha256 of its UNCOMPRESSED
    # canonical JSON. Required for every verdict a model produced; `None`
    # together on a gate-decided pair.
    input_payload_path: str | None = None
    input_payload_sha: str | None = None
    # `run_id -> grader_version`. A verdict names the grade generation it was
    # gated on. See the module docstring.
    grade_version_seen: dict[str, str] = field(default_factory=dict)
    graded_against_task_set_commit: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict.

        `asdict` and nothing else: every field is a str, int, bool, `None` or a
        dict of those, so there is no tuple to preserve and no enum to unwrap.
        `grade_schema.to_dict` encodes because a caller holding a `GradeFailure`
        is its expected case; adding the same walk here would be a conversion
        with nothing to convert.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JudgeRecord:
        return _build(cls, data)


def append_judgment(path: Path, record: JudgeRecord) -> None:
    """Append one judgment as one JSON line. Never truncates.

    Mode `"a"` and no update or delete API, for the same reason the event log
    and the grade file have none: a re-judge under a different judge model,
    prompt version or rubric is a new line, and the disagreement between the two
    lines is the finding. Overwriting would destroy the only evidence that the
    judge is not deterministic.

    `flush()` + `os.fsync()` per line because a judging batch is thousands of
    paid model calls and is killed by an operator or a rate limit far more often
    than it finishes -- an unflushed tail means paying again for verdicts that
    were already bought.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_judgments(path: Path) -> tuple[list[JudgeRecord], int]:
    """Read a judgment file. Returns `(records, malformed_lines)`.

    A malformed line is skipped and COUNTED, on the `load_grades` precedent:
    raising would make one damaged byte -- a torn write from a killed batch,
    most likely -- unread every good verdict in the file. Returning the count
    rather than swallowing it is the other half: a silent skip turns a truncated
    file into a smaller-looking collection, and the driver's resume is keyed on
    which comparisons are already judged, so an unreadable verdict read as an
    absent one is a model call paid for twice.

    A missing file is `([], 0)`, not an error: nothing has been judged yet is
    the first state every collection is in.
    """
    path = Path(path)
    if not path.exists():
        return [], 0

    records: list[JudgeRecord] = []
    malformed = 0
    # errors="replace" so a torn multibyte sequence becomes U+FFFD and fails
    # json.loads -- one COUNTED line -- rather than raising UnicodeDecodeError
    # out of the iterator and losing every verdict after it in the file.
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                # Counted, not ignored. A trailing newline does not produce one
                # of these; a bare blank line means something wrote into the
                # file that was not a judgment.
                malformed += 1
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(data, dict):
                malformed += 1
                continue
            try:
                records.append(JudgeRecord.from_dict(data))
            except (TypeError, ValueError):
                # A line that parses as JSON but cannot become a JudgeRecord --
                # a missing required field from a hand-edited file. Same
                # treatment: countable damage, not a reason to lose the rest.
                malformed += 1
    return records, malformed


def write_payload(
    payloads_dir: Path, judgment_id: str, payload: dict[str, Any]
) -> tuple[str, str]:
    """Store one judge payload gzipped. Returns `(path, sha256_hex)`.

    Order is the contract, and each step is placed against a specific failure:

    1. Canonicalise with `sort_keys=True`. The same payload assembled in a
       different dict order must produce the same bytes, or the sha stops
       identifying the payload and starts identifying the assembly.
    2. Scan the canonical text for secrets and raise `PayloadSecretsFound`
       BEFORE anything is created. The payload carries the repository source
       shown to a third-party model.
    3. sha256 the UNCOMPRESSED canonical bytes, never the archive. A digest over
       the compressed form would move with the zlib level or a gzip header
       change, and a verdict whose recorded input digest no longer matches its
       own input is indistinguishable from a tampered one.
    4. Write to `<judgment_id>.json.gz.tmp` and `os.replace` into place. The
       rename is atomic, so a killed batch leaves either a whole payload or no
       payload -- a torn gzip beside an already-written jsonl line is a verdict
       that names an input nobody can read, which breaks "a re-judge is a
       re-score, not a re-run" (section 4.3) exactly as a missing payload does,
       while looking like a present one.

    `mtime=0` and `filename=""` are both required for the archive to be a
    function of the payload alone. Left to itself, `GzipFile` stamps the current
    time into the header and copies FNAME off the file handle -- which here
    would be the TEMPORARY name, so every artifact of one payload would differ
    in bytes and in a name that no longer exists after the rename. Neither
    surfaces as an error; both surface as two archives of one payload that no
    longer compare equal.
    """
    canonical = json.dumps(payload, sort_keys=True).encode()

    found = scan_secrets(canonical.decode())
    if found:
        raise PayloadSecretsFound(
            f"judge payload for {judgment_id} matched {', '.join(sorted(found))}; "
            "not written"
        )

    sha = hashlib.sha256(canonical).hexdigest()

    payloads_dir = Path(payloads_dir)
    payloads_dir.mkdir(parents=True, exist_ok=True)
    final = payloads_dir / f"{judgment_id}.json.gz"
    tmp = payloads_dir / f"{judgment_id}.json.gz.tmp"
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(canonical)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, final)
    return str(final), sha


def read_payload(path: str | Path) -> dict[str, Any]:
    """Read back one gzipped judge payload.

    No sha check here: the digest lives on the `JudgeRecord`, and a reader that
    verified silently against a digest it was handed alongside the file would
    prove only that the two travelled together. Whoever compares them holds
    both.
    """
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode())
