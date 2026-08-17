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

from bakeoff.container import RunContainer
from bakeoff.preflight import (
    EXIT_ALL_PASSED,
    EXIT_TESTS_FAILED,
    _EXIT_MEANING,
    _Runner,
    _existing_prefixes,
    failed_node_ids,
)
from bakeoff.tasks import materialize

#: What this derivation asserts, as a version, and it is in the fingerprint for
#: the reason `PREFLIGHT_VERSION` is in preflight's key: neither the manifest
#: digest nor the image digest moves when THIS file changes, so without it a
#: warm cache serves a quarantine derived by the old rules and a change to
#: those rules is inert on exactly the tasks about to be graded.
ORACLE_VERSION: str = "1"

#: preflight's pytest exit meanings plus the codes the `timeout` wrapper and
#: the shell contribute. Non-{0,1} is refused whatever the code, but the
#: message has to name a cause an operator can act on: 127 is a runner that is
#: not on PATH, 137 is the container being killed (OOM, most often), and each
#: of those reads as "no tests failed" to a classifier that only checks for
#: exit 1.
_ORACLE_EXIT_MEANING = {
    **_EXIT_MEANING,
    125: "the `timeout` wrapper itself failed",
    126: "the runner was found but could not be executed",
    127: "the runner is not on PATH in this image",
    137: "the command was killed (SIGKILL -- usually the container OOM)",
}


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


def _classify(result) -> set[str]:
    """What failed in one p2p run, or a refusal that the run did not happen.

    The whole point of reading pytest's exit code rather than `!= 0` is the
    Phase 0c failure one module over: 2/3/4/5 are non-zero and mean the
    environment is broken, not that a test failed. Here the polarity is worse
    than in preflight -- a broken run reports NO failed node ids, so a
    classifier that only asked "did anything fail?" would read a suite that
    never collected as a clean run and derive an empty quarantine from two of
    them.
    """
    code = result.exit_code
    if code == EXIT_ALL_PASSED:
        return set()
    if code == EXIT_TESTS_FAILED:
        return failed_node_ids(result.stdout + result.stderr)
    meaning = _ORACLE_EXIT_MEANING.get(code)
    detail = f" -- {meaning}" if meaning else ""
    raise OracleError(
        f"the p2p run at the reference state exited {code}{detail}. The "
        "quarantine is derived from which tests failed, and a run that did "
        "not happen reports none -- so this would be indistinguishable from a "
        "clean run and would quarantine nothing.\n"
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
    first = _classify(runner.pass_to_pass(tests, scope=scope))
    second = _classify(runner.pass_to_pass(tests, scope=scope))

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
        raise OracleError(
            "the quarantine covers the entire declared p2p list ("
            + ", ".join(sorted(tests.p2p))
            + "), so the graded run would deselect every node id it selects "
            "and pytest would exit 5. That surfaces as an ungraded record "
            "rather than as the broken oracle it is."
        )

    return quarantine


def _derive(task, image: str, cache_root: Path, timeout_s: int) -> tuple[str, ...]:
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
    tree = Path(cache_root) / "oracle-tree" / task.task_id
    shutil.rmtree(tree, ignore_errors=True)
    start_sha = materialize(task, tree / "repo", cache_root)

    try:
        with RunContainer(image=image, repo_path=str(tree / "repo"),
                          base_sha=start_sha) as container:
            patch = tree / "repo" / ".bakeoff-solution.patch"
            patch.write_text(task.solution_diff)
            try:
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

            runner = _Runner(container, task.tests.runner, timeout_s)
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


def ensure_oracle(task, image: str, cache_root: Path,
                  timeout_s: int = 600) -> Oracle:
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
        quarantined=tuple(_derive(task, image, cache_root, timeout_s)),
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
