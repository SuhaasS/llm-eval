"""Refuse to spend money on a task that cannot be shown to be a task.

This is the check the harness has never had. Everything else in the codebase
validates the HARNESS (capture, attribution, isolation) or a run's OUTPUT
(turns, tool calls, a diff). Nothing validated the INPUT, and that is the
defect that invalidated every Phase 0c capability figure: the image shipped
no pytest, the fixture was not importable, and the only verification command
available raised ModuleNotFoundError with the bug fixed and unfixed alike.
Gemma burned 30 of 30 turns on it and the 9/9 was read as capability.

`smoke_test.assert_agent_can_verify_its_work` was the first fix and it is too
weak: it proves a binary is on PATH. What has to be true is stronger and is
per task -- that THIS task, in THIS image, with the repo at THIS start state,
is red before the reference fix and green after it. A task that is green
before is a task that was already done; a task that is still red after a
correct fix makes a solved run and an idle run leave identical evidence, and
no amount of downstream logging can tell them apart.

Six of six "model failures" so far have been harness defects. That base rate
is the argument for running this before every matrix rather than trusting a
task author.

Offline: a Docker daemon and a materialized repo, no credentials, nothing
spent. Deliberately not folded into `verify_logger.py`, which is the section
6.6 LOGGING gate and whose defining property is that it needs neither a
daemon nor a network.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path

from bakeoff.container import RunContainer
from bakeoff.images import base_tag, image_labels
from bakeoff.runners import (
    KIND_FAILED,
    KIND_LOAD_ERROR,
    KIND_NOTHING_RAN,
    KIND_PASSED,
    Outcome,
    for_framework,
)
from bakeoff.runners.node_adapter import verify_selected
from bakeoff.tasks import (
    _DEFAULT_PYTHON,
    _GRADING_KEYS,
    _under,
    task_runtime,
)

# Re-exported, not re-defined. These moved to `bakeoff.runners.pytest_adapter`
# when the exit-code judgement became per-framework (broadening 7), and they
# are imported back under their EXACT bare names because four modules, twelve
# tests and six `scripts/mutation_check.py` anchors reference them that way. A
# namespaced re-spelling (`adapter.f2p_modules(...)`) would rot every one of
# those anchors silently -- `mutation_check` fails a missing anchor with STALE
# ANCHOR, but only on a run somebody makes.
from bakeoff.runners.pytest_adapter import (  # noqa: F401
    EXIT_ALL_PASSED,
    EXIT_COLLECTION_FAILURES,
    EXIT_COLLECTION_INTERRUPTED,
    EXIT_NOTHING_COLLECTED,
    EXIT_TESTS_FAILED,
    EXIT_USAGE_ERROR,
    _EXIT_MEANING,
    _FAILED_LINE,
    _PROCESS_EXIT_MEANING,
    _PYTHON_BASENAME,
    _runner_python,
    collection_error_modules,
    f2p_modules,
    failed_node_ids,
)

#: `_Runner`'s adapter default, and the ONLY thing that may ever use it. See
#: `_Runner.__init__` for why a default exists at all and why no caller may
#: rely on it.
_PYTEST_ADAPTER = for_framework("pytest")

#: What this gate asserts, as a version. It joins `run_matrix`'s preflight
#: cache key and the grader's, because none of the other three components
#: (manifest digest, image id, start sha) moves when THIS file changes -- so
#: without it every warm cache serves a verdict written by the old gate and a
#: newly added assertion is inert on exactly the tasks about to be run. Bump
#: it with any change to what preflight asserts. 1 is the implicit version of
#: every verdict cached before the scoped-p2p and grading assertions landed.
#: 3 adds the `strip_paths` assertion: a verdict cached under 2 was written
#: by a gate that never looked at that key at all.
#: 4 accepts an f2p run that could not COLLECT (broadening 2). A verdict
#: cached under 3 was written by a gate that refused that task shape outright,
#: and one cached under 4 was written by a gate whose p2p-before run carries
#: `--ignore` on exactly those tasks.
#: 5 adds three refusals: the `image.env` read-back mismatch, the refusal of
#: a task whose declared test paths IMPORT hypothesis while the manifest
#: declares no CI (availability alone is not the trigger -- the package is a
#: common transitive dependency), and the refusal of an rg probe that could
#: not answer (an exit code that is neither 0 nor 1). A verdict cached under 4
#: was written by a gate that looked at none of them, so a task whose
#: determinism lever silently failed to apply -- or was never declared, or
#: whose scan environment was broken -- would keep serving a PASS.
#: 6 makes the gate's bound a manifest value (`budget.suite_timeout_s`)
#: instead of a function default. `manifest_digest` alone does not cover this:
#: a manifest that ALREADY carries the key loads fine under the older loader,
#: which ignores unknown `budget` sub-keys, so its digest does not move when
#: this code lands and every warm cache would serve a verdict gated at 600
#: against a manifest that asks for something else.
#: 7 adds the `image.python` read-back: a verdict cached under 6 was written
#: by a gate that never asked which interpreter the container runs, so a task
#: built against a stale or mismatched base keeps serving a PASS while its
#: suite runs under an interpreter the task was not cut for.
#: 8 reads every submodule's state back out of the container. A verdict cached
#: under 7 was written by a gate that never asked whether the tree's gitlinks
#: are populated -- and `git status --porcelain` reports a superproject whose
#: submodule directory is empty as CLEAN, so no other assertion in this file
#: can see it either.
#: 9 changes what 8's submodule assertions ASSERT, which is why an additive-
#: looking round still moves the key. Three ways a verdict cached under 8 is
#: not the verdict this gate would give: the status-to-gitlink match became
#: boundary-anchored, so a tree carrying sibling paths (`vendor/lib` beside
#: `vendor/libdep`) could file an entry under the wrong path and flip GO/NO-GO
#: under the old `startswith`; the reverse cross-check now reports an index
#: gitlink no `git submodule status` line named, which 8 recorded as a
#: positive empty list; and the orphan read moved to `--get-regexp -z`, so a
#: submodule whose NAME contains a space is no longer reported as an orphan
#: under a path that is not a path.
#: 10 adds the runner adapter (broadening 7). A verdict cached under 9 was
#: written by a gate whose every red/green branch read a pytest EXIT CODE,
#: which classifies a vitest or jest run by rules those frameworks do not
#: follow -- measured, both answer a failing test, an unresolvable import, a
#: syntax error, a nonexistent file argument and a broken config with 1 alike
#: -- and which made none of the three node assertions: that every declared
#: f2p id actually RAN (a `-t` pattern matching nothing exits 0 with every
#: test reported skipped), that the scoped p2p run stayed inside tests.paths
#: (a positional is a substring filter over the absolute path, not a path),
#: and that the framework's cache flags are in tests.runner (vitest writes
#: node_modules/.vite, which every JavaScript .gitignore hides from the
#: dirty-tree check). It also covers `evidence["framework"]`, which landed
#: earlier in the same broadening with no bump of its own: this is the first
#: commit that changes what the gate ASSERTS, and the ones before it were a
#: refactor whose argv is byte-identical on both branches -- so a warm verdict
#: served across them describes a gate that would return the same verdict,
#: which is the property this version exists to protect.
#: 11 is the same rule applied to that broadening's own review. Three evidence
#: keys changed what an absence MEANS without changing any verdict:
#: `duplicate_full_names` and `scope_files_outside` went from `[]` ("measured,
#: nothing found") to `None` ("no scoped run was made, or it wrote no report"),
#: and `f2p_before_not_run` joined them -- it was `[]` on the runner-gate early
#: return, which starts no container at all, and on a node f2p run that wrote
#: no report, so it claimed a measurement it never made. A cached blob and a
#: fresh one would otherwise carry the identical version string with `[]`
#: meaning two different things in them, which is exactly what a reader uses
#: this number to rule out. The gate's GO/NO-GO is unchanged across 10 -> 11;
#: what moved is what a stored verdict's evidence can be read to say.
#:
#: 11 -> 12: `pytest_adapter._FAILED_LINE` learned `SUBFAILED` (fix 3,
#: 2026-09-02). A v11 verdict was a false NO-GO on any task whose f2p id fails
#: only through `unittest.subTest` under pytest's core-integrated subtests
#: (pytest >= 9): the node's failure printed as `SUBFAILED(label) <id> - ...`
#: rather than `FAILED <id>`, `failed_ids` came back not containing that id,
#: and the before-check's "declared f2p tests did not fail at the start
#: state" fired on a run whose own exit code was 1 -- the Phase 0c
#: contradiction this gate exists to prevent, reintroduced by the parser
#: rather than the image. A cached v11 PASS is unaffected (a v11 gate that
#: passed still passed for the right reason); a cached v11 NO-GO on a subtest
#: task is stale and must be re-run under 12 to be believed.
#:
#: 12 -> 13: the bare-runner probe (fix 2, 2026-09-02). A v12 verdict never
#: asked whether the repo's OWN pytest configuration -- its pyproject.toml
#: addopts, read with none of `tests.runner`'s extra arguments -- is itself a
#: usage error in this image. `bidict-389-putall-rollback-clean`'s gated
#: runner bypasses this with `--override-ini=addopts=` and passes, while the
#: bare command an agent naturally types (`python -m pytest tests/`) exits 4
#: from turn one, bug fixed and unfixed alike: the loop truncates after
#: "edits" and every arm is scored on an unverified guess, exactly the
#: failure mode this file's module docstring names. A cached v12 PASS on a
#: pytest task must be re-run under 13 to be believed; a v12 NO-GO is
#: unaffected (nothing this version adds can turn a NO-GO into a GO).
#: 13 -> 14 is not a new assertion. It retires cached PASS verdicts observed
#: through a container this code can no longer interrogate:
#: `preflight-tree/<task_id>` was one path per task, reused across
#: invocations, and the Docker VM served the second container the empty
#: directory it had cached for that mount source (measured 2026-09-02,
#: `[files], [], [files], []` over four cycles) -- which on a vitest task is
#: `No test files found` at exit 1, the shape a PASS cannot be told apart
#: from. The tree is not in the cache key and could not be: the verdict
#: outlives the artifact it describes, so the version is the only lever that
#: retires a suspect one. Both caches key through `preflight_cache_key`, so
#: `preflight.json` (run_matrix) and `preflight-grade.json` (grade.py)
#: invalidate together -- one offline re-gate per task per driver, no
#: credentials and no spend.
#: 14 -> 15: node selection and deselection became per-file (round 2 item 1,
#: 2026-09-03), and four things about a cached v14 verdict follow from it.
#: (1) A v14 NO-GO for a cross-file duplicate `fullName` is STALE: that
#: refusal is gone, because `-t` now runs paired with one file's positional
#: and a quarantine of `a::works` cannot reach `b::works`. (2) The jest
#: per-file positional gained its mount anchor and its escape, and jest
#: stopped emitting `--testPathIgnorePatterns` at all -- so a v14 verdict was
#: taken with a filter that could match a second file, and on the one run that
#: used `ignored`, with the repository's own ignore list REPLACED by the
#: flag. (3) `ambiguous_file_filters` is a new refusal, so a v14 PASS on a
#: VITEST task whose executed files include one path contained in another's is
#: stale in the other direction. (4) `duplicate_full_names`' CONTENT grows for
#: an unchanged task: under v14 the scoped run deselected every f2p title
#: globally, so an identically-titled test in another file never reached a
#: terminal status and never entered `executed_names`; under v15 group 0 runs
#: those files unfiltered and the key lists collisions the v14 verdict for the
#: identical task did not. The key's shape and meaning are unchanged -- what
#: changed is what the gate could see -- so a reader diffing two cached
#: verdicts across this bump must not read that growth as a regression.
#: No pytest verdict moves: `pytest_adapter` emits one group whose argv is the
#: v14 argv, and `file_filter_matches` cannot fire there.
#: 15 -> 16: `submodules_unneeded` (round 2 item 2, 2026-09-02), and the
#: EVIDENCE SHAPE is the reason, not the new assertion. Every `submodules`
#: entry now carries `declared_unneeded` and `empty`, on every task whether or
#: not it declares the key, and a new top-level `submodules_empty_after_suite`
#: joins `submodules` and `submodules_orphaned`. None of those manifests'
#: digests move, so a warm v15 blob and a fresh v16 blob would otherwise sit in
#: one cache describing different shapes -- and a reader who cannot tell them
#: apart reads an absent field as a positive negative claim.
#: The residual is narrower and comes second: a v15 verdict on a manifest that
#: already DECLARED the key. `preflight_cache_key` runs through
#: `manifest_digest`, which hashes the manifest bytes, so adding the key
#: changes the digest and no v15 verdict can be served in the ordinary case.
#: It is reachable only because `load_task` has no top-level unknown-key
#: refusal: a manifest could have carried `submodules_unneeded` before this
#: code shipped, been gated at 15 (which ignored it and NO-GOed on `stale`),
#: and that stale NO-GO would otherwise be served forever.
#: 16 -> 17: one evidence schema (round 2 item 5, 2026-09-03). A 16 verdict is
#: missing every key measured inside the container -- from `uid` and `head`
#: through `suite_timeout_s`, the strip's two keys and the three
#: `grading_*_exit` -- because they were written where they were measured and
#: are simply absent from any path that did not measure them. Twenty of the
#: forty-two keys were in that family, and four of them (`grading_build_exit`,
#: `grading_typecheck_exit`, `grading_lint_exit`, `scope_prefixes_absent`) are
#: absent from the ordinary healthy GO verdict, which is the blob a task
#: author reads most. Absent renders identically to "written by a gate too old
#: to have this key", which is the one thing this version string exists to let
#: a reader rule out. Since 17 every verdict carries every key, `None` where
#: the gate did not look, and `early_return` names the pre-container refusal
#: rather than leaving a reader to infer it from which nulls are present.
#: NO VERDICT MOVES across this boundary: a cached 16 PASS was a PASS for the
#: same reasons and a 16 NO-GO is still a NO-GO. What is re-run is the
#: READING, not the judgement.
#:
#: 17 -> 18: the gate records how long its own bounded runs took. A verdict
#: written before this carries `suite_timeout_s` -- the bound the argv held
#: -- beside nothing at all saying what the run under it cost, so every
#: `budget.suite_timeout_s` in the task set was sized from a number measured
#: by hand in a shell, outside the image the gate runs in
#: (`~/.cache/bakeoff-probe/reports/d4-suite-timeout.md`, 2026-09-02:
#: `pytest-10210` at 106.34 s by `time docker run`, declared at 240 s). Since
#: 18 every verdict carries `bounded_run_durations_s` -- one entry per
#: bounded invocation, `null` for a run that did not happen -- and
#: `bounded_run_duration_max_s`. NO VERDICT MOVES across this boundary: a
#: cached PASS was a PASS for the same reasons and a NO-GO is still a NO-GO.
#: What is re-run is the MEASUREMENT, not the judgement, and re-running it is
#: the point.
#:
#: 18 -> 19: adds a refusal a node manifest can trip and a pytest one cannot:
#: a declared f2p or p2p id that names MORE THAN ONE test in its own file. A
#: node id is `<file>::<fullName>` with no positional index, so two
#: identically titled tests in one file are the same id -- measured
#: 2026-09-02, an exact anchored `-t` runs both (one passed and one failed in
#: the same run) and the deselection skips both, so the red-before check was
#: satisfied by whichever one fails, the green-after check by both passing,
#: and no count downstream disagreed. A cached PASS under 18 on a NODE task
#: is stale: it was taken by a gate that could not see the shape. A cached
#: NO-GO is unaffected -- nothing this version adds turns a NO-GO into a GO
#: -- and no PYTEST verdict moves at all, since `pytest_adapter.duplicate_ids`
#: is `{}` as a claim its node ids back. The new evidence key
#: `same_file_duplicate_ids` is `{}` on every verdict this version writes for
#: a task at least one of whose node runs reported, and `None` where nothing
#: counted.
#:
#: 19 -> 20: a property-based determinism check for the node frameworks
#: (round 2 item 12, 2026-09-03). Measured 2026-09-02 against fast-check
#: 3.23.2 and 2.25.0: `QualifiedParameters.readSeed` falls back to
#: `Date.now() ^ (Math.random() * 0x100000000)` when no seed is configured,
#: the package reads NO environment variable anywhere, and ten fresh vitest
#: runs of one property over unchanged code gave `0 0 0 1 1 0 0 0 0 0` -- so a
#: node task whose p2p-before sweep runs an unpinned fast-check/jest-fuzz/
#: jsverify suite was certified on whichever draw the gate happened to get.
#: Since 20, the gate scans the p2p-before run's `Outcome.files_run` -- what
#: the certified sweep actually loaded, not the declared `tests.paths` -- and
#: refuses a task where a swept file imports one of those frameworks and
#: nothing swept pins a seed with `configureGlobal(... seed: ...)`. There is
#: no `image.env` lever for this one (unlike hypothesis's `CI=1`): the one
#: external seed lever that works, `NODE_OPTIONS` preloading a module that
#: calls `fc.configureGlobal`, is defeated by jest's module registry. A
#: cached PASS under 19 on a NODE task whose sweep runs a property-based
#: suite is stale: it was taken by a gate that could not see the shape. A
#: cached NO-GO is unaffected -- nothing this version adds turns a NO-GO into
#: a GO -- and no PYTEST verdict moves at all, since `PytestAdapter.
#: property_scan` returns `None` and the block never runs. The three new
#: evidence keys (`property_framework_imported_by_suite`,
#: `property_framework_seed_pinned`, `property_scan_files`) are `None` on
#: every verdict this version writes where the node scan did not run --
#: the pre-container early return, a pytest task, or a node run whose
#: p2p-before sweep wrote no report.
#:
#: 20 -> 21: `_Runner.run`'s round-2 item 12 fix wave (impl-12-review.md
#: finding 1, 2026-09-03). The refusal 19->20 added tells an author to add
#: the framework's ignore flag to `tests.runner` -- and that remedy did not
#: work: `_Runner.run` built every check's argv as `tests.runner + <that
#: check's own suffix>`, so a manifest-declared array-valued flag (jest's
#: `--testPathIgnorePatterns`, a greedy yargs array) at the tail of
#: `tests.runner` swallowed the very next bare token -- which is the f2p
#: SELECT check's own file positional and the scoped p2p run's own scope
#: positional. Measured against `yaml-474-single-newline-empty-value` with
#: HARVESTING.md's own worked remedy applied: the f2p SELECT check reported
#: "did not RUN" (its target file became an ignore pattern instead of a
#: selection) and the scoped p2p run left `tests.paths` and ran 23 files
#: outside it. Since 21, `_Runner.run` emits `adapter.report_args` FIRST in
#: every group's argv rather than last -- both spellings
#: (`["--json", "--outputFile=…"]` / `["--reporter=json",
#: "--outputFile=…"]`) open with a token starting with `-`, which is what
#: ends a yargs array, so a trailing manifest flag can now only ever
#: swallow report args this code does not read back. A cached PASS under 20
#: on a node task whose `tests.runner` carries a trailing array-valued flag
#: is stale: it was taken by a gate whose SELECT and scoped-p2p checks
#: silently ran the wrong files. A cached NO-GO is unaffected -- nothing
#: this version adds turns a NO-GO into a GO -- and no PYTEST verdict moves
#: at all, since `PytestAdapter.report_args` is `[]` and this reorder is
#: behaviourally inert on an empty list. No evidence key is added or
#: removed; what moves is which argv every existing key was measured
#: against.
#:
#: 21 -> 22: `base_image_labels`: a verdict cached under 21 was written by a
#: gate that recorded what the interpreter answered and not what the image
#: claimed to be, so a base whose labels and interpreter disagree is
#: indistinguishable in the older file from one where they agree.
PREFLIGHT_VERSION: str = "22"


def preflight_cache_key(task, image: str, start_sha: str) -> str:
    """What a cached PASS is keyed on -- including the gate that produced it.

    `PREFLIGHT_VERSION` is in here because none of the other three components
    moves when `preflight.py` changes: a manifest digest describes the task, an
    image id describes the environment, a start sha describes the tree, and a
    new assertion touches none of them. Without the version every warm cache
    serves a verdict written by the OLD gate, and an assertion added to catch a
    defect is inert on exactly the tasks about to be run -- the pruned mirror's
    "an older revision's output is served forever" defect, one subsystem over.

    It lives HERE, beside the constant it depends on, and not in either driver.
    Two consumers now read the same caches -- `run_matrix` writes
    `preflight.json`, `grade.py` reads it and writes `preflight-grade.json` --
    and a key defined in one of them and imported by the other makes the
    collection driver a dependency of the offline grader for one f-string. Two
    COPIES would be worse still: that is how a verdict written under one gate
    gets served to another.

    The join itself moved to `_key_parts`, shared with `verdict_matches_key`,
    so the two cannot drift.
    """
    return _key_parts(task.manifest_digest, image, start_sha, PREFLIGHT_VERSION)


def _key_parts(manifest_digest: str, image: str, start_sha: str,
               preflight_version: str) -> str:
    """The ONE encoding of a preflight cache key.

    Two callers derive it from different places -- `preflight_cache_key` from
    a live task and the module constant, `verdict_matches_key` from a stored
    blob's own fields -- and a second copy of the join is how a verdict
    written under one gate gets served to another. The same rule
    `_declared_grading` and `EVIDENCE_KEYS` already follow: derive, never
    restate.
    """
    return f"{manifest_digest}|{image}|{start_sha}|{preflight_version}"


def verdict_matches_key(verdict: dict, key: str) -> bool:
    """Whether a STORED verdict blob describes what `key` names.

    `<cache>/preflight/<task_id>.json` is keyed on the task id and nothing
    else: it is overwritten by every preflight run of that task, PASS or
    NO-GO, under any manifest, image, tree or gate version -- and it is
    written BEFORE the `ok` test, while `preflight.json` is written only on
    PASS. So the two can disagree, and a reader that took seconds out of the
    blob because the FILENAME matched would publish an earlier gate's
    measurement under this run's line, which resolves and is therefore worse
    than the null it replaced.

    Reads the blob's four fields under the names `to_dict` writes them;
    the join itself is `_key_parts`, shared with `preflight_cache_key`.
    """
    return _key_parts(
        str(verdict.get("manifest_digest", "")),
        str(verdict.get("image", "")),
        str(verdict.get("start_sha", "")),
        str(verdict.get("preflight_version", "")),
    ) == key

#: A declared `tests.paths` prefix that does not exist at the post-fix state.
#: NOT a problem: `PreflightResult.ok` is `not problems`, and the grader's
#: restore step tolerates exactly this input, so a problem here would NO-GO a
#: task the ladder was built to grade. The code and the evidence keep the
#: author error loud without making it fatal.
SCOPE_PREFIX_MISSING = "scope_prefix_missing"

#: The scoped p2p run collected nothing -- pytest exit 5, or no declared
#: prefix survived the existence filter. This one IS a NO-GO, and the driver
#: branches on the code rather than on a prose prefix: closed sets, not
#: composite strings, applies to this repo's own dataclass first.
SCOPE_COLLECTS_NOTHING = "scope_collects_nothing"

#: Why the gate returned before starting a container, or `None` because it did
#: not. The set is {EARLY_RETURN_RUNNER_MISMATCH, None} and there is exactly
#: one pre-container `return PreflightResult` to name; a second one adds a
#: second constant HERE and extends `test_the_early_return_names_itself`. A
#: closed code, like `problem_codes`' members and for the same reason:
#: `problems` is prose for a human and nothing can machine-read it.
#:
#: It earns its place because under one schema "many nulls" stops being a
#: proxy for "no container ran". A node task leaves `python_observed`, both
#: `bare_runner_*` and both `hypothesis_*` null by design; an explicit-`p2p`
#: task leaves the whole scoped block null; an ordinary GO leaves every
#: `grading_*_exit` null. Four distinct null-sets, none of them this one, and
#: reconstructing the route by intersecting them is exactly the inference
#: `problem_codes` was added to stop.
EARLY_RETURN_RUNNER_MISMATCH = "runner_does_not_match_framework"

#: Every bounded invocation `preflight` can make, in the order the gate makes
#: them. The grading entries come off `tasks._GRADING_KEYS` for the reason
#: `_declared_grading` and `EVIDENCE_KEYS` both give: a hand-listed copy goes
#: stale the first time a check is added to `TaskGrading`, and that failure is
#: the silent one.
#:
#: The order is the order the gate MAKES them, which is not the order the
#: manifest declares: the bare-runner probe runs before the f2p selection, and
#: the scoped p2p runs last, after the grading commands, because the grader's
#: ladder runs build and typecheck before p2p and a grading command can write
#: into the tree.
#:
#: `bare_runner` is a COLLECTION (`--co -q`), not a suite run, and it is in
#: here anyway -- which is why these are "bounded runs" and not "suite runs".
#: It carries the same `timeout <suite_timeout_s>` prefix, it has its own exit
#: 124 branch, and a count of the gate's bounded commands that leaves one out
#: is wrong about the number the one-hour SSO window is spent on. That is the
#: same off-by-one this commit corrects in three documents.
BOUNDED_RUN_KEYS: tuple[str, ...] = (
    "bare_runner",
    "f2p_before", "p2p_before",
    "f2p_after", "p2p_after",
    *(f"grading_{key}" for key in _GRADING_KEYS),
    "p2p_scoped_after",
)


#: Every key `preflight` can write. ONE list, filled from by the early
#: return and by the full path alike, because the
#: alternative has already failed twice inside this file: `PREFLIGHT_VERSION`
#: 11 moved three keys from `[]` to `None`, and the `bare_runner_skipped` note
#: in the early-return block records a fourth "left out of this seed once" --
#: each found by review rather than by a test. A key written where it is
#: measured is ABSENT from every path that does not measure it, and absent
#: renders identically to "written by a gate too old to have this key", which
#: is the one thing `PREFLIGHT_VERSION` exists to let a reader rule out. Two
#: absences that render identically are the same defect one layer down.
#:
#: `test_evidence_keys_lists_exactly_what_preflight_writes` derives this set
#: from the source by an AST walk and is what keeps it from going stale the
#: next time an item adds a key -- the plan that introduced this constant was
#: itself two keys short against its own round's landing order, and only the
#: derivation caught it.
#:
#: The grading keys come off `tasks._GRADING_KEYS` rather than being restated,
#: for the reason `_declared_grading` gives: a hand-listed copy goes stale the
#: first time a check is added to `TaskGrading`, and that failure is the
#: silent one.
#:
#: A tuple, not a frozenset, GROUPED the way the gate's narrative runs --
#: what is knowable before a container starts, then what each stage
#: measures as it runs. This is membership, not write order: the tuple
#: carries forward the old hand-written seed block's grouping, and 113
#: pairwise inversions against each key's actual first-write line (measured
#: 2026-09-03) confirm it is not a write order. It does NOT survive to the
#: stored artifact either: `matrix.write_json`, which writes the blob,
#: dumps with `sort_keys=True` (verified 2026-09-03 against the click-3360
#: blob, which is alphabetical from `ambiguous_file_filters`), so the file a
#: task author diffs is sorted whatever this order is. Every comparison
#: against this tuple is on sets, so order is presentation at both ends and
#: nothing depends on it. A new key is APPENDED to its group -- the AST
#: test above proves membership, not position.
EVIDENCE_KEYS: tuple[str, ...] = (
    "early_return", "framework",
    "image_env_declared", "image_env_observed", "image_env_mismatch",
    "hypothesis_importable", "hypothesis_imported_by_suite",
    "python_declared", "python_observed",
    #: Read HOST-side and needing no container, yet deliberately written
    #: beside the interpreter probe rather than earlier where it could be:
    #: reading it before the runner-gate early return would leave that path
    #: carrying a label observation beside a `python_observed: None` -- one
    #: evidence family answering on a path the other cannot, which is the
    #: asymmetry a reader diffing two verdicts cannot resolve.
    "base_image_labels",
    "submodules", "submodules_orphaned", "submodules_empty_after_suite",
    "runner_cache_flags", "runner_cache_flags_missing",
    "bare_runner_argv", "bare_runner_exit", "bare_runner_skipped",
    "uid", "claude_version", "head",
    "stripped_paths", "stripped_paths_present",
    "suite_timeout_s",
    # Listed beside the bound rather than at the sites they are filled from,
    # because the only reading either supports is against that bound.
    "bounded_run_durations_s", "bounded_run_duration_max_s",
    "f2p_before_exit", "f2p_before_not_run",
    "f2p_collection_errors", "f2p_red_kind",
    "p2p_before_ignored", "p2p_before_exit",
    "dirty_after_tests",
    "f2p_after_exit", "p2p_after_exit",
    *(f"grading_{key}_exit" for key in _GRADING_KEYS),
    "scope_prefixes", "scope_prefixes_absent", "p2p_scoped_after_exit",
    "duplicate_full_names", "scope_files_run", "scope_files_outside",
    "ambiguous_file_filters", "same_file_duplicate_ids",
    "property_framework_imported_by_suite", "property_framework_seed_pinned",
    "property_scan_files",
)


def _evidence_seed() -> dict:
    """The schema, with every key at "the gate did not look".

    One rule, stated once: `None` is "the gate did not look", and every key
    that means anything else overwrites it where it is measured.

    Every key starts `None`, INCLUDING the ones given a real value in the next
    few statements of `preflight` (`framework`, `image_env_declared`,
    `python_declared`, and the two cache-flag keys). Do not hand-seed those
    here: a seed carrying real values for some keys and `None` for the rest is
    the two-family split rebuilt by hand, which is the thing this constant
    replaced.

    Not a sentinel string. Four of these keys are string-typed
    (`dirty_after_tests`, `claude_version`, `f2p_red_kind`,
    `bare_runner_skipped`), so a sentinel would be indistinguishable from a
    measurement on exactly them. Not a `collections.defaultdict` either: that
    hides the drift instead of failing it, and a key nothing ever touches
    still does not appear in `to_dict()` -- which leaves the reader this
    schema exists for exactly where they started.

    Every key is seeded to the absence ITS OWN schema defines: `None`
    for a scalar, and for `bounded_run_durations_s` -- whose value is a key
    set -- the all-null dict, because "this run did not happen" has to be a
    null inside that dict and never a missing entry.

    The inner dict is built HERE, per call, and never a module-level
    constant: a shared mutable would alias one dict across every
    `PreflightResult` in the process, and one gate's measurements would
    appear in the next gate's verdict.
    """
    seed = dict.fromkeys(EVIDENCE_KEYS)
    seed["bounded_run_durations_s"] = dict.fromkeys(BOUNDED_RUN_KEYS)
    return seed


# Files that would give one task a different agent context from another, and
# would do it invisibly: section 5.2 pins the session config precisely because
# CLAUDE.md and friends substantially change agent behaviour.
_CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md", ".claude", ".cursorrules")

#: Fix 2's four known addopts flags, mapped to the plugin `image.pip` is
#: missing when a bare pytest invocation cannot even parse them. Deliberately
#: not a general flag-to-plugin parser -- that would be a second, private
#: copy of pytest's own plugin registry, going stale the moment a task's
#: addopts names a fifth plugin. Matched as a substring rather than as a
#: token, because argparse's own message quotes the flag WITH its `=value`
#: (`unrecognized arguments: --numprocesses=auto`).
_BARE_RUNNER_PLUGIN_HINTS = (
    ("--numprocesses", "pytest-xdist"),
    ("--cov", "pytest-cov"),
    ("--timeout", "pytest-timeout"),
    ("--hypothesis-profile", "hypothesis"),
)


def _bare_runner_plugin_hint(stderr: str) -> str:
    """Which package `image.pip` is missing, or the honest fallback.

    Takes the WHOLE stderr, not one line -- measured against the real image
    2026-09-02, argparse's own `error()` prints the usage banner as line 1
    and the flag it actually choked on as line 2
    (`python -m pytest: error: unrecognized arguments: --numprocesses=auto`),
    so a lookup keyed on line 1 alone finds nothing on the exact task fix-2
    was measured against.

    Named after the one flag fix-2 was measured against
    (`bidict-389-putall-rollback-clean`'s `--numprocesses=auto`, pytest-xdist)
    and widened to the three other addopts flags whose absence gives the
    identical usage-error shape. Anything else is real but unmapped, and
    "a pytest plugin" says that honestly rather than guessing a package name
    this function has no way to know.
    """
    for flag, package in _BARE_RUNNER_PLUGIN_HINTS:
        if flag in stderr:
            return package
    return "a pytest plugin"


@dataclass(frozen=True)
class PreflightResult:
    task_id: str
    task_version: int
    start_sha: str
    image: str
    manifest_digest: str
    problems: tuple[str, ...] = ()
    #: The schema, never a subset of it. Defaulted to the seed rather than to
    #: `{}` so a hand construction -- `grade.py`'s three, and any future one --
    #: satisfies `__post_init__` without knowing the key set exists.
    evidence: dict = field(default_factory=_evidence_seed)
    #: Which gate produced this verdict. A stored verdict outlives the code
    #: that wrote it, and every grade copies this as
    #: `graded_under_preflight_version` -- discrimination is preflight's
    #: claim, so a grade that requires it has to name the gate that made it.
    preflight_version: str = ""
    #: A typed channel beside the prose `problems`, for the outcomes a caller
    #: has to BRANCH on. Nothing machine-reads `problems`; the grade driver
    #: would have been the first, through a string-prefix match no test can
    #: really guard. Not every code is a problem -- see SCOPE_PREFIX_MISSING.
    problem_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an evidence dict that is not the schema, in both directions.

        Here rather than at the two `return` sites because it then covers both
        of them and every future one with no call-site discipline -- and
        call-site discipline is precisely what has already failed twice inside
        this file (`PREFLIGHT_VERSION` 11's three keys, and the
        `bare_runner_skipped` note's "left out of this seed once"). In a
        function where a third `return` is cheap to add, that is stronger than
        it looks.

        A RAISE, where `_gitlink_paths` argues the opposite for its own
        failure, and the difference is what causes it. That one guards against
        a defect in the DATA -- an unreadable index in some task's tree -- which
        an operator can hit and which must therefore reach the driver as a
        NO-GO rather than as a traceback. This one can only be caused by an
        edit to `preflight` itself: with the seed in place every key in
        `evidence` is either seeded or an overwrite of a seeded key, and the
        one input-dependent write (`grading_<key>_exit`) takes its names from
        `dataclass_fields(task.grading)`, which `load_task`'s unknown-key
        refusal closes and which raises `TypeError` inside `_declared_grading`
        first for anything that is not a dataclass at all. No task input
        reaches the key set, so a traceback in a developer's own run is the
        right answer and cannot turn a task NO-GO into a crash.

        `ValueError` and not a named exception: `TaskError`, `ContainerError`
        and `UnknownModelError` all exist because something CATCHES them, and
        nothing may catch this one -- a caught schema error is the drift being
        read past again.
        """
        if set(self.evidence) != set(EVIDENCE_KEYS):
            # Both directions are named because the remedies differ: a MISSING
            # key means a path that builds the result by hand, an UNLISTED one
            # means a write that was never added to the tuple.
            raise ValueError(
                "preflight evidence is not the schema: missing "
                f"{sorted(set(EVIDENCE_KEYS) - set(self.evidence))}, unlisted "
                f"{sorted(set(self.evidence) - set(EVIDENCE_KEYS))}. Every key "
                "the gate can write is seeded from EVIDENCE_KEYS so that 'the "
                "gate did not look' is a null and never an absent key; a key "
                "written on one path and not another cannot be read across a "
                "set of cached verdicts, which outlive the code that wrote "
                "them."
            )
        # AFTER the outer key-set check above, which is what guarantees this
        # key exists at all before these two clauses read it -- one layer
        # down, where a nested value is exactly where the outer check stops
        # being enforced.
        durations = self.evidence["bounded_run_durations_s"]
        if not isinstance(durations, dict):
            raise ValueError(
                "preflight evidence['bounded_run_durations_s'] is "
                f"{type(durations).__name__}, not a dict of "
                f"{len(BOUNDED_RUN_KEYS)} bounded runs. `_evidence_seed` fills "
                "the shape on every path, including the pre-container early "
                "return, so a non-dict here is a caller that built `evidence` "
                "some other way."
            )
        if set(durations) != set(BOUNDED_RUN_KEYS):
            raise ValueError(
                "preflight evidence['bounded_run_durations_s'] is not the "
                f"schema: missing "
                f"{sorted(set(BOUNDED_RUN_KEYS) - set(durations))}, unlisted "
                f"{sorted(set(durations) - set(BOUNDED_RUN_KEYS))}. Every "
                "bounded invocation the gate can make is seeded from "
                "BOUNDED_RUN_KEYS so that 'this run did not happen' is a "
                "null and never an absent key -- the same rule the outer "
                "schema follows, one layer down, where a nested dict is "
                "exactly where it stops being enforced."
            )

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_version": self.task_version,
            "start_sha": self.start_sha,
            "image": self.image,
            "manifest_digest": self.manifest_digest,
            "problems": list(self.problems),
            "problem_codes": list(self.problem_codes),
            "evidence": self.evidence,
            "preflight_version": self.preflight_version,
            "ok": self.ok,
        }


