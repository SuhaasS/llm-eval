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

import json
import sys
from pathlib import Path

import pytest

from bakeoff.preflight import PREFLIGHT_VERSION, preflight_cache_key
from scripts.run_matrix import arms_missing_from_config, config_name_for


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


def test_the_config_file_follows_the_provider_and_offline_ignores_it():
    """Spec §1. The offline stub config is provider-neutral: nothing is
    spent and no credential is read, so both providers share it."""
    assert config_name_for("live", "openrouter") == "litellm_config_openrouter.yaml"
    assert config_name_for("live", "bedrock") == "litellm_config.yaml"
    assert config_name_for("offline", "openrouter") == "litellm_smoke_offline.yaml"
    assert config_name_for("offline", "bedrock") == "litellm_smoke_offline.yaml"


def _write_config(path, *model_names):
    lines = ["model_list:"]
    for name in model_names:
        lines.append(f"  - model_name: {name}")
        lines.append("    litellm_params:")
        lines.append(f"      model: openai/{name}-stub")
    path.write_text("\n".join(lines) + "\n")


def test_an_arm_present_in_the_config_is_not_reported_missing(tmp_path):
    config = tmp_path / "config.yaml"
    _write_config(config, "kimi-k2-6", "kimi-k3")
    assert arms_missing_from_config(["kimi-k2-6"], config) == []


def test_an_arm_absent_from_the_config_is_named(tmp_path):
    """This is the whole defect: a typo'd --models value or a provider/config
    mismatch (openrouter arms requested against a bedrock-only config, say)
    must be refused here, before an image is built or a token spent -- not
    discovered as a permanent `gave_up` record after LiteLLM 4xxs on an
    unknown model group."""
    config = tmp_path / "config.yaml"
    _write_config(config, "claude-sonnet-5-runtime", "gemma-4-31b")
    assert arms_missing_from_config(["kimi-k2-6", "kimi-k3"], config) == [
        "kimi-k2-6",
        "kimi-k3",
    ]


def test_only_the_missing_arms_are_named_not_the_ones_present(tmp_path):
    config = tmp_path / "config.yaml"
    _write_config(config, "kimi-k2-6")
    assert arms_missing_from_config(["kimi-k2-6", "kimi-k3"], config) == ["kimi-k3"]


def test_no_requested_arms_means_nothing_missing(tmp_path):
    config = tmp_path / "config.yaml"
    _write_config(config, "kimi-k2-6")
    assert arms_missing_from_config([], config) == []


class _PyTask:
    """Only what the base-selection path reads.

    `task_set_commit` is here for one reason: `main` prints
    `tasks[0].task_set_commit` in its task-set banner, BEFORE the base block,
    so without it `test_main_stops_before_any_task_image_when_the_bases_disagree`
    dies of an AttributeError several lines short of the thing it pins.
    Nothing else `main` touches before `prepare_bases` is missing from this
    fake.
    """

    def __init__(self, task_id, python, framework="pytest", node="22"):
        self.task_id = task_id
        self.task_set_commit = ""
        self.image = type("I", (), {"python": python, "node": node})()
        # `task_runtime` reads `tests.framework` and NEVER `image.node`
        # directly: a pytest task carries an unread node default, and a fake
        # that omits the key would make the base-selection path pass on a
        # shape the loader cannot produce.
        self.tests = type("T", (), {"framework": framework})()


def _fake_task(framework="pytest", task_id="t", python="3.12", node="22"):
    return _PyTask(task_id, python, framework=framework, node=node)


