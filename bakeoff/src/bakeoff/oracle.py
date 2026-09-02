"""The manifest is the oracle; this module derives only the flake quarantine.

Spec section 4.2.1. A task's `tests.f2p` and `tests.p2p` are DECLARED, by the
harvester, and preflight has already proved that declaration describes the
repository: f2p red at the start state, green after the reference fix, and the
rest of the suite green on both sides. Re-deriving those sets here would be a
second answer to a question that already has one, and the second answer is the
one nothing gates. So this module computes exactly one thing the manifest
cannot state -- which of the declared tests are UNSTABLE at the reference
state, and therefore cannot be evidence against a submission.

The derivation is two identical p2p runs at the post-fix state and the
symmetric difference of what failed. That is the whole of it, and its limits
are the reason it is not the only defence:

* It catches LOUD flakes only. A test that fails 5% of the time passes two
  draws about 90% of the time, so most genuine flakes are invisible here.
  `p2p_failed_node_ids` is recorded per grade, and the offline cross-arm view
  is the second line: a node id that fails on some arms and passes on others,
  across the matrix, is a flake with far more draws behind the claim than two.
  This module exists to keep the obvious ones from costing a whole task; it
  does not claim to find them all.

* A test red in BOTH runs is not a flake and is not quarantined -- it raises.
  Red-in-both at the reference state means the oracle itself is broken (a
  dependency the image does not ship, a test that needs network, an order
  dependence the scoped run exposes), and quarantining it would silently
  shrink the regression check on every submission of that task, forever, in an
  append-only store. Preflight should already have refused such a task; if it
  did not, the disagreement is a defect to surface, not to absorb.

* The two runs use the SAME scope the grader's check 6 uses (`tests.paths`),
  because `--deselect` of a node id pytest did not collect is IGNORED
  (measured), not an error. A quarantine derived at the rootdir yields ids
  that are silent no-ops under the scoped grading run -- a quarantine that
  reads as applied and subtracts nothing.

* A quarantine that swallows an entire declared `p2p` list raises. Deselecting
  every selected node id makes pytest exit 5, which the grader records as an
  infrastructure problem -- so the task would surface as a pile of ungraded
  records rather than as the broken oracle it is.

The cache is VERDICT-ONLY. Nothing on disk here is an artifact a later run
consumes; the file holds the two derived ids and the fingerprint that says
what they describe. That is the distinction the pruned-mirror cache got wrong
(HANDOFF.md): a cached artifact has to be re-verified against itself, because
the key describes it from the outside. A cached verdict does not -- a
fingerprint mismatch is the only way it can be wrong, and a mismatch
re-derives. For the same reason an unreadable entry rebuilds rather than
raising: raising leaves the damaged file on disk and the task ungradable until
an operator deletes it by hand.

The fingerprint refuses a tag. `bakeoff-task-click:v1` names whatever was last
built under that tag, so a rebuilt image serves the old quarantine under a key
that still matches -- the "an older revision's output is served forever"
defect, which is why every cache key in this codebase takes a digest.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from bakeoff.container import RunContainer, fresh_tree
from bakeoff.preflight import (
    _Runner,
    _existing_prefixes,
)
from bakeoff.runners import KIND_FAILED, KIND_PASSED, for_framework
from bakeoff.runners.pytest_adapter import _PROCESS_EXIT_MEANING
from bakeoff.tasks import materialize

#: What this derivation asserts, as a version, and it is in the fingerprint for
#: the reason `PREFLIGHT_VERSION` is in preflight's key: neither the manifest
#: digest nor the image digest moves when THIS file changes, so without it a
#: warm cache serves a quarantine derived by the old rules and a change to
#: those rules is inert on exactly the tasks about to be graded.
#:
#: 1 -> 2: the derivation's two reference runs are bounded by
#: `budget.suite_timeout_s` rather than by a function default. The manifest
#: digest does not cover it -- a manifest that already carries the key loads
#: under the older loader, which ignores unknown `budget` sub-keys -- so
#: without this every cached quarantine would be one derived at 600 against a
#: manifest asking for something else.
#: 2 -> 3: `_classify` reads an `Outcome` rather than an exit code. A
#: quarantine cached under 2 was derived by rules that could not classify a
#: node run at all -- vitest and jest answer a failing test, an unresolvable
#: import, a syntax error and a broken config with exit 1 alike -- and whose
#: "the quarantine swallowed the whole p2p list" guard depended on pytest's
#: exit 5, which those frameworks answer with 0 and a report of every test
#: skipped. No quarantine on today's corpus changes: every stored task is a
#: pytest one and the pytest adapter maps each exit code onto the kind this
#: derivation already branched on.
#:
#: 3 -> 4: `pytest_adapter._FAILED_LINE` learned `SUBFAILED` (fix 3,
#: 2026-09-02). `_classify` takes `set(outcome.failed_ids)` straight from that
#: regex on `KIND_FAILED`, and `derive_quarantine` XORs the two reference
#: runs' sets -- so a p2p node that flakes only through `unittest.subTest`
#: under pytest's core-integrated subtests (pytest >= 9) was invisible to
#: both runs' sets under 3 and could never land in the quarantine, whatever it
#: did across the two references. A quarantine derived under 3 for a task with
#: such a node silently omits it, and `_check_p2p` then leaves it selected at
#: grade time -- P2P_REGRESSION on a submission the flaky node never touched.
#: No quarantine on today's corpus changes retroactively without a re-run:
#: the fix widens what the SAME two reference runs can report, so a cached
#: verdict must be re-derived, not merely re-read, to pick it up.
#:
#: 4 -> 5: node selection and deselection became per-file (round 2 item 1,
#: 2026-09-03). `_derive`'s two reference runs are now the per-file argv
#: sequence, so a quarantine cached under 4 for a NODE task was derived from
#: runs in which a deselection crossed files -- it can name an id that never
#: needed quarantining, and miss one whose own file's run was distorted by
#: another file's deselection. No pytest quarantine changes: that adapter
#: emits one group whose argv is the v4 argv. A cached node verdict must be
#: re-derived rather than re-read.
ORACLE_VERSION: str = "5"

#: preflight's pytest exit meanings plus the codes the `timeout` wrapper and
#: the shell contribute. Non-{0,1} is refused whatever the code, but the
#: message has to name a cause an operator can act on: 127 is a runner that is
#: not on PATH, 137 is the container being killed (OOM, most often), and each
#: of those reads as "no tests failed" to a classifier that only checks for
#: exit 1. Imported rather than re-defined -- `preflight.py`'s own bare-probe
#: gate wants the exact same codes and this module already imports from
#: `preflight.py`, so a second copy here would be the thing that drifts.
_ORACLE_EXIT_MEANING = _PROCESS_EXIT_MEANING


class OracleError(RuntimeError):
    """The quarantine could not be derived, so the task cannot be graded.

    Loud on purpose. Every condition that raises here would otherwise produce
    an empty or an over-broad quarantine, and both are silent: an empty one
    lets a known-unstable test fail a correct submission, an over-broad one
    subtracts the regression check the grade is supposed to make.
    """


@dataclass(frozen=True)
class Oracle:
    #: What this quarantine describes -- (manifest digest, image digest,
    #: ORACLE_VERSION). A mismatch is the only way a stored verdict can be
    #: wrong about the thing it was derived from, which is what makes the
    #: cache safe to trust without re-checking the artifact.
    fingerprint: str
    quarantined: tuple[str, ...]
    oracle_version: str = ORACLE_VERSION

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "quarantined": list(self.quarantined),
            "oracle_version": self.oracle_version,
        }


def oracle_fingerprint(task, image: str) -> str:
    """The cache key, which refuses a tag before it hashes anything.

    A tag is not an identity: `docker build -t bakeoff-task-click:v1` moves it,
    and a fingerprint built over the moved tag still matches the entry derived
    against the image it used to name. The digest check is first rather than
    folded into the hash because a hash over a tag is not a weaker key, it is a
    wrong one, and there is nothing downstream that could notice.
    """
    if "sha256:" not in image:
        raise OracleError(
            f"{image!r} is not a digest-pinned image: a tag moves when the "
            "image is rebuilt, so the cached quarantine would keep matching a "
            "key that no longer names the environment it was derived in"
        )
    payload = f"{task.manifest_digest}|{image}|{ORACLE_VERSION}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _classify(outcome, result) -> set[str]:
    """What failed in one p2p run, or a refusal that the run did not happen.

    The whole point of reading pytest's exit code rather than `!= 0` is the
    Phase 0c failure one module over: 2/3/4/5 are non-zero and mean the
    environment is broken, not that a test failed. Here the polarity is worse
    than in preflight -- a broken run reports NO failed node ids, so a
    classifier that only asked "did anything fail?" would read a suite that
    never collected as a clean run and derive an empty quarantine from two of
    them.

    Stated over `Outcome.kind` rather than over an exit code, because on vitest
    and jest there is no exit code to state it over: measured 2026-09-01, both
    return 1 for a failing test and for a broken config, and BOTH return 0 for
    a `-t` pattern that matched nothing -- which is exactly what a quarantine
    covering the whole p2p list produces. `KIND_NOTHING_RAN` is what carries
    that now; the exit code used to.
    """
    if outcome.kind == KIND_PASSED:
        return set()
    if outcome.kind == KIND_FAILED:
        return set(outcome.failed_ids)
    meaning = _ORACLE_EXIT_MEANING.get(outcome.exit_code) or outcome.explain
    detail = f" -- {meaning}" if meaning else ""
    # `result` is carried alongside the outcome solely for this tail, which the
    # exit-code version already had. Every path through here is a BROKEN
    # ORACLE, which is the failure an operator has to debug from the message
    # alone -- so dropping the output would be the one regression this refactor
    # could make that no test would see.
    raise OracleError(
        f"the p2p run at the reference state exited {outcome.exit_code} "
        f"({outcome.kind}){detail}. The quarantine is derived from which tests "
        "failed, and a run that did not happen reports none -- so this would "
        "be indistinguishable from a clean run and would quarantine nothing.\n"
        + (result.stdout or result.stderr)[-2000:]
    )


def derive_quarantine(runner, tests, scope: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Node ids that failed in exactly one of two identical reference runs.

    Classification is EAGER -- each result is classified before the next run
    is started. Collecting both results first and classifying afterwards reads
    the same until the first run is the broken one, at which point the second
    invocation has already been paid for (a full suite, under the timeout) to
    produce a verdict that is about to be discarded.

    `scope` is passed through to BOTH runs and must be the scope the grader
    uses. See the module docstring: an id derived outside the graded scope
    deselects nothing and does it silently.
    """
    first_result = runner.pass_to_pass(tests, scope=scope)
    first = _classify(runner.classify(first_result), first_result)
    second_result = runner.pass_to_pass(tests, scope=scope)
    second = _classify(runner.classify(second_result), second_result)

    both = first & second
    if both:
        raise OracleError(
            "these tests failed in BOTH reference runs: "
            + ", ".join(sorted(both))
            + ". That is not a flake -- the reference state is the oracle, so "
            "a test that is reliably red there means the task's own p2p "
            "declaration is wrong. Quarantining it would shrink the "
            "regression check on every submission of this task instead."
        )

    quarantine = tuple(sorted(first ^ second))

    if tests.p2p and set(tests.p2p) <= set(quarantine):
        # Ids against ids, which is exact only while every declared p2p entry
        # is a LEAF node id. A declared non-leaf (a module or a class) selects
        # many items that quarantined leaf ids can never be a superset of, so
        # this predicate fails OPEN there -- a selection deselected down to
        # nothing gets past it. Accepted rather than papered over: the
        # grade-time surface is a NAMED scope_collected_nothing / exit-5
        # record, not silence, and the leaf-shape requirement on `tests.p2p`
        # goes into HARVESTING.md (Task 7) where the set is authored.
        #
        # On pytest this surfaces because deselecting every selected id makes
        # pytest exit 5. On vitest and jest it exits **0** with every test
        # reported skipped (measured 2026-09-01) -- so what carries it there is
        # `classify`'s rule that zero assertions with a terminal status is
        # `KIND_NOTHING_RAN` whatever the exit code was. The guard's claim is
        # unchanged; the mechanism behind it is no longer the exit code.
        raise OracleError(
            "the quarantine covers the entire declared p2p list ("
            + ", ".join(sorted(tests.p2p))
            + "), so the graded run would deselect every node id it selects "
            "and the suite would report nothing collected. That surfaces as "
            "an ungraded record rather than as the broken oracle it is."
        )

    return quarantine


