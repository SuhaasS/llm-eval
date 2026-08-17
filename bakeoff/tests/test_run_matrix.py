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
