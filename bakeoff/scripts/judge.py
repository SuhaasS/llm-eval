#!/usr/bin/env python3
"""Judge a stored collection: one line per verdict, beside two files it only
reads. See `docs/superpowers/specs/2026-08-18-judge-design.md`, section
"`scripts/judge.py` -- the batch driver", and spec sections 4.2.3 and 4.3.

The judge RANKS; the deterministic ladder GATES, and the two are never averaged.
So this driver is a sibling of `scripts/grade.py` rather than a stage of it: it
reads the event log and `grades/grades.jsonl`, writes
`<event-log>/judgments/judgments.jsonl` and the gzipped payloads beside it, and
opens neither input for writing. Two derived views, two files, no cross-writes.

THE ORDER IS THE CONTENT
------------------------

Every step below is placed against a specific way a judging pass produces
confident, well-formed, wrong output:

1. **Malformed judgments refuse the resume** unless `--re-judge`. An unreadable
   judgment is not an absent one, and a resume that confused the two would pay a
   second time for a verdict already bought -- then leave two lines for one
   comparison under one `(judge_model_id, judge_prompt_version, rubric_version)`,
   which is the shape the file reserves for a deliberate re-judge.
2. **Any malformed grade line refuses the batch outright**, and no flag cures it
   (plan ambiguity 8). An unreadable gate might BE the gate: a run read as
   ungraded is one the judge either drops from a pair -- silently changing which
   comparisons exist -- or, if the damage sat on the losing side, one it ranks.
3. **The gating view is the LAST line per `run_id` in file order.** A re-grade
   appends rather than replaces, so a run can hold several lines, and the last
   one is the verdict a later reader lands on.
4. **Excluded runs (`resolved is None`) are dropped BEFORE grouping.** Such a
   row is not an observation of the model; ranking it ranks the infrastructure.
   Dropping after grouping would leave it in a cell, where it would either be
   paired or silently change which model won the max-attempt tiebreak.
5. **A run with no grade line is warned about and never judged.** Rescuing an
   ungraded run is out of scope -- with no gate to defer to there is nothing for
   the judge to be blind about.
6. **The rubric gate is asserted immediately before the append**, not at
   selection. The file is append-only, so a rubric line about a run the ladder
   failed is a permanent number somebody averages into a resolve rate. This is
   §4.2.3's central rule and it gets `grade.py`'s `_assert_verdict_invariant`
   treatment: a violation is a DRIVER bug and raises.
7. **`write_payload` runs BEFORE `append_judgment`.** A line pointing at a
   payload that failed to write is a verdict naming an input nobody can read,
   which breaks "a re-judge is a re-score, not a re-run" (§4.3) exactly as a
   missing payload does while looking like a present one. The reverse order
   fails safe: no line, and the unit is retried on the next pass.

THE EXIT CONTRACT
-----------------

Exit 0 means every selected unit produced its lines. Exit 1 means at least one
did not, or the resume was refused. A unit is one rubric call, one pairwise
vote, or one gate-decided pair -- the same granularity the resume is keyed on --
and each is wrapped in a bare `except Exception` so that one unreadable diff,
one model that cannot produce JSON, or one payload that tripped the secret scan
costs its own line and not the rest of the batch. `MalformedVerdict` after
exhausted retries lands here: an error, no line, exit 1.

WHAT IS LAZY, AND WHY IT MATTERS
--------------------------------

The live judge is built on the FIRST call that needs one. A batch whose
comparisons the ladder already settled must not mint a credential for a judge it
never asks -- gate-decided pairs cost nothing, which is the point of them -- and
minting at construction starts a credential clock (~1h in practice) before the
first vote, so a long pass would reach it holding a dead token. `live_completion`
is itself lazy about its router; this closure is lazy about `live_completion`.

Usage:
    python scripts/judge.py --event-log ~/.cache/bakeoff/eventlog
    python scripts/judge.py --event-log PATH --only-task calc-1 --samples 0 1
    python scripts/judge.py --event-log PATH --no-rubric --votes 3

Exit codes:
    0  every selected unit produced its lines
    1  at least one unit errored, or the batch was refused
"""

from __future__ import annotations