def _present(container, names, *, dangling_counts: bool = False) -> list[str]:
    """Which of `names` exist in the container's tree, in declared order.

    `test -e` FOLLOWS symlinks, which is right for a context file -- sqlglot's
    CLAUDE.md is a symlink to AGENTS.md, and a DANGLING link named `.claude`
    gives the agent no context at all.

    `dangling_counts` adds a `-L` probe, and the strip assertion needs it for
    the opposite reason: its claim is "this path is gone from the tree", and a
    link whose target was stripped is a path the agent's `ls` still shows.
    Measured 2026-09-01: for a symlink to a missing target, `[ -e x ]` exits 1
    and `[ -L x ]` exits 0.

    One helper for every caller -- the context files, the scope filter (used
    by preflight and by the grader's check 6 alike) and the strip -- because a
    second copy of this probe is where the symlink semantics drift.
    """
    probes = ["-e", "-L"] if dangling_counts else ["-e"]
    return [
        name for name in names
        if any(container.exec(["test", probe, name]).exit_code == 0
               for probe in probes)
    ]


def _rg_probe(container, pattern: str, scanned: list[str], problems: list[str],
              what: str, consequence: str, *,
              multiline: bool = False) -> bool | None:
    """Three-valued `rg -q` over `scanned`. `None` is "could not answer".

    0 is a match, 1 is no match, and anything else -- an unreadable path, a bad
    pattern, no rg -- is UNKNOWN. A quiet boolean there silently disarms the
    caller, which is the whole reason this is not a `bool`.

    UNKNOWN is not silent either: `scanned` is non-empty at every call site, so
    the probe RAN and did not answer, and that is a controller-ruling ambiguity
    rather than the "never ran" shape an empty `scanned` gives. The two would
    render identically as `None` in `evidence`, so this appends a problem
    naming the argv and the exit code.

    `consequence` is the caller's own closing sentence rather than one sentence
    shared by all three. The hypothesis probe's misreading disarms a check; the
    seed-pin probe's misreading REFUSES a task that was already fine. One
    sentence covering both would be wrong for one of them, and the hypothesis
    caller's text is passed verbatim so that message stays byte-identical --
    `test_the_hypothesis_rg_message_is_byte_identical_after_the_extraction`
    pins it.

    `--` before the paths: an entry starting with `-` would otherwise parse as
    an rg flag, turning a scan target into a silent argv change.

    INHERITED DEFAULTS, stated once here rather than rediscovered per caller:
    `rg` without `--no-ignore` honours `.gitignore`/`.ignore` and skips hidden
    files, so a gitignored test file is not scanned -- the SILENT direction for
    an import probe. Left as-is because it is the behaviour the hypothesis
    probe has always had, and changing it here would change that probe too;
    a change must move both, deliberately.
    """
    argv = ["rg", "-q"] + (["-U"] if multiline else []) + [pattern, "--",
                                                           *scanned]
    probe = container.exec(argv)
    if probe.exit_code == 0:
        return True
    if probe.exit_code == 1:
        return False
    problems.append(
        f"the {what} could not answer: `" + " ".join(argv)
        + f"` exited {probe.exit_code}, "
        "not 0 (match) or 1 (no match). rg exits 2 on an "
        "unreadable path or a bad pattern and it is asserted "
        "present above, so this names an environment problem "
        "preflight cannot see through -- "
        + consequence
    )
    return None


