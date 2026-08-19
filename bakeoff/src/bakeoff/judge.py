"""The LLM judge: what it is shown, and what it must never be shown.

See `docs/superpowers/specs/2026-08-18-judge-design.md`, sections "What the
judge receives, and what it must never receive" and §4.2.3.

The judge RANKS; the deterministic ladder GATES, and the two are never
averaged. This module builds the input to a ranking call and nothing else --
it writes no `JudgeRecord` (`judge_schema.py`), reads no event log, and cannot
promote a `GradeFailure` into a pass.

The judge receives exactly six things: the task prompt, the reference diff,
the candidate diff, the deterministic check results as name plus status, the
`SimilarityContext` (§4.2.1), and -- for a pairwise -- a second submission.
It must NEVER receive any of four classes, each a distinct leak with its own
test in `tests/test_judge.py`:

1. **Model identity.** `RunRecord.model` and the arm name are the obvious
   half. `versions.bedrock_model_id`, the `sampling` block, `finish_reasons`,
   `system_prompt_sha` and `tool_schema_sha` are the less obvious half, and
   `artifacts.*` is the one that reads as innocent: every artifact path embeds
   the arm in a directory component, so a builder that copies `record.artifacts`
   verbatim ships the model name while copying no field called `model`.
2. **Cost.** `cost_usd` and the token counts identify arms nearly as well as
   names do -- Sonnet's `cache_read` signature and the candidates' null cost
   from `pricing_error` are both fingerprints.
3. **Timing.** `time.*`, `started_at`/`finished_at`, and every `duration_s` on
   a `CheckResult`. Same reasoning.
4. **Any prior verdict.** `GradeRecord.resolved`, `grade_failure`, another
   judge's `JudgeRecord` -- a prior verdict in the payload turns three
   independent votes into one vote and two confirmations, which is the failure
   that makes the vote count look like agreement.

**The guard is a whitelist, never a redactor.** The payload is assembled field
by field from named sources, so a `RunRecord` field added tomorrow is absent by
default rather than leaked by default. A deny-list is the same code with the
opposite failure direction: redaction fails open, whitelisting fails closed,
and `SCHEMA_VERSION` moves for additive fields precisely because readers cannot
be trusted to notice them. The structure that enforces this is the split
between `payload_inputs_from` and the two builders -- the first is the ONLY
function here that touches a `RunRecord` or a `GradeRecord`, and the builders
take `PayloadInputs`, so they physically cannot leak a record field because
they never see a record.

Both builders return plain JSON-ready dicts. `judge_schema.write_payload`
gzips and hashes exactly what is returned, so the payload the record proves is
the payload that was sent -- a builder that returned dataclasses would let
serialization differ between the sent copy and the stored one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bakeoff.grade_schema import GradeRecord
from bakeoff.schema import RunRecord
from bakeoff.similarity import SimilarityContext, similarity_context
from bakeoff.tasks import TaskManifest


@dataclass(frozen=True)
class PayloadInputs:
    """THE whitelist chokepoint: everything the judge may see about one run.

    Every field here was copied out of a record by name. Nothing downstream
    re-reads a record, so this class is the complete and only surface -- a
    leak that is not visible in these five fields cannot exist.

    `checks` is `dict[str, str]` with exactly the keys `name` and `status`, not
    `CheckResult`, on purpose: `CheckResult` also carries `output_path` (an arm
    name in a path), `duration_s` (timing) and `detail` (a captured traceback
    naming the harness), and passing the dataclass through would leak all three
    the moment somebody serialized it with `asdict`.
    """

    task_prompt: str
    reference_diff: str
    candidate_diff: str
    checks: tuple[dict[str, str], ...]
    similarity: SimilarityContext


def payload_inputs_from(
    record: RunRecord, grade: GradeRecord, task: TaskManifest
) -> PayloadInputs:
    """Copy the six permitted facts out of one run, and nothing else.

    The ONLY function in this module that touches a `RunRecord` or a
    `GradeRecord`. Into the payload it reads exactly
    `record.artifacts.final_diff`, `grade.checks` (name and status),
    `task.prompt`, `task.solution_diff` and `task.extra_files`; it also reads
    `record.run_id` for the `ValueError` below, which goes to the operator and
    never to a model. Keeping that list in one function is what makes the
    whitelist auditable: a reviewer checks one body rather than trusting that
    no builder anywhere reached back into a record.

    **The anchor is `task.solution_diff`, NOT `task.reference_diff`.** The
    reference diff is the merged PR whole -- fix half PLUS test half -- and the
    agent was handed the test half in its start state (`tasks.py` commits it
    into the start state, which is why `start_sha` is not `base_sha`). Anchoring
    on the whole does two wrong things at once: it hands the judge the oracle
    for the `wrote_tests` rubric flag, and it compares the submission against a
    diff the agent was never asked to produce, so every honest submission looks
    incomplete by exactly the size of the test half.

    `task.extra_files` goes to `similarity_context` as `drop_paths`, which is
    what that field is recorded for (`tasks.py:213`): a changelog both sides
    touched otherwise inflates the file overlap and the line ratio with work
    neither side was asked to do.

    Raises `ValueError` when `final_diff` is `None`. The driver should never
    let that happen -- an ungraded run is never judged, and `NO_FINAL_DIFF` is
    a `NotGradedReason` -- but the alternative to raising is a payload asking a
    model to rank an empty string against a reference, which produces a
    well-formed verdict about nothing. An EMPTY diff is different and is
    allowed through: it is a real submission, and the ladder's
    `patch_non_empty` rung is in `checks` saying so.
    """
    candidate_diff = record.artifacts.final_diff
    if candidate_diff is None:
        raise ValueError(
            "cannot judge a run with no artifacts.final_diff "
            f"(run_id={record.run_id!r}): an ungraded run is never judged"
        )

    return PayloadInputs(
        task_prompt=task.prompt,
        reference_diff=task.solution_diff,
        candidate_diff=candidate_diff,
        checks=tuple(
            {"name": check.name, "status": check.status}
            for check in grade.checks
        ),
        similarity=similarity_context(
            candidate_diff, task.solution_diff, drop_paths=task.extra_files
        ),
    )


def build_rubric_payload(inputs: PayloadInputs) -> dict[str, Any]:
    """The absolute-rubric payload: one submission against the reference.

    A closed key set, asserted exactly by the tests at every nesting level. The
    keys are written out literally rather than derived from `asdict(inputs)`,
    because a field added to `PayloadInputs` tomorrow must be a deliberate
    addition here too -- `asdict` would make the next field automatic, which is
    the deny-list failure direction re-introduced one layer up.
    """
    return {
        "kind": "rubric",
        "task_prompt": inputs.task_prompt,
        "reference_diff": inputs.reference_diff,
        "candidate_diff": inputs.candidate_diff,
        "checks": _checks(inputs),
        "similarity": inputs.similarity.to_dict(),
    }


def build_pairwise_payload(
    first: PayloadInputs, second: PayloadInputs
) -> dict[str, Any]:
    """The blind pairwise payload: two submissions, in SHOWN order.

    `first` and `second` are presentation order and the position assignment is
    already applied by the caller. Randomizing here would put the assignment
    somewhere `JudgeRecord.position_assignment` could not observe it, and a
    stored assignment that does not match what was sent makes the position-swap
    probe measure nothing -- which is worse than not running the probe, because
    it reports a bias figure.

    Blind: neither slot carries a run id, a model, or anything a judge could
    map back to an arm. `task_prompt` and `reference_diff` are taken from
    `first` because the pairing rule (§4.2.3) is sample *i* against sample *i*
    ON THE SAME TASK, so the two agree by construction.
    """
    return {
        "kind": "pairwise",
        "task_prompt": first.task_prompt,
        "reference_diff": first.reference_diff,
        "submission_first": _submission(first),
        "submission_second": _submission(second),
    }


def _submission(inputs: PayloadInputs) -> dict[str, Any]:
    """One anonymous side of a pairwise. `diff`, not `candidate_diff`: the two
    sides are peers here, and neither is the reference."""
    return {
        "diff": inputs.candidate_diff,
        "checks": _checks(inputs),
        "similarity": inputs.similarity.to_dict(),
    }


def _checks(inputs: PayloadInputs) -> list[dict[str, str]]:
    """Fresh dicts, so a caller mutating the payload cannot reach back into
    `PayloadInputs` -- which is shared between the two payload kinds and
    between the three votes over one comparison."""
    return [dict(check) for check in inputs.checks]
