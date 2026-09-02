"""The collection driver's own checks, starting with its preflight cache.

`resolve_tasks` is the only thing standing between a warm cache and a matrix
run on tasks nobody re-validated, and until this file existed nothing tested
it. The cache key is the interesting part: it is what decides whether a
verdict written by an older gate is served or re-earned.

The key itself now lives in `bakeoff.preflight`, beside the constant it
depends on, because `scripts/grade.py` reads the same caches: a key defined in
the collection driver and imported by the offline grader would make one a
dependency of the other for one f-string, and two copies would be how a
verdict written under one gate gets served to another. These tests follow it
there -- `resolve_tasks` is still the caller they describe.
"""

from __future__ import annotations

import sys

import pytest

from bakeoff.preflight import PREFLIGHT_VERSION, preflight_cache_key


class _Task:
    manifest_digest = "digest"


def test_a_cached_verdict_from_an_older_preflight_is_not_served():
    """The key was `manifest_digest|image|start_sha`, and none of the three
    moves when `preflight.py` gains an assertion -- so every warm cache would
    serve a PASS written by the old gate and the new assertions would be inert
    on exactly the tasks about to be graded. This is the pruned-mirror
    closeout's "an older revision's output is served forever" defect one
    subsystem over, and it gets the same fix and the same test.

    Stated as a miss against the stored key rather than as a substring check
    on the new one: what `resolve_tasks` actually does is compare the key it
    builds now against the key stored beside the verdict, so that comparison
    is the behaviour worth pinning.
    """
    task, image, start_sha = _Task(), "sha256:image", "s" * 40

    key = preflight_cache_key(task, image, start_sha)

    # What every entry in ~/.cache/bakeoff/preflight.json carries today.
    stored_v1 = f"{task.manifest_digest}|{image}|{start_sha}"
    assert key != stored_v1

    # And a future bump must not be served by this one either.
    stored_older = f"{stored_v1}|{PREFLIGHT_VERSION}-older"
    assert key != stored_older


def test_an_unchanged_task_under_the_same_gate_still_hits():
    """The bump must cost one re-validation, not every resume. Re-validating
    80 tasks on each restart turns a 30-second resume into half an hour, and a
    gate that is expensive to run is a gate that gets skipped."""
    task, image, start_sha = _Task(), "sha256:image", "s" * 40

    assert preflight_cache_key(task, image, start_sha) == preflight_cache_key(
        task, image, start_sha
    )


def test_the_key_still_moves_with_the_manifest_the_image_and_the_start_state():
    """The version is an addition to the key, not a replacement for it."""
    image, start_sha = "sha256:image", "s" * 40
    base = preflight_cache_key(_Task(), image, start_sha)

    other = _Task()
    other.manifest_digest = "different"
    assert preflight_cache_key(other, image, start_sha) != base
    assert preflight_cache_key(_Task(), "sha256:other", start_sha) != base
    assert preflight_cache_key(_Task(), image, "t" * 40) != base


class _PyTask:
    """Only what the base-selection path reads.

    `task_set_commit` is here for one reason: `main` prints
    `tasks[0].task_set_commit` in its task-set banner, BEFORE the base block,
    so without it `test_main_stops_before_any_task_image_when_the_bases_disagree`
    dies of an AttributeError several lines short of the thing it pins.
    Nothing else `main` touches before `prepare_bases` is missing from this
    fake.
    """

    def __init__(self, task_id, python):
        self.task_id = task_id
        self.task_set_commit = ""
        self.image = type("I", (), {"python": python})()


def test_the_version_set_comes_from_the_task_set_not_from_the_allowlist(
    monkeypatch
):
    """The driver builds what the TASKS need. Deriving it from
    `tasks._PYTHON_VERSIONS` instead would build every allowed interpreter on
    every invocation -- three image builds for a task set that uses one, in
    the offline half that is supposed to be free."""
    import scripts.run_matrix as rm

    asked = {}

    # A `def`, not the one-line `setdefault(...) or {...}` it is tempting to
    # write: `setdefault` RETURNS the list it stored, a non-empty list is
    # truthy, and the `or` then short-circuits to it -- so the fake hands
    # `prepare_bases` a list instead of the mapping and the test fails on the
    # fake rather than on the code.
    def _fake_build(root, versions):
        asked["versions"] = list(versions)
        return {v: "sha256:" + v for v in versions}

    monkeypatch.setattr(rm, "build_base_images", _fake_build)
    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    bases, expected = rm.prepare_bases(
        [_PyTask("a", "3.12"), _PyTask("b", "3.11"), _PyTask("c", "3.12")]
    )

    assert sorted(asked["versions"]) == ["3.11", "3.12"]
    assert bases == {"3.11": "sha256:3.11", "3.12": "sha256:3.12"}
    assert expected == "2.1.220"