import argparse
import itertools
import random
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from bakeoff.eventlog import EventLog  # noqa: E402
from bakeoff.grade_schema import GradeRecord, load_grades  # noqa: E402
from bakeoff.judge import (  # noqa: E402
    JUDGE_MODEL_ID_DEFAULT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SAMPLING,
    RUBRIC_VERSION,
    CompleteFn,
    PayloadInputs,
    judge_pair_vote,
    judge_rubric,
    live_completion,
    payload_inputs_from,
    prompt_sha,
)
from bakeoff.judge_schema import (  # noqa: E402
    JudgeRecord,
    append_judgment,
    load_judgments,
    write_payload,
)
from bakeoff.tasks import TaskError, load_task_set  # noqa: E402

# Imported, never restated. `grade.py` owns where the grade file lives and this
# driver's whole gate comes out of it, so a second copy of the path is how a
# moved grade file turns into a judging pass that reports "nothing is graded"
# and judges nothing -- silent, and in the direction that looks like success.
# The import costs `scripts.grade`'s module graph (which reaches `docker`, an
# import with no side effects and a declared dependency) and buys one
# derivation. Same reasoning as `grade.py` importing `preflight_cache_key`.
from scripts.grade import grades_path  # noqa: E402

DEFAULT_TASK_SET = REPO / "taskset"

#: How many ids a warning names before it stops listing them. A collection is
#: ~800 runs and a warning that prints all of them is one nobody reads.
_MAX_NAMED = 10


class ResumeRefused(RuntimeError):
    """The judgment file or the grade file could not be read completely, so the
    batch cannot be trusted to know what it already has -- or what the gate said.

    Its own class rather than `scripts.grade`'s, on that module's precedent: the
    two drivers refuse for related reasons over different files, and a shared
    class would make a `except ResumeRefused` around one of them catch the
    other's refusal about a file it never touched.
    """


class GateInvariantError(RuntimeError):
    """A rubric `JudgeRecord` was about to be written for a run whose
    `GradeRecord.resolved` is not `True`.

    A violation is a DRIVER bug, and the file is append-only: a line written
    once is permanent. §4.2.3's central rule is that the judge never rescues a
    failed gate, and a rubric profile sitting beside `resolved is False` is
    precisely the number a downstream view folds into a resolve rate. `None` is
    the same refusal for a different reason -- an excluded run is not an
    observation of the model at all.
    """


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def judgments_path(event_log_root: Path | str) -> Path:
    return Path(event_log_root) / "judgments" / "judgments.jsonl"


def payloads_root(event_log_root: Path | str) -> Path:
    """The gzipped payloads, BESIDE the jsonl rather than inside it.

    Payloads carry two full diffs and would dominate the line file. Kept under
    the same `judgments/` directory so the verdicts and the inputs they were
    derived from move as one thing -- copying the jsonl without this directory
    produces a file of verdicts that can no longer be re-scored (§4.3).
    """
    return Path(event_log_root) / "judgments" / "payloads"


# ---------------------------------------------------------------------------
# the gate the judge defers to
# ---------------------------------------------------------------------------


def gating_view(grades: list[GradeRecord]) -> dict[str, GradeRecord]:
    """One `GradeRecord` per `run_id` -- the LAST line, in file order.

    The grade file is append-only and a re-grade appends rather than replaces,
    so a run can hold several lines under several grader versions. The last one
    is the current verdict, on the same reasoning `grade.py`'s resume uses to
    skip: it is the one a later reader of the file would land on.

    Deliberately NOT keyed on `grader_version` as `grade.py`'s audit is. That
    function is answering "how did this collection grade", where two versions
    disagreeing about one run is the finding worth keeping. This one is
    answering "may this run be judged, and against what", which admits exactly
    one answer per run -- and `grade_version_seen` on the judgment records which
    generation gave it.
    """
    view: dict[str, GradeRecord] = {}
    for grade in grades:
        view[grade.run_id] = grade
    return view


def _assert_rubric_gate(grade: GradeRecord, run_id: str) -> None:
    """The last thing between a driver bug and a permanent line. See
    `GateInvariantError`."""
    if grade.resolved is not True:
        raise GateInvariantError(
            f"{run_id}: a rubric judgment was about to be written for a run "
            f"whose grade says resolved={grade.resolved!r}. The judge never "
            "rescues a failed gate and never ranks an excluded run, and this "
            "file is append-only, so the line would be permanent"
        )


# ---------------------------------------------------------------------------
# resume identity
# ---------------------------------------------------------------------------


