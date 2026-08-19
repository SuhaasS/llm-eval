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

That split is the whole security argument, so it is a TEST and not a comment:
`test_only_payload_inputs_from_touches_a_run_record_or_a_grade_record` parses
this file and fails if any other function reads an attribute off a record.
The second half below -- prompts, parsing and vote handling -- is exactly the
code that rule was written for: a helper there that reached for `record.model`
to label a vote would satisfy every leak test in the file, because those
exercise the two builders as written and not the rule the builders follow.
Everything below takes a `PayloadInputs`, a payload dict or a string.

Both builders return plain JSON-ready dicts. `judge_schema.write_payload`
gzips and hashes exactly what is returned, so the payload the record proves is
the payload that was sent -- a builder that returned dataclasses would let
serialization differ between the sent copy and the stored one.

**The second half: prompts, parsing, votes.** Three facts shape it.

*The seam is `CompleteFn`, a stateless `str -> str`.* Not a client object and
not a wrapper around "judge this pair", because the Invariants section
requires three votes to be three independent calls with no shared context, and
a raw-completion seam makes shared context structurally impossible rather than
merely discouraged. `live_completion` at the bottom of this file is the one
implementation that opens a socket, and it opens none until it is CALLED --
everything else here is pure, and every test above the seam drives a fake.

