import pytest

from bakeoff.container import ContainerError, RunContainer

integration = pytest.mark.integration


def test_rejects_tag_instead_of_digest():
    """Spec section 5.1: images pin by digest, never by tag.

    Not marked integration -- this validates __init__ and needs no daemon,
    so the pinning guarantee is checked on every machine, not only ones
    where Docker happens to be running.
    """
    with pytest.raises(ContainerError, match="digest"):
        RunContainer(image="python:3.11", repo_path="/tmp/x", base_sha="abc")


@integration
def test_exec_returns_exit_code_and_output(alpine_container):
    result = alpine_container.exec(["echo", "hello"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.duration_ms >= 0


@integration
def test_exec_captures_nonzero_exit(alpine_container):
    result = alpine_container.exec(["sh", "-c", "exit 3"])
    assert result.exit_code == 3


@integration
def test_repo_is_actually_mounted(git_container):
    """Guards against a silently empty bind mount.

    On macOS the Docker VM mounts $HOME but not /var/folders, so a repo under
    pytest's default tmp_path appears inside the container as an empty
    directory with no error raised. Every snapshot assertion below would then
    pass for the wrong reason -- an empty mount looks exactly like a clean
    tree. Fail here instead, loudly.
    """
    result = git_container.exec(["ls", "/repo/tests/test_a.py"])
    assert result.exit_code == 0, (
        "repo did not mount; run with --basetemp under $HOME"
    )


@integration
def test_git_is_available_in_container(git_container):
    """Guards against the other source of vacuous passes.

    network_mode="none" means git cannot be installed at runtime, and a
    missing git makes every snapshot_diff call return empty output -- which
    reads as a clean tree rather than as a broken container.
    """
    result = git_container.exec(["git", "--version"])
    assert result.exit_code == 0
    assert "git version" in result.stdout


@integration
def test_snapshot_diff_returns_empty_for_clean_tree(git_container):
    diff, files = git_container.snapshot_diff(git_container.base_sha)
    assert diff == ""
    assert files == []


@integration
def test_snapshot_diff_includes_untracked_files(git_container):
    git_container.exec(["sh", "-c", "echo new > /repo/added.txt"])
    diff, files = git_container.snapshot_diff(git_container.base_sha)
    assert "added.txt" in files
    assert "new" in diff


@integration
def test_restore_paths_reverts_agent_edits_to_test_files(git_container):
    """Spec section 4.2.1 check 2: the agent must not be able to influence
    its own grader."""
    git_container.exec(["sh", "-c", "echo tampered > /repo/tests/test_a.py"])
    git_container.restore_paths(git_container.base_sha, ["tests/test_a.py"])
    result = git_container.exec(["cat", "/repo/tests/test_a.py"])
    assert "tampered" not in result.stdout


@integration
def test_snapshot_diff_raises_when_git_fails(alpine_container):
    """A failed snapshot must not be indistinguishable from a clean tree.

    git reports "not a git repository" on stderr, and snapshot_diff returns
    stdout, so without an exit-code check this call returns ("", []) -- which
    a Checkpoint stores as "the agent had changed nothing by this turn."
    That is a fabricated measurement feeding the cost-at-budget-K curve, not
    a visible error. Fail instead.
    """
    with pytest.raises(ContainerError, match="git"):
        alpine_container.snapshot_diff("abc123")


@integration
def test_network_is_disabled(alpine_container):
    result = alpine_container.exec(
        ["sh", "-c", "wget -q -T 2 -O- http://example.com || echo BLOCKED"]
    )
    assert "BLOCKED" in result.stdout
