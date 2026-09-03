"""The generated task Dockerfile.

Tested as a string, without a daemon, on purpose. The two guarantees that
matter are structural properties of this text, and a test that has to build
an image to check them is a test nobody runs before committing.

Both guarantees exist because the failures are total and invisible in a
record: a root task image makes Claude Code refuse `bypassPermissions` and
exit before emitting one event, and an inherited ENTRYPOINT turns
RunContainer's `sleep infinity` into an argument to it so the container
exits immediately. Either way every arm records zero turns, and the eval
reads four capability failures.
"""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

import bakeoff.images as images
import bakeoff.tasks as tasks
from bakeoff.images import (
    ImageError,
    _strip_build_context,
    build_task_image,
    render_dockerfile,
)


def _lines(**kwargs) -> list[str]:
    kwargs.setdefault("base_image", "sha256:base")
    kwargs.setdefault("apt", [])
    kwargs.setdefault("pip", [])
    kwargs.setdefault("build", [])
    kwargs.setdefault("env", {})
    return [line.strip() for line in render_dockerfile(**kwargs).splitlines()]


def test_the_image_never_ends_as_root():
    lines = [line for line in _lines(apt=["less"]) if line.startswith("USER")]

    assert lines[-1] == "USER eval"


def test_the_entrypoint_is_always_cleared():
    assert "ENTRYPOINT []" in _lines(build=["pip install -e ."])


def test_a_task_with_no_dependencies_is_still_a_valid_image():
    """The degenerate manifest -- no apt, no pip, no build -- must still
    produce something buildable, or the simplest possible task is the one
    that cannot run."""
    lines = _lines()

    assert lines[3] == "FROM sha256:base"
    assert "COPY repo /repo" in lines
    assert "ENTRYPOINT []" in lines


def test_task_pins_are_installed_after_the_base_image_s():
    """The base pins pytest so the reference fixture has a runner; a real
    repository pins its own, and they will not always agree. Measured on
    pallets/click, whose suite does not COLLECT under the base image's
    pytest 9.1.1. Section 5.1's "dependencies from lockfile" is per task, so
    the task's pin has to win."""
    lines = _lines(pip=["pytest==8.3.5"])
    pip_index = next(i for i, line in enumerate(lines) if line.startswith("RUN pip"))
    from_index = next(i for i, line in enumerate(lines) if line.startswith("FROM"))

    assert from_index < pip_index
    assert 'pytest==8.3.5' in lines[pip_index]


def test_build_commands_run_against_the_scaffold_repo():
    """`pip install -e .` has to resolve against /repo specifically: the bind
    mount replaces /repo at run time, so an editable install points at the
    tree the agent is editing. Installing from anywhere else -- or
    non-editably -- resolves imports to site-packages and the agent's edits
    do nothing."""
    lines = _lines(build=["pip install -e ."])
    copy_index = lines.index("COPY repo /repo")
    build_index = lines.index("RUN cd /repo && pip install -e .")

    assert copy_index < build_index


def test_the_file_says_it_is_generated():
    """A hand-edited copy is how the two structural guarantees come back."""
    assert _lines()[0].startswith("# GENERATED")


def test_the_build_context_loses_the_stripped_paths(tmp_path):
    """The context is `git archive base_sha`, so it carries paths the START
    state does not -- and the scaffold is what `image.build` resolves against.
    A committed venv or vendored tree left here puts `pip install -e .` on an
    import path the run tree does not have, so the environment the image pins
    is not the environment the agent works in and nothing downstream compares
    the two."""
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text("{}\n")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "dep.py").write_text("VERSION = '1.0'\n")
    (repo / "CLAUDE.md").write_text("# notes\n")
    (repo / "calc.py").write_text("x = 1\n")

    _strip_build_context(repo, ["CLAUDE.md", ".claude", "vendor"])

    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".claude").exists()
    assert not (repo / "vendor").exists()
    assert (repo / "calc.py").exists(), "only what was named"


def test_a_stripped_symlink_is_removed_rather_than_followed(tmp_path):
    """`is_dir()` follows the link and `shutil.rmtree` then raises
    "Cannot call rmtree on a symbolic link". Measured upstream: sqlglot's
    CLAUDE.md is a symlink to AGENTS.md."""
    repo = tmp_path / "repo"
    (repo / "real").mkdir(parents=True)
    (repo / "real" / "a.md").write_text("x\n")
    (repo / "CLAUDE.md").symlink_to(repo / "real")

    _strip_build_context(repo, ["CLAUDE.md"])

    assert not (repo / "CLAUDE.md").is_symlink()
    assert (repo / "real" / "a.md").exists(), "the link's target is not the target"


def test_a_strip_through_an_intermediate_symlink_does_not_escape_repo_dir(tmp_path):
    """`git archive` preserves symlinks, and only the FINAL path component is
    ever checked with `is_symlink()` -- an upstream `docs -> /elsewhere` plus
    a strip path of `docs/secret` would resolve outside `repo_dir` and delete
    there if nothing else guarded it. This pins the guard, not just the
    docstring that explains it."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret").write_text("keep me\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").symlink_to(outside)

    with pytest.raises(ImageError):
        _strip_build_context(repo, ["docs/secret"])

    assert (outside / "secret").exists(), "escaping the guard must not delete outside repo_dir"
    assert (repo / "docs").is_symlink(), "context is untouched by the refusal"


def test_a_strip_through_a_dangling_intermediate_symlink_is_not_an_error(tmp_path):
    """`docs` points nowhere, so `docs/x` matches nothing -- the same claim
    `test_a_path_missing_from_the_build_context_is_not_an_error` makes for a
    plain missing path. This must stay a no-op rather than reaching the
    escape guard: `target.parent.resolve()` follows the stored symlink text
    literally even though nothing is there, which would otherwise land
    outside `repo_dir` and raise on a path that removes nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").symlink_to(tmp_path / "nowhere-at-all")

    _strip_build_context(repo, ["docs/x"])

    assert (repo / "docs").is_symlink(), "the dangling link itself was not named and stays"