*Two vocabularies, deliberately kept apart.* The pairwise prompt shows
"Submission A" and "Submission B" in the order the payload carries, and the
model answers `A`/`B`/`TIE` -- that is the WIRE vocabulary and it means
presentation order. `JudgeRecord.verdict` is `"a"`/`"b"`/`"tie"` and means the
caller's own pair, which is a different claim entirely. `parse_pairwise_
response` therefore returns `"first"`/`"second"`/`"tie"` and never `"a"`/`"b"`:
only `judge_pair_vote` knows the position draw, so only it can map back. A
parser that returned `"a"` would be guessing, and it would be right half the
time -- which is the shape of bug that yields a complete, confident, inverted
Elo table.

*Parsing is strict and a malformed verdict is a retry, never a repair turn.*
The retry re-sends the identical rendered prompt as a fresh independent call.
Handing the model back its own bad output makes attempt two a correction of
attempt one, which is the same defect as asking one call for three opinions.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from bakeoff.grade_schema import GradeRecord
from bakeoff.schema import RunRecord
from bakeoff.similarity import SimilarityContext, similarity_context
from bakeoff.tasks import TaskManifest

#: PINNED, never an alias. `luna`/`sol`/`terra` are three models rather than
#: three names for one, and `judge_model_id` is what makes a verdict
#: reproducible -- a floating alias is a moving oracle in exactly the way an
#: unpinned image tag is. GPT-class, which sits outside all four compared
#: families (Anthropic, Google, NVIDIA, Moonshot) as the spec's "Model
#: configuration" section requires: a Claude judge inflates Sonnet 5.
JUDGE_MODEL_ID_DEFAULT = "openai.gpt-5.6-sol"

#: Moves on ANY change to the prompt text, including one that reads as
#: cosmetic. Whitespace and ordering move model output, so a prompt edit under
#: an unchanged version silently mixes two generations of verdict in one file.
JUDGE_PROMPT_VERSION = 1

#: Moves when the dimensions, the flags or their anchors change -- a score of
#: 1 does not mean the same thing across a rubric edit, so the two generations
#: must not be averaged.
RUBRIC_VERSION = "1.0.0"

#: As sent. The cap is spelled `max_completion_tokens` by hand: the candidate
#: arms reach that name through a litellm patch that renames `max_tokens`, and
#: `litellm_patches` is deliberately never imported by an in-process caller
#: (it patches litellm process-wide on import). A judge call that shipped the
#: old name would go out silently uncapped.
JUDGE_SAMPLING: dict[str, Any] = {
    "temperature": 0.0,
    "max_completion_tokens": 4096,
}

#: The five orthogonal dimensions, in the order they are asked and stored.
#: Three-point anchored scale on purpose -- 1-10 scales show poor inter-rater
#: agreement, and this profile is reported per model rather than summed into a
#: rank.
RUBRIC_DIMENSIONS: tuple[str, ...] = (
    "functional_equivalence",
    "completeness",
    "cross_file_consistency",
    "scope_discipline",
    "convention_adherence",
)

#: Unscored binary flags. Diagnostic, never a score.
RUBRIC_FLAGS: tuple[str, ...] = (
    "introduced_stub",
    "left_debug_artifacts",
    "wrote_tests",
)

#: THE seam: rendered prompt in, raw model text out. See the module docstring
#: -- it is stateless so that three votes cannot share context even by
#: accident, and it is what Task 5's live client and every test fake satisfy.
CompleteFn = Callable[[str], str]

_Parsed = TypeVar("_Parsed")


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
    map back to an arm.

    `task_prompt` and `reference_diff` are taken from `first`, and the two
    sides are CHECKED to agree rather than trusted to. The pairing rule
    (§4.2.3) is sample *i* against sample *i* ON THE SAME TASK, which makes
    this the class of invariant that deserves an assert instead of a sentence
    in a docstring: taking `first`'s and discarding `second`'s silently means a
    mispaired call renders a payload showing one task's prompt above another
    task's submission, and the judge answers it. The verdict comes back well
    formed and confident, the stored payload looks exactly like a legitimate
    one, and nothing in the `JudgeRecord` can reveal it.

    Compared as strings rather than by task id, which is strictly stronger --
    and is why `PayloadInputs` needs no id. It also catches two runs on the
    same task whose MANIFESTS differ: a re-harvested task, or an edit to
    `task.yaml` mid-collection. There the id matches, the anchors do not, and
    the two submissions were graded against different references, so the
    comparison is not like-for-like even though the pairing was.

    A validity guard, not a leak guard, and the only thing about the pair a
    builder can check at all without seeing a record.
    """
    if (first.task_prompt, first.reference_diff) != (
        second.task_prompt,
        second.reference_diff,
    ):
        raise ValueError(
            "pairwise submissions must come from the same task and the same "
            "manifest: the prompt or the reference anchor differs between the "
            "two sides, so this pair is not a like-for-like comparison"
        )

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


# --- prompts -----------------------------------------------------------------

_RUBRIC_INSTRUCTIONS = """\
You are scoring one submitted code change against a reference change that a
human wrote, reviewed and shipped for the same task. The submission is
anonymous: nothing below says who or what produced it, and there is nothing
to infer.

Score whether the submission ACCOMPLISHES what the reference accomplished.
Different code that reaches the same result scores full marks; code that
resembles the reference but reaches less does not.

Two of the sections below are inputs to your judgement and not the judgement
itself. The deterministic check results are facts already established by
running the suite -- do not re-litigate them, and a passing gate is not by
itself a full score. The overlap figures are three independent facts and
deliberately not a similarity score: a diff that is bigger or smaller than the
reference is not by that fact better or worse.
"""

_PAIRWISE_INSTRUCTIONS = """\
You are comparing two submitted code changes for the same task against a
reference change that a human wrote, reviewed and shipped. Both submissions
are anonymous, nothing below identifies either one, and they are shown in a
RANDOM order that carries no information -- do not prefer a submission for
appearing first or second.

Decide which submission better accomplishes what the reference accomplished.
Judge the result, explicitly NOT "is the same code": a different design that
reaches the same behaviour is not worse for being different. Weigh functional
equivalence, completeness, cross-file consistency, scope discipline and
convention adherence, and say which submission comes out ahead overall.

TIE is a real answer, not a way of declining the question. Use it when the two
are genuinely equivalent, not when one is slightly ahead.

Two of the sections below are inputs to your judgement and not the judgement
itself. The deterministic check results are facts already established by
running the suite -- do not re-litigate them, and a passing gate is not by
itself a win. The overlap figures are three independent facts and deliberately
not a similarity score: a diff that is bigger or smaller than the reference is
not by that fact better or worse.
"""

_RUBRIC_DIMENSIONS_BLOCK = """\
## Dimensions

Score each on 0, 1 or 2 -- 0 fails, 1 partial, 2 meets. The scale is short on
purpose. Do not invent half points, and do not sum or average the five.

1. functional_equivalence
   0 -- does not accomplish what the reference accomplished.
   1 -- accomplishes part of it, or only on some inputs or code paths.
   2 -- accomplishes what the reference accomplished.
   Judge the RESULT. This is explicitly NOT "is the same code": a different
   design, different names and a different structure that reach the same
   behaviour score 2.

2. completeness
   0 -- most of the task is unaddressed, or the change is a stub.
   1 -- the main part is addressed, with TODOs or unhandled cases left.
   2 -- every part of the task is addressed, with no stubs and no TODOs.

3. cross_file_consistency
   0 -- callers, signatures or imports are left inconsistent with the change.
   1 -- mostly consistent, with a call site or a signature missed.
   2 -- every caller updated and every signature aligned.

4. scope_discipline
   0 -- substantial unrelated edits or a gratuitous refactor rides along.
   1 -- mostly on target, with incidental churn.
   2 -- only what the task required.

5. convention_adherence
   0 -- ignores the surrounding idiom, naming and error handling.
   1 -- broadly follows them, with local departures.
   2 -- matches the surrounding idiom, naming and error handling.
"""

_RUBRIC_FLAGS_BLOCK = """\
## Flags

Unscored and binary. They are diagnostic and are never folded into a score, so
answer each on what the diff shows rather than on how the submission read
overall.

- introduced_stub -- a placeholder stands in for real work: an empty body, a
  bare `pass`, a `NotImplementedError`, a TODO where the logic belongs.
- left_debug_artifacts -- prints, commented-out code, stray logging or scratch
  files left behind.
- wrote_tests -- the submission adds or changes tests of its own.
"""

_RUBRIC_RESPONSE_FORMAT = """\
## Reply format

Reply with ONE JSON object and nothing else. Every dimension and every flag
must be present. Scores are the integers 0, 1 or 2 -- never true, false or a
decimal. Flags are JSON true or false -- never 0 or 1.

{
  "dimension_scores": {
    "functional_equivalence": <0, 1 or 2>,
    "completeness": <0, 1 or 2>,
    "cross_file_consistency": <0, 1 or 2>,
    "scope_discipline": <0, 1 or 2>,
    "convention_adherence": <0, 1 or 2>
  },
  "flags": {
    "introduced_stub": <true or false>,
    "left_debug_artifacts": <true or false>,
    "wrote_tests": <true or false>
  },
  "reasoning": "<name the specific lines that drove each score>"
}

The angle brackets mark slots to fill and must not appear in your reply. This
is the shape of an answer, not an answer: no score above is a default.
"""

_PAIRWISE_RESPONSE_FORMAT = """\
## Reply format

Reply with ONE JSON object and nothing else. `verdict` is exactly "A", "B" or
"TIE"; `reasoning` names the specific differences that decided it.

{"verdict": "<A, B or TIE>", "reasoning": "<what decided it>"}

The angle brackets mark slots to fill and must not appear in your reply. This
is the shape of an answer, not an answer: "A" is not a default.
"""


def render_rubric_prompt(payload: dict[str, Any]) -> str:
    """The absolute-rubric prompt, interpolated verbatim from the payload.

    Verbatim is the contract. A renderer that summarized, truncated or
    re-wrapped a diff would change what was judged while the stored payload
    went on showing the whole thing, and `judge_prompt_sha` would attest to
    text nobody kept -- so the record would look complete and prove nothing.

    Takes the payload rather than `PayloadInputs` so that the bytes hashed
    into `input_payload_sha` and the bytes rendered into the prompt come from
    one object. Building the two from separate sources is how they drift.
    """
    return "\n".join(
        [
            _RUBRIC_INSTRUCTIONS,
            _section("Task given to the author of the submission",
                     payload["task_prompt"]),
            _section("Reference change (human-authored, reviewed, shipped)",
                     payload["reference_diff"]),
            _section("Submitted change", payload["candidate_diff"]),
            _section("Deterministic check results",
                     _render_checks(payload["checks"])),
            _section("Overlap with the reference (context, not a score)",
                     _render_similarity(payload["similarity"])),
            _RUBRIC_DIMENSIONS_BLOCK,
            _RUBRIC_FLAGS_BLOCK,
            _RUBRIC_RESPONSE_FORMAT,
        ]
    )


def render_pairwise_prompt(payload: dict[str, Any]) -> str:
    """The blind pairwise prompt. `submission_first` is shown as "Submission A".

    The labels are the WIRE vocabulary and mean presentation order only. The
    payload arrives already in shown order (`build_pairwise_payload` applies
    the caller's position draw), so this function does no ordering of its own
    -- a renderer that re-ordered would put the assignment somewhere
    `JudgeRecord.position_assignment` could not observe it, and a stored
    assignment that disagrees with what was sent makes the position-swap probe
    report a bias figure it did not measure.
    """
    return "\n".join(
        [
            _PAIRWISE_INSTRUCTIONS,
            _section("Task given to the authors of both submissions",
                     payload["task_prompt"]),
            _section("Reference change (human-authored, reviewed, shipped)",
                     payload["reference_diff"]),
            _submission_section("Submission A", payload["submission_first"]),
            _submission_section("Submission B", payload["submission_second"]),
            _PAIRWISE_RESPONSE_FORMAT,
        ]
    )


def prompt_sha(rendered: str) -> str:
    """sha256 of the EXACT rendered text, task text and all.

    Per call rather than per comparison: position changes the text, so three
    votes over one pair legitimately carry up to two distinct shas. This is
    the evidence that the declared `judge_prompt_version` was honest -- a
    prompt edited without a version bump shows up as a sha nothing else in the
    file shares.
    """
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _section(heading: str, body: str, level: int = 2) -> str:
    return f"{'#' * level} {heading}\n\n{body}\n"


def _submission_section(label: str, submission: dict[str, Any]) -> str:
    """One anonymous side of a pairwise, under its wire label."""
    return "\n".join(
        [
            f"## {label}\n",
            _section("Change", submission["diff"], level=3),
            _section("Deterministic check results",
                     _render_checks(submission["checks"]), level=3),
            _section("Overlap with the reference (context, not a score)",
                     _render_similarity(submission["similarity"]), level=3),
        ]
    )


def _render_checks(checks: list[dict[str, str]]) -> str:
    """Name and status, which is the whole of what the judge may see about the
    deterministic gate. `not_configured` is rendered as itself rather than
    dropped: a check that did not run is not a check that passed."""
    if not checks:
        return "(no deterministic checks ran)"
    return "\n".join(
        f"- {check['name']}: {check['status']}" for check in checks
    )


def _render_similarity(similarity: dict[str, Any]) -> str:
    """The three facts of the similarity context, as three facts.

    No total and no derived score, matching `SimilarityContext` itself -- the
    failure this design guards against is someone summing them into a number,
    and a prompt that presented them as one line of "similarity: 0.8" would do
    exactly that in the one place no test can see.

    A `None` ratio is rendered as words. `inf` or `0.0` both read to a model
    as a measurement, and one of them reads as "the submission changed
    nothing", which is the opposite of what `None` means here.
    """
    files = similarity["file_overlap"]
    symbols = similarity["symbol_overlap"]
    ratio = similarity["diff_size_ratio"]
    ratio_text = (
        "not computable (the reference changes no lines)"
        if ratio is None
        else f"{ratio:.2f}"
    )
    return "\n".join(
        [
            f"- files changed by both: {_names(files['common'])}",
            f"- files only the submission changed: "
            f"{_names(files['candidate_only'])}",
            f"- files only the reference changed: "
            f"{_names(files['reference_only'])}",
            f"- symbols touched by both: {_names(symbols['common'])}",
            f"- symbols only the submission touched: "
            f"{_names(symbols['candidate_only'])}",
            f"- symbols only the reference touched: "
            f"{_names(symbols['reference_only'])}",
            f"- changed lines, submission / reference: {ratio_text}",
        ]
    )


def _names(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "(none)"


# --- parsing -----------------------------------------------------------------


class MalformedVerdict(RuntimeError):
    """A model reply that cannot be read as a verdict.

    Its own class because the vote protocol treats it as retryable while every
    other exception is a real failure: a transport error retried as a
    malformed verdict would burn the malformed-verdict budget on a problem
    more calls cannot fix, and a malformed verdict raised as a generic error
    would abandon a comparison one re-ask would have settled.
    """


@dataclass(frozen=True)
class RubricResult:
    """One absolute-rubric profile: five scores, three flags, the reasoning.

    A profile and not a total. There is no `score` property and no ordering,
    for the same reason `SimilarityContext` has none -- the rubric is
    diagnostic, reported per model, and the failure to design against is
    someone summing the five into a rank that then competes with the pairwise
    Elo the eval actually ranks on.
    """

    dimension_scores: dict[str, int]
    flags: dict[str, bool]
    reasoning: str


@dataclass(frozen=True)
class VoteOutcome:
    """One pairwise vote, with everything the `JudgeRecord` needs to prove it.

    `payload` and `rendered_prompt` are the objects that were actually sent,
    carried out rather than rebuilt: Task 6 stores `write_payload(payload)`
    and `prompt_sha(rendered_prompt)`, and a caller that reconstructed either
    from the inputs would attest to something other than what the model saw --
    which is precisely what the stored sha exists to rule out.
    """

    #: "a_first" | "b_first" -- the draw this vote was shown under.
    position_assignment: str
    #: "a" | "b" | "tie", canonical and already mapped back through the draw.
    verdict: str
    #: SHOWN order, as sent.
    payload: dict[str, Any]
    rendered_prompt: str
    reasoning: str


#: Wire vocabulary to shown-order vocabulary. Case-folded before lookup.
_SHOWN_BY_WIRE: dict[str, str] = {"A": "first", "B": "second", "TIE": "tie"}

#: (position assignment, shown-order verdict) -> canonical verdict. Written
#: out as six entries rather than computed, because the inversion is the one
#: piece of arithmetic in this module that fails silently: a vote protocol
#: that randomizes position and forgets to invert records the loser as the
#: winner on half the comparisons, and every Elo number downstream is computed
#: from it with nothing looking wrong.
_CANONICAL_VERDICT: dict[tuple[str, str], str] = {
    ("a_first", "first"): "a",
    ("a_first", "second"): "b",
    ("a_first", "tie"): "tie",
    ("b_first", "first"): "b",
    ("b_first", "second"): "a",
    ("b_first", "tie"): "tie",
}

#: What `JudgeRecord.verdict` accepts from a model call. `gate_decided` is not
#: here: it is the driver's own answer for a pair no judge was asked about.
_CANONICAL_VERDICTS: tuple[str, ...] = ("a", "b", "tie")


def parse_rubric_response(text: str) -> RubricResult:
    """Strict domain validation over the first JSON object in the reply.

    Every rule here is a specific way a well-formed-looking answer is wrong:

    * **Booleans are rejected explicitly.** `isinstance(True, int)` is True and
      `True == 1`, so an `isinstance` check records a model that answered
      `true` for a dimension as an honest partial score, and the stored
      profile is indistinguishable from one. `type(v) is int` is the check.
    * **The dimension and flag key sets are closed.** A renamed or missing
      dimension is a malformed verdict, not a default: filling a gap with 0
      publishes an accusation the model never made, and tolerating an unknown
      name lets a rubric edit half-land.
    * **Reasoning must be non-empty.** `full_reasoning_text` is the only thing
      that makes a verdict auditable by a human and is what a disputed kappa
      is re-examined against.

    Unknown TOP-LEVEL keys are tolerated -- models editorialize, and spending
    three calls to refuse a stray `confidence` field buys nothing.
    """
    parsed = _json_object(text)

    scores = _closed_block(parsed, "dimension_scores", RUBRIC_DIMENSIONS)
    for name, value in scores.items():
        if type(value) is not int or value not in (0, 1, 2):
            raise MalformedVerdict(
                f"dimension {name!r} is {value!r}, not the integer 0, 1 or 2"
            )

    flags = _closed_block(parsed, "flags", RUBRIC_FLAGS)
    for name, value in flags.items():
        if type(value) is not bool:
            raise MalformedVerdict(
                f"flag {name!r} is {value!r}, not JSON true or false"
            )

    return RubricResult(
        dimension_scores={name: scores[name] for name in RUBRIC_DIMENSIONS},
        flags={name: flags[name] for name in RUBRIC_FLAGS},
        reasoning=_reasoning(parsed),
    )


def parse_pairwise_response(text: str) -> str:
    """The verdict in SHOWN-ORDER terms: "first", "second" or "tie".

    Never "a"/"b". The reply's `A`/`B` is presentation order, and canonical
    a/b is the caller's own pair -- a different claim, and one this function
    has no way to evaluate because it never sees the position draw. Returning
    "a" here would be a guess that is right half the time, which is how a
    complete and confident Elo table ends up inverted. `judge_pair_vote` owns
    the mapping because it owns the draw.

    Case-insensitive and whitespace-tolerant on the verdict itself; anything
    outside A/B/TIE is malformed rather than coerced.
    """
    return _pairwise_verdict_and_reasoning(text)[0]


def _pairwise_verdict_and_reasoning(text: str) -> tuple[str, str]:
    """Both halves in one parse, so `judge_pair_vote` need not parse twice.

    `parse_pairwise_response` is the published single-value signature and
    delegates here; two independent parses of one reply is two chances for
    them to disagree about whether it was well formed.
    """
    parsed = _json_object(text)

    verdict = parsed.get("verdict")
    if not isinstance(verdict, str):
        raise MalformedVerdict(f"'verdict' is {verdict!r}, not a string")

    shown = _SHOWN_BY_WIRE.get(verdict.strip().upper())
    if shown is None:
        raise MalformedVerdict(
            f"'verdict' is {verdict!r}, not one of 'A', 'B' or 'TIE'"
        )

    return shown, _reasoning(parsed)


def _json_object(text: str) -> dict[str, Any]:
    """The first balanced `{...}` in the reply, parsed.

    Code fences need no special handling: the scan starts at the first `{`,
    so ```` ```json ```` above it and prose below it fall outside the slice
    either way. Stripping fences first would be a second rule doing the first
    rule's job, with its own way to be wrong.
    """
    raw = _first_balanced_object(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedVerdict(
            f"the reply's first JSON object does not parse: {exc}"
        ) from exc


def _first_balanced_object(text: str) -> str:
    """Brace-balanced and string-aware, not `find('{')` to `rfind('}')`.

    Reasoning about a diff quotes code, and code holds braces. A naive
    extractor truncates the object at the first `}` inside the prose, reports
    a malformed verdict for a perfectly good answer, and then spends the whole
    retry budget re-asking a model that was right the first time.

    String tracking starts only after the opening brace, so an unbalanced
    quote in the prose above the object -- `my "verdict:` -- cannot swallow
    the brace that opens it.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if depth == 0:
            if char == "{":
                start, depth = index, 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise MalformedVerdict(
        f"the reply carries no complete JSON object: {text[:200]!r}"
    )


def _closed_block(
    parsed: dict[str, Any], key: str, names: tuple[str, ...]
) -> dict[str, Any]:
    """Exactly `names`, no more and no fewer. Names both sides of the mismatch
    in the message, because "malformed" alone leaves an operator diffing a
    prompt against a constant tuple by eye."""
    block = parsed.get(key)
    if not isinstance(block, dict):
        raise MalformedVerdict(f"{key!r} is missing or is not a JSON object")

    if set(block) != set(names):
        missing = sorted(set(names) - set(block))
        unknown = sorted(set(block) - set(names))
        raise MalformedVerdict(
            f"{key!r} must carry exactly {list(names)}: "
            f"missing {missing}, unknown {unknown}"
        )
    return block


def _reasoning(parsed: dict[str, Any]) -> str:
    reasoning = parsed.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise MalformedVerdict(
            f"'reasoning' is {reasoning!r}: a verdict nobody can audit is "
            "not a verdict"
        )
    return reasoning


# --- the vote protocol -------------------------------------------------------


def judge_rubric(
    inputs: PayloadInputs, complete: CompleteFn, retries: int = 2
) -> tuple[dict[str, Any], str, RubricResult]:
    """One absolute-rubric call. Returns `(payload, rendered prompt, result)`.

    All three come back because all three are evidence: the caller stores the
    payload gzipped, the prompt's sha, and the profile, and any of the three
    rebuilt afterwards from the inputs could differ from what was sent.
    """
    payload = build_rubric_payload(inputs)
    rendered = render_rubric_prompt(payload)
    result = _ask_and_parse(
        rendered, complete, parse_rubric_response, retries
    )
    return payload, rendered, result


def judge_pair_vote(
    a: PayloadInputs,
    b: PayloadInputs,
    rng: random.Random,
    complete: CompleteFn,
    retries: int = 2,
) -> VoteOutcome:
    """One blind, position-randomized pairwise vote.

    The order of operations is the content, and it is not interchangeable:

    1. Draw the position BEFORE building anything. Building an `a_first`
       payload and swapping it afterwards produces the same bytes today and
       is the shape that lets `position_assignment` and the payload disagree
       tomorrow -- at which point the position-swap probe reports a bias
       figure it did not measure.
    2. Build in shown order, so the payload that is stored is the payload that
       was seen.
    3. Render, and hand the rendered text back on the outcome. The caller
       shas it with `prompt_sha` -- per CALL rather than per comparison,
       since position changes the text -- and it is carried out rather than
       rebuilt so the sha attests to what the model actually saw.
    4. Complete and parse. A `MalformedVerdict` re-sends the IDENTICAL prompt
       as a fresh independent call; the model is never shown its own bad
       output, because a repair turn makes attempt two a correction of
       attempt one rather than a new opinion.
    5. Map the shown-order verdict back through the draw. This is the step
       whose failure is silent and total (see `_CANONICAL_VERDICT`).

    `rng` is passed in rather than drawn from module state so a driver can
    seed one generator for a whole batch and replay it. Three votes over one
    comparison are three calls with three independent draws -- a single call
    asked for three opinions is one vote wearing three hats.
    """
    position_assignment = "a_first" if rng.random() < 0.5 else "b_first"
    first, second = (a, b) if position_assignment == "a_first" else (b, a)

    payload = build_pairwise_payload(first, second)
    rendered = render_pairwise_prompt(payload)
    shown, reasoning = _ask_and_parse(
        rendered, complete, _pairwise_verdict_and_reasoning, retries
    )

    return VoteOutcome(
        position_assignment=position_assignment,
        verdict=_CANONICAL_VERDICT[(position_assignment, shown)],
        payload=payload,
        rendered_prompt=rendered,
        reasoning=reasoning,
    )


def majority(verdicts: Sequence[str]) -> str:
    """Strict majority over canonical verdicts; anything short of one is a tie.

    Strict, not plurality: 2 of 4 is not a majority, and calling it one would
    let a split decision be published as a win. Ties are permitted at the vote
    level and at this level both -- the spec asks for that explicitly, and a
    tie-breaking rule invented here would manufacture a preference the three
    votes did not express.

    Refuses "first"/"second" loudly. They are presentation terms and mean
    nothing without the position draw, so a caller that passed raw parser
    output would otherwise get a confident answer in a vocabulary no
    `JudgeRecord.verdict` accepts -- stored as a verdict rather than raised as
    a bug. Refuses an empty sequence for the same reason: no votes is not a
    tie, it is a driver that called this with nothing.
    """
    if not verdicts:
        raise ValueError(
            "majority over zero votes is not a tie: nothing was voted on"
        )

    unknown = sorted(set(verdicts) - set(_CANONICAL_VERDICTS))
    if unknown:
        raise ValueError(
            f"majority takes canonical verdicts {list(_CANONICAL_VERDICTS)}, "
            f"got {unknown} -- 'first'/'second' are presentation terms and "
            "must be mapped back through the position assignment first"
        )

    winner, count = Counter(verdicts).most_common(1)[0]
    return winner if count * 2 > len(verdicts) else "tie"


def _ask_and_parse(
    rendered: str,
    complete: CompleteFn,
    parse: Callable[[str], _Parsed],
    retries: int,
) -> _Parsed:
    """Complete, parse, and on a malformed verdict re-ask the SAME prompt.

    `retries` counts re-asks, so `retries=2` is three calls in total. It is a
    separate counter from the transport retries Task 5's router carries, and
    conflating them hides which of the two a batch is burning: a model that
    cannot produce JSON and a model that cannot be reached fail the same
    number of times and need opposite responses.

    Every attempt re-sends `rendered` unchanged -- see `judge_pair_vote` step
    4 for why a repair turn is a different (and worse) thing.
    """
    if retries < 0:
        raise ValueError(f"retries must be >= 0, got {retries}")

    last: MalformedVerdict | None = None
    for _ in range(retries + 1):
        try:
            return parse(complete(rendered))
        except MalformedVerdict as exc:
            last = exc

    raise MalformedVerdict(
        f"no parsable verdict after {retries + 1} attempts on the same "
        f"prompt; last failure: {last}"
    ) from last


# --- the live completion seam ------------------------------------------------

#: The mantle chat-completions route, us-east-1 -- the same base
#: `config/litellm_config.yaml` gives nemotron and kimi. NOT gemma's
#: `/openai/v1`: the path differs per model family on this endpoint and the two
#: are not interchangeable, so a judge pointed at the wrong one 404s.
#:
#: The region is baked into the host and is therefore coupled to
#: `live_completion`'s `region` argument, which is what the token is minted
#: FOR. A bearer token is region-scoped, so moving one without the other mints
#: a credential this host rejects. Both move together or neither does.
MANTLE_BASE = "https://bedrock-mantle.us-east-1.api.aws/v1"


def live_completion(
    judge_model_id: str = JUDGE_MODEL_ID_DEFAULT,
    region: str = "us-east-1",
) -> CompleteFn:
    """The live `CompleteFn`: one rendered prompt in, the model's raw text out.

    The router is built ONCE, LAZILY, inside the closure -- the `task_resolver`
    pattern (`scripts/grade.py:317`). Two reasons, and the second is the one
    that bites: a batch whose comparisons are all decided by the deterministic
    ladder must not mint a credential for a judge it never asks, and minting at
    construction starts the credential clock before the first call. The real
    window is about an hour (the Identity Center session policy caps it well
    below `MANTLE_TOKEN_TTL`), so a long grading pass that built its judge up
    front would reach the first vote holding a dead token.

    Stateless per call, which is the `CompleteFn` contract: three votes over
    one comparison are three independent calls carrying no shared context. The
    router is shared; the conversation is not, because there is no
    conversation -- each call is one user turn holding the whole rendered
    prompt.

    Auth failures propagate. Retrying past one is how a batch is spent against
    a token that expired mid-run, and the error an operator needs to see says
    `ExpiredToken`, not "no parsable verdict after 3 attempts".

    Building the router also leaves the process with the AWS-named bearer
    variable unset, which is credential hygiene rather than a side effect
    nobody asked for -- see `_judge_router`.
    """
    # A one-slot cache rather than `nonlocal`: the closure only ever reads and
    # fills it, so there is no rebind to get wrong.
    built: dict[str, Any] = {}

    def complete(prompt: str) -> str:
        if "router" not in built:
            built["router"] = _judge_router(judge_model_id, region)

        response = built["router"].completion(
            model=judge_model_id,
            messages=[{"role": "user", "content": prompt}],
            # As sent, hand-spelled. `bakeoff.litellm_patches` is what renames
            # `max_tokens` -> `max_completion_tokens` for the candidate arms,
            # and it applies process-wide ON IMPORT -- so it is deliberately
            # never imported by an in-process caller and this router is
            # unpatched by design. `smoke_bedrock.live` reproduces the same
            # rename by hand (line 379) for exactly this reason.
            **JUDGE_SAMPLING,
        )
        # `content` is None on an empty completion. Returned as "" rather than
        # passed on, so the strict parser reports a malformed verdict and
        # re-asks -- which is the right response to an empty reply, and is not
        # what an AttributeError three frames deeper would produce.
        return response.choices[0].message.content or ""

    return complete


def _judge_router(judge_model_id: str, region: str) -> Any:
    """One single-deployment LiteLLM Router for the judge, credential and all.

    Annotated `Any` rather than `Router`: naming the type would mean importing
    litellm at module scope, and this module is imported by the payload
    builders and the leak tests, none of which should pay for it.

    `openai/` twice over is not a typo: the prefix is LiteLLM's provider (the
    OpenAI-compatible chat-completions handler) and `openai.` is Bedrock's
    vendor namespace inside the model id, exactly as `openai/google.gemma-4-31b`
    reads in `config/litellm_config.yaml`.

    The token is passed as this deployment's `api_key` -- an in-memory value,
    never an environment variable and never a file. That is what keeps it off
    `AWS_BEARER_TOKEN_BEDROCK`, the name LiteLLM's bedrock/ handler falls back
    to when a deployment has no api_key: a process that exported it there would
    bearer-authenticate every SigV4 arm and fail them all with
    `bedrock:CallWithBearerToken`, while the judge itself stayed green
    (`smoke_bedrock.py:125` scrubs it for the same reason).

    `num_retries=2` is TRANSPORT retries -- a connection reset or a 5xx. It is
    a separate counter from `_ask_and_parse`'s malformed-verdict retries, and
    conflating the two hides which one a batch is burning: a model that cannot
    produce JSON and a model that cannot be reached fail the same number of
    times and need opposite responses.

    `disable_cooldowns=True` for the reason `config/litellm_config.yaml`'s
    router_settings gives, measured against litellm 1.95.0: one auth failure
    cools a deployment down, and this group is single-deployment so there is
    nothing to fail over to. The retries then return `RouterRateLimitError` --
    a plain ValueError with no status code -- and the operator reads "No
    deployments available" instead of the expired credential that caused it.
    """
    # Imported FIRST, and the ordering is load-bearing rather than tidy:
    # `import litellm` runs `load_dotenv()` (smoke_bedrock's
    # `scrub_placeholders` documents the same trap costing an AWS_PROFILE), so
    # `bakeoff/.env` does not reach `os.environ` until this line has run. A
    # credential read before it misses a token sitting in the file the
    # operator just filled in.
    from litellm import Router

    from scripts.smoke_bedrock import LITELLM_BEARER_ENV, normalize_mantle_token

    # A token the operator put under the AWS name is a WORKING credential, and
    # the scrub below is about to remove it. Adopt it onto the name the
    # harness reads first, so the sequence relocates a credential rather than
    # destroying one -- `smoke_bedrock.main` runs these two in the same order.
    normalize_mantle_token()

    router = Router(
        model_list=[
            {
                "model_name": judge_model_id,
                "litellm_params": {
                    "model": f"openai/{judge_model_id}",
                    "api_base": MANTLE_BASE,
                    "api_key": _mantle_token(region),
                },
            }
        ],
        num_retries=2,
        disable_cooldowns=True,
    )

    # The variable is scrubbed rather than merely left unset, because THIS
    # PROCESS DID NOT SET IT -- `load_dotenv` did, three lines up, out of a
    # `.env` written for the live smoke. Nothing in the harness may hold it:
    # LiteLLM's bedrock/ handler falls back to it for any deployment with no
    # api_key, so a bedrock-runtime deployment built later in this process
    # bearer-authenticates and fails with `bedrock:CallWithBearerToken` while
    # the judge, which passes its key literally, stays green. The judge's own
    # call does not depend on the ordering; `smoke_bedrock.py:243` scrubs
    # after construction for a config whose deployments do, and keeping the
    # two sequences identical is what lets one comment explain both.
    os.environ.pop(LITELLM_BEARER_ENV, None)
    return router


def _mantle_token(region: str) -> str:
    """The mantle bearer token: the environment's, else one minted in memory.

    Imported from `scripts.smoke_bedrock` rather than reimplemented, so the
    harness has ONE derivation with one set of failure messages
    (`proxy.py:427` reaches for the same functions). The token is held in
    memory for the life of the router and is never written to disk: a
    long-lived copy in `.env` would outlive the run that needed it and sit
    there with no expiry anyone tracks.

    Two sources and no third. The caller has already run
    `normalize_mantle_token`, so a token supplied under the AWS name is
    findable here under `MANTLE_ENV` -- that is a rename, not a source.

    A missing credential raises here and NAMES the variable. LiteLLM's own
    answer to an absent token is `Invalid API Key format: Must start with
    pre-defined prefix`, which reads as a config error and sends an operator
    into `litellm_config.yaml` looking for a typo that is not there.
    """
    from scripts.smoke_bedrock import MANTLE_ENV, derive_mantle_token

    token = os.environ.get(MANTLE_ENV) or derive_mantle_token(region)
    if not token:
        raise RuntimeError(
            f"no mantle credential for the judge: set {MANTLE_ENV}, or run "
            f"`aws sso login` so a short-term token can be minted for {region}"
        )
    return token
