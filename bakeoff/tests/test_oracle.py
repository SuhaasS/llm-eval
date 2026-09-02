"""The flake quarantine and its verdict-only cache (spec section 4.2.1).

Nothing here touches Docker: `derive_quarantine` takes a runner, and the
`ensure_oracle` tests monkeypatch `_derive`, which is the only seam that
materializes a tree and starts a container.
"""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff.oracle import (
    ORACLE_VERSION,
    Oracle,
    OracleError,
    _derive,
    derive_quarantine,
    ensure_oracle,
    oracle_fingerprint,
)
from bakeoff.runners.pytest_adapter import ADAPTER


class FakeRunner:
    """pass_to_pass returns queued (exit_code, stdout) pairs in order."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def pass_to_pass(self, tests, extra_deselect=(), scope=()):
        self.calls.append({"scope": scope})
        code, out = self._results.pop(0)
        return SimpleNamespace(exit_code=code, stdout=out, stderr="")

    def classify(self, result):
        """Delegated to the REAL pytest adapter rather than stubbed.

        `_Runner.classify` does exactly this, and every pair queued above is a
        pytest exit code with pytest `-q` output -- so a hand-written mapping
        here would be a second opinion about what pytest's numbers mean,
        sitting inside the fixtures that exist to pin what the oracle does
        with them.
        """
        return ADAPTER.classify(
            exit_code=result.exit_code, stdout=result.stdout,
            stderr=result.stderr, report=None,
        )


TESTS = SimpleNamespace(f2p=("tests/test_x.py::test_a",), p2p=())


def test_the_oracle_version_moved_with_what_derivation_means():
    """It is in the fingerprint for the reason `PREFLIGHT_VERSION` is in
    preflight's key: neither the manifest digest nor the image digest moves
    when this file's rules change, so without the bump a warm cache serves a
    quarantine derived by the old rules on exactly the tasks about to be
    graded.

    Pinned to a literal so a bump is a DELIBERATE edit rather than a side
    effect: 1 -> 2 is the derivation's two reference runs reading their
    `timeout` bound off `task.budget.suite_timeout_s` instead of a function
    default, where a manifest that already declares the key loads fine under
    the older loader (which ignores unknown `budget` sub-keys) and would
    otherwise go on being derived at 600 against a bound it does not ask
    for."""
    assert ORACLE_VERSION == "2"


def test_both_runs_green_yields_empty_quarantine():
    assert derive_quarantine(FakeRunner([(0, ""), (0, "")]), TESTS) == ()


def test_failed_in_exactly_one_run_is_quarantined():
    flaky = "FAILED tests/test_y.py::test_flaky - AssertionError"
    got = derive_quarantine(FakeRunner([(1, flaky), (0, "")]), TESTS)
    assert got == ("tests/test_y.py::test_flaky",)


def test_order_of_the_flake_does_not_matter():
    flaky = "FAILED tests/test_y.py::test_flaky - AssertionError"
    got = derive_quarantine(FakeRunner([(0, ""), (1, flaky)]), TESTS)
    assert got == ("tests/test_y.py::test_flaky",)


def test_the_quarantine_is_derived_under_the_grading_scope():
    runner = FakeRunner([(0, ""), (0, "")])
    derive_quarantine(runner, TESTS, scope=("tests/",))
    assert [c["scope"] for c in runner.calls] == [("tests/",), ("tests/",)]


def test_failed_in_both_runs_is_a_broken_oracle_not_a_flake():
    bad = "FAILED tests/test_y.py::test_broken - AssertionError"
    with pytest.raises(OracleError, match="test_broken"):
        derive_quarantine(FakeRunner([(1, bad), (1, bad)]), TESTS)


@pytest.mark.parametrize("code", [2, 3, 4, 5, 124, 127, 137])
def test_a_run_that_did_not_run_is_refused_rather_than_quarantining_nothing(code):
    # One queued result on purpose: classification must be eager, so the
    # broken first run raises before a second pass_to_pass is attempted.
    with pytest.raises(OracleError):
        derive_quarantine(FakeRunner([(code, "")]), TESTS)


def test_the_refusal_names_the_exit_code():
    # `\b`-anchored: a bare "2" also matches the word "p2p", which appears in
    # nearly every message this module raises.
    with pytest.raises(OracleError, match=r"exited 2\b"):
        derive_quarantine(FakeRunner([(2, "")]), TESTS)


def test_the_quarantine_is_sorted():
    """The verdict is stored, so its ORDER has to be a property of the input.

    `first ^ second` is a set, and a set of strings iterates in an order that
    depends on PYTHONHASHSEED -- so without `sorted()` two derivations of the
    identical quarantine write two different JSON files, and a reviewer
    diffing the oracle cache across a rebuild sees churn that means nothing.

    Six ids rather than two on purpose: this test's power against an unsorted
    implementation is the chance that hash order is not already sorted order,
    and with two ids that is a coin flip -- measured, a `tuple(first ^ second)`
    mutant survived 3 of 8 seeds. With six it does not survive any.
    """
    ids = [f"t{n}.py::test_{c}" for n, c in enumerate("fbeadc")]
    out = "\n".join(f"FAILED {node} - AssertionError" for node in ids)
    got = derive_quarantine(FakeRunner([(1, out), (0, "")]), TESTS)
    assert got == tuple(sorted(ids))


def test_a_quarantine_that_swallows_the_whole_p2p_list_is_refused():
    explicit = SimpleNamespace(f2p=(), p2p=("t.py::test_only",))
    flaky = "FAILED t.py::test_only - AssertionError"
    with pytest.raises(OracleError, match="p2p"):
        derive_quarantine(FakeRunner([(1, flaky), (0, "")]), explicit)


def test_a_partial_quarantine_of_the_p2p_list_is_allowed():
    explicit = SimpleNamespace(f2p=(), p2p=("t.py::test_a", "t.py::test_b"))
    flaky = "FAILED t.py::test_a - AssertionError"
    got = derive_quarantine(FakeRunner([(1, flaky), (0, "")]), explicit)
    assert got == ("t.py::test_a",)


def test_a_tag_is_refused_as_an_oracle_key():
    task = SimpleNamespace(manifest_digest="abc")
    with pytest.raises(OracleError, match="digest"):
        oracle_fingerprint(task, "bakeoff-task-click:v1")


def test_fingerprint_moves_with_oracle_version(monkeypatch):
    task = SimpleNamespace(manifest_digest="abc")
    before = oracle_fingerprint(task, "sha256:img")
    monkeypatch.setattr("bakeoff.oracle.ORACLE_VERSION", "999")
    assert oracle_fingerprint(task, "sha256:img") != before


def test_fingerprint_moves_with_the_image():
    task = SimpleNamespace(manifest_digest="abc")
    assert oracle_fingerprint(task, "sha256:a") != oracle_fingerprint(task, "sha256:b")


def test_fingerprint_moves_with_the_manifest_digest():
    a = SimpleNamespace(manifest_digest="abc")
    b = SimpleNamespace(manifest_digest="def")
    assert oracle_fingerprint(a, "sha256:img") != oracle_fingerprint(b, "sha256:img")


# --- the verdict-only cache --------------------------------------------------


class _Deriver:
    """Stands in for `_derive`, counting how often the container work runs."""

    def __init__(self, quarantined=("tests/test_y.py::test_flaky",)):
        self.quarantined = quarantined
        self.calls = 0

    def __call__(self, task, image, cache_root):
        self.calls += 1
        return self.quarantined


def _task(task_id="click-3360", digest="abc"):
    return SimpleNamespace(
        task_id=task_id,
        manifest_digest=digest,
        budget=SimpleNamespace(suite_timeout_s=600, wall_clock_timeout_s=900),
    )


def test_a_cold_cache_derives_once_and_stores_the_verdict(tmp_path, monkeypatch):
    deriver = _Deriver()
    monkeypatch.setattr("bakeoff.oracle._derive", deriver)

    got = ensure_oracle(_task(), "sha256:img", tmp_path)

    assert deriver.calls == 1
    assert got.quarantined == ("tests/test_y.py::test_flaky",)
    assert got.oracle_version == ORACLE_VERSION
    stored = json.loads((tmp_path / "oracle" / "click-3360.json").read_text())
    assert stored["fingerprint"] == oracle_fingerprint(_task(), "sha256:img")
    assert stored["quarantined"] == ["tests/test_y.py::test_flaky"]


def test_a_matching_fingerprint_does_not_re_derive(tmp_path, monkeypatch):
    deriver = _Deriver()
    monkeypatch.setattr("bakeoff.oracle._derive", deriver)

    first = ensure_oracle(_task(), "sha256:img", tmp_path)
    second = ensure_oracle(_task(), "sha256:img", tmp_path)

    assert deriver.calls == 1
    assert second == first


def test_a_stale_fingerprint_re_derives_and_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr("bakeoff.oracle._derive", _Deriver(("old::id",)))
    ensure_oracle(_task(), "sha256:old-image", tmp_path)

    fresh = _Deriver(("new::id",))
    monkeypatch.setattr("bakeoff.oracle._derive", fresh)
    got = ensure_oracle(_task(), "sha256:new-image", tmp_path)

    assert fresh.calls == 1
    assert got.quarantined == ("new::id",)
    stored = json.loads((tmp_path / "oracle" / "click-3360.json").read_text())
    assert stored["quarantined"] == ["new::id"]
    assert stored["fingerprint"] == got.fingerprint


def test_an_unreadable_cache_entry_re_derives_rather_than_raising(
    tmp_path, monkeypatch
):
    # The pruned-mirror lesson, one subsystem over: raising leaves the damaged
    # entry on disk and the task ungradable until an operator deletes it.
    entry = tmp_path / "oracle" / "click-3360.json"
    entry.parent.mkdir(parents=True)
    entry.write_text("{ not json")

    deriver = _Deriver()
    monkeypatch.setattr("bakeoff.oracle._derive", deriver)
    got = ensure_oracle(_task(), "sha256:img", tmp_path)

    assert deriver.calls == 1
    assert got.quarantined == ("tests/test_y.py::test_flaky",)


def test_a_cached_verdict_survives_the_round_trip_as_a_tuple(tmp_path, monkeypatch):
    monkeypatch.setattr("bakeoff.oracle._derive", _Deriver(("a::b", "c::d")))
    ensure_oracle(_task(), "sha256:img", tmp_path)

    def _refuse(*args, **kwargs):
        raise AssertionError("re-derived on a warm cache")

    monkeypatch.setattr("bakeoff.oracle._derive", _refuse)
    got = ensure_oracle(_task(), "sha256:img", tmp_path)
    assert got.quarantined == ("a::b", "c::d")


def test_the_cache_is_keyed_per_task(tmp_path, monkeypatch):
    deriver = _Deriver()
    monkeypatch.setattr("bakeoff.oracle._derive", deriver)
    ensure_oracle(_task("one"), "sha256:img", tmp_path)
    ensure_oracle(_task("two"), "sha256:img", tmp_path)
    assert deriver.calls == 2
    assert (tmp_path / "oracle" / "one.json").exists()
    assert (tmp_path / "oracle" / "two.json").exists()


def test_ensure_oracle_refuses_a_tag_before_deriving(tmp_path, monkeypatch):
    deriver = _Deriver()
    monkeypatch.setattr("bakeoff.oracle._derive", deriver)
    with pytest.raises(OracleError, match="digest"):
        ensure_oracle(_task(), "bakeoff-task-click:v1", tmp_path)
    assert deriver.calls == 0


def test_a_stored_quarantine_of_the_wrong_type_re_derives(tmp_path, monkeypatch):
    # A JSON string would become a per-character tuple, every character a
    # --deselect argument pytest ignores, and the Oracle would be well-formed.
    entry = tmp_path / "oracle" / "click-3360.json"
    entry.parent.mkdir(parents=True)
    entry.write_text(json.dumps({
        "fingerprint": oracle_fingerprint(_task(), "sha256:img"),
        "quarantined": "t.py::test_a",
        "oracle_version": ORACLE_VERSION,
    }))

    deriver = _Deriver()
    monkeypatch.setattr("bakeoff.oracle._derive", deriver)
    got = ensure_oracle(_task(), "sha256:img", tmp_path)

    assert deriver.calls == 1
    assert got.quarantined == ("tests/test_y.py::test_flaky",)


def test_an_empty_stored_quarantine_is_still_a_cache_hit(tmp_path, monkeypatch):
    # `[]` is a real verdict -- two green reference runs -- and the commonest
    # one. A truthiness check here would re-derive it on every grading pass.
    monkeypatch.setattr("bakeoff.oracle._derive", _Deriver(()))
    ensure_oracle(_task(), "sha256:img", tmp_path)

    def _refuse(*args, **kwargs):
        raise AssertionError("re-derived an empty verdict")

    monkeypatch.setattr("bakeoff.oracle._derive", _refuse)
    assert ensure_oracle(_task(), "sha256:img", tmp_path).quarantined == ()


def test_oracle_is_frozen():
    oracle = Oracle(fingerprint="f", quarantined=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        oracle.fingerprint = "g"


# --- the scope _derive actually hands the runner -----------------------------
#
# `_derive` is one non-mechanical line: it filters tests.paths through
# preflight's existence check instead of passing them raw, because that is the
# filter the grader's check 6 applies and because an unfiltered absent prefix
# makes pytest exit 4 on an input preflight deliberately tolerates. Nothing
# else pins it -- reverting to `scope=task.tests.paths` passes every other
# test here and would pass a click integration test too, since all of click's
# declared prefixes exist. This is the test that fails.
#
# `_existing_prefixes` is left UNPATCHED on purpose: it is the code under
# test. Only Docker and the tree are stubbed.


class _FakeContainer:
    """Answers `test -e` from a set of paths that exist; everything else 0."""

    def __init__(self, existing):
        self.existing = set(existing)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec(self, argv):
        if argv[:2] == ["test", "-e"]:
            code = 0 if argv[2] in self.existing else 1
            return SimpleNamespace(exit_code=code, stdout="", stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


def _stub_derive_environment(monkeypatch, tmp_path, existing, results):
    """Stub out Docker and the tree; return the runner `_derive` will use."""
    runner = FakeRunner(results)
    monkeypatch.setattr(
        "bakeoff.oracle.RunContainer",
        lambda image, repo_path, base_sha: _FakeContainer(existing),
    )

    def _make_runner(container, argv, timeout_s, adapter=None):
        runner.timeout_s = timeout_s
        # Recorded, not ignored: `_derive` must pass the task's adapter rather
        # than leave `_Runner` on its pytest default, and a stub that swallowed
        # the argument would hide the day it stopped.
        runner.adapter = adapter
        return runner

    monkeypatch.setattr("bakeoff.oracle._Runner", _make_runner)

    def _fake_materialize(task, dest, cache_root):
        Path(dest).mkdir(parents=True)
        return "start" * 8

    monkeypatch.setattr("bakeoff.oracle.materialize", _fake_materialize)
    return runner


def _derive_task(paths, p2p=()):
    return SimpleNamespace(
        task_id="click-3360",
        solution_diff="diff --git a/x b/x\n",
        tests=SimpleNamespace(
            f2p=("tests/test_x.py::test_a",),
            p2p=p2p,
            paths=paths,
            runner=("python", "-m", "pytest", "-q"),
        ),
        budget=SimpleNamespace(suite_timeout_s=600, wall_clock_timeout_s=900),
    )


def test_the_quarantine_is_derived_only_over_prefixes_that_exist(
    tmp_path, monkeypatch
):
    runner = _stub_derive_environment(
        monkeypatch, tmp_path, existing={"tests/"}, results=[(0, ""), (0, "")]
    )
    task = _derive_task(paths=("tests/", "docs/"))

    assert _derive(task, "sha256:img", tmp_path) == ()
    # "docs/" does not exist at the reference state. Passed raw it is a
    # positional argument pytest cannot collect -- exit 4, which `_classify`
    # refuses, so a task preflight passed on purpose becomes ungradable.
    assert [c["scope"] for c in runner.calls] == [("tests/",), ("tests/",)]


def test_an_explicit_p2p_list_derives_with_no_scope(tmp_path, monkeypatch):
    # `pass_to_pass` ignores `scope` on the explicit branch, so computing one
    # there pays a `test -e` per prefix to build a value nothing reads.
    runner = _stub_derive_environment(
        monkeypatch, tmp_path, existing={"tests/"}, results=[(0, ""), (0, "")]
    )
    task = _derive_task(paths=("tests/",), p2p=("tests/test_x.py::test_b",))

    assert _derive(task, "sha256:img", tmp_path) == ()
    assert [c["scope"] for c in runner.calls] == [(), ()]


def test_the_derivation_tree_does_not_outlive_the_derivation(
    tmp_path, monkeypatch
):
    # The tree holds the reference fix applied. preflight restores its own on
    # the same ground; here the whole tree is disposable, so it is removed.
    _stub_derive_environment(
        monkeypatch, tmp_path, existing={"tests/"}, results=[(0, ""), (0, "")]
    )
    _derive(_derive_task(paths=("tests/",)), "sha256:img", tmp_path)
    assert not (tmp_path / "oracle-tree" / "click-3360").exists()


def test_the_tree_is_removed_even_when_the_derivation_refuses(
    tmp_path, monkeypatch
):
    _stub_derive_environment(
        monkeypatch, tmp_path, existing={"tests/"}, results=[(2, "")]
    )
    with pytest.raises(OracleError):
        _derive(_derive_task(paths=("tests/",)), "sha256:img", tmp_path)
    assert not (tmp_path / "oracle-tree" / "click-3360").exists()


def test_the_derivation_bounds_its_two_reference_runs_by_the_manifest(
    tmp_path, monkeypatch
):
    """The quarantine is derived from two full suite runs at the reference
    state. Under a bound the manifest does not ask for, a slow-but-healthy
    suite raises OracleError ("the p2p run exited 124") and the task becomes
    ungradable -- for a number the task author already declared."""
    runner = _stub_derive_environment(
        monkeypatch, tmp_path, existing={"tests/"}, results=[(0, ""), (0, "")]
    )
    task = _derive_task(paths=("tests/",))
    task.budget.suite_timeout_s = 4321  # SimpleNamespace, assigns directly

    _derive(task, "sha256:" + "a" * 64, tmp_path)

    assert runner.timeout_s == 4321


def test_neither_oracle_entry_point_takes_a_timeout_parameter():
    """Same reasoning as `preflight`: the parameter is deleted rather than
    defaulted, so `grade.py`'s `ensure_oracle(task, image, Path(cache))` call
    cannot be left on a bound the manifest does not ask for."""
    import inspect

    assert "timeout_s" not in inspect.signature(ensure_oracle).parameters
    assert "timeout_s" not in inspect.signature(_derive).parameters


def test_the_quarantine_still_refuses_a_run_that_could_not_collect():
    """The oracle is deliberately UNCHANGED by broadening 2.

    Both of its runs are at the POST-FIX state, where preflight has already
    proved f2p exits 0 -- so no collection error can reach it. If one does, the
    polarity is the worst in the codebase: a broken run reports no failed node
    ids, so a classifier that softened here would read a suite that never
    collected as a clean run and derive an empty quarantine from two of them.
    """
    from types import SimpleNamespace

    from bakeoff.oracle import OracleError, _classify

    result = SimpleNamespace(
        exit_code=4, stdout="ERROR tests/a.py\n1 error in 0.01s\n", stderr="",
    )
    # The outcome comes from the real adapter, and the raw result is threaded
    # through beside it for the output tail. `_classify` stopped reading the
    # exit code directly when the judgement became per-framework (broadening
    # 7); the claim this test makes is unchanged.
    with pytest.raises(OracleError) as excinfo:
        _classify(ADAPTER.classify(exit_code=result.exit_code,
                                   stdout=result.stdout,
                                   stderr=result.stderr, report=None), result)

    assert "exited 4" in str(excinfo.value)


def test_the_oracle_refuses_every_kind_that_is_not_passed_or_failed():
    """A quarantine is derived from which tests failed, and a run that did not
    happen reports none -- indistinguishable from a clean run, and it would
    quarantine nothing. `load_error`, `nothing_ran` and `environment` all reach
    the raise, and `nothing_ran` is the one that matters most on node: a
    quarantine that swallows the whole p2p list exits 0 there (measured), so
    the exit code cannot carry this any more."""
    from bakeoff.oracle import OracleError, _classify
    from bakeoff.runners import (
        KIND_ENVIRONMENT, KIND_LOAD_ERROR, KIND_NOTHING_RAN, Outcome,
    )

    result = SimpleNamespace(stdout="the tail an operator needs", stderr="")
    for kind, code in (
        (KIND_LOAD_ERROR, 4), (KIND_NOTHING_RAN, 5), (KIND_ENVIRONMENT, 127),
    ):
        with pytest.raises(OracleError) as excinfo:
            _classify(Outcome(kind=kind, exit_code=code, explain="x"), result)
        # The tail is not decoration: every path through here is a broken
        # oracle, and a refusal with no output is one an operator cannot act on.
        assert "the tail an operator needs" in str(excinfo.value)


def test_the_derivation_hands_the_runner_the_tasks_adapter(
    tmp_path, monkeypatch
):
    """`_derive` builds its `_Runner` with the adapter for the task's declared
    framework, never leaving it on the pytest default. Under jest, exit 1 is
    what a config error, an import error and a failing assertion all return
    alike -- so a default here would read a broken reference run as "these
    tests failed" and quarantine them, shrinking the regression check on every
    submission of that task, forever."""
    from bakeoff.runners import for_framework

    runner = _stub_derive_environment(
        monkeypatch, tmp_path, existing={"tests/"}, results=[(0, ""), (0, "")]
    )

    _derive(_derive_task(paths=("tests/",)), "sha256:img", tmp_path)

    assert runner.adapter is for_framework("pytest")