def test_a_path_missing_from_the_build_context_is_not_an_error(tmp_path):
    """`materialize` raises on exactly this, and `run_matrix` builds the image
    BEFORE materializing -- raising in both places means one typo is reported
    twice and fixed twice."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _strip_build_context(repo, ["nope", "also/nope"])

    assert list(repo.iterdir()) == [], "a missing path must not create or touch anything"


def _archive_bytes(source) -> bytes:
    """A tar of `source`, in the shape `git archive --format=tar` emits."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(source.rglob("*")):
            # `recursive=False`: `rglob` already yields every descendant, and
            # tarfile.add recurses by default -- so the default adds each
            # directory's contents a second time.
            tar.add(path, arcname=str(path.relative_to(source)),
                    recursive=False)
    return buffer.getvalue()


def _fake_run_with_archive(archive: bytes, real_run):
    """`git archive` returns `archive`; `git ls-tree`/`git cat-file` report no
    gitlinks and no `.gitmodules`, so `_extract_submodules`'s derivation sees
    zero submodules against the fake `tmp_path / "mirror"` these call sites
    point `ensure_mirror` at, rather than needing that path to be a real repo.
    Everything else falls through to `real_run`, which is what keeps the
    docker calls (faked separately via `images._run`) and any other caller
    honest -- same shared-module patch this file already uses.
    """
    def fake_run(args, **kwargs):
        argv = list(args)
        if argv[:2] == ["git", "archive"]:
            return subprocess.CompletedProcess(args, 0, stdout=archive, stderr=b"")
        if argv[:2] == ["git", "ls-tree"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if argv[:2] == ["git", "cat-file"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return real_run(args, **kwargs)

    return fake_run


def test_build_task_image_strips_the_context_it_unpacks(tmp_path, monkeypatch):
    """The CALL SITE, not the helper.

    Deleting the one `_strip_build_context(...)` line in `build_task_image`
    leaves all three helper tests above green, and an un-stripped scaffold is
    invisible in every record -- the exact shape a mutation anchor exists for.
    Pinned here instead, and in the DEFAULT suite: `addopts = "-m 'not
    integration'"` means an integration-only pin proves nothing on the run an
    implementer or CI actually makes.

    Only `git archive` and the docker calls are faked; the real `tar -x` runs,
    so the unpack this asserts against is the one production performs. The
    `subprocess.run` patch lands on the shared `subprocess` MODULE rather than
    on a name `images.py` owns, so for the duration it is process-wide --
    `real_run`, captured before the patch, is what keeps every other caller
    honest, and monkeypatch restores the attribute on the way out."""
    source = tmp_path / "source"
    (source / ".claude").mkdir(parents=True)
    (source / ".claude" / "settings.json").write_text("{}\n")
    (source / "CLAUDE.md").write_text("# notes\n")
    (source / "calc.py").write_text("x = 1\n")
    archive = _archive_bytes(source)
    real_run = subprocess.run
    fake_run = _fake_run_with_archive(archive, real_run)

    # `build_task_image` imports ensure_mirror from bakeoff.tasks INSIDE the
    # function, so the patch has to land on the source module.
    monkeypatch.setattr("bakeoff.tasks.ensure_mirror",
                        lambda url, sha, cache: tmp_path / "mirror")
    monkeypatch.setattr("bakeoff.images.subprocess.run", fake_run)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")

    class _Image:
        apt = ()
        pip = ()
        build = ()
        python = "3.12"
        node = "22"
        env = {}

    class _Task:
        task_id = "t"
        task_version = 1
        repo_url = "file:///nowhere"
        base_sha = "0" * 40
        image = _Image()
        # `task_runtime` reads this; build_task_image asks it which base
        # it is rendering for (D15).
        tests = type("T", (), {"framework": "pytest"})()
        strip_paths = ("CLAUDE.md", ".claude")
        submodules_unneeded = ()
        test_files = ()
        solution_files = ()
        extra_files = ()

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    unpacked = tmp_path / "build" / "image-t" / "repo"
    assert (unpacked / "calc.py").exists(), "the archive was unpacked at all"
    assert not (unpacked / "CLAUDE.md").exists()
    assert not (unpacked / ".claude").exists()


def _submodule_fixture(tmp_path):
    """A superproject mirror plus a submodule mirror, both real bare repos.

    Real rather than faked because the thing under test is that TWO archives
    are taken and the second lands inside the first's empty directory --
    measured 2026-09-01 (git 2.50.1): `git archive` emits `.gitmodules` and a
    zero-entry directory for the gitlink, so a stubbed single archive could
    not express the bug.
    """
    def sh(*args, cwd):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)

    lib = tmp_path / "lib"
    (lib / "libdep").mkdir(parents=True)
    (lib / "libdep" / "__init__.py").write_text("VALUE = 1\n")
    sh("git", "init", "-q", cwd=lib)
    sh("git", "config", "user.email", "t@t.test", cwd=lib)
    sh("git", "config", "user.name", "t", cwd=lib)
    sh("git", "add", "-A", cwd=lib)
    sh("git", "commit", "-q", "-m", "lib", cwd=lib)
    pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=lib,
                            check=True, capture_output=True,
                            text=True).stdout.strip()

    sup = tmp_path / "sup"
    sup.mkdir()
    (sup / "calc.py").write_text("x = 1\n")
    # A plain directory that is a SIBLING of the submodule's parent, for the
    # ancestor-strip test below. It cannot be `vendor/...`: a strip covering
    # the submodule from above is refused by the loader now
    # (`_refuse_submodule_conflicts`), so the only ancestor entry the image
    # path can still be handed is one that does not overlap a gitlink.
    (sup / "docs" / "api").mkdir(parents=True)
    (sup / "docs" / "api" / "page.md").write_text("# api\n")
    sh("git", "init", "-q", cwd=sup)
    sh("git", "config", "user.email", "t@t.test", cwd=sup)
    sh("git", "config", "user.name", "t", cwd=sup)
    sh("git", "add", "-A", cwd=sup)
    sh("git", "commit", "-q", "-m", "base", cwd=sup)
    sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
       str(lib), "vendor/libdep", cwd=sup)
    sh("git", "add", "-A", cwd=sup)
    sh("git", "commit", "-q", "-m", "sub", cwd=sup)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=sup, check=True,
                          capture_output=True, text=True).stdout.strip()
    return {"sup": sup, "lib": lib, "base": base, "pinned": pinned}