def _observed_env(container, keys: tuple[str, ...]) -> dict[str, str | None]:
    """What the container's environment actually holds, per declared key.

    `printenv`, never `sh -c 'echo $KEY'`, because the exit code is the whole
    discriminator: measured 2026-09-01, `printenv KEY` exits 0 with the value
    even when that value is the empty string, and exits 1 with empty stdout
    when the key is unset. `echo` cannot tell those apart, and two different
    absences that render identically are the same defect one layer down.
    `None` here means "not set"; `""` means "set to nothing".

    Only the DECLARED keys are read. A dump of the whole environment would put
    values nobody asked about into `preflight.json`, which outlives the run
    and is read by hand.
    """
    observed: dict[str, str | None] = {}
    for key in keys:
        result = container.exec(["printenv", key])
        observed[key] = (
            result.stdout.rstrip("\n") if result.exit_code == 0 else None
        )
    return observed


def _declared_env(task) -> dict[str, str]:
    """The manifest's `image.env`, or {}.

    `getattr` twice, like `_declared_grading`'s and the strip's: this module's
    entry point takes an untyped `task`, and a manifest object predating the
    key must not crash the gate.
    """
    return dict(getattr(getattr(task, "image", None), "env", {}) or {})


#: `Python 3.11.16` -> the leading two components. Anchored, and the trailing
#: group is deliberately not required to be numeric-only: measured shapes
#: include `3.13.0rc1`.
_PYTHON_VERSION_LINE = re.compile(r"^Python\s+(\d+)\.(\d+)(?:\.|\s|$)")


def _parse_python_version(raw: str) -> str:
    """`"Python 3.11.16"` -> `"3.11"`. `""` when it cannot be read.

    Major.minor, compared for EQUALITY, and both halves of that are load
    bearing.

    Not a prefix test: measured, `"3.13.15".startswith("3.1")` is True and so
    is `"Python 3.13.15".startswith("Python 3.1")`, so a `startswith`
    read-back accepts 3.13 for a manifest declaring 3.1 -- a green gate over
    the wrong interpreter, which no later stage re-derives.

    Not the whole string either: the manifest carries no patch level and must
    not have to. The upstream tag is republished with security fixes, so a
    task pinned to 3.11.16 would NO-GO the day 3.11.17 ships.

    `""` rather than a raise: this is a gate that COLLECTS problems, and an
    unparseable answer is reported as the mismatch it is alongside whatever
    else is wrong, not as a traceback that hides the other four.
    """
    match = _PYTHON_VERSION_LINE.match(raw.strip())
    return f"{match.group(1)}.{match.group(2)}" if match else ""


def _declared_python(task) -> str:
    """The manifest's `image.python`, or the base Dockerfile's default.

    `getattr` twice, like `_declared_env`'s: this module's entry point takes an
    untyped `task`, and a manifest object predating the key must not crash the
    gate.

    The fallback is IMPORTED, never restated. TWO copies of the default exist --
    the Dockerfile's `ARG BASE_PYTHON_VERSION` and `tasks._DEFAULT_PYTHON` --
    and they are pinned equal by
    tests/test_images.py::test_every_copy_of_the_default_version_says_the_same_thing.
    This module restates neither; a third copy here would be the only unpinned
    one, and it would sit in the gate that exists to catch exactly this class of
    disagreement. (There were three: `images._DEFAULT_PYTHON` was the other, and
    round 2 item 15 removed it -- it had no reader but its own pin.)
    """
    return getattr(getattr(task, "image", None), "python", "") or _DEFAULT_PYTHON