def test_the_version_set_comes_from_the_task_set_not_from_the_allowlist(
    monkeypatch
):
    """The driver builds what the TASKS need. Deriving it from
    `tasks._PYTHON_VERSIONS` instead would build every allowed interpreter on
    every invocation -- three image builds for a task set that uses one, in
    the offline half that is supposed to be free.

    This is the only test in the tree that fakes `build_base_images` and
    calls `prepare_bases` -- `_fake_build` now returns `BaseImage`s, since
    `prepare_bases` unwraps `.image_id` before handing `bases` to
    `assert_one_agent` and to its caller. The `bases ==` assertion below stays
    against PLAIN STRINGS, unchanged: that is itself the pin that
    `prepare_bases` still hands id strings downstream."""
    import scripts.run_matrix as rm
    from bakeoff.images import BaseImage

    asked = {}

    # A `def`, not the one-line `setdefault(...) or {...}` it is tempting to
    # write: `setdefault` RETURNS the list it stored, a non-empty list is
    # truthy, and the `or` then short-circuits to it -- so the fake hands
    # `prepare_bases` a list instead of the mapping and the test fails on the
    # fake rather than on the code.
    def _fake_build(root, versions):
        asked["versions"] = sorted(versions)
        return {pair: BaseImage("sha256:" + pair[1], reused=False,
                                reason="the tag names no image")
                for pair in versions}

    monkeypatch.setattr(rm, "build_base_images", _fake_build)
    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    bases, expected = rm.prepare_bases(
        [_PyTask("a", "3.12"), _PyTask("b", "3.11"), _PyTask("c", "3.12")]
    )

    assert sorted(asked["versions"]) == [("python", "3.11"), ("python", "3.12")]
    assert bases == {("python", "3.11"): "sha256:3.11",
                     ("python", "3.12"): "sha256:3.12"}
    assert expected == "2.1.220"


def test_the_driver_says_which_bases_it_built_and_which_it_reused(
    monkeypatch, capsys
):
    import scripts.run_matrix as rm
    from bakeoff.images import BaseImage

    def _fake_build(root, versions):
        return {
            ("python", "3.11"): BaseImage("sha256:reused11", reused=True),
            ("python", "3.13"): BaseImage(
                "sha256:built13", reused=False,
                reason="the tag said bakeoff.base.version='3.11', "
                       "this base is '3.13'",
            ),
        }

    monkeypatch.setattr(rm, "build_base_images", _fake_build)
    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    rm.prepare_bases(
        [_PyTask("a", "3.11"), _PyTask("b", "3.13")]
    )

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("base py3.11")]
    assert lines and lines[0].startswith("base py3.11  sha256:")
    assert lines[0].endswith("(reused)")
    built_lines = [line for line in out.splitlines() if line.startswith("base py3.13")]
    assert built_lines and built_lines[0].startswith("base py3.13  sha256:")
    assert built_lines[0].endswith(
        "(built: the tag said bakeoff.base.version='3.11', this base is '3.13')"
    )


def test_a_cold_machine_and_a_mutated_tag_do_not_print_the_same_line(
    monkeypatch, capsys
):
    """The 'two absences that render identically' pin, and the reason
    finding 1 exists."""
    import scripts.run_matrix as rm
    from bakeoff.images import BaseImage

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    monkeypatch.setattr(
        rm, "build_base_images",
        lambda root, versions: {
            ("python", "3.11"): BaseImage(
                "sha256:x", reused=False, reason="the tag names no image"
            )
        },
    )
    rm.prepare_bases([_PyTask("a", "3.11")])
    cold_line = next(
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("base py3.11")
    )

    monkeypatch.setattr(
        rm, "build_base_images",
        lambda root, versions: {
            ("python", "3.11"): BaseImage(
                "sha256:x", reused=False,
                reason="the tag said bakeoff.base.version='3.9', "
                       "this base is '3.11'",
            )
        },
    )
    rm.prepare_bases([_PyTask("a", "3.11")])
    mutated_line = next(
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("base py3.11")
    )

    assert cold_line != mutated_line


def test_prepare_bases_hands_assert_one_agent_ids_not_wrappers(monkeypatch):
    """Pins that the unwrap happens before `assert_one_agent`, so
    `resolve_tasks`, `assert_one_agent` and `resolve_task` stay untouched."""
    import scripts.run_matrix as rm
    from bakeoff.images import BaseImage

    monkeypatch.setattr(
        rm, "build_base_images",
        lambda root, versions: {
            ("python", "3.11"): BaseImage("sha256:x", reused=True)
        },
    )
    seen = {}

    def _fake_assert(bases):
        seen["bases"] = bases
        return "2.1.220"

    monkeypatch.setattr(rm, "assert_one_agent", _fake_assert)

    rm.prepare_bases([_PyTask("a", "3.11")])

    assert all(isinstance(v, str) for v in seen["bases"].values())


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
        rm.assert_one_agent({("python", "3.12"): "sha256:3.12",
                             ("python", "3.11"): "sha256:3.11"})

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
        rm.assert_one_agent({("python", "3.12"): "sha256:a",
                             ("python", "3.11"): "sha256:b"})

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
        rm.assert_one_agent({("python", "3.12"): "sha256:a",
                             ("python", "3.11"): "sha256:b"})


