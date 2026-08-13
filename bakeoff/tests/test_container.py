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
    with pytest.raises(ContainerError, match="digest"):
        RunContainer(image="python@latest", repo_path="/tmp/x", base_sha="abc")


def test_accepts_either_form_of_content_pin():
    """A registry digest travels between machines; a bare image ID pins the
    image config, and so every layer, for an image built locally and never
    pushed. Both are content pins; a tag is not."""
    RunContainer(
        image="python@sha256:" + "0" * 64, repo_path="/tmp/x", base_sha="abc"
    )
    RunContainer(image="sha256:" + "0" * 64, repo_path="/tmp/x", base_sha="abc")


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
def test_snapshot_does_not_disturb_the_agents_own_index(git_container):
    """Checkpoints are now taken WHILE the agent works, so staging must not
    touch the index the agent is using.

    `git add -A` against the real index would silently stage the agent's
    in-progress work under it -- a later `git commit` by the agent would
    then sweep in files it never staged, and `git status` would lie. The
    snapshot stages into a throwaway GIT_INDEX_FILE instead.
    """
    git_container.exec(["sh", "-c", "echo agent-staged > /repo/staged.txt"])
    git_container.exec(["git", "add", "staged.txt"])
    git_container.exec(["sh", "-c", "echo agent-unstaged > /repo/loose.txt"])

    git_container.snapshot_diff(git_container.base_sha)

    staged = git_container.exec(["git", "diff", "--cached", "--name-only"])
    assert staged.stdout.split() == ["staged.txt"], (
        "snapshot leaked into the agent's index"
    )


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


@integration
def test_isolated_network_reaches_the_proxy_and_nothing_else(
    internal_network, image_digest, tmp_path
):
    """Spec section 5.1's second branch: network off, OR through a recording
    proxy. The agent runs in this container and must reach the LiteLLM proxy
    to call a model at all -- so "no network" is not an option, and "some
    network" has to mean exactly one endpoint.

    Both halves are asserted. Checking only that the internet is blocked
    would pass on a container with no network at all, which is the config
    the agent cannot work under.
    """
    (tmp_path / "repo").mkdir()
    with RunContainer(
        image=image_digest,
        repo_path=str(tmp_path / "repo"),
        base_sha="",
        network=internal_network,
    ) as container:
        reachable = container.exec(
            ["sh", "-c", "wget -q -T 5 -O- http://litellm:4000/ping || echo UNREACHABLE"]
        )
        assert "PROXY-OK" in reachable.stdout, reachable.stdout

        blocked = container.exec(
            ["sh", "-c", "wget -q -T 5 -O- http://example.com || echo BLOCKED"]
        )
        assert "BLOCKED" in blocked.stdout


@integration
def test_isolation_is_measured_from_the_networks_actually_joined(
    internal_network, image_digest, tmp_path
):
    """`isolated` was `bool(network)` -- the argument the caller passed, not a
    property anything checked. That is the one place runner.py's own rule
    (configuration is never reported as observation) did not hold, and `True`
    would have survived both ways it can be wrong: a `network` naming a
    routable network, and a container that also joined the default bridge.

    Asserted beside the behavioural test above, not instead of it. That one
    proves the property holds; this one proves the RECORD would have noticed if
    it did not.
    """
    (tmp_path / "repo").mkdir()
    with RunContainer(
        image=image_digest,
        repo_path=str(tmp_path / "repo"),
        base_sha="",
        network=internal_network,
    ) as container:
        isolated, evidence = container.network_isolation()

    assert isolated is True
    assert internal_network in evidence
    assert "internal" in evidence


