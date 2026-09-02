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

from bakeoff.images import _strip_build_context, build_task_image, render_dockerfile


def _lines(**kwargs) -> list[str]:
    kwargs.setdefault("base_image", "sha256:base")
    kwargs.setdefault("apt", [])
    kwargs.setdefault("pip", [])
    kwargs.setdefault("build", [])
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


def test_a_path_missing_from_the_build_context_is_not_an_error(tmp_path):
    """`materialize` raises on exactly this, and `run_matrix` builds the image
    BEFORE materializing -- raising in both places means one typo is reported
    twice and fixed twice."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _strip_build_context(repo, ["nope", "also/nope"])


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