def test_bases_that_agree_yield_the_single_version(monkeypatch):
    import scripts.run_matrix as rm

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    assert rm.assert_one_agent({("python", "3.12"): "sha256:a",
                                ("python", "3.11"): "sha256:b"}) == "2.1.220"


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
        rm, "load_task_set_with_refusals",
        lambda path, only=None: ([_PyTask("a", "3.11"), _PyTask("b", "3.12")], []),
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
        {("python", "3.11"): "sha256:B11", ("python", "3.12"): "sha256:B12"},
        "2.1.220", tmp_path, force=True,
    )

    assert seen == {"a": "sha256:B11", "b": "sha256:B12"}


def test_the_base_set_is_the_runtimes_the_task_set_actually_needs(monkeypatch):
    from bakeoff.tasks import task_runtime

    tasks = [_fake_task(framework="pytest"), _fake_task(framework="vitest")]

    assert {task_runtime(t) for t in tasks} == {("python", "3.12"), ("node", "22")}


def test_disagreeing_claude_versions_across_bases_refuse_the_whole_invocation(
    monkeypatch
):
    """Broadening 5's cross-base check, unchanged in CONTRACT and widened only
    in key type -- it takes `{key: image_id}`, probes each with
    `base_claude_version`, and raises `ImageError`. Two Dockerfiles carrying two
    `ARG CLAUDE_CODE_VERSION` lines is where it earns the most: nothing in a
    record compares `Versions.claude_code` across tasks.

    An EMPTY probe is a refusal, not agreement: preflight's guard is `elif
    expected_claude_version and ...`, falsy on "", so "every base failed to
    answer" would silently disable the agent-version check for every task."""
    from bakeoff.images import ImageError
    import scripts.run_matrix as rm

    monkeypatch.setattr(
        rm, "base_claude_version",
        lambda image: "2.1.219" if "node" in image else "2.1.220")
    with pytest.raises(ImageError):
        rm.assert_one_agent({("python", "3.12"): "sha256:py",
                             ("node", "22"): "sha256:node"})

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "")
    with pytest.raises(ImageError):
        rm.assert_one_agent({("python", "3.12"): "sha256:py",
                             ("node", "22"): "sha256:node"})


def test_a_node_task_is_built_against_the_node_base(monkeypatch, tmp_path):
    """The mapping is indexed by `task_runtime(task)`, not by
    `task.image.python` -- which every task carries, node ones included, so
    indexing by it would hand a vitest task the python base. That image builds
    and that container starts; the runner is simply not there, and it reaches
    the model as exit 127 on every arm."""
    import scripts.run_matrix as rm

    seen = {}

    def _fake_build(task, base, build_root, cache):
        seen[task.task_id] = base
        return "sha256:img-" + task.task_id

    monkeypatch.setattr(rm, "build_task_image", _fake_build)
    monkeypatch.setattr(rm, "image_entrypoint", lambda image: ["/inherited"])

    rm.resolve_tasks(
        [_fake_task(framework="pytest", task_id="py"),
         _fake_task(framework="vitest", task_id="js")],
        {("python", "3.12"): "sha256:BPY", ("node", "22"): "sha256:BNODE"},
        "2.1.220", tmp_path, force=True,
    )

    assert seen == {"py": "sha256:BPY", "js": "sha256:BNODE"}


# --- round 2 item 3: a unique host path per bind-mounted tree ---------------


class _ResolvableTask(_PyTask):
    """`_PyTask` plus the fields `resolve_tasks` reads once the entrypoint
    check lets it through -- the existing base-selection tests all stop at
    that `continue`, so none of them reach the tree at all."""

    def __init__(self, task_id="t", python="3.12"):
        super().__init__(task_id, python)
        self.base_sha = "b" * 40
        self.declared_start_sha = "s" * 40
        self.manifest_digest = "digest"
        self.task_version = 1