def _resume_key(judgment: JudgeRecord) -> tuple:
    """What makes two lines the same unit of work.

    TUPLES OF SCALARS, never records: `JudgeRecord` carries dict fields on a
    frozen dataclass and is therefore unhashable, so a set of records would
    raise on the first insert rather than skip anything.

    The three version fields are the whole gate. A verdict is only reproducible
    against the model that gave it, the prompt text it saw and the rubric it was
    scored under, so a change to any of the three makes a NEW generation of
    verdict rather than a resume -- and the disagreement between the two lines
    is the finding the file exists to keep.

    A gate-decided pair carries `vote_index=None` and so resumes as its own
    unit. Keying it on 0 would make it collide with the first vote of a pair
    that later becomes judgeable under a re-grade, and a second pass would write
    it again beside itself.

    An unrecognised `kind` -- a hand-edited line -- gets a key that can match
    nothing, so it can neither be mistaken for a completed unit nor collide with
    one whose fields happen to be `None`.
    """
    versions = (
        judgment.judge_model_id,
        judgment.judge_prompt_version,
        judgment.rubric_version,
    )
    if judgment.kind == "rubric":
        return ("rubric", judgment.run_id) + versions
    if judgment.kind == "pairwise":
        return (
            "pairwise",
            judgment.task_id,
            judgment.sample_index,
            judgment.run_id_a,
            judgment.run_id_b,
            judgment.vote_index,
        ) + versions
    return ("unreadable-kind", judgment.judgment_id, judgment.kind)


def _rubric_key(run_id: str, judge_model_id: str) -> tuple:
    return ("rubric", run_id, judge_model_id, JUDGE_PROMPT_VERSION,
            RUBRIC_VERSION)


def _pairwise_key(task_id: str, sample_index: int, run_id_a: str,
                  run_id_b: str, vote_index: int | None,
                  judge_model_id: str) -> tuple:
    return ("pairwise", task_id, sample_index, run_id_a, run_id_b, vote_index,
            judge_model_id, JUDGE_PROMPT_VERSION, RUBRIC_VERSION)


# ---------------------------------------------------------------------------
# the seams
# ---------------------------------------------------------------------------


def lazy_live_completion(judge_model_id: str) -> CompleteFn:
    """`live_completion`, built on the first prompt that needs it.

    The `task_resolver` pattern (`scripts/grade.py:317`) one layer further out.
    `live_completion` already defers its ROUTER; this defers `live_completion`
    itself, which is what a fully gate-decided batch needs: that function
    resolves `scripts.smoke_bedrock` at construction, and a batch that makes no
    call should not require the credential module to be importable at all.

    A one-slot dict rather than `nonlocal`, so there is no rebind to get wrong.
    Not thread-safe (check-then-set), which is fine while votes are sequential
    and is the same shape `live_completion`'s own cache has.
    """
    built: dict[str, CompleteFn] = {}

    def complete(prompt: str) -> str:
        if "fn" not in built:
            built["fn"] = live_completion(judge_model_id)
        return built["fn"](prompt)

    return complete


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# the three kinds of line
# ---------------------------------------------------------------------------


def _rubric_line(record, grade: GradeRecord, task, inputs: PayloadInputs,
                 payloads: Path, path: Path, complete: CompleteFn,
                 judge_model_id: str) -> JudgeRecord:
    """One absolute-rubric profile: one call, `vote_index=0`.

    Three votes are pairwise-only. The rubric is diagnostic rather than
    decisive, it is reported per model as a profile and never summed into a
    rank, and one call per run is what the spec's cost math assumes (4 rubric
    calls per task-sample).

    `write_payload` first, then the gate assert, then the append. The payload is
    required, not optional (§4.3), and the assert sits in the last position
    before the permanent write for the reason `GateInvariantError` gives.
    """
    payload, rendered, result = judge_rubric(inputs, complete)
    judgment_id = uuid.uuid4().hex
    payload_path, payload_sha = write_payload(payloads, judgment_id, payload)

    judgment = JudgeRecord(
        judgment_id=judgment_id,
        judged_at=_now(),
        judge_model_id=judge_model_id,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        # Of the EXACT text this call sent, carried out of `judge_rubric`
        # rather than re-rendered: a sha over a rebuilt prompt attests to
        # something other than what the model saw.
        judge_prompt_sha=prompt_sha(rendered),
        judge_sampling=dict(JUDGE_SAMPLING),
        rubric_version=RUBRIC_VERSION,
        kind="rubric",
        task_id=task.task_id,
        run_id=record.run_id,
        dimension_scores=dict(result.dimension_scores),
        flags=dict(result.flags),
        # `sample_index` stays `None` here. The schema splits its fields into a
        # rubric half and a pairwise half with `kind` saying which is populated,
        # and sample-vs-sample pairing is a pairwise claim; the run's own index
        # is one `EventLog.read_run` away for any reader that wants it.
        vote_index=0,
        full_reasoning_text=result.reasoning,
        input_payload_path=payload_path,
        input_payload_sha=payload_sha,
        grade_version_seen={record.run_id: grade.grader_version},
        graded_against_task_set_commit=task.task_set_commit,
    )

    _assert_rubric_gate(grade, record.run_id)
    append_judgment(path, judgment)
    return judgment


