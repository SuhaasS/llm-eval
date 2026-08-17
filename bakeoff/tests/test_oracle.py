"""The flake quarantine and its verdict-only cache (spec section 4.2.1).

Nothing here touches Docker: `derive_quarantine` takes a runner, and the
`ensure_oracle` tests monkeypatch `_derive`, which is the only seam that
materializes a tree and starts a container.
"""

import json
from types import SimpleNamespace

import pytest

from bakeoff.oracle import (
    ORACLE_VERSION,
    Oracle,
    OracleError,
    derive_quarantine,
    ensure_oracle,
    oracle_fingerprint,
)


class FakeRunner:
    """pass_to_pass returns queued (exit_code, stdout) pairs in order."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def pass_to_pass(self, tests, extra_deselect=(), scope=()):
        self.calls.append({"scope": scope})
        code, out = self._results.pop(0)
        return SimpleNamespace(exit_code=code, stdout=out, stderr="")


TESTS = SimpleNamespace(f2p=("tests/test_x.py::test_a",), p2p=())


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
    with pytest.raises(OracleError, match="2"):
        derive_quarantine(FakeRunner([(2, "")]), TESTS)


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

    def __call__(self, task, image, cache_root, timeout_s):
        self.calls += 1
        return self.quarantined


def _task(task_id="click-3360", digest="abc"):
    return SimpleNamespace(task_id=task_id, manifest_digest=digest)


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


def test_oracle_is_frozen():
    oracle = Oracle(fingerprint="f", quarantined=())
    with pytest.raises(Exception):
        oracle.fingerprint = "g"