def _sub_task_stub(fixture, strip_paths=(), submodules_unneeded=()):
    class _Image:
        apt = ()
        pip = ()
        build = ()
        python = "3.12"
        node = "22"
        env = {}

    class _Task:
        task_id = "t"
        task_version = 1
        repo_url = str(fixture["sup"])
        base_sha = fixture["base"]
        image = _Image()
        # `task_runtime` reads this; build_task_image asks it which base
        # it is rendering for (D15).
        tests = type("T", (), {"framework": "pytest"})()
        strip_paths = ()
        submodules_unneeded = ()
        test_files = ()
        solution_files = ()
        extra_files = ()

    _Task.strip_paths = tuple(strip_paths)
    _Task.submodules_unneeded = tuple(submodules_unneeded)
    return _Task()


def test_the_build_context_carries_submodule_content(tmp_path, monkeypatch):
    """The context is two archives, not one. Measured 2026-09-01: `git archive`
    emits a submodule as an EMPTY DIRECTORY entry (`.gitmodules` is in the
    archive, the content is not), so a one-archive context ships an image whose
    `pip install -e .` resolves against a directory the run tree will have
    content in -- the same silent image/run disagreement a non-editable
    install produces, and nothing downstream compares the two.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")

    build_task_image(_sub_task_stub(fixture), "sha256:base",
                     tmp_path / "build", tmp_path / "cache")

    repo = tmp_path / "build" / "image-t" / "repo"
    assert (repo / "vendor" / "libdep" / "libdep" / "__init__.py").read_text() \
        == "VALUE = 1\n"
    assert (repo / ".gitmodules").exists()


def test_the_build_context_still_carries_no_git_directory(tmp_path, monkeypatch):
    """`git archive` twice, never a copied run tree: a run tree is base_sha
    PLUS the committed test half, so copying one bakes the oracle into a layer
    no later step can tell from a dependency.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")

    build_task_image(_sub_task_stub(fixture), "sha256:base",
                     tmp_path / "build", tmp_path / "cache")

    repo = tmp_path / "build" / "image-t" / "repo"
    assert [p for p in repo.rglob(".git")] == []


def test_an_ancestor_strip_path_coexists_with_the_submodule_extract(
        tmp_path, monkeypatch):
    """An ancestor entry over a PLAIN directory, and the two do not interfere.

    This test used to strip `vendor` with the submodule at `vendor/libdep`,
    which is the one shape that separates extract-then-strip from
    strip-then-extract by outcome. That manifest is now refused at load
    (`tasks._refuse_submodule_conflicts`, both directions) because the same
    shape does far worse at materialization time than it does here -- the
    strip removes the gitlink and `_init_submodules` then chdirs into a
    directory that does not exist. So the ordering claim is pinned by that
    refusal, not by this file, and what is left to pin here is the ordinary
    case: an ancestor strip of a sibling directory removes exactly that
    directory, and the second archive still lands its content inside the
    empty gitlink directory the first one left.

    Kept rather than deleted: `build_task_image` still applies the strip and
    the extract in one order, and a future edit that dropped the second
    archive, or ran the strip over the whole context after it, would make
    the submodule assertion below fail.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")

    build_task_image(_sub_task_stub(fixture, strip_paths=("docs",)),
                     "sha256:base", tmp_path / "build", tmp_path / "cache")

    repo = tmp_path / "build" / "image-t" / "repo"
    assert not (repo / "docs").exists()
    assert (repo / "vendor" / "libdep" / "libdep" / "__init__.py").read_text() \
        == "VALUE = 1\n"
    assert (repo / ".gitmodules").exists()   # the strip touches nothing else


def test_no_second_archive_is_taken_for_a_declared_unneeded_submodule(
        tmp_path, monkeypatch):
    """`test_the_build_context_carries_submodule_content`, inverted.

    The directory the image needs is already in the context -- measured
    2026-09-02, `git archive <base_sha> | tar -x` creates the gitlink's path as
    an EMPTY directory -- so the image is correct with no second archive, and
    no mirror is built, which is what makes an ssh url cost nothing.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")
    pruned = []
    real_pruned = tasks.ensure_pruned_mirror
    monkeypatch.setattr(
        tasks, "ensure_pruned_mirror",
        lambda url, sha, cache: (pruned.append((url, sha)),
                                 real_pruned(url, sha, cache))[1],
    )

    build_task_image(
        _sub_task_stub(fixture, submodules_unneeded=("vendor/libdep",)),
        "sha256:base", tmp_path / "build", tmp_path / "cache")

    repo = tmp_path / "build" / "image-t" / "repo"
    sub = repo / "vendor" / "libdep"
    assert sub.is_dir()
    assert list(sub.iterdir()) == []
    assert str(fixture["lib"]) not in [url for url, _sha in pruned]