def _derive(task, image: str, cache_root: Path) -> tuple[str, ...]:
    """The Docker-touching half: a fresh tree at the reference state, twice run.

    Separated from `derive_quarantine` so the derivation's semantics are
    testable without a daemon, and so `ensure_oracle`'s cache behaviour is
    testable without either.

    The tree is rebuilt rather than reused: `materialize` refuses an existing
    destination on purpose (a shared tree lets one derivation start from
    another's dirty state), and this function applies the solution diff. It is
    also removed on the way OUT, on the ground preflight restores its own tree
    on: a tree left holding the reference fix is a trap for the next caller
    and for anyone who inspects the cache by hand. In a `finally`, because the
    paths that leave it behind are exactly the failure paths -- a diff that
    does not apply, a refused exit code, a red-in-both refusal.
    """
    # A path no container has mounted, never `task_id` alone. The Docker VM
    # caches the directory it serves for a bind-mount source, so a second
    # derivation on one path is served the cached, EMPTY copy (measured
    # 2026-09-02, `[files], [], [files], []` over four cycles) -- and against
    # an empty tree the reference fix does not apply, so `_derive` raises and
    # a sound task becomes ungradable. Loud, but for the wrong reason.
    tree = fresh_tree(Path(cache_root) / "oracle-tree" / task.task_id)
    start_sha = materialize(task, tree / "repo", cache_root)

    try:
        with RunContainer(image=image, repo_path=str(tree / "repo"),
                          base_sha=start_sha) as container:
            patch = tree / "repo" / ".bakeoff-solution.patch"
            patch.write_text(task.solution_diff)
            try:
                # NO `--index`, deliberately. The index under this tree was
                # written by `materialize` on the HOST, and `git apply
                # --index` compares the index's cached STAT DATA rather than
                # content (`ce_match_stat`) -- `st_dev`, `st_ino`, `st_uid`
                # and `st_gid` all read differently through virtiofs, so it
                # refuses a patch that applies and the derivation would raise
                # "the reference fix does not apply" on every task. The
                # quarantine needs the fix in the WORKING TREE and never in
                # the index, so the absence costs nothing; `grade_run`, whose
                # restore does need `--index`, pays for it with
                # `grader._refresh_index`.
                applied = container.exec(
                    ["git", "apply", ".bakeoff-solution.patch"])
            finally:
                patch.unlink(missing_ok=True)
            if applied.exit_code != 0:
                raise OracleError(
                    "the reference fix does not apply, so there is no "
                    "reference state to derive a quarantine at: "
                    + (applied.stderr or applied.stdout).strip()[:1000]
                )

            # Off the manifest, and the parameter is gone rather than
            # defaulted: `grade.py` calls `ensure_oracle(task, image,
            # Path(cache))` with no bound, so a default here would derive the
            # quarantine under a number the task does not ask for while
            # preflight gated it under one that it does. A slow-but-healthy
            # reference run then exits 124, `_classify` raises, and the task
            # becomes ungradable -- silently, since the cache stores only the
            # verdict.
            #
            # The adapter is passed EXPLICITLY, never left on `_Runner`'s
            # pytest default: this derivation's every refusal is stated over
            # what the run DID, and under jest exit 1 is what a config error,
            # an import error and a failing assertion all return alike. A call
            # site left on the default would read a broken jest run as "these
            # tests failed" and quarantine them -- shrinking the regression
            # check on every submission of that task, forever.
            runner = _Runner(container, task.tests.runner,
                             task.budget.suite_timeout_s,
                             for_framework(task.tests.framework))
            # Filtered through preflight's own existence check rather than
            # passed raw, because that is the filter the grader's check 6
            # applies. A declared prefix absent at the post-fix state is an
            # input preflight tolerates (SCOPE_PREFIX_MISSING is evidence, not
            # a problem), and an unfiltered positional prefix makes pytest
            # exit 4 -- which `_classify` would, correctly, refuse, turning a
            # gradable task into an ungradable one over a path the gate
            # deliberately let through.
            #
            # Guarded like preflight's, and for the same reason: an explicit
            # `tests.p2p` makes `pass_to_pass` ignore `scope` entirely, so
            # computing it there pays one `test -e` per declared prefix to
            # build a value nothing reads.
            scope: tuple[str, ...] = ()
            if not task.tests.p2p:
                scope = _existing_prefixes(container, task.tests.paths)
            return derive_quarantine(runner, task.tests, scope=scope)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def ensure_oracle(task, image: str, cache_root: Path) -> Oracle:
    """The task's quarantine, derived once per (manifest, image, version).

    Deriving is two full suite runs inside a container; across 80 tasks that
    is the difference between a grading pass an operator will re-run and one
    they will not. The cache holds a verdict and nothing else, so a matching
    fingerprint is the whole of the proof -- see the module docstring for why
    that is safe here and was not for the pruned mirror.
    """
    fingerprint = oracle_fingerprint(task, image)
    path = Path(cache_root) / "oracle" / f"{task.task_id}.json"

    if path.exists():
        try:
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            stored = {}
        quarantined = stored.get("quarantined") if isinstance(stored, dict) else None
        # `isinstance(..., list)`, not truthiness: `tuple("t.py::test_a")` is a
        # 13-element tuple of characters, every one of them a --deselect
        # argument pytest ignores, and the resulting Oracle is well-formed. An
        # empty list is a real verdict and must stay a hit, so the type is the
        # test and `or ()` cannot be.
        if (
            isinstance(stored, dict)
            and stored.get("fingerprint") == fingerprint
            and isinstance(quarantined, list)
        ):
            return Oracle(
                fingerprint=fingerprint,
                quarantined=tuple(quarantined),
                # Not read back from the file: the fingerprint already pins
                # ORACLE_VERSION, so a stored value that disagreed with it
                # would be describing a derivation this entry is not.
                oracle_version=ORACLE_VERSION,
            )

    oracle = Oracle(
        fingerprint=fingerprint,
        quarantined=tuple(_derive(task, image, cache_root)),
        # Explicit on this path too, so the two constructions are the same
        # statement. Leaning on the default here and naming it there makes the
        # cache-miss version look like it could differ from the cache-hit one.
        oracle_version=ORACLE_VERSION,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Plain write_text, not the atomic-rename dance the event log uses: this is
    # a verdict, re-derivable at the cost of two suite runs, and a torn write
    # fails the JSON parse above and rebuilds. The event log's records are the
    # opposite -- paid for in tokens and not re-derivable at any price.
    path.write_text(json.dumps(oracle.to_dict(), indent=2) + "\n")
    return oracle