def _stub_resolve_tasks(monkeypatch, rm, on_preflight):
    monkeypatch.setattr(rm, "build_task_image", lambda *a, **k: "sha256:img")
    monkeypatch.setattr(rm, "image_entrypoint", lambda image: [])
    monkeypatch.setattr(rm, "materialize", lambda *a, **k: "s" * 40)
    monkeypatch.setattr(rm, "write_json", lambda *a, **k: None)
    monkeypatch.setattr(rm, "preflight", on_preflight)


def _preflight_result(task, kw, problems=(), evidence=None):
    # `evidence` defaults through to `_evidence_seed()` -- never a bare `{}`,
    # which `PreflightResult.__post_init__` (item 5) refuses as a schema that
    # is not the schema -- so the two existing callers that never pass
    # `evidence` keep getting a valid, fully-seeded dict exactly as they did
    # before this parameter existed. round 2 item 14's three tests pass a
    # seed overridden with `build_generated_*` instead of transcribing a
    # fourth copy of this constructor's field list.
    from bakeoff.preflight import PreflightResult, _evidence_seed

    return PreflightResult(
        task_id=task.task_id, task_version=task.task_version,
        start_sha=kw["start_sha"], image=kw["image"],
        manifest_digest=task.manifest_digest,
        problems=problems,
        preflight_version=PREFLIGHT_VERSION,
        evidence=evidence or _evidence_seed(),
    )


def test_two_preflights_of_one_task_mount_different_host_paths(
    tmp_path, monkeypatch
):
    """The site the probe measured. `preflight-tree/<task_id>` was one stable
    path per task, `rmtree`'d at the TOP of the next invocation -- and the
    Docker VM serves a replaced host inode from its cache, so the second
    container saw an EMPTY `/repo` (measured 2026-09-02, `[files], [],
    [files], []` over four cycles). On a vitest task that is `No test files
    found` at exit 1: the gate PASSES while measuring nothing."""
    import scripts.run_matrix as rm

    mounted: list[str] = []

    def _recording(task, **kw):
        mounted.append(str(kw["repo_path"]))
        return _preflight_result(task, kw)

    _stub_resolve_tasks(monkeypatch, rm, _recording)
    task = _ResolvableTask()
    for _ in range(2):
        rm.resolve_tasks([task], {("python", "3.12"): "sha256:B"},
                         "2.1.220", tmp_path, force=True)

    assert mounted[0] != mounted[1]
    assert all(task.task_id in p for p in mounted)


@pytest.mark.parametrize("problems", [(), ("f2p green at start",)])
def test_the_preflight_tree_does_not_outlive_the_resolution(
    tmp_path, monkeypatch, problems
):
    """The tree was left behind on purpose and removed by the NEXT
    invocation, which is the stale mount written out. It now dies with the
    resolution -- on the NO-GO path too, which is the one that `continue`s
    past every line a cleanup could have sat on."""
    import scripts.run_matrix as rm

    _stub_resolve_tasks(
        monkeypatch, rm,
        lambda task, **kw: _preflight_result(task, kw, problems=problems),
    )
    task = _ResolvableTask()
    rm.resolve_tasks([task], {("python", "3.12"): "sha256:B"},
                     "2.1.220", tmp_path, force=True)

    assert list((tmp_path / "preflight-tree" / task.task_id).iterdir()) == []


