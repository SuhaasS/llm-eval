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

from bakeoff.images import render_dockerfile


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