def test_a_declared_unneeded_submodule_leaves_no_dotgit_in_the_context(
        tmp_path, monkeypatch):
    """The house form of `test_the_build_context_still_carries_no_git_directory`,
    over the skipped path: a skip that reached for a `git clone` instead of a
    `git archive` would leave one.

    `rglob(".git")`, never `rglob(".git*")` against a one-element list: that
    also matches `.gitignore` and `.gitattributes`, and `rglob`'s order is
    unspecified.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")

    build_task_image(
        _sub_task_stub(fixture, submodules_unneeded=("vendor/libdep",)),
        "sha256:base", tmp_path / "build", tmp_path / "cache")

    repo = tmp_path / "build" / "image-t" / "repo"
    assert [p for p in repo.rglob(".git")] == []
    assert (repo / ".gitmodules").exists()


def test_a_task_with_no_submodules_takes_no_extra_archive(tmp_path, monkeypatch):
    """Backwards compatibility. `pallets/click` at its base_sha has no gitlink,
    and a zero-submodule task must not gain a git call against the mirror.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    real_run = subprocess.run
    archives = []

    def counting_run(args, **kwargs):
        if list(args)[:2] == ["git", "archive"]:
            archives.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr("bakeoff.images.subprocess.run", counting_run)

    class _Image:
        apt = ()
        pip = ()
        build = ()
        python = "3.12"
        node = "22"
        env = {}

    class _Task:
        task_id = "t"
        task_version = 1
        repo_url = str(fixture["sup"])
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=fixture["sup"], check=True,
            capture_output=True, text=True).stdout.strip()
        image = _Image()
        # `task_runtime` reads this; build_task_image asks it which base
        # it is rendering for (D15).
        tests = type("T", (), {"framework": "pytest"})()
        strip_paths = ()
        submodules_unneeded = ()
        test_files = ()
        solution_files = ()
        extra_files = ()

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    assert len(archives) == 1


def test_env_lines_come_after_every_build_step():
    """Placement does not change the final image config -- an ENV anywhere in
    the file lands in it -- but it decides whether the BUILD sees the value,
    and it must not.

    `image.build` is arbitrary shell (`pip install -e .` on the click task).
    CI=1 changes pip's own behaviour and is read by a great many build
    scripts, so a build that succeeded when the author measured it could start
    failing, or succeeding differently, for a variable declared to make a test
    runner deterministic. Nothing in image.build is measured by preflight's
    ladder, so a value that leaked into it would be an unmeasured difference
    in an artifact every arm shares.
    """
    lines = _lines(build=["pip install -e ."],
                   env={"CI": "1", "TZ": "America/New_York"})
    last_run = max(i for i, line in enumerate(lines) if line.startswith("RUN "))
    ci_index = next(i for i, line in enumerate(lines)
                    if line.startswith("ENV CI="))
    tz_index = next(i for i, line in enumerate(lines)
                    if line.startswith("ENV TZ="))

    assert ci_index > last_run
    assert tz_index > last_run
    # ...and still before the USER switch, so the file reads top-to-bottom as
    # root-setup then eval-runtime.
    assert ci_index < lines.index("USER eval")
    assert tz_index < lines.index("USER eval")


def test_env_lines_are_sorted_so_the_image_id_is_a_function_of_the_manifest():
    """One ENV line per key, in sorted order. Dict insertion order would make
    the Dockerfile text -- and therefore the built image id, which is what
    Versions.container_image_digest records -- depend on YAML key order rather
    than on the manifest's content."""
    forward = _lines(env={"CI": "1", "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/h",
                          "TZ": "UTC"})
    reverse = _lines(env={"TZ": "UTC", "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/h",
                          "CI": "1"})

    assert forward == reverse
    assert [line for line in forward if line.startswith("ENV ")] == [
        'ENV CI="1"',
        'ENV HYPOTHESIS_STORAGE_DIRECTORY="/tmp/h"',
        'ENV TZ="UTC"',
    ]


def test_a_task_with_no_env_emits_no_env_line():
    """The degenerate manifest is the common one -- click declares no env --
    and an empty `ENV` line is a build error, not a no-op."""
    assert not [line for line in _lines() if line.startswith("ENV ")]