def _vote_line(task, sample_index: int, run_id_a: str, run_id_b: str,
               inputs_a: PayloadInputs, inputs_b: PayloadInputs,
               vote_index: int, grade_a: GradeRecord, grade_b: GradeRecord,
               rng: random.Random, payloads: Path, path: Path,
               complete: CompleteFn, judge_model_id: str) -> JudgeRecord:
    """One blind, position-randomized pairwise vote.

    `inputs_a`/`inputs_b` are CANONICAL a and b -- the two run ids
    lexicographically sorted -- and `judge_pair_vote` owns the position draw and
    the inversion back out of it, so nothing here reorders anything. A driver
    that swapped the sides on the way in would produce a `position_assignment`
    that no longer describes the stored payload, and the position-swap probe
    would then report a bias figure it did not measure.

    `write_payload` BEFORE `append_judgment`: see the module docstring, step 7.
    """
    outcome = judge_pair_vote(inputs_a, inputs_b, rng, complete)
    judgment_id = uuid.uuid4().hex
    payload_path, payload_sha = write_payload(
        payloads, judgment_id, outcome.payload
    )

    judgment = JudgeRecord(
        judgment_id=judgment_id,
        judged_at=_now(),
        judge_model_id=judge_model_id,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        # Per CALL, not per comparison: position changes the text, so three
        # votes over one pair legitimately carry up to two distinct shas.
        judge_prompt_sha=prompt_sha(outcome.rendered_prompt),
        judge_sampling=dict(JUDGE_SAMPLING),
        rubric_version=RUBRIC_VERSION,
        kind="pairwise",
        task_id=task.task_id,
        run_id_a=run_id_a,
        run_id_b=run_id_b,
        sample_index=sample_index,
        position_assignment=outcome.position_assignment,
        verdict=outcome.verdict,
        vote_index=vote_index,
        full_reasoning_text=outcome.reasoning,
        input_payload_path=payload_path,
        input_payload_sha=payload_sha,
        # Two entries, because the spec's scalar cannot describe a pair whose
        # two sides were gated by different grader generations.
        grade_version_seen={
            run_id_a: grade_a.grader_version,
            run_id_b: grade_b.grader_version,
        },
        graded_against_task_set_commit=task.task_set_commit,
    )

    append_judgment(path, judgment)
    return judgment


def _gate_decided_line(task, sample_index: int, run_id_a: str, run_id_b: str,
                       grade_a: GradeRecord, grade_b: GradeRecord,
                       path: Path, judge_model_id: str) -> JudgeRecord:
    """The one verdict no model produced.

    One side failed the deterministic gate and the other did not, so the
    objective result settles it and asking a judge would let it disagree with a
    fact. There is no call to record, and every field that would evidence one is
    `None` or empty TOGETHER -- filling any of them would assert a model call
    that never happened:

    * `position_assignment`, `vote_index`, `input_payload_path` and
      `input_payload_sha` are `None`, per the schema's own contract.
    * `judge_prompt_sha` is `""` because no prompt was rendered, and
      `judge_sampling` is `{}` because nothing was sent. Both are the
      as-sent evidence fields; a sha or a sampling block copied in from the
      constants would attest to a request that was never made.
    * `full_reasoning_text` is `""`. Driver-authored prose in the field
      reserved for a model's own words is indistinguishable from a verdict when
      the file is read in bulk, and `verdict == "gate_decided"` beside
      `gate_decided_by` already says everything there is to say.

    `judge_model_id`, `judge_prompt_version` and `rubric_version` ARE populated,
    even though no judge was asked, because they are the resume identity: this
    line is a unit of work and a later pass has to be able to see it is done.
    """
    judgment = JudgeRecord(
        judgment_id=uuid.uuid4().hex,
        judged_at=_now(),
        judge_model_id=judge_model_id,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_prompt_sha="",
        judge_sampling={},
        rubric_version=RUBRIC_VERSION,
        kind="pairwise",
        task_id=task.task_id,
        run_id_a=run_id_a,
        run_id_b=run_id_b,
        sample_index=sample_index,
        verdict="gate_decided",
        gate_decided_by="a" if grade_a.resolved is True else "b",
        grade_version_seen={
            run_id_a: grade_a.grader_version,
            run_id_b: grade_b.grader_version,
        },
        graded_against_task_set_commit=task.task_set_commit,
    )
    append_judgment(path, judgment)
    return judgment