def test_two_runs_of_one_cell_mount_different_host_paths(tmp_path, monkeypatch):
    """A run-level retry, or two invocations sharing a one-second stamp, put
    two containers on one cell's run tree. `materialize` refuses an existing
    destination, so today that is loud -- but the leaf is what keeps it from
    becoming the stale mount the moment the retry lands, and it is also what
    makes `--keep` a path a reader can trust to be this run's."""
    from types import SimpleNamespace

    import bakeoff.proxy_callback
    import bakeoff.runner
    import scripts.run_matrix as rm

    dests: list[str] = []

    def _fake_materialize(task, dest, cache):
        dests.append(str(dest))
        return "s" * 40

    def _stub_record():
        return SimpleNamespace(artifacts=SimpleNamespace(container_stdout=""))

    monkeypatch.setattr(rm, "materialize", _fake_materialize)
    monkeypatch.setattr(rm, "to_task_spec", lambda *a, **k: object())
    monkeypatch.setattr(rm, "write_json", lambda *a, **k: None)
    monkeypatch.setattr(bakeoff.runner, "execute_run",
                        lambda **kw: _stub_record())
    monkeypatch.setattr(bakeoff.proxy_callback, "unattributed_count",
                        lambda d: 0)

    cell = SimpleNamespace(task_id="t", model="sonnet", sample_index=0,
                           label="t/sonnet/0")
    task = SimpleNamespace(
        budget=SimpleNamespace(max_turns=5, wall_clock_timeout_s=60))
    resolved = {"start_sha": "s" * 40, "image": "sha256:img"}
    artifacts = tmp_path / "artifacts"

    def _run(keep):
        return rm.run_cell(
            cell, task, resolved, SimpleNamespace(keep=keep), None,
            tmp_path / "wire", None, artifacts, "collection", "stamp",
        )

    _, _, _, first_kept = _run(False)
    _, _, _, second_kept = _run(False)

    assert dests[0] != dests[1]
    assert first_kept is None and second_kept is None
    # Neither leaf outlives its run: the whole allocation goes, not just
    # `repo`, and that is safe because the name is never reissued.
    run_root = artifacts / "t-sonnet-0"
    assert list((run_root / "tree").iterdir()) == []

    _, _, _, kept = _run(True)
    assert kept == dests[2]
    # The leaf survives (the stubbed `materialize` never creates `repo`
    # itself), and the returned path is what `main` writes into the row.
    assert Path(kept).parent.is_dir()
    assert list((run_root / "tree").iterdir()) == [Path(kept).parent]


def test_run_matrix_prints_the_unselected_refusals_as_warnings(
    monkeypatch, tmp_path, capsys
):
    """A `--tasks` selection that loads past an uncommitted broken sibling
    must say so on stdout -- `load_task_set_with_refusals`'s whole point is
    that the skip is otherwise recorded nowhere. Modelled on
    `test_main_stops_before_any_task_image_when_the_bases_disagree`:
    `prepare_bases` is stubbed to raise immediately after the banner, so the
    warning is observable without building anything."""
    from bakeoff.tasks import RefusedManifest
    import scripts.run_matrix as rm

    refused = RefusedManifest(
        directory=tmp_path / "set" / "b",
        error=f"{tmp_path / 'set' / 'b' / 'task.yaml'}: is not an allowed "
              "image.env key",
        committed=False,
    )
    monkeypatch.setattr(
        rm, "load_task_set_with_refusals",
        lambda path, only=None: ([_PyTask("a", "3.11")], [refused]),
    )
    monkeypatch.setattr(
        rm, "prepare_bases",
        lambda tasks: (_ for _ in ()).throw(rm.ImageError("stop")),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["run_matrix.py", "--preflight-only", "--tasks", "a",
         "--event-log", str(tmp_path / "log")],
    )

    rm.main()

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert str(tmp_path / "set" / "b") in out
    assert "is not an allowed image.env key" in out


def test_run_matrix_stops_before_any_image_when_a_selected_manifest_is_broken(
    monkeypatch, tmp_path, capsys
):
    """The refusal path -- a manifest the selection itself names -- must stop
    before any image is built, exactly like the base-disagreement refusal
    already tested above. `prepare_bases` and `resolve_tasks` are sentinels
    that fail the test if reached at all."""
    from bakeoff.tasks import TaskError
    import scripts.run_matrix as rm

    def _load(path, only=None):
        raise TaskError(
            f"{tmp_path / 'set'}: 1 manifest(s) in this task set did not "
            "load, and every one of them is required here:\n"
            "  - [SELECTED by --tasks as 'a']\n"
            "    is not an allowed image.env key"
        )

    def _must_not_run(*args, **kwargs):
        raise AssertionError(
            "an image-building call ran after a selected manifest refused"
        )

    monkeypatch.setattr(rm, "load_task_set_with_refusals", _load)
    monkeypatch.setattr(rm, "prepare_bases", _must_not_run)
    monkeypatch.setattr(rm, "resolve_tasks", _must_not_run)
    monkeypatch.setattr(
        sys, "argv",
        ["run_matrix.py", "--preflight-only", "--tasks", "a",
         "--event-log", str(tmp_path / "log")],
    )

    assert rm.main() == 1
    out = capsys.readouterr().out
    assert out.startswith("task set: ")