def test_build_task_image_passes_the_manifests_env_through(tmp_path,
                                                           monkeypatch):
    """The wiring, not the rendering. Dropping `env=dict(task.image.env)` from
    the call in `build_task_image` leaves every render_dockerfile test above
    green while no task image carries any environment at all -- and the
    failure is silent, because a hypothesis suite whose determinism lever
    never applied is simply a suite that sometimes passes.

    Faked exactly the way `test_build_task_image_strips_the_context_it_unpacks`
    fakes it, and for its stated reasons: `git archive` runs through
    `subprocess.run` DIRECTLY (images.py:252), not through `_run`, so patching
    `_run` alone leaves it shelling out to a mirror that does not exist and
    raising `ImageError: git archive ... failed`. `real_run`, captured before
    the patch, keeps every other caller honest -- the patch lands on the
    shared `subprocess` module and is process-wide for its duration.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_text("x = 1\n")
    archive = _archive_bytes(source)
    real_run = subprocess.run
    fake_run = _fake_run_with_archive(archive, real_run)

    rendered = {}
    real_render = images.render_dockerfile

    def capture(*args, **kwargs):
        rendered.update(kwargs)
        return real_render(*args, **kwargs)

    monkeypatch.setattr("bakeoff.tasks.ensure_mirror",
                        lambda url, sha, cache: tmp_path / "mirror")
    monkeypatch.setattr("bakeoff.images.subprocess.run", fake_run)
    # `_run` covers `docker build` AND `image_id`, which calls it -- there is
    # no separate `image_id` to patch.
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr("bakeoff.images.render_dockerfile", capture)

    class _Image:
        apt = ()
        pip = ()
        build = ()
        python = "3.12"
        node = "22"
        env = {"CI": "1"}

    class _Task:
        task_id = "envwiring"
        task_version = 1
        repo_url = "file:///nowhere"
        base_sha = "0" * 40
        image = _Image()
        # `task_runtime` reads this; build_task_image asks it which base
        # it is rendering for (D15).
        tests = type("T", (), {"framework": "pytest"})()
        strip_paths = ()
        submodules_unneeded = ()
        test_files = ()
        solution_files = ()
        extra_files = ()

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    assert rendered["env"] == {"CI": "1"}


# --- the parameterised base image ---------------------------------------------


def test_each_version_gets_its_own_tag():
    """One tag for two interpreters means the second build silently replaces
    the first, and every task afterwards resolves the same tag to the wrong
    base -- which builds, runs, and is green."""
    assert images.base_tag("python", "3.11") == "bakeoff-eval-agent:base-python-3.11"
    assert images.base_tag("python", "3.12") != images.base_tag("python", "3.11")


def test_build_base_image_passes_the_version_as_a_build_arg(monkeypatch):
    calls = []
    monkeypatch.setattr(images, "_run", lambda args, cwd=None: calls.append(args) or "")
    monkeypatch.setattr(images, "image_id", lambda tag: "sha256:" + tag)

    images.build_base_image(Path("/repo_root"), "python", "3.11")

    argv = calls[0]
    assert "--build-arg" in argv
    assert argv[argv.index("--build-arg") + 1] == "BASE_PYTHON_VERSION=3.11"
    assert "-t" in argv and argv[argv.index("-t") + 1] == \
        "bakeoff-eval-agent:base-python-3.11"


def test_the_arg_is_not_named_PYTHON_VERSION(monkeypatch):
    """Measured 2026-09-01: the official python: images set their own
    ENV PYTHON_VERSION (3.11.16, 3.12.13, 3.13.15) and ENV beats ARG, so after
    FROM the expansion reads the base image's patch level -- even after the ARG
    is redeclared. The current file never expands it after FROM, so today the
    collision is latent; a later edit that did would read a plausible wrong
    value. The name is the whole defence."""
    calls = []
    monkeypatch.setattr(images, "_run", lambda args, cwd=None: calls.append(args) or "")
    monkeypatch.setattr(images, "image_id", lambda tag: "sha256:" + tag)

    images.build_base_image(Path("/repo_root"), "python", "3.13")

    assert "PYTHON_VERSION=3.13" not in calls[0]


def test_one_build_per_distinct_version_not_per_request(monkeypatch):
    """The drivers call this with one entry per TASK. Building per task pays
    an image build for every duplicate, and on a 60-task set that is the
    difference between a preflight pass and one nobody waits for."""
    built = []
    monkeypatch.setattr(
        images, "build_base_image",
        lambda root, runtime, version: built.append((runtime, version))
        or ("sha256:" + version),
    )

    bases = images.build_base_images(
        Path("/r"),
        [("python", "3.12"), ("python", "3.11"), ("python", "3.12"),
         ("python", "3.12")],
    )

    assert sorted(built) == [("python", "3.11"), ("python", "3.12")]
    assert bases == {("python", "3.11"): "sha256:3.11",
                     ("python", "3.12"): "sha256:3.12"}


def test_every_copy_of_the_default_version_says_the_same_thing():
    """THREE copies of "3.12" exist by the end of this broadening -- the
    Dockerfile's ARG default, `tasks._DEFAULT_PYTHON` (which is
    `TaskImage.python`'s default), and `images._DEFAULT_PYTHON` (restated
    because this module cannot import `tasks` at module scope). Two that can
    drift means a manifest declaring no `python:` loads as a task whose base
    nobody built -- the build succeeds, the suite runs, and the interpreter is
    not the one the default named.

    `preflight` is deliberately NOT a fourth copy: it imports
    `_DEFAULT_PYTHON` from `tasks` (Task 4)."""
    from bakeoff.tasks import _DEFAULT_PYTHON as manifest_default

    dockerfile = (
        Path(__file__).resolve().parent.parent / "docker" / "eval-agent.Dockerfile"
    ).read_text()

    assert images._DEFAULT_PYTHON == manifest_default
    assert f"ARG BASE_PYTHON_VERSION={manifest_default}\n" in dockerfile
    assert "FROM python:${BASE_PYTHON_VERSION}-slim-bookworm\n" in dockerfile


def test_every_allowlisted_version_is_a_tag_this_module_can_name():
    from bakeoff.tasks import _PYTHON_VERSIONS

    tags = {images.base_tag("python", v) for v in _PYTHON_VERSIONS}

    assert len(tags) == len(_PYTHON_VERSIONS)


@pytest.mark.integration
# `task_image` as well, even though no TASK image is built here: the section
# 6.6 gate selects `integration and not task_image` and is documented as
# needing no network, and a real base build pulls from Docker Hub and runs
# apt. A gate that quietly grew a network dependency would change what it
# needs without changing what it is called.
@pytest.mark.task_image
def test_a_non_default_base_really_builds_and_carries_the_pins():
    """The claim this broadening rests on, checked against a daemon rather
    than against a rendered string. Measured 2026-09-01: 3.11 gives Python
    3.11.16, pytest 9.1.1, claude 2.1.220, uid 1000."""
    repo_root = Path(__file__).resolve().parent.parent
    image = images.build_base_image(repo_root, "python", "3.11")

    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
         "python --version && pytest --version && claude --version && id -u"],
        capture_output=True, text=True, check=True,
    )

    assert probe.stdout.startswith("Python 3.11.")
    assert "pytest 9.1.1" in probe.stdout
    assert "2.1.220" in probe.stdout
    assert probe.stdout.rstrip().endswith("1000")


# --- per-runtime base images --------------------------------------------------


def test_base_tag_names_the_runtime_and_the_version():
    """The tag string moves (`base-3.12` -> `base-python-3.12`) and nothing
    else does: the python Dockerfile is unchanged, so the image ID is
    unchanged, and every cache in this repo keys on the ID -- preflight's, the
    oracle's fingerprint, Versions.container_image_digest. No warm verdict is
    invalidated by the rename."""
    from bakeoff.images import base_tag

    assert base_tag("python", "3.12") == "bakeoff-eval-agent:base-python-3.12"
    assert base_tag("node", "22") == "bakeoff-eval-agent:base-node-22"


def test_build_base_images_builds_each_pair_exactly_once(monkeypatch):
    calls = []
    from bakeoff import images

    monkeypatch.setattr(images, "_run", lambda args, cwd=None: calls.append(args))
    monkeypatch.setattr(images, "image_id", lambda tag: f"sha256:{tag}")

    built = images.build_base_images(
        Path("/repo"),
        {("python", "3.12"), ("node", "22"), ("python", "3.12")},
    )

    assert set(built) == {("python", "3.12"), ("node", "22")}
    assert len(calls) == 2


def test_each_runtime_gets_its_own_dockerfile_and_build_arg(monkeypatch):
    """Two files, not one with a switched FROM. node:22-bookworm-slim already
    occupies uid 1000 with a `node` user, so `useradd --uid 1000 eval` exits 4
    there and succeeds on python:3.12-slim-bookworm (measured 2026-09-01) --
    and a shell conditional around a useradd is the shape that half-succeeds
    and leaves the image running as root, which Claude Code refuses."""
    from bakeoff import images

    seen = []
    monkeypatch.setattr(images, "_run", lambda args, cwd=None: seen.append(args))
    monkeypatch.setattr(images, "image_id", lambda tag: "sha256:x")

    images.build_base_image(Path("/repo"), "node", "22")

    argv = " ".join(seen[0])
    assert "eval-agent-node.Dockerfile" in argv
    assert "BASE_NODE_VERSION=22" in argv
    assert "BASE_PYTHON_VERSION" not in argv


def test_both_base_dockerfiles_pin_the_same_claude_code_version():
    """Versions.claude_code is read from the transcript, per run, and nothing
    compares it ACROSS tasks. Two Dockerfiles carrying two ARG lines is a real
    way for two arms of one comparison to run different agents, invisibly."""
    root = Path(__file__).resolve().parent.parent / "docker"
    pattern = re.compile(r"^ARG CLAUDE_CODE_VERSION=(\S+)", re.M)

    pins = {
        pattern.search(p.read_text()).group(1)
        for p in (root / "eval-agent.Dockerfile",
                  root / "eval-agent-node.Dockerfile")
    }

    assert len(pins) == 1


def test_both_base_dockerfiles_run_as_a_non_root_eval_user_and_clear_entrypoint():
    root = Path(__file__).resolve().parent.parent / "docker"
    for name in ("eval-agent.Dockerfile", "eval-agent-node.Dockerfile"):
        text = (root / name).read_text()
        assert "USER eval" in text
        assert "ENTRYPOINT []" in text
        assert "--uid 1000 eval" in text


def test_the_node_dockerfile_deletes_the_images_own_uid_1000_user_first():
    """Measured 2026-09-01: node:22-bookworm-slim ships `node:x:1000:1000`, and
    `useradd --create-home --uid 1000 eval` fails there with exit 4. Without
    the userdel the build dies; with it removed later, it dies again."""
    text = (Path(__file__).resolve().parent.parent / "docker"
            / "eval-agent-node.Dockerfile").read_text()

    assert text.index("userdel") < text.index("--uid 1000 eval")


def test_the_node_dockerfile_installs_the_runners_outside_repo():
    """Baking node_modules at /repo is REPLACED by the bind mount -- measured,
    `node_modules GONE` -- which is the failure `pip install -e .` avoids and
    node has no site-packages to avoid it with. /node_modules resolves because
    node's resolver walks up from the importing file."""
    text = (Path(__file__).resolve().parent.parent / "docker"
            / "eval-agent-node.Dockerfile").read_text()

    # M15: `npm install --prefix /` fails on a bare tree with `Tracker
    # "idealTree" already exists`, so /package.json is written first and the
    # install runs with `cd /` and no --prefix.
    assert "> /package.json" in text
    assert "--save-dev" not in text   # the runners must stay under dependencies
    assert "/repo/node_modules" not in text
    assert "NODE_PATH" not in text  # measured unnecessary; see the plan's D4