def test_two_bases_that_disagree_on_the_agent_stop_the_invocation(monkeypatch):
    """With one base, preflight's `expected_claude_version` refusal did this.
    Passing each task its OWN base's version keeps the half that catches a
    task image whose pip/build clobbered `claude`, and loses exactly one
    thing: cross-BASE agreement. Nothing downstream restores it --
    `Versions.claude_code` is read from the transcript, per run, and no reader
    compares it across tasks."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(
        rm, "base_claude_version",
        lambda image: "2.1.220" if image.endswith("3.12") else "2.1.999",
    )

    with pytest.raises(rm.ImageError) as excinfo:
        rm.assert_one_agent({"3.12": "sha256:3.12", "3.11": "sha256:3.11"})

    assert "2.1.220" in str(excinfo.value)
    assert "2.1.999" in str(excinfo.value)


def test_a_base_that_answers_nothing_is_a_refusal_not_an_agreement(monkeypatch):
    """`base_claude_version` returns "" when the `docker run` exits non-zero.
    Collapsing on the SET would make "every base failed to answer" a single
    value and therefore an agreement -- and the "" it returned is falsy, so
    preflight's `elif expected_claude_version and ...` guard would never fire
    and the agent-version check would be silently off for the whole matrix.
    Emptiness is checked BEFORE agreement."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "")

    with pytest.raises(rm.ImageError) as excinfo:
        rm.assert_one_agent({"3.12": "sha256:a", "3.11": "sha256:b"})

    assert "3.11" in str(excinfo.value) and "3.12" in str(excinfo.value)


def test_one_base_that_answers_nothing_is_also_a_refusal(monkeypatch):
    """The mixed case, which a set-collapse check would report as a
    disagreement with a misleading message and an all-empty check would
    miss."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(
        rm, "base_claude_version",
        lambda image: "2.1.220" if image.endswith("3.12") else "",
    )

    with pytest.raises(rm.ImageError):
        rm.assert_one_agent({"3.12": "sha256:a", "3.11": "sha256:b"})


def test_bases_that_agree_yield_the_single_version(monkeypatch):
    import scripts.run_matrix as rm

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    assert rm.assert_one_agent({"3.12": "sha256:a", "3.11": "sha256:b"}) == \
        "2.1.220"


def test_main_stops_before_any_task_image_when_the_bases_disagree(
    monkeypatch, tmp_path
):
    """The ordering claim, driven through `main` rather than asserted about
    it. A refusal that fires AFTER `resolve_tasks` has already built task
    images -- or after the proxy is up -- is a refusal that costs the thing it
    exists to protect. `resolve_tasks` is replaced by a sentinel that fails
    the test if it is reached at all."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(
        rm, "load_task_set",
        lambda path, only=None: [_PyTask("a", "3.11"), _PyTask("b", "3.12")],
    )
    monkeypatch.setattr(
        rm, "prepare_bases",
        lambda tasks: (_ for _ in ()).throw(rm.ImageError("bases disagree: boom")),
    )

    def _must_not_run(*args, **kwargs):
        raise AssertionError(
            "resolve_tasks ran after the base check refused: a task image was "
            "built for an invocation that cannot produce a comparison"
        )

    monkeypatch.setattr(rm, "resolve_tasks", _must_not_run)
    monkeypatch.setattr(
        sys, "argv",
        ["run_matrix.py", "--preflight-only", "--event-log", str(tmp_path / "log")],
    )

    assert rm.main() == 1


def test_each_task_is_built_against_the_base_its_manifest_names(
    monkeypatch, tmp_path
):
    """The mapping, not the first entry of it. A task handed the wrong base
    still builds and still runs -- under an interpreter it was not cut for,
    and only preflight's read-back says so.

    The fake `image_entrypoint` returns a non-empty entrypoint so every task
    is rejected on the line right after its image is built. That is the
    earliest point at which the base has already been chosen, and it keeps
    this test off `materialize` and `preflight`, neither of which it is about.
    If `resolve_tasks` is ever refactored so the entrypoint check no longer
    follows the build, this test needs a new stopping point.
    """
    import scripts.run_matrix as rm

    seen = {}

    def _fake_build(task, base, build_root, cache):
        seen[task.task_id] = base
        return "sha256:img-" + task.task_id

    monkeypatch.setattr(rm, "build_task_image", _fake_build)
    monkeypatch.setattr(rm, "image_entrypoint", lambda image: ["/inherited"])

    rm.resolve_tasks(
        [_PyTask("a", "3.11"), _PyTask("b", "3.12")],
        {"3.11": "sha256:B11", "3.12": "sha256:B12"},
        "2.1.220", tmp_path, force=True,
    )

    assert seen == {"a": "sha256:B11", "b": "sha256:B12"}