# --- round 2 item 7: the gate records how long its own bounded runs took ---


def test_the_gate_prints_the_slowest_run_beside_the_bound():
    """The format is asserted whole because it is the artifact -- an author
    reads this line and edits a manifest from it. The denominator is the
    schema's nine and not this task's ceiling, which is eight on a node task
    and lower again with an explicit `tests.p2p`; the phrase "the schema's"
    is what keeps that from being read as a per-task worst case, which is
    the exact 9-vs-8 conflation this commit corrects in three documents."""
    from bakeoff.preflight import BOUNDED_RUN_KEYS
    import scripts.run_matrix as rm

    durations = dict.fromkeys(BOUNDED_RUN_KEYS) | {
        "f2p_before": 106.3, "p2p_before": 98.0,
    }
    verdict = {
        "evidence": {
            "suite_timeout_s": 240,
            "bounded_run_durations_s": durations,
            "bounded_run_duration_max_s": 106.3,
        },
    }

    assert rm.suite_time_line(verdict) == (
        "suite time  slowest 106.3s of the 240s bound; 204.3s over 2 of "
        "the schema's 9 bounded runs"
    )


def test_the_line_says_which_kind_of_absence_it_is():
    """Three absences that render identically are the defect this whole
    round is about, and a print is where they are easiest to collapse."""
    from bakeoff.preflight import BOUNDED_RUN_KEYS
    import scripts.run_matrix as rm

    all_null = {
        "evidence": {
            "suite_timeout_s": 240,
            "bounded_run_durations_s": dict.fromkeys(BOUNDED_RUN_KEYS),
            "bounded_run_duration_max_s": None,
        },
    }
    assert rm.suite_time_line(all_null) == "suite time  no bounded command ran"

    no_key = {"evidence": {}, "preflight_version": "13"}
    assert rm.suite_time_line(no_key) == (
        "suite time  not recorded: written by preflight_version 13"
    )

    unrecorded_bound = {
        "evidence": {
            "suite_timeout_s": None,
            "bounded_run_durations_s": dict.fromkeys(BOUNDED_RUN_KEYS) | {
                "f2p_before": 1.0},
            "bounded_run_duration_max_s": 1.0,
        },
    }
    assert "unrecorded bound" in rm.suite_time_line(unrecorded_bound)


def test_a_cached_verdict_from_another_gate_is_not_printed(tmp_path):
    """Name the concrete disagreement -- the blob is written before the `ok`
    test and `preflight.json` only on PASS, so the two files can disagree
    and a filename is not evidence about which gate wrote it."""
    import scripts.run_matrix as rm

    blob = {
        "manifest_digest": "d", "image": "sha256:img", "start_sha": "s" * 40,
        "preflight_version": PREFLIGHT_VERSION, "ok": True,
    }
    path = tmp_path / "preflight" / "t.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(blob))

    key = preflight_cache_key(
        type("T", (), {"manifest_digest": "d"})(), "sha256:img", "s" * 40)

    assert rm.cached_verdict(tmp_path, "t", key) == blob
    assert rm.cached_verdict(
        tmp_path, "t",
        preflight_cache_key(type("T", (), {"manifest_digest": "d"})(),
                            "sha256:other", "s" * 40),
    ) is None
    assert rm.cached_verdict(
        tmp_path, "t",
        preflight_cache_key(type("T", (), {"manifest_digest": "d"})(),
                            "sha256:img", "t" * 40),
    ) is None
    assert rm.cached_verdict(
        tmp_path, "t",
        preflight_cache_key(type("T", (), {"manifest_digest": "d"})(),
                            "sha256:img", "s" * 40) + "x",
    ) is None
    not_pass_path = tmp_path / "preflight" / "u.json"
    not_pass_path.write_text(json.dumps(blob | {"ok": False}))
    assert rm.cached_verdict(tmp_path, "u", key) is None
    assert rm.cached_verdict(tmp_path, "nonexistent", key) is None