def test_a_node_task_image_re_asserts_the_bases_runner_pins_after_build():
    """A task's own dependency install can remove the runners: `npm ci`'s
    documented contract is to delete node_modules before installing, and at the
    `/` prefix that is where they live. HARVESTING recommends `npm install`
    instead -- but a convention this codebase cannot enforce is one a task will
    eventually violate, and the failure reaches the model as exit 127 on every
    arm of that task."""
    text = render_dockerfile("base", apt=[], pip=[],
                             build=["npm install --prefix / lodash"],
                             runtime="node")

    assert text.index("npm install --prefix / lodash") < text.index(
        "BAKEOFF_VITEST_VERSION")
    assert "npm ci" in text  # the remedy is named in the failure message


def test_a_python_task_image_renders_byte_identically_to_today():
    assert render_dockerfile("base", apt=["less"], pip=["pytest==8.3.5"],
                             build=[]) == render_dockerfile(
        "base", apt=["less"], pip=["pytest==8.3.5"], build=[],
        runtime="python")


def _runtime_task_stub(framework):
    class _Image:
        apt = ()
        pip = ()
        build = ()
        env = {}
        # BOTH, on every task. That is the shape the loader produces -- a
        # vitest manifest still carries the python default nobody read -- and
        # it is why `task_runtime` is the only correct index into `bases`.
        python = "3.12"
        node = "22"

    class _Tests:
        pass

    _Tests.framework = framework

    class _Task:
        task_id = "rt"
        task_version = 1
        repo_url = "file:///nowhere"
        base_sha = "0" * 40
        image = _Image()
        tests = _Tests()
        strip_paths = ()
        submodules_unneeded = ()
        test_files = ()
        solution_files = ()
        extra_files = ()

    return _Task()