@integration
def test_a_routable_network_is_reported_as_not_isolated(image_digest, tmp_path):
    """The finding this exists to make. A non-internal network gives the agent
    a route off the host, and every arm's `isolated: true` would have gone on
    saying otherwise."""
    import docker

    client = docker.from_env()
    network = client.networks.create("bakeoff-test-routable", driver="bridge")
    (tmp_path / "repo").mkdir()
    try:
        with RunContainer(
            image=image_digest,
            repo_path=str(tmp_path / "repo"),
            base_sha="",
            network=network.name,
        ) as container:
            isolated, evidence = container.network_isolation()
        assert isolated is False
        assert "ROUTABLE" in evidence
    finally:
        network.remove()


@integration
def test_no_network_at_all_is_not_reported_as_isolated(alpine_container):
    """network_mode=none is trivially unroutable and deliberately NOT isolated:
    section 5.1's property is "no route off the host EXCEPT the recording
    proxy", and with no network there is no proxy either. Same answer the old
    `bool(network)` gave, now for the stated reason rather than by accident.

    The reason is load-bearing and was measured wrong first. Docker reports
    network_mode=none as membership of a network literally NAMED `none`, whose
    `Internal` flag is false -- so reading that flag alone labels the one
    configuration with no connectivity at all "ROUTABLE". Right verdict, wrong
    evidence, and the evidence is what this field is for.
    """
    isolated, evidence = alpine_container.network_isolation()
    assert isolated is False
    assert "null-driver" in evidence
    assert "ROUTABLE" not in evidence
    assert "no recording proxy" in evidence


class _AddSequence:
    """A `git add -A` that fails the way the real race does, N times."""

    def __init__(self, failures: int, message: str):
        self.failures = failures
        self.message = message
        self.add_calls = 0

    def __call__(self, cmd, env=None):
        from bakeoff.container import ExecResult

        if cmd[:3] == ["git", "add", "-A"]:
            self.add_calls += 1
            if self.add_calls <= self.failures:
                return ExecResult(128, "", self.message, 1)
            return ExecResult(0, "", "", 1)
        return ExecResult(0, "diff-body", "", 1)


def _container_with(exec_stub) -> RunContainer:
    container = RunContainer(
        image="sha256:" + "0" * 64, repo_path="/tmp/x", base_sha="abc"
    )
    container.exec = exec_stub  # type: ignore[method-assign]
    container.checked_exec = lambda cmd, env=None: exec_stub(cmd, env)  # type: ignore
    return container


RACE = "fatal: unable to stat 'calc.py.tmpQ3xR1a': No such file or directory"


def test_a_lost_race_against_the_agents_atomic_write_is_retried():
    """Checkpoints are captured WHILE the agent edits, and Claude Code's
    Write is atomic: it creates `<name>.tmpXXXX` and renames it. A rename
    landing between git's readdir and its stat makes `git add -A` exit 128.
    Measured 2026-08-12, once in seven offline arms -- and it took the whole
    run down, because the recorder is called from inside the agent's stdout
    loop.

    Retried rather than ignored: `--ignore-errors` would drop the file from
    the snapshot and report success, which is a wrong diff rather than a
    missing one."""
    stub = _AddSequence(failures=1, message=RACE)
    container = _container_with(stub)

    diff, _files = container.snapshot_diff("abc")

    assert stub.add_calls == 2
    assert diff == "diff-body"


def test_a_stat_failure_that_is_not_a_race_still_raises():
    """The window is a single rename, so a failure that survives every
    attempt is a broken container -- and a snapshot that silently returned
    empty output would be stored as "the agent had changed nothing", which is
    a fabricated measurement feeding the section 5.5 curve."""
    container = _container_with(_AddSequence(failures=99, message=RACE))

    with pytest.raises(ContainerError, match="unable to stat"):
        container.snapshot_diff("abc")


def test_an_unrelated_git_failure_is_not_retried():
    """Retrying "not a git repository" three times buys nothing and delays
    the report of a container that will never work."""
    stub = _AddSequence(failures=99, message="fatal: not a git repository")
    container = _container_with(stub)

    with pytest.raises(ContainerError, match="not a git repository"):
        container.snapshot_diff("abc")
    assert stub.add_calls == 1