def _existing_prefixes(container, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """The declared prefixes that exist in the tree, in declared order.

    The same filter the grader's check 6 applies, and it has to be the same
    one: an unfiltered positional prefix makes pytest exit 4 (usage error) on
    exactly the input the grader's restore step already tolerates -- a
    declared path absent at the start state, which `_validate_prefixes`
    accepts on purpose. Filtered here and unfiltered there, or the reverse,
    and the gated argv is not the graded argv.
    """
    return tuple(_present(container, prefixes))


def _parse_submodule_status(
    out: str, paths: tuple[str, ...]
) -> tuple[list[dict], list[str]]:
    """`git submodule status`, one dict per line, paths taken from `paths`.

    The FIRST CHARACTER is the verdict and the other two values are context.
    Measured 2026-09-01 (git 2.50.1, and confirmed at 2.54.0 inside the
    container): a leading space means the submodule is initialised AND its HEAD
    equals the index gitlink; `-` means uninitialised, which includes the empty
    directory `git clone --local` leaves behind and the state a transient
    `-c submodule.<name>.url=` produces; `+` means initialised at a different
    commit.

    `paths` is the AUTHORITATIVE path set, from `git ls-files -s -z`'s 160000
    entries. The status line is human-readable -- `<char><sha> <path>[
    (describe)]` -- with no `-z` form and no escaping, so splitting it on `" ("`
    mis-parses any path containing those bytes. Same rule as `_chunk_path`:
    paths come from git, never from a regex over a display line. The line is
    used for its FIRST CHARACTER and nothing else.

    Measured 2026-09-01 on the run tree `materialize` builds: the describe
    suffix is `(heads/main)`, not a detached-HEAD sha, because the pruned
    mirror publishes `refs/heads/main` at the gitlink. Nothing here reads it.

    This is an OBSERVATION, not a restatement of the manifest: the expected sha
    is never passed in, it is the index gitlink git compares against on its own.
    That is what makes the check catch a tree emptied after `materialize`'s own
    post-condition passed.

    Returns `(parsed, unmatched)`. An `unmatched` line is one `git ls-files`
    has no gitlink for, which means the two readers disagree about this tree --
    a state nothing here can interpret. It is reported as a problem rather than
    resolved by falling back to `tail.strip()`: that fallback would put a
    DISPLAY-derived path back into the evidence, which is the exact thing
    taking paths from `ls-files` exists to prevent.

    THE MATCH IS BOUNDARY-ANCHORED, not a bare `startswith`, and that is a
    correction. Measured: the line ` <sha> vendor/libdep (heads/main)` against
    an index whose only gitlink is `vendor/lib` satisfies a bare `startswith`,
    so the entry is filed under `vendor/lib` -- a path that IS in the
    authoritative set, so nothing downstream can tell it is the wrong one, and
    `stale` then names a submodule the line was never about. ` <sha> vendorx`
    against `vendor` is the same defect with no separator at all. The suffix
    git appends is always space-delimited (`" (heads/main)"`, measured), so
    equality-or-`path + " "` admits every real line and neither sibling.
    `max(key=len)` stays as belt-and-braces for a tree carrying both
    `vendor/lib` and `vendor/lib dep`, where two entries can still match one
    line; it is no longer the thing keeping a sibling out.

    Each entry carries the RAW MARKER beside `initialised`, because that
    boolean collapses two states an operator has to tell apart: `-` is an
    empty or uninitialised directory (the suite imports nothing) and `+` is an
    initialised submodule at the WRONG commit (the suite imports content that
    is not `base_sha`'s, and passes or fails for reasons that are not the
    model's). Both are NO-GO and the remedy differs, so the record says which.
    """
    parsed, unmatched = [], []
    for line in out.splitlines():
        if not line.strip():
            continue
        marker, rest = line[0], line[1:]
        sha, _, tail = rest.partition(" ")
        match = [path for path in paths
                 if tail == path or tail.startswith(path + " ")]
        if not match:
            unmatched.append(line)
            continue
        parsed.append({
            "path": max(match, key=len),
            "sha": sha,
            "initialised": marker == " ",
            "marker": marker,
        })
    return parsed, unmatched


def _gitlink_paths(container) -> tuple[str, ...] | None:
    """Every 160000 entry in the container's index. `None` if it could not be read.

    `container.exec` and an explicit `exit_code` branch, NOT `_checked_exec` --
    and the reason is the gate's contract, not a style preference.
    `_checked_exec` raises `ContainerError`, and `preflight` COLLECTS problems
    and returns a `PreflightResult`; `run_matrix`'s `preflight(...)` call is not
    wrapped, so a raise from in here would surface as a traceback instead of the
    NO-GO the driver knows how to handle. (It also returns an `ExecResult`, not
    a `str`, so `.split` on it would be an `AttributeError` rather than the read
    this needs.)

    The exit code still has to be looked at, for the reason `_checked_exec`
    exists at all: an empty stdout from a failed `ls-files` is byte-identical
    to a repository with no submodules. `None` here means "not read"; the
    caller turns that into a problem plus `None` evidence, the same shape the
    `git submodule status` branch uses.

    `partition`, never `split("\t", 1)[1]`. A record beginning `160000 ` with
    no TAB in it makes the indexed form raise `IndexError` -- an exception
    this function has no contract for, escaping a gate whose caller does not
    wrap it, which is the traceback-instead-of-NO-GO failure the paragraph
    above exists to prevent. A record that yields no path contributes none,
    and that is not a silent drop: the matching `git submodule status` line
    then finds nothing to match, lands in `unmatched`, and is reported as the
    two readers disagreeing -- which is what a record neither reader can name
    IS.
    """
    result = container.exec(["git", "ls-files", "-s", "-z"])
    if result.exit_code != 0:
        return None
    paths = []
    for record in result.stdout.split("\0"):
        if record.startswith("160000 "):
            _meta, _tab, path = record.partition("\t")
            if path:
                paths.append(path)
    return tuple(paths)


def _directory_is_empty(container, path: str) -> bool | None:
    """Whether `path` in the container is an existing, EMPTY directory.

    `None` is "could not be read", never `False`. `container.exec` with an
    explicit exit-code branch rather than `_checked_exec`, for the reason
    `_gitlink_paths` documents: this gate COLLECTS problems and its caller
    does not wrap it, so a raise here is a traceback instead of a NO-GO.

    This exists because git is BLIND here. Measured 2026-09-02 (git 2.50.1):
    a file inside an UNINITIALISED submodule directory is reported by neither
    `git status --porcelain`, nor `-uall`, nor `git ls-files -o`, nor
    `git add -A` followed by `git diff --cached <base_sha>` (zero bytes) --
    git does not descend into a gitlink path in any state. So the clean-tree
    check next door cannot see a suite that writes in there, and the only
    reader that can is the filesystem.

    `ls -A` answers both halves in one exec: a non-zero exit is "absent or
    unreadable" and empty stdout at exit 0 is "present and empty". The exit
    code is not compared against a literal -- a missing directory and a
    permission error are both answers this gate cannot interpret. `--` guards
    a path beginning with a dash, and `container.exec` runs with
    `workdir=REPO_MOUNT`, so a repo-relative path resolves.
    """
    result = container.exec(["ls", "-A", "--", path])
    if result.exit_code != 0:
        return None
    return not result.stdout.strip()


def _declared_grading(task) -> list[tuple[str, tuple[str, ...]]]:
    """The non-empty `grading.*` argvs, keyed by check name.

    The keys come off the dataclass rather than a hand-written list here, for
    the reason `tasks._GRADING_KEYS` gives: a copy goes stale the first time a
    check is added, and its failure is the silent one -- the new key is
    accepted by the loader and never validated by the gate.
    """
    grading = getattr(task, "grading", None)
    if grading is None:
        return []
    return [
        (spec.name, tuple(getattr(grading, spec.name)))
        for spec in dataclass_fields(grading)
        if getattr(grading, spec.name)
    ]


def _elapsed_s(result) -> float:
    """The wall-clock seconds one bounded invocation took, off its own result.

    Measured by `RunContainer.exec` with `time.monotonic()` around
    `exec_run` ON THE HOST, so it INCLUDES the docker exec round trip and
    the demux of both streams. That is the right number and not an
    approximation of a better one: `budget.suite_timeout_s` bounds a
    `timeout` INSIDE the container, but what the one-hour SSO window is
    spent on -- and what an operator waits for -- is the host-side interval,
    and sizing a bound against the smaller number is how a suite that fits
    the gate is killed under the grader.

    Not a `time.monotonic()` taken here. `CheckResult.duration_s` is already
    built from this field one subsystem over (`grader._timing`), and a second
    clock around the same call is a second thing that can be wrong about one
    interval.

    Attribute access, NOT `getattr(result, "duration_ms", None)` --
    which is what `grader._timing` does, for a reason that does not apply
    here. That helper is called with `result=None` on the rungs that ran no
    command; every call site below holds a real `ExecResult`, and a default
    there would let a stub that forgot the field record `None` as though it
    were a measurement.
    """
    return result.duration_ms / 1000.0


#: Exit codes that OUTRANK an ordinary test failure when one check is several
#: commands. `grader._check_f2p`, `_check_p2p` and `_check_command` branch on
#: 124 (`grader._TIMEOUT_EXIT`) and on 125/126/127/137 (`grader._INFRA_EXITS`)
#: BEFORE they read a failure -- and group 0 of a node deselect-branch run
#: exits 1 whenever its post-exclusion scope is empty (measured 2026-09-02,
#: both frameworks, with a report of zero tests). A "first non-zero wins"
#: merge would therefore report 1 for a check whose SECOND command was killed
#: at the bound, and `timed_out` -- a distinct recorded fact -- becomes
#: unrepresentable for every multi-group node check. Spelled here rather than
#: imported: preflight importing grader.py would be a dependency in the wrong
#: direction, and the two consumers are named above so a reader can check the
#: pair.
_OUTRANKING_EXITS: tuple[int, ...] = (124, 137, 127, 126, 125)


def _argv_lines(runner: "_Runner") -> str:
    """Every command that check ran, one per line, indented.

    Every group, not just the first: a node p2p check is 1 + K commands and a
    message showing one of them names a command that is not the whole of what
    happened -- which is the reconstruction `last_argvs` exists to avoid.
    """
    return "".join(f"  {' '.join(argv)}\n" for argv in runner.last_argvs)


def _merge_exit_codes(codes: list[int]) -> int:
    """One check's exit code, out of its groups', by RANK and not by position.

    An outranking code anywhere wins, in `_OUTRANKING_EXITS` order; then the
    first non-zero in group order; then 0. Position alone would report the
    ordinary 1 that an emptied group-0 scope produces for a check whose
    second command was killed at the suite bound.
    """
    for outranking in _OUTRANKING_EXITS:
        if outranking in codes:
            return outranking
    for code in codes:
        if code != 0:
            return code
    return 0


@dataclass(frozen=True)
class _MergedResult:
    """Several commands' results, as the one result a check reads.

    Shaped like `container.ExecResult` and deliberately not that class: this
    is a value no invocation produced, and a reader who finds it in a
    traceback should be told so by its name. Built only when a check ran more
    than one command; a single group returns the container's own object.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class _Runner:
    """Test invocations inside the container, always under a timeout.

    `RunContainer.exec` blocks with no timeout of its own, so a suite that
    hangs would hang the gate that runs before every matrix. coreutils
    `timeout` is in the base image for exactly this -- Docker offers no way
    to kill a running exec from outside.
    """

    def __init__(self, container: RunContainer, runner: tuple[str, ...],
                 timeout_s: int, adapter=_PYTEST_ADAPTER):
        self.container = container
        self.runner = list(runner)
        self.timeout_s = timeout_s
        #: What "the tests failed" MEANS for this task's framework. Every
        #: caller passes it explicitly and nothing may rely on the default:
        #: once a manifest can declare `tests.framework` (broadening 7 Task
        #: 4), a call site left on it classifies a jest run with pytest's
        #: exit codes -- where 1 is what a config error, an import error and
        #: a failing assertion all return alike -- and stamps a model failure
        #: on an environment defect, permanently, in an append-only store.
        #:
        #: The default exists for exactly one reason, and it is not a
        #: convenience: `test_grading_p2p_with_no_extras_is_the_argv_
        #: preflight_validated` is the argv-identity gate on this refactor,
        #: it constructs a `_Runner` with three positional arguments, and it
        #: may not be edited -- a gate that is rewritten to accommodate the
        #: change it gates has stopped gating anything.
        self.adapter = adapter
        #: Every argv of the most recent CHECK, in group order, so a problem
        #: can name what was actually run rather than a reconstruction of it
        #: -- a second copy of the branch logic is a second thing that can be
        #: wrong about what happened, inside the check that exists to be right
        #: about it. A list of lists since a node check is 1 + K commands;
        #: `last_argv` below is the first of them.
        #:
        #: Initialised HERE, at construction, for the reason the rest of this
        #: file initialises its absences: a field that appears on first use
        #: raises `AttributeError` out of `last_timeout_s` on a path where no
        #: command ever ran.
        self.last_argvs: list[list[str]] = []
        #: What the last invocation asked for BY ID, so `classify` can report
        #: which of those ids never ran -- only the caller knows this, because
        #: a report cannot tell a test that was SKIPPED from one that was never
        #: selected.
        #:
        #: Written by `select` and by `pass_to_pass`'s explicit branch -- minus
        #: what the same argv deselects, since a deselected id was asked NOT to
        #: run -- and reset to `()` by its DESELECT branch. That reset is
        #: load-bearing rather than tidy: a leftover value there would make a
        #: correct scoped run report the f2p ids as "did not run", which is
        #: exactly what a correct p2p run does to them.
        #:
        #: Initialised HERE, at construction, for the reason the rest of this
        #: file initialises its absences: a field that appears on first use
        #: raises `AttributeError` out of `classify` on the deselect branch,
        #: which never selects by id -- a crash on the path that is meant to
        #: report an absence.
        self._selected: tuple[str, ...] = ()
        #: The report the last invocation wrote, parsed, or `None`. Read back
        #: through `cat` because the file lives in the container's /tmp, which
        #: is never bind-mounted -- deliberately, so it cannot reach a
        #: submission diff.
        self.last_report: dict | None = None

    def run(self, argv_groups: list[list[str]]):
        """Run every group of ONE check, and answer as that check.

        A node selection is one command per file and a node deselection is
        1 + K of them, because `-t` matches `fullName` and no flag pairs a
        name pattern with a file. pytest emits exactly one group, whose argv
        is the one it always emitted.

        An EMPTY sequence raises rather than falling through to the bare
        runner: `select_argvs(())` is `[]`, and running the runner with no
        filter at all would execute the WHOLE SUITE as the selection and
        classify it as one. A FLAT argv raises too -- the old signature's
        shape reaching this one iterates a list of strings and splats each
        into a command made of letters, which does not fail loudly.
        """
        if not argv_groups:
            raise ValueError(
                "a suite check needs at least one argv group; an empty "
                "sequence is what an empty selection produces, and running "
                "the bare runner for it would execute the whole suite as the "
                "selection"
            )
        if any(isinstance(group, str) for group in argv_groups):
            raise TypeError(
                "run() takes a SEQUENCE of argvs, not one argv: a flat list "
                "of strings would be splatted character by character into a "
                "command made of letters"
            )
        report_path = self.adapter.report_path()
        self.last_argvs = []
        results, reports = [], []
        for extra in argv_groups:
            if report_path:
                # Deleted BEFORE the measured command, as a separate exec,
                # because a config error writes no report at all (measured
                # 2026-09-01, vitest 3.2.7 and jest 30.5.0 both) -- so a
                # leftover file from the previous invocation would stand in as
                # this run's evidence. The path is FIXED rather than
                # per-invocation because it is an argv element and the gated
                # argv must equal the graded argv; the rm is what makes a
                # fixed name safe. Per GROUP, not per check: group 1 must not
                # inherit group 0's report either.
                self.container.exec(["rm", "-f", report_path])
                # FIRST, not last (round 2 item 12's fix wave, 2026-09-03,
                # impl-12-review.md finding 1). `self.runner` is
                # manifest-declared and may end in a trailing array-valued
                # flag -- jest's `--testPathIgnorePatterns` is a documented
                # greedy yargs array that swallows the next bare token, and
                # HARVESTING.md's own worked remedy puts one there. Measured:
                # with the report args LAST, that bare token was `extra`'s own
                # leading file positional, so the check's own selection became
                # another ignore pattern instead -- the f2p SELECT check ran
                # 23 files with the target excluded ("did not RUN", 3279
                # pending) and the scoped p2p run left `tests.paths` outright.
                # `report_args` -- `["--json", "--outputFile=…"]` (jest) /
                # `["--reporter=json", "--outputFile=…"]` (vitest) -- always
                # opens with a token starting with `-`, which is exactly what
                # ends a yargs array; that is the same correctness rule
                # `node_adapter.p2p_argvs`'s own comment already states for
                # the groups THAT adapter builds internally. Emitting it first
                # means every group's argv opens with `-`, so a manifest's
                # trailing array flag can only ever swallow report args this
                # code does not need back -- never the check's own suffix.
                # `--` was measured and rejected: after it yargs stops
                # parsing, so a group's own `-t` becomes another OR'd path
                # pattern instead of a name filter (measured: "Ran all test
                # suites matching <path>|-t|<pattern>").
                extra = [*self.adapter.report_args(report_path), *extra]
            argv = ["timeout", str(self.timeout_s), *self.runner, *extra]
            self.last_argvs.append(argv)
            results.append(self.container.exec(argv))
            reports.append(self._read_report(report_path))
        self.last_report = (
            self.adapter.merge_reports(reports) if report_path else None)
        if len(results) == 1:
            return results[0]
        return _MergedResult(
            exit_code=_merge_exit_codes([r.exit_code for r in results]),
            stdout="\n".join(r.stdout for r in results),
            stderr="\n".join(r.stderr for r in results),
            # `getattr` with a default because a test double may answer a
            # result object without it, and a missing duration may not take
            # the classification of a real suite run down with it.
            duration_ms=sum(getattr(r, "duration_ms", 0) for r in results),
        )

    def _read_report(self, report_path: str | None) -> dict | None:
        """The parsed report, or `None` -- which is a measurement, not a gap.

        `None` means the runner produced no machine-readable evidence, and
        `classify` turns that into an ENVIRONMENT outcome. That is the correct
        reading of a config error, of a runner that could not start, and of a
        report this code could not parse: in all three the command did not say
        what it did, and reading silence as success is the Phase 0c failure.
        """
        if not report_path:
            return None
        read = self.container.exec(["cat", report_path])
        if read.exit_code != 0:
            return None
        try:
            return json.loads(read.stdout)
        except (ValueError, TypeError):
            return None

    def classify(self, result) -> Outcome:
        """What that invocation did, in terms no framework owns.

        Asked of the ADAPTER rather than read off the exit code, because the
        exit code is pytest's answer and only pytest's: measured 2026-09-01,
        vitest 3.2.7 and jest 30.5.0 both exit 1 for a failing test, an
        unresolvable import, a syntax error, a nonexistent file argument and a
        broken config alike. Never a comparison against the manifest -- the
        confinement equality below is preflight's own claim to make.

        `not_run` is filled HERE and not by the adapter, because only the
        caller knows what it asked for: in the report a test that was skipped
        and a test that was never selected are the same shape. `Outcome` is
        frozen, so the answer is a replaced copy. It stays empty on pytest --
        `report_path()` is `None` there, so `last_report` never leaves `None`
        -- which is honest, because pytest answers this with exit 4 and an
        `ERROR: not found:` line rather than with the 0 and the all-skipped
        report both node frameworks give (measured).
        """
        outcome = self.adapter.classify(
            exit_code=result.exit_code, stdout=result.stdout,
            stderr=result.stderr, report=self.last_report,
        )
        if self._selected and self.last_report is not None:
            missing = verify_selected(
                self.last_report, self._selected, self.adapter)
            if missing:
                outcome = dataclasses.replace(outcome, not_run=missing)
        return outcome

    @property
    def last_argv(self) -> list[str]:
        """The FIRST group's argv, or `[]` when nothing has run.

        Read-only and derived, so the two cannot drift: every group of one
        check carries the same `timeout` prefix and the same runner, which is
        what lets `last_timeout_s` read the first and answer for the check.
        A problem message that wants to show the command prints every group
        instead -- `last_argvs` -- because on a node check one of them is not
        the whole story.
        """
        return self.last_argvs[0] if self.last_argvs else []

    @property
    def last_timeout_s(self) -> int | None:
        """The bound the last invocation actually CARRIED, off its own argv.

        Preflight's evidence and the grader's `GradeRecord.suite_timeout_s`
        both come through here rather than off the manifest, for the reason
        `last_argv` exists at all: a second read of the configuration is a
        second thing that can be right about what was ASKED for while the argv
        carried something else, sitting inside the record that exists to say
        what happened.

        `None` when no invocation has been made, or when the argv carries no
        `timeout` prefix -- "nobody bounded this", which is a different claim
        from any number, and one a `0` or a fallback to `self.timeout_s` would
        make unrepresentable.
        """
        if len(self.last_argv) >= 2 and self.last_argv[0] == "timeout":
            return int(self.last_argv[1])
        return None

    def select(self, node_ids: tuple[str, ...]):
        """Run exactly these ids, and remember that it was them.

        `_selected` is written AFTER the invocation and never before: `run`
        does not read it, and writing it first would leave a failed exec
        claiming a selection the container never saw.
        """
        result = self.run(self.adapter.select_argvs(node_ids))
        self._selected = tuple(node_ids)
        return result

    def pass_to_pass(self, tests, extra_deselect: tuple[str, ...] = (),
                     scope: tuple[str, ...] = (),
                     ignore: tuple[str, ...] = ()):
        """The p2p set: whatever the manifest declared, or everything else.

        Both branches are real. An explicit list is what a task needs when
        part of its suite is legitimately red at base_sha and cannot be a
        regression check; the empty default is the honest one otherwise,
        because an enumerated copy of a pinned suite goes stale for no
        benefit. Reading the field only when it is non-empty is what keeps it
        from being a manifest key that looks like a measurement and is not.

        The first two keyword parameters exist for the offline grader and
        default to inert: `extra_deselect` appends --deselect for the flake
        quarantine, and `scope` prepends path prefixes to the deselect branch
        so check 6 grades the repo's declared suite rather than whatever
        scratch files an agent left at the rootdir (measured: eight of them in
        one stored record). With both empty, the argv is byte-identical to what
        preflight validated -- a test pins that, because the moment the graded
        command and the gated command drift apart, the oracle stops describing
        the thing being graded.

        `ignore` appends `--ignore=<path>` and has exactly ONE caller:
        preflight's p2p run at the START state, on a task whose f2p module does
        not import there. That run sweeps the rootdir, so the erroring module
        aborts collection before `--deselect` is ever applied -- measured
        2026-09-01, pytest 9.1.1 and 8.3.5, exit 2 -- and the p2p baseline the
        acceptance depends on cannot be observed at all without it. The
        flag is spliced into both branches below, so on a task with an
        explicit `tests.p2p` it is emitted and inert -- positional ids never
        collect the f2p module -- which is why one splice point, not two, is
        the honest shape.

        It is safe here and only here because preflight's p2p-BEFORE run is not
        an argv the grader ever makes: the graded p2p runs at the
        post-submission state, and the preflight run that must match it
        byte-for-byte is p2p-AFTER, which gets no `ignore`. What the ignore
        hides -- a non-f2p test inside the ignored module -- is measured by
        that same p2p-after run, which collects the module once the reference
        lands and must exit 0.

        The argv itself is now the adapter's, in ONE call rather than composed
        from pieces: vitest and jest express selection and deselection through
        a single `-t <regex>`, and emitting two is not "last one wins" --
        measured 2026-09-02, vitest REJECTS the second (exit 1, no report) and
        jest COMMA-JOINS them into a pattern matching neither, running nothing
        at exit 0. The BRANCH -- explicit `tests.p2p` or the deselect default
        -- stays here, because it is a statement about the manifest rather
        than about the framework.

        `_selected` follows that branch and the DESELECT one resets it to
        `()`. A leftover selection there would make a correct scoped run
        report the f2p ids as "did not run" -- which is precisely what a
        correct p2p run does to them. On the explicit branch it is the p2p
        list MINUS `extra_deselect`, so a renamed entry in it is answered by
        the same rule that answers a renamed f2p id, and a quarantined entry
        -- asked not to run by this same argv -- is not.
        """
        if tests.p2p:
            result = self.run(self.adapter.p2p_argvs(
                selected=tuple(tests.p2p), scope=(),
                deselected=tuple(extra_deselect), ignored=tuple(ignore)))
            # MINUS what this same argv deselects. `_selected` is "what the
            # last invocation asked for BY ID", and a quarantined id was asked
            # NOT to run: `p2p_args` folds `deselected` into a negative
            # lookahead inside the single `-t`, so those tests are SKIPPED,
            # carry no terminal status, and `executed_names` -- which yields
            # only `passed`/`failed` -- never sees them. Left unsubtracted they
            # fall straight through `verify_selected` into `Outcome.not_run`,
            # and on this branch the quarantine is a SUBSET of `tests.p2p` by
            # construction (`derive_quarantine` derives it from two
            # `pass_to_pass` runs over that same list). The grader's `did not
            # run` branch would then fire on every healthy node run of any task
            # carrying one flake -- `not_graded` on every cell of every arm,
            # permanently, in an append-only file.
            #
            # `ignore` is deliberately NOT subtracted. It has exactly one
            # caller -- preflight's p2p run at the START state -- which reads
            # no `not_run` at all, and the grader never passes it. Subtracting
            # it here would be a rule with no reader.
            self._selected = tuple(
                node_id for node_id in tests.p2p
                if node_id not in extra_deselect
            )
            return result
        result = self.run(self.adapter.p2p_argvs(
            selected=(), scope=tuple(scope),
            deselected=tuple(tests.f2p) + tuple(extra_deselect),
            ignored=tuple(ignore)))
        self._selected = ()
        return result


def preflight(
    task,
    image: str,
    repo_path: Path,
    start_sha: str,
    expected_claude_version: str = "",
) -> PreflightResult:
    """Every reason this task is not a task. Empty `problems` means GO.

    Problems are COLLECTED rather than raised one at a time. A task with a
    missing dependency usually also fails red-before and green-after, and
    reporting the first one sends the author round the loop three times for
    one cause.
    """
    problems: list[str] = []
    problem_codes: list[str] = []
    evidence: dict = _evidence_seed()
    #: Every rootdir-relative file ANY node run in this gate loaded or
    #: executed. Accumulated at each classification site rather than read at
    #: the end, because `_Runner.last_report` is overwritten by the next `run`
    #: -- and over EVERY run rather than the scoped one alone, because
    #: `select_argvs` is per-file too (the f2p check carries the same
    #: positional) and an explicit-`tests.p2p` task makes no scoped run at all.
    seen_files: set[str] = set()
    files_measured = False
    #: Every `<file>::<fullName>` that MORE THAN ONE assertion in a single
    #: `testResults` entry reached a verdict for, with the largest count any
    #: run reported. Accumulated by MAX and never by SUM: the same test
    #: executes in the before-run and again in the after-run, so a sum reads
    #: every ordinary test as a duplicate.
    #:
    #: Over EVERY node run rather than the scoped one, and that is measured:
    #: the scoped p2p run DESELECTS the f2p ids, and a deselected test is
    #: `skipped` on vitest and `pending` on jest -- neither is terminal, so it
    #: never reaches `executed_names`. A declared f2p duplicate is visible only
    #: in the f2p runs, and a declared p2p one only in the p2p runs, which is
    #: also the task shape (explicit `tests.p2p`) that makes no scoped run.
    same_file_dupes: dict[str, int] = {}
    dupes_measured = False

    def _note(outcome, report):
        nonlocal files_measured, dupes_measured
        if outcome.files_run is not None:
            files_measured = True
            seen_files.update(outcome.files_run)
        # `report_path() is None` is pytest, whose `duplicate_ids` is `{}` as a
        # CLAIM its node ids back -- not an absence. A node run that wrote no
        # report is the absence, and it leaves this flag alone: the flag says
        # "at least one run was counted", never "every run was".
        if adapter.report_path() is None or report is not None:
            dupes_measured = True
            for node_id, count in adapter.duplicate_ids(report).items():
                same_file_dupes[node_id] = max(
                    same_file_dupes.get(node_id, 0), count)
        return outcome

    tests = task.tests
    # `getattr` with a default, like `_declared_grading`'s and the strip's:
    # this function takes an untyped `task`, and a manifest object predating
    # the key must not crash the gate. Broadening 7 Task 4 adds the field.
    #
    # Recorded in `evidence` because a cached verdict outlives the code that
    # wrote it and every other key here is now framework-dependent --
    # `f2p_before_exit` most of all, since 1 means "a test failed" under
    # pytest and means nothing at all under vitest.
    adapter = for_framework(tests.framework)
    evidence["framework"] = adapter.name
    #: `"python"` or `"node"`, DERIVED from `tests.framework` and never
    #: declared -- the same single source `run_matrix` builds the base from.
    #: Read here because the interpreter read-back below is a claim about the
    #: python base and a node task makes none: measured, the node base ships
    #: no `python` at all, so `python --version` exits non-zero, the read-back
    #: files `python_observed: ""` and a NO-GO, and every node task in the set
    #: is refused for the absence of a thing it never declared (D12 -- a node
    #: manifest may not carry `image.python`; `load_task` refuses it).
    runtime = task_runtime(task)[0]
    # NOT a parameter with a default. Both drivers call this with `task` and
    # neither passes a bound, so reading it here makes "a consumer left on the
    # constant" unrepresentable rather than merely tested for -- and that
    # divergence is the silent one: a suite that fits the gate's bound and is
    # killed under the grader's stamps `timed_out`, which is `resolved:
    # False`, on every arm, permanently, over a number the model never saw.
    timeout_s = task.budget.suite_timeout_s

    # Written BEFORE the guard below, because these two are properties of
    # the MANIFEST and are knowable with no daemon: a task refused for a
    # bad runner still records what it declared. Every OTHER key is
    # already at `_evidence_seed()`'s `None` -- "the gate did not look" --
    # and is overwritten where it is measured, so nothing here needs a
    # hand-written seed and nothing may get one: `image_env_observed: {}`
    # or `image_env_mismatch: []` on this path would be a CLAIM that the
    # gate looked and agreed, from a gate that never started a container.
    evidence["image_env_declared"] = declared = _declared_env(task)
    #: `None` on a node task, and that is the null rule rather than a
    #: convenience: `_declared_python` falls back to `_DEFAULT_PYTHON` when the
    #: key is absent, so a node manifest -- which `load_task` FORBIDS from
    #: declaring `image.python` at all -- would otherwise publish "3.12" as a
    #: thing this task declared. `None` here says the gate did not look,
    #: which is what actually happened.
    evidence["python_declared"] = declared_python = (
        _declared_python(task) if runtime == "python" else None
    )

    # BEFORE the runner gate, so a manifest refused for a bad runner still
    # records what its declared framework wanted. Needs no container: both
    # values are a function of the adapter and of `tests.runner`.
    missing_cache_flags = [
        flag for flag in adapter.no_cache_args
        if flag not in tests.runner
    ]
    evidence["runner_cache_flags"] = list(adapter.no_cache_args)
    evidence["runner_cache_flags_missing"] = missing_cache_flags
    # The `!= "pytest"` comparison is the one string test in this file and it
    # is deliberate: pytest's failure is already loud and node's is not, and
    # putting that behind a boolean on the adapter would be a second thing to
    # keep in sync with a rule whose whole content is that sentence. Without
    # `-p no:cacheprovider` pytest writes `.pytest_cache/` and the dirty-tree
    # check below fires; a new refusal on that branch could only refuse a
    # manifest that loads today.
    if missing_cache_flags and adapter.name != "pytest":
        problems.append(
            "tests.runner is missing "
            + ", ".join(missing_cache_flags)
            + f", which {adapter.name} needs so the suite writes nothing "
            "into the tree. Measured 2026-09-01: vitest creates "
            "<cwd>/node_modules/.vite and `--no-cache` prevents it "
            "entirely. Section 5.6 stages everything, so the artifact "
            "would land in every submission diff -- and unlike pytest's "
            ".pytest_cache this one is INVISIBLE to the dirty-tree check "
            "below, because every JavaScript repository's .gitignore "
            "carries node_modules/."
        )

    if not any(adapter.runner_marker in part for part in tests.runner):
        # The DECLARED framework and the declared argv are cross-checked
        # rather than derived from each other, so each catches the other's
        # typo. The old gate was `any("pytest" in part)`, which could only
        # ever mean one framework; a manifest declaring vitest whose runner
        # invokes jest would be classified by the wrong adapter, and the
        # red/green distinction is built on what THIS runner reports.
        problems.append(
            f"tests.runner is {list(tests.runner)!r} but tests.framework is "
            f"{adapter.name!r}, and no argument contains "
            f"{adapter.runner_marker!r}: preflight can only distinguish "
            "'tests failed' from 'the environment is broken' through the "
            "adapter the manifest DECLARED, so a runner the declared adapter "
            "cannot read would be classified by the wrong rules -- which for "
            "the node frameworks means exit 1 for a config error graded as a "
            "test failure -- and without that distinction the gate is "
            "worthless"
        )
        #: `bare_runner_skipped` was left out of the old hand-written seed
        #: once: it was written only in the node `else` branch far below, so
        #: this return recorded `bare_runner_exit: None` with no key at all
        #: saying why -- the same "two absences that render identically" shape
        #: one layer down, and the second occasion this file got it wrong
        #: (`PREFLIGHT_VERSION` 11's three keys were the first). The schema
        #: seeds it now; this writes the reason.
        evidence["bare_runner_skipped"] = (
            "tests.runner does not match tests.framework: no container "
            "started, so the bare-runner probe never ran"
        )
        #: Beside it, and NOT a duplicate of it: `bare_runner_skipped` answers
        #: why that one probe did not run (`HARVESTING.md` documents it by
        #: name for the node task, where `early_return` is null), and
        #: `early_return` answers why the whole gate returned.
        evidence["early_return"] = EARLY_RETURN_RUNNER_MISMATCH
        return PreflightResult(
            task_id=task.task_id, task_version=task.task_version,
            start_sha=start_sha, image=image,
            manifest_digest=task.manifest_digest,
            problems=tuple(problems), evidence=evidence,
            preflight_version=PREFLIGHT_VERSION,
            problem_codes=tuple(problem_codes),
        )

    with RunContainer(image=image, repo_path=str(repo_path),
                      base_sha=start_sha) as container:
        # --- the environment, because each of these reads as a model failure

        uid = container.exec(["id", "-u"]).stdout.strip()
        evidence["uid"] = uid
        if uid == "0":
            problems.append(
                "the image runs as root: Claude Code refuses bypassPermissions "
                "under root and exits before emitting a single event, so every "
                "arm would record zero turns"
            )

        version = container.exec(["claude", "--version"])
        evidence["claude_version"] = version.stdout.strip()
        if version.exit_code != 0:
            problems.append("`claude --version` failed: no agent in this image")
        elif expected_claude_version and not version.stdout.strip().startswith(
            expected_claude_version
        ):
            problems.append(
                f"claude is {version.stdout.strip()!r}, base image pins "
                f"{expected_claude_version!r}: two tasks would run different "
                "agents and the comparison across them is not one"
            )

        # WHAT THE IMAGE SAYS ABOUT ITSELF, beside what the interpreter says.
        # A task image is `FROM <base image id>` and docker propagates a
        # parent's labels verbatim (measured 2026-09-02), so these are the base
        # image's own three keys read off the artifact under test -- no second
        # lookup of which base tag produced it, and correct for a node task,
        # which makes no interpreter claim at all.
        #
        # RECORDED, NEVER REFUSED ON, and the asymmetry is the point. A label is
        # configuration the build stamped; `python_observed` below is an
        # observation. When they disagree the interpreter is the authority and
        # this file carries both, because a verdict cached under this key
        # outlives the code that wrote it and "the image claimed 3.11 and ran
        # 3.13" is a finding a reader has to be able to reconstruct.
        #
        # `{}` is an image carrying no `bakeoff.base.*` keys -- every base built
        # before those labels existed, and every task image derived from one.
        # `None` is "nobody could be asked": no such image, or no docker binary
        # to ask with. Two absences that render identically are the same defect
        # one layer down; these two do not.
        base_seen = image_labels(image)
        evidence["base_image_labels"] = (
            None if base_seen is None
            else {k: v for k, v in base_seen.items()
                  if k.startswith("bakeoff.base.")}
        )

        # The interpreter, read back out of the container rather than trusted
        # from the manifest. `image.python` selects which base the drivers
        # build, and the tag they build it under is MUTABLE and LOCAL: a stale
        # `bakeoff-eval-agent:base-3.11` left by an earlier Dockerfile, a task
        # image built against the wrong entry of the bases map, or an
        # `image.build` step that puts another interpreter earlier on PATH all
        # leave every rendering and unit test green. The failure is then the
        # worst shape this repository knows -- the suite runs, the gate is
        # green, and the interpreter is not the one the task was cut for.
        #
        # ONE OF THE THREE SHAPES BELOW IS NO LONGER REACHABLE THROUGH THE
        # DRIVER, and that is deliberate rather than a gap. A hand-mutated base
        # TAG is now repaired by `images.build_base_images` before this
        # container starts -- it reads the tag's own `bakeoff.base.*` labels,
        # finds they are not this base's, and rebuilds, naming the reason.
        # Measured 2026-09-02, BEFORE that check existed the driver repaired it
        # anyway, silently, via a cache-hit `docker build -t` retag, and a probe
        # of this very refusal recorded PASS on a base it had deliberately
        # broken. A tag cannot carry FORGED labels either: `docker tag` writes a
        # name and not a config, so the only way to a lying label is to build an
        # image -- and measured, such an image still answers 3.11.16 here and
        # still lands on this refusal. The other two shapes -- a task image
        # built against the wrong entry of the bases map, an `image.build`
        # putting another interpreter first on PATH -- are reachable through the
        # driver and are what a real task set produces. THIS PROBE IS
        # PYTHON-ONLY; the node analogue is `images._NODE_RUNNER_REASSERTION`,
        # re-run at every task image build. `tests/test_preflight.py
        # ::test_an_interpreter_that_is_not_the_declared_one_is_refused` drives
        # `preflight()` directly and is what pins the refusal itself.
        #
        # Plain `python`, deliberately, and NOT the runner's interpreter.
        # `image.python` is a claim about the BASE image, and `python` is what
        # the Dockerfile's ARG selects; a task whose `tests.runner` names
        # /opt/venv/bin/python is describing a different environment the key
        # makes no claim about.
        #
        # Measured 2026-09-01: `python --version` writes to STDOUT (Python 2
        # wrote it to stderr; 3.4+ does not), so `.stdout` is the right field.
        #
        # NOT RUN AT ALL ON A NODE TASK, and the gate is written AROUND the
        # three branches rather than as an `if` wrapping them, so the
        # mutation-check anchor on the equality below keeps its exact text and
        # its exact indentation. `node:22-bookworm-slim` ships no `python`
        # (measured 2026-09-02: `exec: "python": executable file not found`),
        # so this probe exits non-zero, files `python_observed: ""` -- an
        # OBSERVATION -- and refuses every node task in the set for the
        # absence of an interpreter its manifest is forbidden from declaring
        # (D12). `None` is the correct answer there: the gate did not look.

        python = (container.exec(["python", "--version"])
                  if runtime == "python" else None)
        if python is None:
            # A node task. `python_declared` and `python_observed` both stay
            # `None`, which is a third absence beside the two below -- "the
            # gate never started a container" and "the probe ran and answered
            # nothing" -- and it must not render as either. There is no
            # node analogue to add here: the runtime read-back a node task
            # needs is the runner pin, and that is asserted at BUILD time by
            # `images._NODE_RUNNER_REASSERTION`, where the remedy (rebuild) is
            # named at the step that caused it.
            pass
        elif python.exit_code != 0:
            # An observed empty answer, not an unobserved one: the container
            # started and the probe ran, it just did not exit 0. `None` above
            # is reserved for the path that never started a container at all.
            evidence["python_observed"] = ""
            problems.append(
                "`python --version` failed inside the image: the base this "
                f"task declares (image.python: {declared_python!r}) either was "
                "not the one it was built on, or its interpreter is no longer "
                "first on PATH. Every check below runs the suite through it"
            )
        else:
            evidence["python_observed"] = observed_python = python.stdout.strip()
            if _parse_python_version(observed_python) != declared_python:
                problems.append(
                    f"the container runs {observed_python!r} but the manifest "
                    f"declares image.python: {declared_python!r}. The base tag "
                    "is local and mutable, so this is a stale or mismatched "
                    "base rather than a manifest error: rebuild the bases "
                    "(`run_matrix.py --preflight-only` builds the set the task "
                    "set needs) and rebuild this task's image against the "
                    "right one. That command REUSES a base whose "
                    "`bakeoff.base.*` labels already match what its Dockerfile "
                    "would stamp, so if it does not fix this, delete the tag "
                    f"first -- `docker rmi {base_tag('python', declared_python)}`"
                    " -- or rebuild it with "
                    "`--pull --no-cache`, which is the only way to pick up a "
                    "republished upstream image, a changed claude.ai installer "
                    "or a moved apt/pip package"
                )

        for tool in ("git", "rg"):
            if container.exec(["sh", "-c", f"command -v {tool}"]).exit_code != 0:
                problems.append(
                    f"{tool} is missing: "
                    + (
                        "snapshot_diff returns empty output, which is "
                        "byte-identical to a clean tree"
                        if tool == "git"
                        else "Claude Code needs it to search"
                    )
                )

        head = container.exec(["git", "rev-parse", "HEAD"]).stdout.strip()
        evidence["head"] = head
        if head != start_sha:
            problems.append(
                f"the container is at {head} but the start state is {start_sha}"
            )

        present = _present(container, _CONTEXT_FILES)
        if present:
            problems.append(
                f"the start state carries {', '.join(present)}: section 5.2 "
                "pins the session config, and a task-local agent file gives "
                "this task a context the others do not have"
            )

        # Fix 2: prove the repo's OWN pytest configuration is not itself a
        # usage error, independent of whatever tests.runner adds. Measured
        # gap (`w3-bidict-389.md`, finding 3): `bidict-389-putall-rollback-
        # clean`'s gated runner is `python -m pytest -q -p no:cacheprovider
        # --override-ini=addopts= tests/` and PASSES, because
        # `--override-ini=addopts=` throws away the repo's own
        # pyproject.toml addopts (`--numprocesses=auto`, pytest-xdist --
        # which the image does not install) -- while the command an agent
        # naturally types, `python -m pytest tests/`, exits 4 from turn one,
        # bug fixed and unfixed alike. This is CLAUDE.md's "the agent must be
        # able to check its own work" with the bypass hiding inside the
        # GATE's own argv rather than in the image.
        #
        # `--co` (collect-only): this asks "does the repo's config parse",
        # not "do the tests pass" -- a real run would double the suite cost
        # for a question the f2p/p2p runs below already answer. NO
        # `--override-ini`, no paths, and deliberately NO `-p
        # no:cacheprovider`: omitting every argument `tests.runner` adds is
        # the whole point, and that flag is one of them. An earlier draft
        # carried it to keep the probe's own collection from writing
        # `.pytest_cache/` into the tree -- but `--co` writes no cache
        # directory at all, with or without the flag (measured 2026-09-02,
        # base-python-3.12: neither run leaves a `.pytest_cache/`, since
        # collection-only never gets to the point cacheprovider persists
        # anything), so the flag was buying nothing. What it WAS doing:
        # disabling cacheprovider removes the command-line options that
        # plugin registers, so a repo whose own `addopts` names one of them
        # (`--lf`, `--ff`, `--nf`, `--cache-clear`, `--cache-show`, `--sw`)
        # becomes a usage error under the probe's argv alone. Measured
        # 2026-09-02, same image, a repo with `addopts = "--lf"`: the
        # agent's own command (`python -m pytest tests/`) exits 0, while
        # `--co -q -p no:cacheprovider` exits 4 with `unrecognized
        # arguments: --lf` -- a false NO-GO that names the repository's
        # config as a usage error and prescribes a missing plugin that was
        # never missing. So the flag is dropped: omitting every argument
        # `tests.runner` adds is what makes this probe bare, and the flag
        # was the one argument still hiding a class of manifest this probe
        # exists to catch honestly.
        #
        # Only for pytest. The node frameworks have no addopts analogue --
        # vitest and jest answer a broken config, a failing test and an
        # unresolvable import all with exit 1 alike (measured, see the
        # runner-adapter note under `PREFLIGHT_VERSION` 10 above), so there is
        # nothing a bare invocation could tell apart there. `bare_runner_exit`
        # stays `None` and `bare_runner_skipped` names why, rather than
        # running a probe that could only ever answer "1".
        if adapter.name == "pytest":
            interpreter = _runner_python(tests.runner)
            bare_argv = [
                "timeout", str(timeout_s), interpreter, "-m", "pytest",
                "--co", "-q",
            ]
            evidence["bare_runner_argv"] = bare_argv
            bare = container.exec(bare_argv)
            evidence["bare_runner_exit"] = bare.exit_code
            # Written on the same statement pair as the exit code, at every
            # one of these sites and nowhere else. A recorded exit with no
            # duration beside it means the gate ran a command and lost what
            # it cost, and the pairing is what makes that unrepresentable
            # rather than merely tested for -- there is no second "did this
            # run happen?" branch to keep in step. Seven pairs cover nine
            # invocations: the grading pair is a loop body and runs once per
            # declared `grading.*` key.
            evidence["bounded_run_durations_s"]["bare_runner"] = _elapsed_s(bare)
            if bare.exit_code == EXIT_USAGE_ERROR:
                # NOT literally line 1. Measured against the real image
                # (2026-09-02): argparse's own `error()` prints the usage
                # banner ("usage: python -m pytest [options] ...") as line 1
                # and the flag it actually choked on ("python -m pytest:
                # error: unrecognized arguments: --numprocesses=auto") as
                # line 2 -- so quoting line 1 would name no flag at all on
                # the exact task this probe exists for. `": error:"` is
                # argparse's own separator between the program name and its
                # message, so it selects that line wherever it lands; a
                # report carrying neither shape falls back to line 1, which
                # is still the best available summary of what pytest said.
                stderr_lines = bare.stderr.strip().splitlines()
                quoted_line = next(
                    (line for line in stderr_lines if ": error:" in line),
                    stderr_lines[0] if stderr_lines else "",
                )
                hint = _bare_runner_plugin_hint(bare.stderr)
                problems.append(
                    "the repository's own pytest configuration is a usage "
                    f"error in this image: {quoted_line}. The gated "
                    "runner's own argv can hide this (an "
                    "--override-ini=addopts= bypass, most often) while the "
                    "bare command an agent naturally types does not -- "
                    f"image.pip needs {hint}"
                )
            elif bare.exit_code == 124:
                problems.append(
                    "the bare pytest collection "
                    f"(`{' '.join(bare_argv)}`) timed out after "
                    f"{timeout_s}s: this task's own configuration cannot "
                    "be trusted to even collect in this image"
                )
            elif bare.exit_code not in (
                EXIT_ALL_PASSED, EXIT_TESTS_FAILED, EXIT_COLLECTION_INTERRUPTED,
                EXIT_NOTHING_COLLECTED,
            ):
                # Everything else used to fall through here in silence: 3
                # (pytest internal error) and 127 (the interpreter this probe
                # resolved is not on PATH) both land in `_PROCESS_EXIT_MEANING`
                # -- NOT `adapter.explain`, whose own `_EXIT_MEANING` has no
                # 127 entry and falls back to the bare digits -- and 127 in
                # particular is "the agent cannot run the command it will
                # naturally type", the exact failure this probe exists to
                # catch, not a code to let through unnamed.
                meaning = (
                    _PROCESS_EXIT_MEANING.get(bare.exit_code)
                    or adapter.explain(bare.exit_code)
                )
                problems.append(
                    "the bare pytest collection "
                    f"(`{' '.join(bare_argv)}`) exited "
                    f"{bare.exit_code} ({meaning})"
                )
            # 0, 2, 5 are not a problem: 2 is a collection error the task
            # may legitimately carry at the start state (broadening 2), 5 is
            # "no tests collected", which the f2p checks judge on their own,
            # and 1 cannot happen with --co (kept in the accepted set anyway,
            # since `--co` never selecting tests is a property of the argv
            # this probe controls, not one worth re-deriving here).
        else:
            evidence["bare_runner_skipped"] = (
                f"{adapter.name} has no addopts analogue: a broken config, "
                "a failing test and an unresolvable import all exit 1 "
                "alike (measured), so a bare invocation could not tell any "
                "of them apart from the others"
            )

        # The strip, checked against the TREE rather than against the manifest
        # or the code that performed it. `materialize` raises when a declared
        # path matches nothing, so this cannot fire on a typo -- what it
        # catches is the artifact disagreeing with the manifest for any other
        # reason (a build step re-creating the path, a stale preflight tree, a
        # future change to the strip that stops working). A strip that did not
        # happen is invisible: the file is in every arm's context and in every
        # submission diff, and no later stage re-derives it.
        #
        # `dangling_counts=True`: stripping a symlink's target and not the link
        # leaves a path `test -e` calls absent and `ls` still shows.
        #
        # `getattr`, like `_declared_grading`'s: this function takes an
        # untyped `task` and a manifest object predating the key must not
        # crash the gate.
        # Both keys are written with a REAL value rather than left at the
        # schema's `None`, because the strip is knowable once a container
        # exists: `[]` here is the measurement "this task strips nothing".
        stripped = tuple(getattr(task, "strip_paths", ()))
        still_there = (
            _present(container, stripped, dangling_counts=True)
            if stripped else []
        )
        evidence["stripped_paths"] = list(stripped)
        evidence["stripped_paths_present"] = still_there
        if still_there:
            problems.append(
                f"the start state still carries {', '.join(still_there)}, "
                "which the manifest's strip_paths says it removed. Section "
                "5.2 pins the session config and section 5.6 stages "
                "everything, so an un-stripped path is both a context this "
                "task has and the others do not, and a file in every "
                "submission diff."
            )

        # `getattr`, like the strip check's two screens up: this function
        # takes an untyped `task`, and a manifest object predating the key
        # must not make the gate raise `AttributeError` -- a traceback instead
        # of a NO-GO. Read once, used by the per-entry join below and by the
        # post-suite read further down.
        unneeded = frozenset(getattr(task, "submodules_unneeded", ()))

        # The submodules, read back out of the container the suite will run
        # in. `materialize` has its own post-condition on the host and it is
        # not this one: a tree can be emptied, re-copied or rebuilt between
        # there and here, and the image is built by a separate `git archive`.
        #
        # This is the failure this whole broadening exists to make loud.
        # Measured 2026-09-01, git 2.50.1: a superproject whose submodule
        # directory has zero entries reports `git status --porcelain` EMPTY --
        # so the tree is clean, the strip check is silent, the dirty-after
        # check is silent, and the only symptom is a suite that cannot import
        # its dependency. That is the Phase 0c shape arriving through the
        # dataset instead of through the image, and it is scored as capability
        # on every arm.
        submodules: list[dict] = []
        status = container.exec(["git", "submodule", "status"])
        if status.exit_code != 0:
            # `None`, not `[]`. A non-zero exit returns empty stdout, which
            # parses to `[]` -- a positive observation of "there are none"
            # manufactured out of a failure, with no problem raised. The two
            # absences must not render identically.
            evidence["submodules"] = None
            evidence["submodules_orphaned"] = None
            problems.append(
                "`git submodule status` failed (exit "
                f"{status.exit_code}): {(status.stdout or status.stderr)[:500]}. "
                "Whether this tree's submodules are initialised is unknown, and "
                "an uninitialised one is invisible in `git status`."
            )
        gitlinks = None if status.exit_code != 0 else _gitlink_paths(container)
        if status.exit_code == 0 and gitlinks is None:
            # Same shape as the branch above, different cause: the index could
            # not be read, so there is no authoritative path set and nothing
            # below can be trusted to name a submodule.
            evidence["submodules"] = None
            evidence["submodules_orphaned"] = None
            problems.append(
                "`git ls-files -s -z` failed, so the tree's gitlinks could not "
                "be read and no submodule claim can be made about it."
            )
        elif status.exit_code == 0:
            submodules, unmatched = _parse_submodule_status(status.stdout,
                                                            gitlinks)
            # Filled in the CALLER, in one pass, and NOT inside
            # `_parse_submodule_status`: that function's docstring states the
            # principle -- it is an OBSERVATION, not a restatement of the
            # manifest -- and `declared_unneeded` is manifest data.
            #
            # `empty` is measured for EVERY submodule, needed ones included.
            # Measured only for declared paths, `None` would carry two
            # meanings ("the read failed" and "the read was not attempted"),
            # which is "a null says which kind of null it is" broken. Both
            # keys are written for every entry, `False` included: a reader who
            # cannot see the field on the other entries cannot tell "this task
            # declared none" from "this gate did not know about the key".
            for entry in submodules:
                entry["declared_unneeded"] = entry["path"] in unneeded
                entry["empty"] = _directory_is_empty(container, entry["path"])
            evidence["submodules"] = submodules
            if unmatched:
                # NOT resolved by falling back to the display line's own text:
                # that would put a display-derived path into the evidence,
                # which is what taking paths from `ls-files` exists to prevent.
                problems.append(
                    "`git submodule status` named submodules the index has no "
                    "gitlink for: " + "; ".join(unmatched[:5])
                    + ". The two readers disagree about this tree."
                )
            # THE REVERSE CROSS-CHECK, and it is a problem for the same
            # reason the forward one is. `unmatched` catches a status line no
            # gitlink explains; this catches a gitlink no status line
            # explains -- and that direction is the QUIETER of the two,
            # because an index gitlink that produces no line contributes no
            # entry, so `submodules` renders as a positive `[]` (or as a
            # shorter list) with nothing anywhere saying a path was dropped.
            # `stale` below reads `submodules`, so the one submodule the gate
            # exists to catch is exactly the one it then cannot see.
            #
            # A path can land here two ways -- git listed nothing for it, or
            # `_parse_submodule_status`'s boundary rule declined a line the
            # index and the display disagree about -- and both are "the two
            # readers disagree about this tree", which is what the message
            # says rather than guessing which.
            unlisted = sorted(set(gitlinks)
                              - {entry["path"] for entry in submodules})
            if unlisted:
                problems.append(
                    "the index carries gitlinks `git submodule status` said "
                    "nothing about: " + ", ".join(unlisted[:5])
                    + ". The two readers disagree about this tree, and this "
                    "direction is silent on its own: an unlisted gitlink "
                    "contributes no entry, so the submodules evidence is a "
                    "positive list with the path simply missing from it and "
                    "the initialisation check never sees it."
                )
            # Inert, therefore recorded rather than refused (measured: git
            # drives submodules off the index, so a stanza with no gitlink is
            # never listed, never fetched and creates no directory). Recorded
            # HERE because in the container it is an observation of the tree
            # the suite will run against.
            # `-z`, and it is the same rule as `_gitlink_paths`' and
            # `_chunk_path`'s: take the value from git's own delimiter, never
            # from a split over a display line. Measured 2026-09-01 (git
            # 2.50.1): without `-z` the output is `<key> <value>` on one line,
            # and a submodule NAME may contain a space -- `[submodule "my
            # sub"]` gives the key `submodule.my sub.path`, so
            # `split(" ", 1)[1]` returns `sub.path <value>` rather than the
            # value. That string is then never in `gitlinks`, so a submodule
            # that is perfectly healthy is reported as an orphan, under a
            # path that is not a path. `-z` emits `<key>\n<value>\0` per
            # record (NEWLINE between the two, NUL between records), which is
            # unambiguous for both -- a key cannot contain a newline and the
            # trailing NUL leaves one empty final record.
            declared_subs = container.exec(
                ["git", "config", "-f", ".gitmodules", "--get-regexp", "-z",
                 r"^submodule\..*\.path$"]
            )
            if declared_subs.exit_code in (0, 1):
                # 1 is git config's ORDINARY "no key matched" -- a repository
                # with no .gitmodules at all, or one whose stanzas all have
                # gitlinks. Collapsing it with >1 would report a genuine
                # failure (an unreadable or malformed .gitmodules) as the
                # measured claim "there are no orphans".
                #
                # `partition`, never `split("\n", 1)[1]`: a valueless key
                # (`path` with no `=`) is a record with no newline in it, and
                # the indexed form would raise `IndexError` out of a gate
                # whose caller does not wrap it -- a traceback instead of a
                # NO-GO, the same failure `_gitlink_paths` documents.
                declared_paths = set()
                for record in declared_subs.stdout.split("\0"):
                    if not record:
                        continue
                    _key, sep, value = record.partition("\n")
                    if sep:
                        declared_paths.add(value)
                evidence["submodules_orphaned"] = sorted(
                    declared_paths - set(gitlinks)
                )
            else:
                evidence["submodules_orphaned"] = None
                problems.append(
                    "reading .gitmodules failed (exit "
                    f"{declared_subs.exit_code}): "
                    f"{(declared_subs.stdout or declared_subs.stderr)[:500]}"
                )
            # The declared state, asserted rather than assumed. Every one of
            # these replaces something `stale` used to say about these paths,
            # and the exclusion two blocks down is what turns the NO-GO into a
            # GO.
            for entry in submodules:
                if entry["empty"] is None:
                    # Re-run rather than thread the first `ExecResult` through
                    # `entry["empty"]`: that field is `bool | None` everywhere
                    # else it is read (the exclusion two blocks down included),
                    # and widening it to carry exit code and head would break
                    # every one of those checks for one diagnostic message.
                    # This only executes on the already-broken path, so the
                    # extra `ls -A` costs nothing on the common one.
                    diag = container.exec(["ls", "-A", "--", entry["path"]])
                    head = (diag.stdout or diag.stderr)[:500]
                    tail = (
                        " Whether the directory is empty is unknown, and for "
                        "a declared-unneeded submodule that is the only claim "
                        "this gate can check."
                        if entry["declared_unneeded"]
                        else " Whether the directory is empty is unknown."
                    )
                    problems.append(
                        f"could not list {entry['path']} to check whether it "
                        f"is empty (exit {diag.exit_code}): {head}." + tail
                    )
                if not entry["declared_unneeded"]:
                    continue
                if entry["marker"] != "-":
                    problems.append(
                        f"the manifest declares {entry['path']} unneeded, but "
                        "`git submodule status` reports marker "
                        f"{entry['marker']!r} -- something populated a path "
                        "the harness was told to leave alone, so the suite is "
                        "reading content this task was not cut against. The "
                        "run tree is bind-mounted over the image's own /repo, "
                        "so this can only have been written into the run tree "
                        "after materialization."
                    )
                if entry["empty"] is False:
                    problems.append(
                        f"the manifest declares {entry['path']} unneeded, but "
                        "the directory is not empty. git cannot see in there "
                        "-- measured 2026-09-02, a file inside an "
                        "uninitialised submodule directory is invisible to "
                        "`git status --porcelain`, to `git ls-files -o` and "
                        "to `git add -A`, so no other check in this gate and "
                        "no submission diff would report it. A suite that "
                        "writes inside a submodule is out of the corpus."
                    )
            # `derive_submodules` refuses this on the HOST, so reaching it
            # means the container's tree is not the one that was derived --
            # recorded as an observation rather than trusted to the host-side
            # refusal, for the reason `_parse_submodule_status`'s docstring
            # gives about not restating configuration.
            declared_missing = sorted(unneeded - set(gitlinks))
            if declared_missing:
                problems.append(
                    "the manifest declares "
                    + ", ".join(declared_missing)
                    + " unneeded, but the container's index carries no "
                    "gitlink there. The manifest and the tree the suite will "
                    "run against disagree about this path."
                )
        # A declared-unneeded submodule is EXCLUDED, and this is the one line
        # that turns the NO-GO into a GO. Everything `stale` says about such a
        # path is wrong by construction: it is uninitialised on purpose, and
        # the three problems above assert the state the declaration promises
        # instead.
        stale = [entry["path"] for entry in submodules
                 if not entry["initialised"] and not entry["declared_unneeded"]]
        if stale:
            problems.append(
                "submodules are not initialised at their gitlink: "
                + ", ".join(stale)
                + ". The directory is empty or at the wrong commit, and "
                "`git status --porcelain` reports a tree in that state as "
                "CLEAN -- so the suite would simply fail to collect and every "
                "arm would be scored on an environment defect. Materialization "
                "should have populated it from the submodule's pruned mirror."
            )

        # The environment, checked against the CONTAINER rather than against
        # the manifest. `image.env` is configuration; what the container holds
        # is the observation, and this is the only place the two meet.
        #
        # An un-applied image.env is invisible in the worst way. Its whole job
        # is determinism -- measured 2026-09-01 against hypothesis 6.167.1, a
        # property-based suite gives `0 0 0 0 1 1 1 1 0 0` over ten fresh runs
        # of unchanged code and `1 1 1 1 1 1` under CI=1 -- so a value that
        # did not take means the gate passed on a lucky draw and every arm is
        # scored against an oracle that answers differently per run.
        #
        # Not redundant with "we generated the Dockerfile ourselves": a stale
        # tag (a task edited without a task_version bump moves manifest_digest
        # and need not move the image id), a base image whose own ENV later
        # collides, and a future edit that emits the lines in the wrong place
        # all leave the rendering tests green.
        #
        # `getattr`, like `_declared_grading`'s and the strip's: this function
        # takes an untyped `task`, and a manifest object predating the key
        # must not crash the gate.
        #
        # BEFORE `_Runner` is built, so a bad environment is reported as
        # itself rather than as five downstream suite failures.
        #
        # `image_env_declared` was written before the guard above, on every
        # path including the one that never reaches a container. What this
        # block writes is the pair only a container can answer -- `observed`
        # and `mismatch` -- overwriting the `None`s set there. Both are
        # written whether or not anything was declared: "this task declares no
        # environment" and "the gate did not look" render identically as a
        # missing key, and a cached verdict outlives the code that wrote it.
        observed = _observed_env(container, tuple(sorted(declared)))
        mismatch = sorted(
            key for key, value in declared.items() if observed.get(key) != value
        )
        evidence["image_env_observed"] = observed
        evidence["image_env_mismatch"] = mismatch
        if mismatch:
            problems.append(
                "the container's environment does not match the manifest's "
                "image.env for "
                + ", ".join(
                    f"{key} (declared {declared[key]!r}, container "
                    f"{observed.get(key)!r})" for key in mismatch
                )
                + ". That key is baked as a Dockerfile ENV so it reaches "
                "every process in the container, including the ones the agent "
                "invents; a value that did not take is silent -- the suite "
                "goes back to being nondeterministic and the gate passes on a "
                "lucky draw. Rebuild the task image (a task edited without a "
                "task_version bump moves manifest_digest and need not move "
                "the image id)."
            )

        # The other direction, and the only check that catches the manifest
        # nobody wrote. Everything above compares DECLARED against OBSERVED,
        # so a property-based task whose author never declared CI passes all
        # of it -- declared is {}, observed is {}, mismatch is empty, GO --
        # and the ladder then runs on a lucky draw. Measured 2026-09-01
        # against hypothesis 6.167.1, that draw is `0 0 0 0 1 1 1 1 0 0` over
        # ten fresh runs of unchanged code on an unchanged tree.
        #
        # TWO probes, and the second is what keeps the refusal honest.
        # INSTALLED is not USED: hypothesis is a common transitive dependency
        # (a dev-extra, a `pip install -e .[test]` in image.build, a
        # dependency of a dependency), and a NO-GO on availability alone would
        # refuse a task whose suite never imports it -- with a remedy the
        # author cannot apply, since nothing they wrote put it there.
        #
        # The interpreter comes off `tests.runner` rather than being
        # hardcoded: a runner of ["/opt/venv/bin/python", "-m", "pytest"]
        # resolves imports against that venv, so probing whichever `python` is
        # first on PATH would answer a question about a different environment.
        # ASKED OF THE ADAPTER, because this is a Python-ecosystem check
        # and nothing about it generalises. A node adapter answers `None`,
        # the whole block below is skipped, and both evidence keys stay
        # `None` -- a recorded absence, never a claim that a JavaScript
        # suite is deterministic.
        interpreter = adapter.hypothesis_interpreter(tests.runner)
        if interpreter is not None:
            importable = container.exec(
                [interpreter, "-c", "import hypothesis"]
            ).exit_code == 0
            evidence["hypothesis_importable"] = importable

            # `rg` is asserted present above, so this adds no dependency.
            # Filtered through `_present` for the reason `_existing_prefixes`
            # gives: a declared prefix absent at the start state is an input
            # this gate tolerates, and handing rg a path that does not exist
            # makes it exit 2 -- which must read as "could not answer", not
            # as "no match".
            scanned = _present(container, tests.paths)
            used: bool | None = None
            if scanned:
                used = _rg_probe(
                    container, r"^\s*(from|import)\s+hypothesis\b", scanned,
                    problems,
                    "hypothesis-import scan (rg over tests.paths)",
                    "silently reading it as 'not imported' would disarm the "
                    "one check that catches an undeclared property-based "
                    "suite.",
                )
            evidence["hypothesis_imported_by_suite"] = used

            if used and "CI" not in declared:
                problems.append(
                    "the declared test paths import hypothesis and the "
                    "manifest declares no image.env CI. A property-based "
                    "suite without it is a coin flip -- measured, ten fresh "
                    "runs of one property test over unchanged code gave "
                    "`0 0 0 0 1 1 1 1 0 0`, and six under CI=1 gave "
                    "`1 1 1 1 1 1` -- so the gate would be certifying a task "
                    "whose red-before/green-after verdict is a draw. Add "
                    "`image.env: {CI: \"1\", HYPOTHESIS_STORAGE_DIRECTORY: "
                    "\"/tmp/bakeoff-hypothesis\"}` and read HARVESTING.md's "
                    "Layer 2 bullet before doing so -- determinism makes "
                    "this oracle reproducible, not correct. (This fires on "
                    "an IMPORT in tests.paths, not on the package being "
                    "installed: hypothesis arriving transitively through "
                    "image.build in a suite that never uses it is fine and "
                    "is recorded as hypothesis_importable without a "
                    "problem.)"
                )

        runner = _Runner(container, tests.runner, timeout_s, adapter)

        # --- red before, and the p2p baseline it is judged against
        #
        # Two runs, ONE verdict, and the verdict comes last. A task whose fix
        # ADDS a symbol puts that symbol in the solution half, so the test half
        # raises ImportError at the start state and pytest exits 4 (measured
        # 2026-09-01, pytest 9.1.1 and 8.3.5: a positional NODE ID whose module
        # will not import is a usage error, not the collection-interrupted 2 a
        # directory sweep gives). That is a real task shape -- section 3.3's
        # loop still runs, the agent just reads an ImportError instead of an
        # assertion, which is exactly what the human who filed the issue read.
        #
        # It is accepted only under a THREE-way conjunction, because the parse
        # alone cannot carry it -- the f2p selection imports ONLY the f2p
        # modules, so an environment defect there produces exactly the confined
        # error set the task shape produces. The three, and what each one and
        # only it can see:
        #
        #   (i)   the errors are confined to the declared f2p modules -- no
        #         stranger module errored, and no declared module stayed quiet;
        #   (ii)  p2p is green BEFORE -- a globally broken image is refused
        #         here, and the regression baseline exists at all;
        #   (iii) f2p is green AFTER -- and this is the ONLY one that refuses a
        #         dependency imported solely by the f2p module, which is
        #         confined under (i) and leaves (ii) green. That is exactly the
        #         Phase 0c integration fixture.
        #
        # (iii) is asserted unconditionally by the green-after block further
        # down and needs nothing here. It is named here because a reader who
        # believes (i) and (ii) suffice will eventually simplify it away.
        #
        # The runs stay in this order -- their order is what each sees of the
        # tree -- and the JUDGEMENT is deferred instead. Problems are collected
        # rather than raised, so nothing else about this function has to move.

        red = runner.select(tests.f2p)
        # Off the ARGV, not off the manifest -- see `_Runner.last_timeout_s`.
        # Written after the first invocation rather than at construction, so
        # the key is absent on the path where no command ever ran and a reader
        # of a cached verdict can tell that from a run that was bounded.
        evidence["suite_timeout_s"] = runner.last_timeout_s
        evidence["f2p_before_exit"] = red.exit_code
        evidence["bounded_run_durations_s"]["f2p_before"] = _elapsed_s(red)
        red_outcome = _note(runner.classify(red), runner.last_report)

        # Every declared f2p id must have RUN, not merely not-passed. Node
        # only in effect -- `not_run` is empty for pytest, which answers a
        # selection that matches nothing with exit 4 -- and skipped on a load
        # error, where a file that did not load holds no assertions, nothing
        # "ran" by construction, and the branch below already names it.
        not_run = sorted(red_outcome.not_run)
        # `None` when this run produced no report on a framework that writes
        # one: `verify_selected` never ran, so `not_run` is empty because
        # nothing was checked rather than because nothing was missing.
        #
        # That is the SECOND of the two paths this key once claimed a
        # measurement on. The first is the runner-gate early return above,
        # which starts no container and runs no selection at all; it is
        # answered by the schema's `None` now, where the `[]` it used to carry
        # is what made `PREFLIGHT_VERSION` move to 11.
        #
        # The same guard `duplicate_full_names` carries, for the same reason, and
        # NOT a bare `last_report is not None` -- pytest writes no report by
        # design and answers a selection matching nothing with exit 4 and an
        # `ERROR: not found:` line, so `[]` there is a claim its framework
        # backs. A missing report is an absence only where one is written.
        evidence["f2p_before_not_run"] = (
            not_run
            if adapter.report_path() is None or runner.last_report is not None
            else None
        )
        if not_run and red_outcome.kind != KIND_LOAD_ERROR:
            problems.append(
                "these declared f2p tests did not RUN at the start state: "
                + ", ".join(not_run)
                + ". Measured 2026-09-01 against vitest 3.2.7 and jest 30.5.0: "
                "a `-t` pattern matching no test exits **0** and reports every "
                "test skipped, so a renamed test makes an unsatisfiable task "
                "read as a green gate and then as a solved run for every arm "
                "of it. pytest answers the same input with exit 4 and an "
                "`ERROR: not found:` line, which is why this check has no "
                "pytest equivalent."
            )

        collected = red_outcome.errored_files
        # EQUALITY, in both directions. A module erroring that no f2p id names
        # is a broken environment; a declared f2p module that did NOT error is
        # a declared id nobody checked -- and measured, a partial collection
        # error hides the rest of the selection entirely, so the id-level rule
        # ("every declared f2p id appears in FAILED/ERROR") is unsatisfiable
        # here and this is that same claim at module granularity.
        confined = collected is not None and collected == f2p_modules(tests.f2p)
        # Written on every path, all three keys: "this task has no collection
        # errors" and "the gate did not look" render identically as a missing
        # key, and a cached verdict outlives the code that wrote it.
        evidence["f2p_collection_errors"] = sorted(collected or ())
        # Four values, not three: "the task is already done" and "the gate
        # could not classify this run" are different facts about a cached
        # verdict, and one name for both is the defect one layer down that
        # "absence is recorded, never implied" exists to prevent.
        evidence["f2p_red_kind"] = (
            "collection_error" if confined
            else "failed" if red_outcome.kind == KIND_FAILED
            else "passed" if red_outcome.kind == KIND_PASSED
            else "unknown"
        )

        # `--ignore` on THIS run only. Without it the erroring module aborts
        # collection of the whole rootdir sweep before `--deselect` is applied
        # (measured: exit 2 on the p2p-before argv verbatim), so the baseline
        # this acceptance depends on cannot be observed at all. Safe here
        # because preflight's p2p-BEFORE is not an argv the grader makes: the
        # graded run is at the post-submission state, matched by p2p-AFTER,
        # which gets no ignore -- and the one thing the ignore hides, a non-f2p
        # test inside the ignored module, is measured by that same p2p-after
        # run once the reference lands.
        ignore = tuple(sorted(collected)) if confined else ()
        evidence["p2p_before_ignored"] = list(ignore)
        green = runner.pass_to_pass(tests, ignore=ignore)
        evidence["p2p_before_exit"] = green.exit_code
        evidence["bounded_run_durations_s"]["p2p_before"] = _elapsed_s(green)
        # Classified IMMEDIATELY after its own invocation: `classify` reads
        # `_Runner.last_report`, which the next `run` overwrites.
        p2p_before_outcome = _note(runner.classify(green), runner.last_report)
        p2p_green = p2p_before_outcome.kind == KIND_PASSED

        # The node half of the same question, and it is a REFUSAL rather than a
        # probe-plus-remedy because there is nothing to declare.
        #
        # Measured 2026-09-02 in bakeoff-eval-agent:base-node-22 against
        # fast-check 3.23.2 and 2.25.0: `QualifiedParameters.readSeed` falls
        # back to `Date.now() ^ (Math.random() * 0x100000000)`, the package
        # reads NO environment variable anywhere (`grep -rl process.env` over
        # the installed tree exits 1), and ten fresh vitest runs of one
        # property over unchanged code gave `0 0 0 1 1 0 0 0 0 0`. So a node
        # property suite gates on whichever draw preflight happened to run.
        #
        # NODE_OPTIONS preloading `fc.configureGlobal({seed})` was measured and
        # rejected: it works on vitest (10/10 stable, both polarities) and is
        # DEFEATED by jest's module registry (the test's own
        # `fc.readConfigureGlobal()` reads `{}`, `--runInBand` included). A
        # lever that silently does nothing on one of two peer frameworks is the
        # failure the image.env read-back above exists to catch -- and this one
        # could not be read back at all, since fast-check prints its seed only
        # on a FAILING run.
        #
        # SCOPED TO THE P2P-BEFORE RUN'S `files_run`, which is the whole design
        # decision. `tests.paths` would scan something other than what this
        # gate certifies: with `tests.p2p: []` the sweep above is rootdir-wide,
        # and measured on yaml-474 it loads 25 suites and 3,497 tests while
        # `tests.paths` is one file. A rootdir `rg` would scan the right
        # question and produce a refusal NO AUTHOR ACTION CLEARS -- excluding
        # the file from the sweep leaves it on disk. `files_run` is the scope
        # where the remedy and the fix are the same action: exclude the file
        # from the sweep, it leaves `files_run`, the refusal clears, and the
        # certified verdict stops depending on a draw.
        #
        # `None` for pytest (`Outcome.files_run`'s contract), so this stays
        # node-only with no framework branch here. `None` also on a node run
        # that wrote no report -- KIND_ENVIRONMENT -- and that case adds no
        # problem of its own, because the missing report is already a NO-GO
        # said better one branch below.
        #
        # `_present` even though the runner just loaded these files: `_relpath`
        # never raises, so a mangled or absolute report `name` would make rg
        # exit 2, which must read as "could not answer" and not as "no match".
        # A FOURTH null shape lives here too, beside the two above:
        # `property_framework_imported_by_suite: None` with
        # `property_scan_files: []` is "the sweep ran and reported files, but
        # every one of them was filtered out here as not present on disk" --
        # distinguishable from the other two `None`s by the empty list rather
        # than `null`, so the taxonomy is satisfied; it is easy to miss
        # because nothing else about this run reads as unhealthy.
        #
        # TWO probes, for the pytest block's reason: the first asks whether the
        # sweep ran a property suite, the second whether it is pinned, and only
        # the second answering no after the first answering yes is refused.
        scan = adapter.property_scan()
        swept = p2p_before_outcome.files_run
        if scan is not None and swept:
            scan_files = _present(container, swept)
            evidence["property_scan_files"] = scan_files
            if scan_files:
                imported = _rg_probe(
                    container, scan.import_pattern, scan_files, problems,
                    "property-framework-import scan (rg over the p2p-before "
                    "run's files_run)",
                    "silently reading it as 'no property framework' would "
                    "disarm the one check that catches an unpinned "
                    "property-based suite.",
                )
                evidence["property_framework_imported_by_suite"] = imported

                pinned: bool | None = None
                if imported:
                    # Only asked when the first probe said yes, so `None` here
                    # is "not asked" and is read beside `imported` above. `-U`,
                    # because a real `configureGlobal({\n seed: 1234,\n})`
                    # spans lines and rg is line-based without it.
                    pinned = _rg_probe(
                        container, scan.pin_pattern, scan_files, problems,
                        "property-seed-pin scan (rg over the p2p-before run's "
                        "files_run)",
                        "silently reading it as 'no seed pin' would refuse a "
                        "task whose suite is already deterministic.",
                        multiline=True,
                    )
                evidence["property_framework_seed_pinned"] = pinned

                if imported and not pinned:
                    problems.append(
                        "the p2p-before sweep ran a file importing a "
                        "JavaScript property-based framework ("
                        + ", ".join(scan.frameworks)
                        + ") and nothing it ran pins a seed. Measured "
                        "2026-09-02 against fast-check 3.23.2 and 2.25.0, the "
                        "seed defaults to `Date.now() ^ (Math.random() * "
                        "0x100000000)` and ten fresh runs of one property over "
                        "unchanged code gave `0 0 0 1 1 0 0 0 0 0` -- so the "
                        "p2p verdict this gate publishes, and the grader's "
                        "checks 5 and 6 after it, are a draw. THERE IS NO "
                        "image.env LEVER TO ADD: fast-check reads no "
                        "environment variable at all, and the one external "
                        "lever that works for vitest (NODE_OPTIONS preloading "
                        "a module that calls fc.configureGlobal) is defeated "
                        "by jest's module registry. TWO remedies, and the "
                        "first is the one that actually changes the verdict: "
                        "(1) take the file out of the SWEEP by adding the "
                        "framework's ignore flag to tests.runner -- and "
                        "re-emit the entries it replaces, because a CLI "
                        "--testPathIgnorePatterns REPLACES both the config's "
                        "list and jest's built-in /node_modules/ rule "
                        "(measured: the naive one-flag form killed yaml-474's "
                        "sweep outright, no report written); (2) pick a repo "
                        "whose own suite calls `fc.configureGlobal({seed: "
                        "...})` in a file the sweep runs. Read "
                        "taskset/HARVESTING.md's 'Screening a JavaScript or "
                        "TypeScript repository' first -- a pinned seed makes "
                        "this oracle reproducible, not correct."
                    )

        if red_outcome.kind == KIND_PASSED:
            problems.append(
                "the f2p tests PASS at the start state: the task is already "
                "done, and every arm would be scored on work it did not do"
            )
        elif confined and p2p_green:
            pass  # accepted: the bug is a collection error, and it is confined
        elif confined:
            problems.append(
                "the f2p tests could not be collected at the start state "
                f"({', '.join(sorted(collected))}), which is an accepted task "
                "shape ONLY while the rest of the suite is green there -- and "
                "the p2p run exited "
                f"{green.exit_code} ({adapter.explain(green.exit_code)}). "
                "The f2p "
                "selection imports only the f2p modules, so a broken image "
                "produces exactly this error set; p2p is what separates them. "
                "If the run below collected nothing, this task's test tree "
                "holds no regression baseline outside the erroring module. If "
                "it still reports that module, the --ignore missed: those "
                "paths come from pytest's ROOTDIR-relative ERROR lines and "
                "--ignore resolves against the working directory, and an "
                "--ignore naming a path that does not exist is accepted "
                "silently (measured).\n"
                + _argv_lines(runner)
                + (green.stdout or green.stderr)[-2000:]
            )
        elif red_outcome.kind != KIND_FAILED:
            # `collected` is absent for two different reasons, and the message
            # must not say "empty" for both: the parse ran and found nothing
            # (a collection failure whose output carried no bare-module ERROR
            # lines) versus the parse never ran at all (an exit code -- e.g.
            # 5, EXIT_NOTHING_COLLECTED, or a timeout -- this framework does
            # not read as a collection failure). "empty" for the second case
            # would read as "the runner reported nothing", when what actually
            # happened is that this branch never looked.
            #
            # The predicate is `collected is not None`, over the PARSE, rather
            # than `red.exit_code in EXIT_COLLECTION_FAILURES`, over the code.
            # Same claim on pytest -- `errored_files` is `None` on exactly the
            # codes outside that tuple -- and it is the claim that survives a
            # framework with no exit codes to consult.
            reported_desc = (
                sorted(collected) if collected
                else "empty" if collected is not None
                else f"not parsed (exit {red.exit_code} is not a collection failure)"
            )
            problems.append(
                f"the f2p tests did not run at the start state -- "
                f"{adapter.explain(red.exit_code)}. This is the Phase 0c "
                "failure: a "
                "broken environment is also a non-zero exit, and an agent "
                "reading the output cannot tell it from the bug. A collection "
                "error IS accepted, but only when every reported ERROR names a "
                "declared f2p module and no other, and here the reported set "
                f"is {reported_desc} against "
                f"declared {sorted(f2p_modules(tests.f2p))}.\n"
                + (red.stdout or red.stderr)[-2000:]
            )
        else:
            # Off the OUTCOME, never off a re-parse of stdout. Byte-identical
            # on pytest: this branch is reached only at KIND_FAILED, which is
            # exit 1, and the adapter fills `failed_ids` from exactly the
            # `failed_node_ids(stdout + stderr)` this line used to call.
            #
            # On node it is the difference between a gate and a NO-GO on every
            # task. vitest and jest report their failures as a human summary --
            # to stdout and to STDERR respectively, measured -- and neither
            # writes a `FAILED <id>` line for pytest's regex to find, so the
            # reported set came back EMPTY and every declared f2p id read as
            # "did not fail at the start state" on a run in which it failed.
            # Caught by `scripts/mutation_check.py`, not by a test: the two
            # scoped-run anchors reported MISSED because this problem was
            # already keeping `result.ok` false on a healthy node task.
            missing = set(tests.f2p) - set(red_outcome.failed_ids)
            if missing:
                problems.append(
                    "declared f2p tests did not fail at the start state: "
                    + ", ".join(sorted(missing))
                )

        if not p2p_green:
            problems.append(
                f"the rest of the suite is not green at the start state -- "
                f"{adapter.explain(green.exit_code)}. A p2p regression "
                "check against "
                "an already-red suite cannot mean anything.\n"
                + (green.stdout or green.stderr)[-2000:]
            )

        # --- the suite does not dirty the tree

        status = container.exec(["git", "status", "--porcelain"]).stdout.strip()
        evidence["dirty_after_tests"] = status
        if status:
            problems.append(
                "running the suite leaves the tree dirty:\n"
                + status[:1000]
                + "\nSection 5.6 stages everything, so these land in every "
                "submission diff and diff size measures the interpreter rather "
                "than the agent. Add them to the manifest's gitignore_extra."
            )

        # The SECOND emptiness read, and it exists because the check
        # immediately above cannot see into a gitlink path. Measured
        # 2026-09-02: `git status --porcelain` reports nothing for a file
        # written inside an uninitialised submodule directory, so "the suite
        # ran with the directory empty" is an assumption without this and an
        # observation with it. A MAPPING, so the stored evidence tells `False`
        # (has content) from `None` (the read failed) per path rather than
        # leaving that to the problem text; `{}` is "this task declares none".
        after = {path: _directory_is_empty(container, path)
                 for path in sorted(unneeded)}
        evidence["submodules_empty_after_suite"] = after
        not_empty = sorted(p for p, ok in after.items() if ok is False)
        unreadable = sorted(p for p, ok in after.items() if ok is None)
        if not_empty:
            problems.append(
                "running the suite left content in "
                + ", ".join(not_empty)
                + ", which the manifest declares unneeded. `git status "
                "--porcelain` -- the check immediately above -- cannot see "
                "into a gitlink path, so this is the only reader that can. A "
                "suite that writes inside a submodule is out of the corpus."
            )
        if unreadable:
            problems.append(
                "could not list "
                + ", ".join(unreadable)
                + " after the suite ran, so whether the suite wrote into a "
                "declared-unneeded submodule is UNKNOWN -- the read failed, "
                "which is not the same answer as the directory having "
                "content. `git status --porcelain` cannot see into a gitlink "
                "path, so nothing else in this gate answers it either."
            )

        # --- green after

        patch = Path(repo_path) / ".bakeoff-solution.patch"
        patch.write_text(task.solution_diff)
        try:
            # NO `--index`, and its ABSENCE is load-bearing. `materialize`
            # writes this tree's index on the HOST; `git apply --index`
            # compares the index's CACHED STAT DATA rather than content
            # (`ce_match_stat`), and virtiofs reports `st_dev`, `st_ino`,
            # `st_uid` and `st_gid` differently inside the container -- so
            # `--index` refuses a patch that applies, on a clean tree, and
            # preflight would NO-GO every task on a Docker Desktop host.
            # Nothing here needs the patch staged, so nothing here needs the
            # refresh `grader._refresh_index` performs for the one call site
            # that does.
            applied = container.exec(["git", "apply", ".bakeoff-solution.patch"])
        finally:
            patch.unlink(missing_ok=True)

        if applied.exit_code != 0:
            problems.append(
                "the reference fix does not apply to the start state: "
                + (applied.stderr or applied.stdout).strip()[:1000]
            )
        else:
            after_f2p = runner.select(tests.f2p)
            evidence["f2p_after_exit"] = after_f2p.exit_code
            evidence["bounded_run_durations_s"]["f2p_after"] = _elapsed_s(after_f2p)
            if _note(runner.classify(after_f2p), runner.last_report).kind != KIND_PASSED:
                problems.append(
                    f"the f2p tests do NOT pass after the reference fix -- "
                    f"{adapter.explain(after_f2p.exit_code)}. A solved run "
                    "and an "
                    "idle run would leave identical evidence. The usual cause "
                    "is a non-editable install: imports resolve to "
                    "site-packages, so nothing the agent writes to /repo has "
                    "any effect.\n"
                    + (after_f2p.stdout or after_f2p.stderr)[-2000:]
                )
            after_p2p = runner.pass_to_pass(tests)
            evidence["p2p_after_exit"] = after_p2p.exit_code
            evidence["bounded_run_durations_s"]["p2p_after"] = _elapsed_s(after_p2p)
            if _note(runner.classify(after_p2p), runner.last_report).kind != KIND_PASSED:
                problems.append(
                    "the reference fix regresses the rest of the suite -- "
                    f"{adapter.explain(after_p2p.exit_code)}. The reference "
                    "is the "
                    "oracle; if it cannot pass, no submission can.\n"
                    + (after_p2p.stdout or after_p2p.stderr)[-2000:]
                )

            # --- each declared grading argv runs clean on the reference
            #
            # A typo'd `typecheck:`, or a tool the image does not ship, is
            # otherwise stamped as typecheck_failed on every record of this
            # task, permanently, in an append-only store -- an accusation
            # against every arm for the task author's error.
            #
            # BEFORE the scoped p2p, because the grader's ladder runs
            # build/typecheck (checks 3-4) before p2p (check 6): a grading
            # command can write into the tree -- a build artifact, a mypy or
            # ruff cache -- and the scoped run has to be measured on the tree
            # the graded one will actually see, not on a cleaner one.
            for key, argv in _declared_grading(task):
                checked = container.exec(["timeout", str(timeout_s), *argv])
                evidence[f"grading_{key}_exit"] = checked.exit_code
                evidence["bounded_run_durations_s"][f"grading_{key}"] = _elapsed_s(checked)
                if checked.exit_code != EXIT_ALL_PASSED:
                    problems.append(
                        f"the declared grading.{key} command exits "
                        f"{checked.exit_code} at the post-fix state: "
                        f"{' '.join(argv)}. The reference is the oracle; a "
                        f"command it cannot satisfy would record "
                        f"{key}_failed against every submission.\n"
                        + (checked.stdout or checked.stderr)[-2000:]
                    )

            # --- the SCOPED p2p is green, which is what the grader runs
            #
            # Check 6 scopes p2p to tests.paths, because an agent's scratch
            # files at the rootdir get collected and counted otherwise. The
            # first draft of that design claimed rootdir-wide green "strictly
            # implies" scoped green; it does not, since scoping changes
            # fixture setup and ordering. So it is measured here instead.
            #
            # Deselect branch only: pass_to_pass ignores `scope` when an
            # explicit p2p list is declared, so running this there pays a full
            # suite invocation to re-assert the selection already validated
            # ten lines up.
            if not tests.p2p:
                scope = _existing_prefixes(container, tests.paths)
                evidence["scope_prefixes"] = list(scope)
                absent = [p for p in tests.paths if p not in scope]
                if absent:
                    # Recorded, never a problem: `ok` is `not problems`, and
                    # the grader's restore step tolerates exactly this input.
                    # A problem here re-arms the NO-GO the filter prevents.
                    evidence["scope_prefixes_absent"] = absent
                    problem_codes.append(SCOPE_PREFIX_MISSING)
                if not scope:
                    problem_codes.append(SCOPE_COLLECTS_NOTHING)
                    problems.append(
                        "none of the declared tests.paths "
                        f"({', '.join(tests.paths)}) exist after the reference "
                        "fix, so the grader's scoped p2p run would collect "
                        "nothing and every submission would be graded against "
                        "an empty regression check"
                    )
                else:
                    scoped = runner.pass_to_pass(tests, scope=scope)
                    evidence["p2p_scoped_after_exit"] = scoped.exit_code
                    evidence["bounded_run_durations_s"]["p2p_scoped_after"] = _elapsed_s(scoped)
                    # Classified IMMEDIATELY after its own invocation, and
                    # bound to a name: three assertions read this one run, and
                    # `classify` reads `_Runner.last_report`, which the next
                    # `run` overwrites.
                    scoped_outcome = _note(runner.classify(scoped), runner.last_report)

                    # Which EXECUTED tests under the scope share a full name
                    # across files. EVIDENCE, not a problem: a node selection
                    # and a node deselection are now one argv per FILE, each
                    # carrying only that file's own titles, so a quarantine of
                    # `a::works` no longer reaches `b::works` and the hazard
                    # this list named is unreachable. It is kept because the
                    # collision is still a fact about the task worth having in
                    # a stored verdict -- and because it is what a reader of an
                    # older NO-GO comes here to look up.
                    #
                    # CROSS-FILE ONLY, by construction: `seen` keys on the name
                    # and reports a collision only when the paths differ. Two
                    # tests sharing a full name in ONE file collapse to the
                    # identical node id string and are invisible to this loop
                    # and to the loader alike; that half is tracked in
                    # `TASKS.md` and is not this key's.
                    seen: dict[str, str] = {}
                    duplicates: list[str] = []
                    for path, name in adapter.executed_names(runner.last_report):
                        first = seen.setdefault(name, path)
                        if first != path:
                            duplicates.append(f"{name!r} in {first} and {path}")
                    # Overwrites the `None` set before the container block.
                    # On an explicit-`tests.p2p` task this branch never runs
                    # and the key stays `None` -- "no scoped run was made",
                    # which is a different fact from "no duplicates were
                    # found", and the two must not render identically.
                    #
                    # A THIRD ABSENCE, and the one this key had wrong: a scoped
                    # run that wrote NO REPORT AT ALL. `executed_names(None)`
                    # yields nothing, so `duplicates` came out `[]` --
                    # byte-identical to "the report was read and holds no
                    # collision" -- for a run that measured nothing whatever.
                    # Measured, that is exactly what a broken config gives on
                    # both node frameworks: exit 1 and no file. The verdict is
                    # still NO-GO (the scoped run is not KIND_PASSED, forty
                    # lines down), but the evidence outlives the verdict, and
                    # it was saying the duplicate check ran and cleared.
                    #
                    # `report_path() is None` is NOT that absence, which is why
                    # the guard is not a bare `last_report is not None`. pytest
                    # writes no report BY DESIGN and its `executed_names` is
                    # empty as a CLAIM -- a pytest node id carries its file, so
                    # `--deselect a.py::test_x` cannot reach `b.py::test_x` and
                    # the hazard does not exist there. A missing report is an
                    # absence only on a framework that writes one.
                    evidence["duplicate_full_names"] = (
                        duplicates
                        if adapter.report_path() is None
                        or runner.last_report is not None
                        else None
                    )

                    files_run = scoped_outcome.files_run
                    # Overwrites the `None`s above; stays `None` on the
                    # explicit-p2p branch, which makes no scoped run, and on
                    # pytest, whose adapter reports no file list at all.
                    evidence["scope_files_run"] = (
                        list(files_run) if files_run is not None else None)
                    # `_under` from `bakeoff.tasks` -- the same component-wise
                    # matcher the diff split uses, never `startswith`, which
                    # is the mistake the runner's own filter already makes
                    # (`tests_helpers/x` starts with `tests` and is not under
                    # `tests/`).
                    #
                    # `None` WHEN `files_run` IS, and that is the same
                    # correction one key up rather than a separate one: `[]`
                    # here read as "measured, nothing left the scope" for two
                    # runs that measured nothing -- a pytest run, whose adapter
                    # reports no file list at all (and whose `scope_files_run`
                    # already said `None` two lines above, so the pair
                    # contradicted each other), and a node run that wrote no
                    # report, where the whole point of the check is that a
                    # positional argument is a substring filter and nobody can
                    # say what it matched. The `if outside:` below is unchanged
                    # by this: `None` is falsy exactly as `[]` was, so no
                    # verdict moves -- only the evidence stops claiming.
                    outside = (
                        None if files_run is None
                        else [p for p in files_run if not _under(p, scope)])
                    evidence["scope_files_outside"] = outside
                    if outside:
                        problems.append(
                            "the scoped p2p run executed files outside "
                            f"tests.paths ({', '.join(scope)}): "
                            + ", ".join(outside)
                            + ". Measured 2026-09-01: a positional argument is "
                            "a SUBSTRING FILTER over the absolute file path on "
                            "vitest and a REGEX on jest, not a path -- `vitest "
                            "run tests/` matched `/repo/jtests/fail.test.cjs`. "
                            "The scoped run exists to keep the agent's scratch "
                            "files out of the regression check, and a filter "
                            "that over-matches restores exactly what it was "
                            "added to remove. Narrow tests.paths, or rename "
                            "the sibling directory in the task repo."
                        )

                    if scoped_outcome.kind != KIND_PASSED:
                        # The OUTCOME, not the exit code -- the last raw one
                        # in this file. Exit 5 is pytest's and only pytest's:
                        # measured 2026-09-01, vitest and jest answer a scope
                        # matching no file with 1 and a `-t` matching nothing
                        # inside a file that loaded with 0, so a mis-scoped
                        # node task fell through to the generic
                        # `PREFLIGHT_FAILED` reason and sent its author
                        # looking for a bug in the task instead of at
                        # `tests.paths`. `KIND_NOTHING_RAN` is pytest-identical
                        # -- `pytest_adapter.classify` returns exactly it for
                        # exit 5 -- so the pytest branch is unchanged.
                        if scoped_outcome.kind == KIND_NOTHING_RAN:
                            problem_codes.append(SCOPE_COLLECTS_NOTHING)
                        problems.append(
                            "the p2p run the GRADER will make is not green "
                            f"after the reference fix -- "
                            f"{adapter.explain(scoped.exit_code)}. Scoping to "
                            "tests.paths changes what is collected, so a green "
                            "rootdir run does not settle this one.\n"
                            + _argv_lines(runner)
                            + (scoped.stdout or scoped.stderr)[-2000:]
                        )

        # Leave the tree exactly as it was found.
        #
        # Not load-bearing today and deliberately kept anyway: the matrix
        # materializes a fresh tree per run and never reuses this one, so a
        # left-behind reference fix could not reach an arm. It is here because
        # that separation is a property of one caller rather than of this
        # function, and a preflight tree still holding the answer is a trap
        # for the next caller and for anyone who inspects it by hand.
        container.exec(["git", "checkout", "--force", "--detach", start_sha])
        container.exec(["git", "clean", "-xfd"])

    # Every node run this gate made, together: `select_argvs` groups by file
    # too, and an explicit-`tests.p2p` task makes no scoped run at all, so a
    # check reading the scoped run alone would be covered by nothing on those
    # tasks. At the function's own indentation because both branches have
    # rejoined here and it needs no container.
    #
    # `[]` is "measured, no declared file's filter reaches another executed
    # one"; the schema's `None`, which `files_measured` leaves in place, is
    # "not measured on this path" -- pytest reports no file list at all, and a
    # node gate whose every run wrote no report measured nothing.
    ambiguous = (
        [f"{a!r} also selects {b}"
         for a in sorted(seen_files) for b in sorted(seen_files)
         if a != b and adapter.file_filter_matches(a, b)]
        if files_measured else None
    )
    evidence["ambiguous_file_filters"] = ambiguous
    if ambiguous:
        problems.append(
            "one declared test file's selection filter also selects another "
            "executed file: " + ", ".join(ambiguous)
            + ". Measured 2026-09-02: vitest's positional is a SUBSTRING filter "
            "that no anchoring reaches -- `/repo/tests/doc/a.test.js` still "
            "matched `/repo/pkg/tests/doc/a.test.js` -- so the per-file grouping "
            "this harness selects and deselects with would carry a name pattern "
            "into a file it does not name, and a quarantine would silently "
            "remove a test from the regression check. jest anchors at the mount "
            "and cannot hit this. Rename or move one of the files in the task "
            "repo, or declare an explicit tests.p2p that does not reach either "
            "file; see taskset/HARVESTING.md."
        )

    evidence["same_file_duplicate_ids"] = (
        dict(sorted(same_file_dupes.items())) if dupes_measured else None)
    # DECLARED ids only, and the boundary is about what THIS component can
    # prove. For a declared id the gate proves the ambiguity from its own runs:
    # the f2p-before run shows two terminal assertions under the one id at
    # `passed` and `failed` (measured), the f2p-after run shows two at
    # `passed`, so red-before and green-after are satisfied by different tests
    # and nothing downstream can say which -- `failed_ids` collapses them and
    # `verify_selected` reports nothing missing. For an UNDECLARED duplicate
    # the gate proves only that both twins ran and both passed; the harm needs
    # the id to reach the quarantine, which `oracle._derive` computes at GRADE
    # time from two reference runs this gate never makes.
    #
    # The accepted residual, stated because a residual nobody records is
    # silence: a flaky undeclared twin CAN be quarantined, `_derive`
    # deselects by node id, and that removes BOTH twins from check 6 -- so a
    # submission that broke the healthy one can still be `resolved: True`. The
    # two halves of the audit are the map above, in this task's cached
    # preflight verdict, and `GradeRecord`'s quarantine; NOTHING joins them,
    # and the intersection is the offline reader's to make.
    declared = frozenset(tests.f2p) | frozenset(tests.p2p)
    declared_dupes = {node_id: count
                      for node_id, count in sorted(same_file_dupes.items())
                      if node_id in declared}
    if declared_dupes:
        problems.append(
            "these declared test ids each name MORE THAN ONE test in their "
            "own file: "
            + "; ".join(f"{node_id!r} names {count} tests"
                        for node_id, count in declared_dupes.items())
            + f". A {adapter.name} id is `<file>::<fullName>` with no "
            "positional index, so two identically titled tests in one file "
            "collapse to the SAME id and there is no way to declare, select "
            "or deselect one of them. Measured 2026-09-02 (vitest 3.2.7, jest "
            "30.5.0) on a file holding two tests titled `outer adds`: `-t "
            "'^(?:outer adds)$'` ran BOTH on both frameworks -- one passed and "
            "one failed in the same run -- so the red-before check is "
            "satisfied by whichever of them fails and the green-after check by "
            "both passing, with nothing saying they are the same test; and a "
            "quarantine of the id deselects both, with p2p_deselected "
            "AGREEING, because two tests really were skipped. Declare a "
            "different test id, or cut the task from a PR whose tests are "
            "uniquely titled; see taskset/HARVESTING.md."
        )

    # ONE null-aware maximum, here, rather than at each reader. `max(())`
    # raises ValueError and `max([1.0, None])` raises TypeError, so a reader
    # computing this for itself gets it wrong on exactly the two verdicts
    # that matter: the task where a run was skipped and the gate that never
    # started a container. Unreached on the early return, where the seed's
    # `None` is the honest answer -- nothing ran.
    measured = [v for v in evidence["bounded_run_durations_s"].values()
                if v is not None]
    evidence["bounded_run_duration_max_s"] = max(measured) if measured else None

    return PreflightResult(
        task_id=task.task_id,
        task_version=task.task_version,
        start_sha=start_sha,
        image=image,
        manifest_digest=task.manifest_digest,
        problems=tuple(problems),
        evidence=evidence,
        preflight_version=PREFLIGHT_VERSION,
        problem_codes=tuple(problem_codes),
    )
