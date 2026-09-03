"""The grader's own record: what an offline grading pass concluded, and from
what. See `docs/superpowers/specs/2026-08-17-offline-grader-design.md`.

The harness does not grade (spec section 6.1) -- `tests_passed` stays `None` in
every `RunRecord`, forever. Grading is a separate, offline, re-runnable batch
over the stored final diffs, and this module holds its output: a `GradeRecord`
per run, appended one JSON line at a time to a file that is never rewritten.

A grade is a DERIVED view, so it is worth nothing without the inputs it was
derived from. `record_schema_version`, `grader_version`, `grader_commit`,
`graded_in_image`, `graded_under_preflight_version`,
`graded_against_manifest_digest` and `graded_against_task_set_commit` name each
one. Re-grading the same run under a different oracle, a different image or a
different grader is a NEW line, not an edit -- the two verdicts disagreeing is
the finding, and an update API would delete it.

Null semantics, in the order they are easiest to get wrong:

* `resolved is None` if and only if `not_graded_reason is not None`. A run that
  was not graded has no verdict, and `False` would be one -- an accusation that
  the model failed, stamped on a run the grader refused to look at. The two
  fields move together and neither is inferable from the other's absence.
* `not_graded_detail` carries the cause the reason only names: the `OracleError`
  text, the joined preflight problems, the declared-vs-found task versions. A
  named bucket with no cause is the same two-absences defect one layer down --
  `TASK_SETUP_FAILED` with nothing behind it cannot be told from
  `TASK_SETUP_FAILED` whose message was dropped.
* `quarantined is None` means no oracle was consulted; `()` means one was and it
  derived an empty quarantine. Every task with a healthy suite lands on `()`, so
  collapsing the two would make "the oracle never ran" indistinguishable from
  the normal case.
* Configuration is never reported as observation. `f2p_declared` and
  `p2p_quarantine_requested` / `p2p_deselect_requested` are what the grader
  ASKED for, read out of the manifest and off the quarantine; `p2p_deselected`
  is what pytest REPORTED deselecting. They are different numbers whenever a
  node id in the manifest no longer exists, which is exactly the drift worth
  seeing.
* `record_schema_version` is COPIED off the run record, never inferred from the
  fields present. A field absent because the writer predates it and a field
  absent because it was never measured render identically; only the version
  tells them apart, and a version reconstructed from the data restates the data.
* Every measurement field is `| None` and `None` means NOT MEASURED. `checks`
  defaults to `()` rather than `None` only because a `GradeRecord` is written by
  the ladder, which appends as it goes: an empty tuple is "the ladder ran no
  check", which on this path is a measurement.
* `not_run_node_ids is None` means NOT MEASURED -- either the ladder never
  reached a check that reads `Outcome.not_run`, or it did and the framework
  reports no executed ids at all (pytest, where the exit code carries this
  instead). A non-empty tuple is an observation. It never round-trips as `()`:
  the branch that writes it only runs when the set is non-empty.

`grader_version` is what gates resume (a run already graded under the current
grader is skipped); `grader_commit` carries `runner.harness_commit()` including
its `-dirty` suffix, and is the evidence that the gate was honest. A clean
version string beside a dirty tree asserts a provenance that does not exist.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any

# The grade file's own schema, independent of the run record's. A reader that
# cannot tell grade schema versions apart reads an absent field as a positive
# negative claim -- the same reason `SCHEMA_VERSION` moves for additive bumps.
# 1.1.0 adds `suite_timeout_s`: with the bound per task, `timed_out` alone
# does not say what was blown.
# 1.2.0 adds `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE`. Additive in
# fields and not in meaning: `not_graded_reason` can now carry a value no
# earlier writer could produce, and a reader that cannot tell the versions
# apart has no way to know whether its absence on a line is a measurement or
# a vocabulary it did not have.
# 1.3.0 adds `GradeRecord.framework`. `p2p_deselected`, `f2p_failed_node_ids`
# and `p2p_failed_node_ids` have framework-dependent shapes and units (see the
# field's own docstring), and a reader that cannot tell this version apart
# from 1.2.0 has no way to know whether a line's numbers came from pytest or
# a node adapter -- the same reason `SCHEMA_VERSION` moves for additive bumps.
# 1.4.0 adds `GradeRecord.not_run_node_ids`: the declared f2p or p2p ids a run
# did not execute. On a 1.3.0 line those ids exist only inside
# `environment_error`'s prose, and a node `fullName` may itself contain ", ",
# so the joined form cannot be split back -- the field's absence on such a
# line is the writer's vocabulary, not a measurement, which is the same
# reason `SCHEMA_VERSION` moves for additive bumps.
# 1.5.0 adds `NotGradedReason.SUBMODULE_EDIT_UNGRADABLE`. Additive in fields
# and not in meaning, exactly as 1.2.0 was: `not_graded_reason` can now carry
# a value no earlier writer could produce, so its absence on an older line is
# a gap in that writer's vocabulary and not a measurement. Every such run
# graded under 1.4.0 or below landed as `EMPTY_PATCH` -- a `GradeFailure`,
# hence `resolved: False`.
GRADE_SCHEMA_VERSION = "1.5.0"

# The oldest `RunRecord` schema whose fields mean what `from_dict` and the
# ladder were written to assume. Below it the run is not graded and
# `NotGradedReason.RECORD_SCHEMA_TOO_OLD` says so.
#
# Honest note: nothing stored today is below 3.5.0 -- measured 2026-08-17 over
# the 33 records in the event logs under `~/.cache/bakeoff` -- so this floor
# gates nothing yet. It is cheap insurance against a record whose field
# semantics predate the reader, of which there are already two in this store's
# history: pre-3.0.0 `turns_used` counts content blocks rather than API calls,
# and pre-2.1.0 `cache_state.warm` is a dataclass default rather than an
# observation. A grade derived from either would be wrong and would not say so.
MIN_GRADABLE_SCHEMA = "3.0.0"


# ---------------------------------------------------------------------------
# where a grading pass puts what it writes
# ---------------------------------------------------------------------------
#
# HERE rather than in `scripts/grade.py`, where both of these were defined and
# from where `scripts/judge.py` imported the first of them. That import is what
# made reading the judge driver's `--help` require a Docker package: importing
# `scripts.grade` for one path derivation drags `bakeoff.images` -> `docker`
# and the rest of the grading module graph behind it, so on an analysis box
# with no daemon the judge driver failed before argparse ran, over a function
# that returns a two-component join.
#
# `grade.py` re-imports both names and goes on exporting them, so
# `scripts.grade.grades_path` stays reachable for every caller that already
# reads it -- the point is one definition in a module that costs nothing to
# import, not a second copy in a lighter place. Two copies of where the grade
# file lives is how a moved grade file turns into a judging pass that reports
# "nothing is graded" and judges nothing: silent, and in the direction that
# looks like success.


def grades_path(event_log_root: Path | str) -> Path:
    return Path(event_log_root) / "grades" / "grades.jsonl"


def artifacts_root(event_log_root: Path | str) -> Path:
    return Path(event_log_root) / "grades" / "artifacts"


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def schema_at_least(version: str, floor: str) -> bool:
    """Is `version` at or above `floor`, compared as integer components?

    Not `version >= floor`. String comparison puts `"3.10.0"` BELOW `"3.9.0"`,
    and `SCHEMA_VERSION` IS 3.10.0 -- the wrap has arrived, so this is no
    longer insurance. Under `>=` every record this harness writes would be
    refused as "too old" against a 3.9.0-or-below floor, and the failure
    arrives with a schema bump rather than with a code change: it is silent
    and it makes a whole collection read as ungradable.

    Components are compared as tuples, padded so `"3.8"` and `"3.8.0"` agree.
    A version that is not dotted integers raises `ValueError` rather than being
    read as too old: an unparsable version is not evidence about age, and
    `RECORD_SCHEMA_TOO_OLD` on a record whose version is `"3.8.0-rc1"` would be
    a confident wrong reason. The caller decides what to do with the raise.
    """
    left = _version_tuple(version)
    right = _version_tuple(floor)
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left >= right


class GradeFailure(str, Enum):
    """Why a graded run did not resolve -- the FIRST rung of the ladder it fell
    off, not every rung it would have failed.

    Set only when `resolved is False`, which means the grader looked. A run the
    grader refused to look at carries `not_graded_reason` instead, and the two
    are never both set: "the model's patch broke the build" and "we never ran
    the build" are different claims about the model.
    """

    EMPTY_PATCH = "empty_patch"
    APPLY_FAILED = "apply_failed"
    BUILD_FAILED = "build_failed"
    TYPECHECK_FAILED = "typecheck_failed"
    F2P_FAILED = "f2p_failed"
    P2P_REGRESSION = "p2p_regression"
    LINT_FAILED = "lint_failed"
    SECRET_FOUND = "secret_found"
    DESTRUCTIVE_UNREVERTED = "destructive_unreverted"


class NotGradedReason(str, Enum):
    """Why no verdict exists for this run.

    Split deliberately by WHOSE problem it was, because the offline view
    computes a resolve rate and every one of these is a row that must be left
    out of the denominator rather than counted as a failure:

    * the run never produced a gradable submission (`EXCLUDED`, `NO_TURNS`,
      `CRASHED`, `NO_FINAL_DIFF`, the two unappliable-diff cases, and
      `SUBMODULE_GITLINK_UNGRADABLE` -- where a submission exists and is
      appliable, but what it carries is a GITLINK pointing at a commit that
      lives only in the run tree that produced it, so the agent may well have
      fixed the bug and the harness cannot see the content. Either a declared
      submodule the agent committed inside, or a repository the agent created
      itself with `git init`/`git clone` in a tracked subdirectory, which
      produces the same chunk on a task with no submodules at all), and
      `SUBMODULE_EDIT_UNGRADABLE` -- where a submission exists but the tree it
      was taken from carried changes `git add -A` stages nothing for, so the
      stored diff is not a description of what the agent produced;
    * the grading environment broke (`ENVIRONMENT_ERROR`,
      `SCOPE_COLLECTED_NOTHING`, `PREFLIGHT_FAILED`, `ORACLE_FAILED`,
      `TASK_SETUP_FAILED`);
    * the inputs do not line up (`TASK_NOT_FOUND`, `TASK_VERSION_MISMATCH`,
      `RECORD_SCHEMA_TOO_OLD`).

    Only the first group says anything about the model, and it does not say the
    model failed the task.
    """

    EXCLUDED = "excluded"
    NO_TURNS = "no_turns"
    CRASHED = "crashed"
    NO_FINAL_DIFF = "no_final_diff"
    BINARY_HUNK_UNAPPLIABLE = "binary_hunk_unappliable"
    LOSSY_DIFF_UNAPPLIABLE = "lossy_diff_unappliable"
    SUBMODULE_GITLINK_UNGRADABLE = "submodule_gitlink_ungradable"
    SUBMODULE_EDIT_UNGRADABLE = "submodule_edit_ungradable"
    ENVIRONMENT_ERROR = "environment_error"
    SCOPE_COLLECTED_NOTHING = "scope_collected_nothing"
    PREFLIGHT_FAILED = "preflight_failed"
    ORACLE_FAILED = "oracle_failed"
    TASK_SETUP_FAILED = "task_setup_failed"
    TASK_NOT_FOUND = "task_not_found"
    TASK_VERSION_MISMATCH = "task_version_mismatch"
    RECORD_SCHEMA_TOO_OLD = "record_schema_too_old"


# The ladder, in the order the checks run. Order is load-bearing twice over: a
# later check's result is meaningless once an earlier one failed (an f2p pass on
# a tree the patch did not apply to is a statement about the reference fix), and
# `grade_failure` names the FIRST rung that failed, so reordering this tuple
# silently changes what a stored grade means.
CHECK_ORDER: tuple[str, ...] = (
    "patch_non_empty",
    "test_restore",
    "build",
    "typecheck",
    "f2p",
    "p2p",
    "lint",
    "secret_scan",
    "destructive_scan",
)


@dataclass(frozen=True)
class CheckResult:
    """One rung of the ladder, as it actually ran.

    `status` is one of `"pass"`, `"fail"`, `"not_configured"` or `"skipped"`,
    and the last two are not the same thing: `not_configured` means the task
    declares no such command (most tasks have no typecheck), `skipped` means the
    command exists and an earlier failure meant it was never run. Collapsing
    them makes "this task has no linter" read as "the linter was cut short".

    `exit_code is None` alongside `timed_out=True` is the ordinary shape of a
    check killed by the wall clock -- there is no exit code to report, and a
    fabricated `124` would be indistinguishable from the command's own.
    """

    name: str
    status: str
    exit_code: int | None = None
    duration_s: float | None = None
    timed_out: bool = False
    # Where the captured stdout/stderr landed, `None` when nothing was written.
    # A grade that says `f2p` failed and cannot show the output is not evidence.
    output_path: str | None = None
    detail: str = ""


def _build(klass: type, data: dict[str, Any]) -> Any:
    """Construct a record class, ignoring fields it does not know.

    Copied from `schema._build` rather than imported, on purpose: the grader is
    a separate, offline reader with its own schema, and importing the harness's
    private helper would couple the grade file's read path to a run-record
    refactor. The reasoning is the same one -- a grade written by a LATER grader
    must not be lost to a `TypeError` over one added field. The reader still
    sees `grade_schema_version`, so it can tell it is holding a newer grade and
    decide; raising would make that decision for it, permanently, in the
    direction of losing the data.
    """
    known = {f.name for f in fields(klass)}
    return klass(**{k: v for k, v in data.items() if k in known})


# Fields that are tuples on the way in and JSON arrays on the way out. Listed
# rather than inferred from the annotations, because `None` and `()` must
# survive the round trip distinctly and a `list -> tuple` sweep over "anything
# iterable" would convert a `None` into an empty tuple, which is a claim.
_TUPLE_FIELDS: tuple[str, ...] = (
    "quarantined",
    "binary_chunks_dropped",
    "f2p_failed_node_ids",
    "p2p_failed_node_ids",
    "not_run_node_ids",
)


@dataclass(frozen=True)
class GradeRecord:
    """One grading pass over one run.

    Identity is `run_id` plus `collection_id`: `run_id` is
    `sha256(task|model|sample|attempt)` and names no episode, so two matrices
    over the same cells mint identical ids and a grade file merged across
    collections would fold them together.
    """

    run_id: str
    collection_id: str
    task_id: str
    model: str
    # Copied off the run record, never inferred. See the module docstring.
    record_schema_version: str
    graded_at: str
    grader_version: str

    # `runner.harness_commit()`, `-dirty` and all. `grader_version` gates
    # resume; this is the evidence that gate was honest.
    grader_commit: str = ""
    # The preflight verdict this grade was taken under, and the image it ran in.
    # A task is only known to discriminate under a specific preflight, and a
    # grade taken in a different image is a different measurement.
    graded_under_preflight_version: str = ""
    graded_in_image: str = ""
    # Whether that image is the one the RUN used. `None` when the comparison
    # could not be made -- the run record names no image, or the grader could
    # not resolve its own. `False` is a real mismatch worth seeing, so it must
    # not be what "we did not check" looks like.
    image_matches_run: bool | None = None
    # The manifest's own `manifest_digest` and `task_set_commit`, copied. A
    # grade must name every input it was derived from, and the task is one.
    # `TASK_VERSION_MISMATCH` catches a BUMPED `task_version`; an edit to
    # `task.yaml` without a bump moves only the digest, which is why the digest
    # is stored beside the version rather than trusted to follow it.
    graded_against_manifest_digest: str = ""
    graded_against_task_set_commit: str = ""
    grade_schema_version: str = GRADE_SCHEMA_VERSION

    # The oracle: which f2p/p2p sets this grade was taken against, and the
    # quarantine derived from them. `None` means no oracle was consulted.
    oracle_fingerprint: str | None = None
    oracle_version: str | None = None
    quarantined: tuple[str, ...] | None = None

    checks: tuple[CheckResult, ...] = ()
    # The verdict. `None` if and only if `not_graded_reason` is set.
    resolved: bool | None = None
    # `GradeFailure` value, set only when `resolved is False`.
    grade_failure: str | None = None
    # `NotGradedReason` value, set only when `resolved is None`.
    not_graded_reason: str | None = None
    # The message the reason promises.
    not_graded_detail: str | None = None

    # Copied off the run record when it carries them, so a reader of the grade
    # file alone can tell an excluded run from a crashed one without joining
    # back to the event log.
    exclusion_class: str | None = None
    crash_error: str | None = None
    assembly_error: str | None = None

    # Did the agent touch the test half? Not a failure on its own -- the ladder
    # restores the tests before f2p runs -- but it is the thing that makes a
    # resolve suspicious, and `None` means the comparison was not made.
    agent_modified_tests: bool | None = None
    # Binary chunks dropped from the submission before apply, by path. `()`
    # means the diff was inspected and carried none; `None` means it was not
    # inspected.
    binary_chunks_dropped: tuple[str, ...] | None = None

    # Which rung the environment broke on, and how. Kept apart from
    # `grade_failure` on purpose: a missing interpreter is not a model failure,
    # and folding the two would put the grader's own breakage into the resolve
    # rate's numerator-adjacent bucket.
    environment_error: str | None = None
    environment_error_check: str | None = None

    #: Which runner adapter produced this record's numbers. `""` means the
    #: grade predates the field, never "pytest" -- a default naming a real
    #: framework would be this field claiming a fact nobody measured.
    #:
    #: It is VALIDATED CONFIGURATION, not a measurement: `run_ladder` sets it
    #: to `for_framework(task.tests.framework).name`, an identity round-trip
    #: through a registry keyed by that same manifest string (`grader.py`'s
    #: own `_State.framework` docstring says so). It is still worth recording
    #: rather than left for a reader to re-derive from the manifest, because
    #: the closed allowlist behind `for_framework` is the single source of
    #: truth for which adapter's semantics -- id shapes, `p2p_deselected`
    #: units -- apply to `f2p_failed_node_ids`, `p2p_failed_node_ids` and
    #: `p2p_deselected` on this line. An OBSERVATION would instead be the
    #: adapter that actually classified each check's `Outcome`; no code path
    #: diverges from the declared framework today, so the two coincide, but
    #: this field records the latter's SOURCE (config), not the former.
    framework: str = ""

    # Config (what was asked) beside observation (what pytest reported). See
    # the module docstring.
    f2p_declared: int | None = None
    f2p_failed_node_ids: tuple[str, ...] | None = None
    p2p_quarantine_requested: int | None = None
    p2p_deselect_requested: int | None = None
    p2p_deselected: int | None = None
    p2p_failed_node_ids: tuple[str, ...] | None = None

    #: Declared f2p or p2p node ids the run did not execute -- `Outcome.not_run`,
    #: which is "requested, and no terminal status came back". `None` is NOT
    #: MEASURED (the ladder never got here, or the framework reports no
    #: executed ids -- pytest, where the exit code carries this instead); a
    #: non-empty tuple is an observation. It never round-trips as `()`: the
    #: branch that writes it only runs when the set is non-empty.
    #:
    #: On the p2p side the quarantine is already subtracted, upstream, by
    #: `preflight._Runner.pass_to_pass` -- a quarantined id was asked NOT to
    #: run, so reporting it here would be a defect, not evidence.
    #:
    #: `environment_error_check` says which check produced it, and ONE field is
    #: enough because only one check can ever write it -- both branches go
    #: through `state.environment`, which raises `_Stop`, so an f2p `not_run`
    #: means `_check_p2p` never ran at all.
    #:
    #: Beside the message rather than instead of it, for the reason
    #: `f2p_failed_node_ids` exists: a node `fullName` is free text and may
    #: contain ", ", so `environment_error`'s joined form is not losslessly
    #: splittable, and a reader counting how often a task set's manifests went
    #: stale would be grepping prose. GRADER_VERSION 7 -> 8 moved for exactly
    #: this shape -- a well-formed verdict beside a lying evidence field.
    not_run_node_ids: tuple[str, ...] | None = None

    #: The `timeout` bound every command in this ladder carried, from
    #: `task.budget.suite_timeout_s`. `None` when no bounded command ran.
    #:
    #: `CheckResult.timed_out` alone stopped being readable the moment the
    #: bound became per task: a reader cannot tell a suite that blew 600 s
    #: from one that blew 1800 s, and a re-grade under an edited manifest is a
    #: NEW line whose disagreement with the old one is the finding -- which it
    #: cannot be if neither line says what it was measured against.
    #:
    #: It is the bound and NOT the headroom. A `timed_out` check is a
    #: `GradeFailure` stamped on the model, and nothing on this record says
    #: whether the grader's host was busier than the gate's: there is no host
    #: or contention block here (`RunRecord.host` has one; a grade does not),
    #: and `duration_s` at a timeout is just this number again. The figure
    #: it is the margin against -- preflight's OBSERVED suite duration in the
    #: same image -- is recorded since `PREFLIGHT_VERSION` 18, in
    #: `<cache>/preflight/<task_id>.json` under `bounded_run_durations_s`;
    #: join on `task_id` and date the gate with `graded_under_preflight_version`.
    #: It is not copied onto this record, and deliberately: it is a property
    #: of the task, it would go stale the moment the task is re-preflighted,
    #: and a stale copy of a measurement is configuration reported as
    #: observation.
    #: So read a `timed_out` grade as "this bound was hit", never as "this
    #: suite needs more than this bound".
    suite_timeout_s: int | None = None

    # Where this grade's captured output lives. `None` when nothing was kept.
    artifacts_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, (list, tuple)):
                return [encode(v) for v in value]
            if isinstance(value, dict):
                return {k: encode(v) for k, v in value.items()}
            return value

        return {k: encode(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GradeRecord:
        data = dict(data)

        checks = data.get("checks")
        if checks is not None:
            data["checks"] = tuple(
                _build(CheckResult, c) if isinstance(c, dict) else c
                for c in checks
            )

        for key in _TUPLE_FIELDS:
            value = data.get(key)
            # `is not None` rather than truthiness: `[]` round-trips to `()`,
            # which is the oracle-ran-and-found-nothing case and is NOT `None`.
            if value is not None:
                data[key] = tuple(value)

        return _build(cls, data)


def append_grade(path: str | os.PathLike[str], record: GradeRecord) -> None:
    """Append one grade as one JSON line. Never truncates.

    Mode `"a"` and no update or delete API, for the same reason the event log
    has none: a re-grade under a different oracle, image or grader is a new
    line, and the disagreement between the two lines is the finding. Overwriting
    would destroy the only evidence that the grader is not deterministic.

    `flush()` + `os.fsync()` per line because a grading batch is minutes of
    container work per run and is killed by an operator far more often than it
    finishes -- an unflushed tail means re-running the expensive part for grades
    that were already computed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_grades(
    path: str | os.PathLike[str],
) -> tuple[list[GradeRecord], int]:
    """Read a grade file. Returns `(records, malformed_lines)`.

    A malformed line is skipped and COUNTED, on the
    `transcript_malformed_lines` precedent: raising would make one damaged byte
    -- a torn write from a killed batch, most likely -- unread every good grade
    in the file. Returning the count rather than swallowing it is the other half:
    a silent skip turns a truncated file into a smaller-looking collection, and
    the driver refuses to RESUME on a non-zero count precisely because a resume
    keyed on "which runs are already graded" cannot afford to mistake an
    unreadable grade for an absent one.

    A missing file is `([], 0)`, not an error: nothing has been graded yet is
    the first state every collection is in.
    """
    path = Path(path)
    if not path.exists():
        return [], 0

    records: list[GradeRecord] = []
    malformed = 0
    # errors="replace" so a torn multibyte sequence becomes U+FFFD and fails
    # json.loads -- one COUNTED line -- rather than raising UnicodeDecodeError
    # out of the iterator and losing every grade after it in the file.
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                # Counted, not ignored. A trailing newline does not produce one
                # of these; a bare blank line means something wrote into the
                # file that was not a grade.
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
                records.append(GradeRecord.from_dict(data))
            except (TypeError, ValueError):
                # A line that parses as JSON but cannot become a GradeRecord --
                # a missing required field from a hand-edited file. Same
                # treatment: countable damage, not a reason to lose the rest.
                malformed += 1
    return records, malformed