# ---------------------------------------------------------------------------
# the batch
# ---------------------------------------------------------------------------


def judge_event_log(event_log_root, tasks, *,
                    complete: CompleteFn | None = None,
                    rng: random.Random | None = None,
                    judge_model_id: str = JUDGE_MODEL_ID_DEFAULT,
                    only_tasks: Iterable[str] | None = None,
                    sample_indices: Iterable[int] | None = None,
                    votes: int = 3, rubric: bool = True,
                    re_judge: bool = False) -> dict:
    """Judge every selected unit in one event log. Returns the batch.

    `complete` and `rng` are the two seams that keep this testable without a
    socket: the first is one rendered prompt in, raw model text out; the second
    is the position draw. Both defaults are the real thing, and `complete`'s is
    LAZY -- see `lazy_live_completion`.

    A UNIT is one rubric call, one pairwise vote, or one gate-decided pair. That
    is the granularity of the resume key, of the `try/except`, and of the exit
    contract, and keeping the three aligned is what makes "exit 0 means every
    selected unit produced its lines" a statement about something.

    `sorted()` at every level -- runs, cells, models, pairs. `list_runs` globs
    and glob order is nondeterministic; sorted is arbitrary but DETERMINISTIC,
    which is what a resumable batch needs, since a killed pass that resumes in a
    different order redoes different work.

    `input_payload_path` is stored exactly as `write_payload` returned it, so it
    is absolute whenever `event_log_root` is. Passing a collection-relative root
    is what makes a stored path portable; the alternative -- rewriting the path
    on the record -- would store something `write_payload` did not write, which
    is the class of drift the payload sha exists to rule out.
    """
    event_log_root = Path(event_log_root)
    if votes < 1:
        # A silent zero would drop every judgeable pair while reporting a clean
        # batch, which is exactly the "dropping pairs breaks the Elo graph"
        # failure the spec forbids -- arrived at by a typo instead of a choice.
        raise ValueError(f"votes must be >= 1, got {votes}")

    path = judgments_path(event_log_root)
    payloads = payloads_root(event_log_root)
    warnings: list[str] = []

    existing, malformed = load_judgments(path)
    if malformed and not re_judge:
        raise ResumeRefused(
            f"{malformed} malformed line(s) in {path}: a resume is keyed on "
            "which comparisons are already judged, and an unreadable judgment "
            "is not an absent one. Read the file, remove the damage, or pass "
            "--re-judge to judge everything again."
        )
    if malformed:
        warnings.append(
            f"{malformed} unreadable line(s) in {path} were skipped. "
            "--re-judge makes them harmless to the resume, which skips "
            "nothing, but the campaign audit below reads the same list -- so "
            "`audited` and `from_prior_invocations` are reported over an "
            "incomplete view of what has already been judged."
        )

    grade_file = grades_path(event_log_root)
    grades, grade_malformed = load_grades(grade_file)
    if grade_malformed:
        # No flag cures this one. An unreadable gate might BE the gate: the run
        # it belongs to reads as ungraded, so it silently leaves a pair -- and
        # if the damage sat on a `resolved is False` line, the pair the judge
        # would have recorded gate-decided becomes one it asks a model about.
        raise ResumeRefused(
            f"{grade_malformed} malformed line(s) in {grade_file}. The judge "
            "defers to the deterministic gate and cannot read it completely, "
            "so there is nothing to defer to. No flag cures this -- --re-judge "
            "governs the judgment file only. Repair or re-run scripts/grade.py."
        )

    gating = gating_view(grades)
    done = {_resume_key(judgment) for judgment in existing}
    by_task = {task.task_id: task for task in tasks}

    log = EventLog(event_log_root)
    stored = sorted(log.list_runs())
    errors: list[str] = []

    ungraded = [run_id for run_id in stored if run_id not in gating]
    if ungraded:
        warnings.append(
            f"{len(ungraded)} run(s) in the event log carry no grade line and "
            f"were not judged: {_named(ungraded)}. Rescuing an ungraded run is "
            "out of scope -- with no gate to defer to the judge has nothing to "
            "be blind about. Run scripts/grade.py first if these belong in the "
            "comparison."
        )
    orphans = sorted(set(gating) - set(stored))
    if orphans:
        warnings.append(
            f"{len(orphans)} grade line(s) name a run this event log does not "
            f"hold: {_named(orphans)}. The usual cause is a grade file copied "
            "from another collection; run ids are unique within a collection "
            "and not across one."
        )

    # Step 4: read the runs, and drop the excluded ones BEFORE grouping.
    records: dict[str, Any] = {}
    excluded: list[str] = []
    for run_id in stored:
        grade = gating.get(run_id)
        if grade is None:
            continue
        if grade.resolved is None:
            excluded.append(run_id)
            continue
        try:
            records[run_id] = log.read_run(run_id)
        except Exception as exc:  # noqa: BLE001 - one run, not the batch
            errors.append(
                f"{run_id}: could not be read from the event log: "
                f"{type(exc).__name__}: {exc}"
            )
    if excluded:
        warnings.append(
            f"{len(excluded)} graded run(s) are excluded (resolved is null) "
            f"and were dropped before pairing: {_named(excluded)}. An excluded "
            "row is not an observation of the model, and ranking it ranks the "
            "infrastructure."
        )

    # Step 5: group into cells, keeping the max-attempt run per model.
    cells: dict[tuple[str, int], dict[str, Any]] = {}
    for run_id in sorted(records):
        record = records[run_id]
        cell = cells.setdefault((record.task_id, record.sample_index), {})
        prior = cell.get(record.model)
        # `run_id` breaks the tie, so two runs of one model at one attempt
        # number resolve the same way on every pass rather than on dict order.
        if prior is None or (record.attempt_number, record.run_id) > (
            prior.attempt_number, prior.run_id
        ):
            cell[record.model] = record

    wanted_tasks = None if only_tasks is None else set(only_tasks)
    wanted_samples = None if sample_indices is None else set(sample_indices)
    if wanted_tasks is not None:
        absent = sorted(wanted_tasks - {task_id for task_id, _ in cells})
        if absent:
            warnings.append(
                f"--only-task named {len(absent)} task(s) with no judgeable "
                f"run in this event log: {_named(absent)}. Not an error -- the "
                "exit contract is about units that were selected and exist -- "
                "but the usual cause is a pointer at the wrong collection, "
                "where every id is missing and the batch reports a clean zero."
            )

    complete = complete if complete is not None else lazy_live_completion(
        judge_model_id
    )
    rng = rng if rng is not None else random.Random()

    judged: list[JudgeRecord] = []
    gate_decided: list[JudgeRecord] = []
    skipped: list[tuple] = []
    missing_tasks: set[str] = set()

    for task_id, sample_index in sorted(cells):
        if wanted_tasks is not None and task_id not in wanted_tasks:
            continue
        if wanted_samples is not None and sample_index not in wanted_samples:
            continue
        task = by_task.get(task_id)
        if task is None:
            missing_tasks.add(task_id)
            continue

        cell = cells[(task_id, sample_index)]
        # Per CELL, and shared across the rubric call and all three votes of
        # every pair the cell produces -- `similarity_context` walks two diffs
        # and the payload builders take `PayloadInputs` by value, so rebuilding
        # per unit would pay for the same walk six times over.
        inputs: dict[str, PayloadInputs] = {}

        if rubric:
            for model in sorted(cell):
                record = cell[model]
                grade = gating[record.run_id]
                # Selection-time gate. The assert before the append is the one
                # that is load-bearing; this is what keeps the batch from
                # paying for a call it must then refuse to record.
                if grade.resolved is not True:
                    continue
                key = _rubric_key(record.run_id, judge_model_id)
                if not re_judge and key in done:
                    skipped.append(key)
                    continue
                try:
                    judged.append(_rubric_line(
                        record, grade, task,
                        _inputs_for(inputs, record, grade, task),
                        payloads, path, complete, judge_model_id,
                    ))
                except Exception as exc:  # noqa: BLE001 - one unit, not the batch
                    errors.append(
                        f"rubric {record.run_id}: {type(exc).__name__}: {exc}"
                    )

        for model_x, model_y in itertools.combinations(sorted(cell), 2):
            # Canonical a/b is the two run ids sorted, NOT the two models: the
            # identity has to be stable under a model rename, and `(x, y)` and
            # `(y, x)` must be one comparison or the position-swap probe can no
            # longer find its own pairs.
            run_id_a, run_id_b = sorted(
                (cell[model_x].run_id, cell[model_y].run_id)
            )
            grade_a, grade_b = gating[run_id_a], gating[run_id_b]
            passed_a = grade_a.resolved is True
            passed_b = grade_b.resolved is True

            if not passed_a and not passed_b:
                # Nothing. Neither side is a submission worth ranking, and a
                # "tie" here would be a verdict about two failures.
                continue

            if passed_a != passed_b:
                key = _pairwise_key(task_id, sample_index, run_id_a, run_id_b,
                                    None, judge_model_id)
                if not re_judge and key in done:
                    skipped.append(key)
                    continue
                try:
                    gate_decided.append(_gate_decided_line(
                        task, sample_index, run_id_a, run_id_b,
                        grade_a, grade_b, path, judge_model_id,
                    ))
                except Exception as exc:  # noqa: BLE001 - one unit, not the batch
                    errors.append(
                        f"gate-decided {run_id_a} vs {run_id_b}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                continue

            for vote_index in range(votes):
                key = _pairwise_key(task_id, sample_index, run_id_a, run_id_b,
                                    vote_index, judge_model_id)
                if not re_judge and key in done:
                    skipped.append(key)
                    continue
                try:
                    # Bound before the call rather than inline: two
                    # positional `PayloadInputs` four lines apart is where an
                    # a/b transposition hides, and a transposed pair produces
                    # a complete, confident, inverted verdict.
                    inputs_a = _inputs_for(
                        inputs, records[run_id_a], grade_a, task
                    )
                    inputs_b = _inputs_for(
                        inputs, records[run_id_b], grade_b, task
                    )
                    judged.append(_vote_line(
                        task, sample_index, run_id_a, run_id_b,
                        inputs_a, inputs_b,
                        vote_index, grade_a, grade_b, rng, payloads, path,
                        complete, judge_model_id,
                    ))
                except Exception as exc:  # noqa: BLE001 - one unit, not the batch
                    errors.append(
                        f"vote {vote_index} {run_id_a} vs {run_id_b}: "
                        f"{type(exc).__name__}: {exc}"
                    )

    if missing_tasks:
        warnings.append(
            f"{len(missing_tasks)} task(s) with graded runs are absent from "
            f"the loaded task set and were not judged: "
            f"{_named(sorted(missing_tasks))}. The payload is anchored on the "
            "manifest's prompt and solution diff, so there is nothing to judge "
            "against."
        )

    # THE AUDIT IS OVER THE CAMPAIGN, NOT OVER THIS INVOCATION, on `grade.py`'s
    # precedent. A judging pass is thousands of paid calls and is killed by an
    # operator or a rate limit far more often than it finishes, so the last
    # invocation routinely writes three lines -- and a census computed from
    # those three would describe a campaign of three.
    fresh = judged + gate_decided
    last: dict[tuple, JudgeRecord] = {}
    for judgment in existing + fresh:
        last[_resume_key(judgment)] = judgment
    fresh_keys = {_resume_key(judgment) for judgment in fresh}

    return {
        "judged": judged,
        "gate_decided": gate_decided,
        "skipped": skipped,
        "errors": errors,
        "warnings": warnings,
        # A census of the judgment file, not a score. Win rates, the rubric
        # profile and Elo are a separate reading pass over these same lines.
        "summary": _census(list(last.values())),
        "audited": len(last),
        "from_prior_invocations": sum(
            1 for key in last if key not in fresh_keys
        ),
    }


def _inputs_for(cache: dict[str, PayloadInputs], record, grade: GradeRecord,
                task) -> PayloadInputs:
    """The whitelist chokepoint's output for one run, built at most once.

    Called INSIDE the per-unit `try`, deliberately. `payload_inputs_from` raises
    `ValueError` on a run with no `artifacts.final_diff`, and a driver that
    built these eagerly would take the whole batch down over one record instead
    of losing one unit's line.
    """
    if record.run_id not in cache:
        cache[record.run_id] = payload_inputs_from(record, grade, task)
    return cache[record.run_id]


def _named(values: list[str]) -> str:
    if len(values) <= _MAX_NAMED:
        return ", ".join(values)
    rest = len(values) - _MAX_NAMED
    return ", ".join(values[:_MAX_NAMED]) + f", and {rest} more"


def _census(judgments: list[JudgeRecord]) -> dict:
    """How many lines of each kind the judgment file holds. A COUNT, not a
    score: no win rate, no rubric mean, no Elo, and nothing joined to
    `resolved`."""
    return {
        "rubric": sum(1 for j in judgments if j.kind == "rubric"),
        "pairwise_votes": sum(
            1 for j in judgments
            if j.kind == "pairwise" and j.verdict != "gate_decided"
        ),
        "gate_decided": sum(
            1 for j in judgments if j.verdict == "gate_decided"
        ),
        "verdicts": dict(Counter(
            j.verdict for j in judgments if j.kind == "pairwise" and j.verdict
        )),
    }


def print_summary(result: dict) -> None:
    print(
        f"\n{len(result['judged'])} judged, "
        f"{len(result['gate_decided'])} gate-decided, "
        f"{len(result['errors'])} errored, "
        f"{len(result['skipped'])} already judged"
    )
    census = result["summary"]
    # Said after the invocation counts and labelled, because the census is the
    # whole campaign: a reader who took it for this pass would read a finished
    # collection off a resume that judged three units.
    print(
        f"\ncensus over {result['audited']} judgment(s) in the judgment file "
        f"({result['from_prior_invocations']} from prior invocation(s)), "
        "deduped to the last line per resume identity"
    )
    print(f"  rubric          {census['rubric']}")
    print(f"  pairwise votes  {census['pairwise_votes']}")
    print(f"  gate-decided    {census['gate_decided']}")
    for verdict, count in sorted(census["verdicts"].items()):
        print(f"  verdict         {verdict}: {count}")

    for warning in result["warnings"]:
        print(f"\nWARNING: {warning}")
    if result["errors"]:
        print(f"\n{len(result['errors'])} unit(s) produced NO line:")
        for line in result["errors"]:
            print(f"  - {line}")
        print(
            "\nThese are holes in the derived view, not verdicts. The runs and "
            "the grades are untouched and re-running this command judges them."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Judge a stored collection offline: rubric profiles and "
                    "blind pairwise votes, in "
                    "<event-log>/judgments/judgments.jsonl. The event log and "
                    "the grade file are never written.",
    )
    parser.add_argument("--event-log", required=True)
    parser.add_argument("--taskset", default=str(DEFAULT_TASK_SET))
    parser.add_argument(
        "--only-task", nargs="+", metavar="TASK_ID",
        help="judge only these task_ids (default: every task in the log)",
    )
    parser.add_argument(
        "--samples", nargs="+", type=int, metavar="INDEX",
        help="judge only these sample indices. Subsample SAMPLES if cost "
             "binds, never pairs: dropping pairs breaks the Elo graph's "
             "connectivity, subsampling only widens the intervals",
    )
    parser.add_argument(
        "--re-judge", action="store_true",
        help="append a new line for every selected unit, even one already "
             "judged under this judge model, prompt version and rubric",
    )
    parser.add_argument(
        "--judge-model", default=JUDGE_MODEL_ID_DEFAULT,
        help="PINNED model id, never an alias -- a floating alias is a moving "
             f"oracle (default: {JUDGE_MODEL_ID_DEFAULT})",
    )
    parser.add_argument("--votes", type=int, default=3)
    parser.add_argument(
        "--no-rubric", action="store_true",
        help="pairwise only. The rubric is diagnostic, so it is the first "
             "thing to run at a lower N when cost binds",
    )
    args = parser.parse_args(argv)

    event_log_root = Path(args.event_log)
    try:
        tasks = load_task_set(Path(args.taskset))
    except TaskError as exc:
        print(f"task set: {exc}")
        return 1

    print(f"event log  {event_log_root}")
    print(f"grades     {grades_path(event_log_root)}")
    print(f"judgments  {judgments_path(event_log_root)}")
    print(f"task set   {args.taskset}  ({len(tasks)} task(s))")
    print(
        f"judge      {args.judge_model}, prompt v{JUDGE_PROMPT_VERSION}, "
        f"rubric {RUBRIC_VERSION}"
    )

    try:
        result = judge_event_log(
            event_log_root, tasks,
            judge_model_id=args.judge_model,
            only_tasks=args.only_task,
            sample_indices=args.samples,
            votes=args.votes,
            rubric=not args.no_rubric,
            re_judge=args.re_judge,
        )
    except ResumeRefused as exc:
        print(f"\nREFUSED: {exc}")
        return 1

    print_summary(result)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