def test_the_gate_totals_the_bounded_time_it_spent(monkeypatch, tmp_path, capsys):
    """The per-task max answers "is this task's bound sized right"; only the
    total answers the question the item asks first, which is whether the
    gate fits inside the credential window -- and the NO-GO row is in the
    total because a task killed by `timeout` is the largest contributor to
    it, not an excluded one."""
    from bakeoff.preflight import BOUNDED_RUN_KEYS, PreflightResult, _evidence_seed
    import scripts.run_matrix as rm

    monkeypatch.setattr(rm, "build_task_image",
                        lambda task, base, build_root, cache: "sha256:img-" + task.task_id)
    monkeypatch.setattr(rm, "image_entrypoint", lambda image: [])   # FALSY -- see below
    monkeypatch.setattr(rm, "materialize", lambda task, repo, cache: "s" * 40)

    def _canned(task_id, ok, suite_timeout_s, durations, problems=()):
        assert ok == (not problems), "ok and problems disagree on the same fixture"
        evidence = _evidence_seed() | {
            "suite_timeout_s": suite_timeout_s,
            "bounded_run_durations_s": dict.fromkeys(BOUNDED_RUN_KEYS) | durations,
            "bounded_run_duration_max_s": max(durations.values()),
        }
        return PreflightResult(
            task_id=task_id, task_version=1, start_sha="s" * 40,
            image="sha256:img-" + task_id, manifest_digest="d",
            problems=problems, evidence=evidence, preflight_version="18",
        )

    _CANNED = {
        "a": _canned("a", True, 600, {"f2p_before": 5.0, "p2p_before": 7.0}),
        "b": _canned("b", True, 600, {"f2p_before": 1.0, "p2p_before": 2.0}),
        "c": _canned("c", False, 240, {"f2p_before": 240.0},
                     problems=("the f2p run timed out",)),
    }
    monkeypatch.setattr(rm, "preflight", lambda task, **kw: _CANNED[task.task_id])

    tasks = [_ResolvableTask(task_id=t) for t in ("a", "b", "c")]
    rm.resolve_tasks(tasks, {("python", "3.12"): "sha256:B"}, "2.1.220",
                     tmp_path, force=True)

    out = capsys.readouterr().out
    assert (
        "gate suite time  255.0s across 3 task(s)\n"
        "                 bounded runs only -- image build, materialization "
        "and container start are NOT in this number"
    ) in out
    assert (
        "suite time  slowest 240.0s of the 240s bound; 240.0s over 1 of the "
        "schema's 9 bounded runs"
    ) in out


# --- round 2 item 14: what an image.build step wrote into the scaffold ------


def test_the_driver_prints_what_the_build_wrote_into_the_scaffold(
    tmp_path, monkeypatch, capsys
):
    """The note is printed on the fresh-gate path, PASS or NO-GO -- a task
    whose gate fails BECAUSE of a vanished file is exactly this line's
    reader. `problems` is non-empty here (the real pytest-10210 shape: the
    bare collection exits 1 on a `ModuleNotFoundError` for a build-generated
    module), so the NO-GO ordering the code implies -- the note BEFORE the
    `preflight NO-GO` block -- is actually exercised rather than left to a
    default `problems=()` that never takes that branch."""
    from bakeoff.preflight import _evidence_seed
    import scripts.run_matrix as rm

    evidence = _evidence_seed() | {
        "build_generated_paths": ["sqlglot/_version.py", "sqlglot.egg-info/PKG-INFO"],
        "build_generated_count": 2,
        "build_generated_state": "scanned",
    }
    _stub_resolve_tasks(
        monkeypatch, rm,
        lambda task, **kw: _preflight_result(
            task, kw, evidence=evidence,
            problems=("the bare pytest collection (`timeout 240 python -m "
                      "pytest --co -q`) exited 1",)),
    )
    task = _ResolvableTask()

    rm.resolve_tasks([task], {("python", "3.12"): "sha256:B"}, "2.1.220",
                     tmp_path, force=True)

    out = capsys.readouterr().out
    assert "the image build wrote 2 file(s) into /repo" in out
    assert "sqlglot/_version.py" in out
    assert "sqlglot.egg-info/PKG-INFO" in out
    assert out.index("the image build wrote") < out.index("preflight NO-GO")


