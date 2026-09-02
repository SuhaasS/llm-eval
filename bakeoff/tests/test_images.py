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
import subprocess
import tarfile
from pathlib import Path

import pytest

import bakeoff.images as images
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

    def fake_run(args, **kwargs):
        if list(args)[:2] == ["git", "archive"]:
            return subprocess.CompletedProcess(args, 0, stdout=archive, stderr=b"")
        return real_run(args, **kwargs)

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
        env = {}

    class _Task:
        task_id = "t"
        task_version = 1
        repo_url = "file:///nowhere"
        base_sha = "0" * 40
        image = _Image()
        strip_paths = ("CLAUDE.md", ".claude")

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    unpacked = tmp_path / "build" / "image-t" / "repo"
    assert (unpacked / "calc.py").exists(), "the archive was unpacked at all"
    assert not (unpacked / "CLAUDE.md").exists()
    assert not (unpacked / ".claude").exists()


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
    lines = _lines(build=["pip install -e ."], env={"CI": "1"})
    last_run = max(i for i, line in enumerate(lines) if line.startswith("RUN "))
    env_index = next(i for i, line in enumerate(lines)
                     if line.startswith("ENV CI="))

    assert env_index > last_run
    # ...and still before the USER switch, so the file reads top-to-bottom as
    # root-setup then eval-runtime.
    assert env_index < lines.index("USER eval")


def test_env_lines_are_sorted_so_the_image_id_is_a_function_of_the_manifest():
    """One ENV line per key, in sorted order. Dict insertion order would make
    the Dockerfile text -- and therefore the built image id, which is what
    Versions.container_image_digest records -- depend on YAML key order rather
    than on the manifest's content."""
    forward = _lines(env={"CI": "1", "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/h"})
    reverse = _lines(env={"HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/h", "CI": "1"})

    assert forward == reverse
    assert [line for line in forward if line.startswith("ENV ")] == [
        'ENV CI="1"',
        'ENV HYPOTHESIS_STORAGE_DIRECTORY="/tmp/h"',
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

    def fake_run(args, **kwargs):
        if list(args)[:2] == ["git", "archive"]:
            return subprocess.CompletedProcess(args, 0, stdout=archive,
                                               stderr=b"")
        return real_run(args, **kwargs)

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
        env = {"CI": "1"}

    class _Task:
        task_id = "envwiring"
        task_version = 1
        repo_url = "file:///nowhere"
        base_sha = "0" * 40
        image = _Image()
        strip_paths = ()

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    assert rendered["env"] == {"CI": "1"}


# --- the parameterised base image ---------------------------------------------


def test_each_version_gets_its_own_tag():
    """One tag for two interpreters means the second build silently replaces
    the first, and every task afterwards resolves the same tag to the wrong
    base -- which builds, runs, and is green."""
    assert images.base_tag("3.11") == "bakeoff-eval-agent:base-3.11"
    assert images.base_tag("3.12") != images.base_tag("3.11")


def test_build_base_image_passes_the_version_as_a_build_arg(monkeypatch):
    calls = []
    monkeypatch.setattr(images, "_run", lambda args, cwd=None: calls.append(args) or "")
    monkeypatch.setattr(images, "image_id", lambda tag: "sha256:" + tag)

    images.build_base_image(Path("/repo_root"), "3.11")

    argv = calls[0]
    assert "--build-arg" in argv
    assert argv[argv.index("--build-arg") + 1] == "BASE_PYTHON_VERSION=3.11"
    assert "-t" in argv and argv[argv.index("-t") + 1] == \
        "bakeoff-eval-agent:base-3.11"


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

    images.build_base_image(Path("/repo_root"), "3.13")

    assert "PYTHON_VERSION=3.13" not in calls[0]


def test_one_build_per_distinct_version_not_per_request(monkeypatch):
    """The drivers call this with one entry per TASK. Building per task pays
    an image build for every duplicate, and on a 60-task set that is the
    difference between a preflight pass and one nobody waits for."""
    built = []
    monkeypatch.setattr(
        images, "build_base_image",
        lambda root, version: built.append(version) or ("sha256:" + version),
    )

    bases = images.build_base_images(Path("/r"), ["3.12", "3.11", "3.12", "3.12"])

    assert sorted(built) == ["3.11", "3.12"]
    assert bases == {"3.11": "sha256:3.11", "3.12": "sha256:3.12"}


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

    tags = {images.base_tag(v) for v in _PYTHON_VERSIONS}

    assert len(tags) == len(_PYTHON_VERSIONS)


@pytest.mark.integration
def test_a_non_default_base_really_builds_and_carries_the_pins():
    """The claim this broadening rests on, checked against a daemon rather
    than against a rendered string. Measured 2026-09-01: 3.11 gives Python
    3.11.16, pytest 9.1.1, claude 2.1.220, uid 1000."""
    repo_root = Path(__file__).resolve().parent.parent
    image = images.build_base_image(repo_root, "3.11")

    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
         "python --version && pytest --version && claude --version && id -u"],
        capture_output=True, text=True, check=True,
    )

    assert probe.stdout.startswith("Python 3.11.")
    assert "pytest 9.1.1" in probe.stdout
    assert "2.1.220" in probe.stdout
    assert probe.stdout.rstrip().endswith("1000")