def _generated_dockerfile(framework, tmp_path, monkeypatch):
    archive = _archive_bytes(tmp_path / "source")
    real_run = subprocess.run
    monkeypatch.setattr("bakeoff.tasks.ensure_mirror",
                        lambda url, sha, cache: tmp_path / "mirror")
    monkeypatch.setattr("bakeoff.images.subprocess.run",
                        _fake_run_with_archive(archive, real_run))
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")

    build_task_image(_runtime_task_stub(framework), "sha256:base",
                     tmp_path / "build", tmp_path / "cache")
    return (tmp_path / "build" / "image-rt" / "Dockerfile").read_text()


def test_the_re_assertion_reaches_a_generated_node_task_image(tmp_path, monkeypatch):
    """`render_dockerfile` growing the parameter is half of D15; the other half
    is `build_task_image` deriving the runtime from the TASK and passing it.
    Nothing in the rendered string can say whether the caller ever asks for
    `runtime="node"`, and a default that is never overridden is a check that
    never runs."""
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "index.js").write_text("module.exports = 1;\n")

    text = _generated_dockerfile("vitest", tmp_path, monkeypatch)

    assert "BAKEOFF_VITEST_VERSION" in text
    assert "BAKEOFF_JEST_VERSION" in text


def test_a_generated_pytest_task_image_carries_no_node_assertion(
    tmp_path, monkeypatch
):
    """The other side of the guard: a python task image is what it was, so no
    stored `container_image_digest` and no warm preflight verdict moves."""
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "calc.py").write_text("x = 1\n")

    text = _generated_dockerfile("pytest", tmp_path, monkeypatch)

    assert "BAKEOFF_VITEST_VERSION" not in text


@pytest.mark.integration
# `task_image` as well, even though no TASK image is built here: the section
# 6.6 gate selects `integration and not task_image` and is documented as
# needing no network, and a real base build pulls from Docker Hub, runs apt
# and reaches the npm registry.
@pytest.mark.task_image
def test_the_node_base_really_builds_and_carries_the_pins():
    """Measured 2026-09-02. The bare `vitest --version` rather than the
    absolute path is the assertion for `ENV PATH=/node_modules/.bin:$PATH`:
    `npm install` puts the shims at /node_modules/.bin and changes no
    environment (M12), so without that ENV a bare `vitest` is exit 127 -- and
    section 3.3 measures a loop run with commands THE AGENT INVENTS, where
    `npx vitest` working and `vitest` not is an inconsistency the agent
    discovers by burning turns."""
    repo_root = Path(__file__).resolve().parent.parent
    image = images.build_base_image(repo_root, "node", "22")

    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
         "node --version && vitest --version "
         "&& node -p 'require(\"/node_modules/vitest/package.json\").version' "
         "&& node -p 'require(\"/node_modules/jest/package.json\").version' "
         "&& jest --version && claude --version && id -u "
         # A loop, not `command -v a b c`: dash's builtin reports only the
         # FIRST name and exits 0, so the multi-argument spelling silently
         # asserts nothing about the four that follow.
         "&& for c in git rg timeout vitest jest; do command -v $c; done "
         "&& echo \"$BAKEOFF_VITEST_VERSION $BAKEOFF_JEST_VERSION\""],
        capture_output=True, text=True, check=True,
    )
    out = probe.stdout

    assert out.startswith("v22.")
    assert "vitest/3.2.7" in out                    # on PATH, not by full path
    assert "\n3.2.7\n" in out                       # vitest, from its package.json
    assert "\n30.5.0\n" in out                      # jest, from its package.json
    # Measured 2026-09-02 and asserted so it stays visible: jest 30.5.0's CLI
    # answers 30.4.2, because @jest/core's published bundle inlines a stale
    # version string. Pinning against `jest --version` would fail this build
    # forever, or force BAKEOFF_JEST_VERSION to a number no package.json in the
    # image agrees with -- which is why the Dockerfile reads the manifest.
    assert "\n30.4.2\n" in out
    assert "2.1.220" in out
    assert "\n1000\n" in out                        # uid, not root
    assert "/usr/bin/git" in out
    # The assertion for `ENV PATH=/node_modules/.bin:$PATH`. `npm install` puts
    # the shims at /node_modules/.bin and changes no environment (M12), so
    # without that ENV these two resolve to nothing and a bare `vitest` is exit
    # 127 -- while `npx vitest` and `npm test` work, an inconsistency section
    # 3.3's loop makes the agent discover by burning turns.
    assert "/node_modules/.bin/vitest" in out
    assert "/node_modules/.bin/jest" in out
    assert "3.2.7 30.5.0" in out                    # the exported pins, for D15