def test_the_driver_prints_nothing_when_the_build_wrote_nothing(
    tmp_path, monkeypatch, capsys
):
    """Seven of the nine tasks measured in the plan's §1.2 hit this branch --
    the ordinary case must stay silent."""
    from bakeoff.preflight import _evidence_seed
    import scripts.run_matrix as rm

    evidence = _evidence_seed() | {
        "build_generated_paths": [], "build_generated_count": 0,
        "build_generated_state": "scanned",
    }
    _stub_resolve_tasks(
        monkeypatch, rm,
        lambda task, **kw: _preflight_result(task, kw, evidence=evidence),
    )
    task = _ResolvableTask()

    rm.resolve_tasks([task], {("python", "3.12"): "sha256:B"}, "2.1.220",
                     tmp_path, force=True)

    out = capsys.readouterr().out
    assert "the image build wrote" not in out


def test_a_scan_that_failed_prints_a_note_instead_of_nothing(
    tmp_path, monkeypatch, capsys
):
    """`build_generated_paths` is falsy on BOTH "nothing found" (the ordinary
    case, above) and "the scan could not run" (`build_generated_state` is
    `"failed: ..."`, the key left `None`) -- the two absences must not render
    identically on the console, the one place an author actually looks, or a
    failed scan reads as a clean one."""
    from bakeoff.preflight import _evidence_seed
    import scripts.run_matrix as rm

    evidence = _evidence_seed() | {
        "build_generated_state": "failed: docker cp exit 1",
    }
    _stub_resolve_tasks(
        monkeypatch, rm,
        lambda task, **kw: _preflight_result(task, kw, evidence=evidence),
    )
    task = _ResolvableTask()

    rm.resolve_tasks([task], {("python", "3.12"): "sha256:B"}, "2.1.220",
                     tmp_path, force=True)

    out = capsys.readouterr().out
    assert "the build-output scan did not complete: docker cp exit 1" in out
    assert "the image build wrote" not in out


def test_a_cached_pass_still_prints_what_the_build_wrote(
    tmp_path, monkeypatch, capsys
):
    """The note is off DISK on this path, not out of a `PreflightResult` --
    `resolve_tasks` returns from the cached branch before `preflight()` is
    ever called, so the courtesy has to come from the stored verdict."""
    from bakeoff.preflight import _evidence_seed
    import scripts.run_matrix as rm

    _stub_resolve_tasks(monkeypatch, rm, lambda task, **kw: (_ for _ in ()).throw(
        AssertionError("preflight() must not run on a cache hit")))
    task = _ResolvableTask()
    image = "sha256:img"
    start_sha = "s" * 40
    key = preflight_cache_key(task, image, start_sha)

    (tmp_path / "preflight").mkdir(parents=True)
    (tmp_path / "preflight" / f"{task.task_id}.json").write_text(json.dumps({
        "ok": True,
        "manifest_digest": task.manifest_digest, "image": image,
        "start_sha": start_sha, "preflight_version": PREFLIGHT_VERSION,
        "evidence": _evidence_seed() | {
            "build_generated_paths": ["sqlglot/_version.py"],
            "build_generated_count": 1,
            "build_generated_state": "scanned",
        },
    }))
    (tmp_path / "preflight.json").write_text(
        json.dumps({task.task_id: {"key": key}}))

    rm.resolve_tasks([task], {("python", "3.12"): "sha256:B"}, "2.1.220",
                     tmp_path, force=False)

    out = capsys.readouterr().out
    assert "preflight cached PASS" in out
    assert "the image build wrote 1 file(s) into /repo" in out
    assert "sqlglot/_version.py" in out