def test_neither_runner_is_pinned_against_its_cli_version():
    """Measured 2026-09-02: `npm install jest@30.5.0` resolves jest, jest-cli
    and @jest/core all to 30.5.0, and `jest --version` still answers 30.4.2 --
    @jest/core's published bundle carries an inlined
    `module.exports = {"version":"30.4.2"}`. So the CLI's answer is the version
    of nothing that is installed, and comparing against it would fail every
    node image build, on every arm, over an upstream packaging bug the model
    never saw.

    VITEST TOO, and its `--version` does answer 3.2.7. Two probes answering one
    question in two shapes is how one of them quietly stops being checked: a
    reader who sees vitest matched on `--version` has no reason to believe the
    jest half is doing something else on purpose.

    Asserted in BOTH places, because they are two independent copies of the
    check -- the base's own and the generated task image's (D15)."""
    text = (Path(__file__).resolve().parent.parent / "docker"
            / "eval-agent-node.Dockerfile").read_text()
    from bakeoff.images import _NODE_RUNNER_REASSERTION

    for where in (text, _NODE_RUNNER_REASSERTION):
        for runner in ("jest", "vitest"):
            assert f'require("/node_modules/{runner}/package.json").version' \
                in where
            # The `--version` calls that remain discard their output: they
            # prove the shim EXECUTES and are never compared to a pin. A
            # capture (`$(... --version)`) is what this refuses.
            assert f"$(/node_modules/.bin/{runner} --version" not in where


def test_the_re_assertion_refuses_a_base_that_declares_no_runner_pins():
    """Without this guard the whole D15 check passed VACUOUSLY, and it passed
    on exactly the input it exists to catch.

    `${BAKEOFF_VITEST_VERSION}` expands to the empty string on any base that
    declares no such ENV, and `case "" in "")` MATCHES -- so the vitest half
    went green against nothing at all. The guard is executed here rather than
    asserted as a substring, because what is being pinned is a shell
    semantics claim and a text match would survive a rewrite that broke it.
    """
    guard = _NODE_GUARD()

    empty = subprocess.run(
        ["sh", "-c", guard], capture_output=True, text=True,
        env={"BAKEOFF_VITEST_VERSION": "", "BAKEOFF_JEST_VERSION": ""},
    )
    half = subprocess.run(
        ["sh", "-c", guard], capture_output=True, text=True,
        env={"BAKEOFF_VITEST_VERSION": "3.2.7", "BAKEOFF_JEST_VERSION": ""},
    )
    both = subprocess.run(
        ["sh", "-c", guard], capture_output=True, text=True,
        env={"BAKEOFF_VITEST_VERSION": "3.2.7", "BAKEOFF_JEST_VERSION": "30.5.0"},
    )

    assert empty.returncode == 1
    assert "not a node base" in empty.stderr
    # ONE pin missing is still not a node base. `&&` between the two tests is
    # what makes that true; `||` between them would accept a half-declared base
    # and leave the other half of the check matching the empty string.
    assert half.returncode == 1
    assert both.returncode == 0


def test_an_empty_pin_would_otherwise_match_both_halves_of_the_check():
    """The measurement the guard above is built on, taken against `sh` rather
    than reasoned about: an empty `case` pattern matches the empty string, and
    jest's old trailing `*` degraded to a bare glob that matches ANYTHING --
    including the empty string `node -p` leaves behind when the runner it is
    asked about has been deleted."""
    probe = subprocess.run(
        ["sh", "-c",
         'case "" in "") echo VITEST_HALF_MATCHED ;; esac; '
         'case "30.4.2" in ""*) echo JEST_HALF_MATCHED ;; esac'],
        capture_output=True, text=True,
    )

    assert probe.stdout.split() == ["VITEST_HALF_MATCHED", "JEST_HALF_MATCHED"]
    # So the trailing `*` is gone: the base asserts the installed
    # package.json string exactly and this re-assertion compares the same one.
    assert '"${BAKEOFF_JEST_VERSION}"*)' not in images._NODE_RUNNER_REASSERTION
    assert '"${BAKEOFF_JEST_VERSION}")' in images._NODE_RUNNER_REASSERTION


def _NODE_GUARD() -> str:
    """The first statement of the re-assertion, as a runnable `sh` command.

    Sliced off the real constant rather than restated, because a second copy
    of the guard is a guard that can stop being the one the build runs.
    """
    first = images._NODE_RUNNER_REASSERTION.split("; \\\n", 1)[0]
    assert first.startswith("RUN "), first
    return first[len("RUN "):]


def test_every_copy_of_the_default_node_version_says_the_same_thing():
    """The node analogue of the python test above, and the same failure: two
    copies of "22" that can drift means a manifest declaring no `image.node`
    loads as a task whose base nobody built. The build succeeds, the suite
    runs, and the runtime is not the one the default named.

    TWO copies here rather than three -- `images.py` has no `_DEFAULT_NODE`,
    because `base_tag` is reached through `task_runtime`, which reads the
    manifest's own default."""
    dockerfile = (
        Path(__file__).resolve().parent.parent / "docker"
        / "eval-agent-node.Dockerfile"
    ).read_text()

    assert f"ARG BASE_NODE_VERSION={tasks._DEFAULT_NODE}\n" in dockerfile
    assert "FROM node:${BASE_NODE_VERSION}-bookworm-slim\n" in dockerfile
    assert tasks._DEFAULT_NODE in tasks._NODE_VERSIONS
