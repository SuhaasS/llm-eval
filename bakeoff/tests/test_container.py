import threading
import time

import pytest

from bakeoff.container import ContainerError, HostSampler, RunContainer

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


# --- the bind mount actually landed ------------------------------------------


class _FakeExec:
    """One `exec_run` answer, in the shape the docker SDK returns under
    `demux=True`: `(exit_code, (stdout, stderr))` with both halves bytes."""

    def __init__(self, exit_code: int = 0, stdout: bytes = b""):
        self.exit_code = exit_code
        self.output = (stdout, b"")


class _FakeContainer:
    """A started container that answers a scripted `/repo` listing.

    `removed` is the assertion this class exists for: `__exit__` is NOT
    invoked when `__enter__` raises, so a post-condition that refuses without
    removing leaves a live container carrying `bakeoff.eval_agent` -- the
    label `HostSampler` counts peers by -- and every concurrent run would file
    `contention_flag: true` against a container nobody is using.
    """

    def __init__(self, listing: bytes):
        self.id = "fake"
        self.listing = listing
        self.removed = False
        self.killed = False

    def exec_run(self, cmd, **_kw):
        if cmd[:1] == ["find"] or cmd[:1] == ["ls"]:
            return _FakeExec(0, self.listing)
        return _FakeExec(0, b"")

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class _FakeClient:
    def __init__(self, container):
        self.containers = self
        self._container = container

    def run(self, *_a, **_kw):
        return self._container


def _fake_docker(monkeypatch, container):
    import bakeoff.container as container_module

    monkeypatch.setattr(
        container_module.docker, "from_env", lambda: _FakeClient(container)
    )


def test_a_bind_mount_that_did_not_land_is_refused_and_the_container_removed(
    monkeypatch, tmp_path
):
    """The production half of the trap `test_integration_node_task._mounted`
    found. A host path the Docker VM does share, rmtree'd and re-materialized
    between containers, is served from a stale mount cache and appears EMPTY
    inside the container -- measured, four cycles of the same path gave
    `[files], [], [files], []`.

    Nothing downstream can tell that apart from a legitimate answer. An empty
    `/repo` exits **1** with `No test files found` under both node frameworks,
    which is the same exit a failing test gives; on the offline grader it
    becomes `APPLY_FAILED` and stamps `resolved: False` -- an accusation
    against a submission the model actually produced -- over an environment
    difference the model never saw. So the check is here, at the one place
    every call site goes through, rather than in each of the four that reuse a
    host path.

    The removal is half the guarantee. `__exit__` is not invoked when
    `__enter__` raises, and the container carries `bakeoff.eval_agent` -- the
    label `HostSampler` counts peers by -- so a refusal that left it running
    would flip `contention_flag` on every concurrent run for as long as it
    survived.
    """
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.txt").write_text("x")
    fake = _FakeContainer(listing=b"")
    _fake_docker(monkeypatch, fake)

    with pytest.raises(ContainerError, match="bind mount"):
        with RunContainer(
            image="sha256:" + "0" * 64,
            repo_path=str(tmp_path / "repo"),
            base_sha="",
        ):
            pass

    assert fake.removed, (
        "a refused container was left running under the label the sampler "
        "counts peers by"
    )


def test_a_bind_mount_that_landed_is_not_refused(monkeypatch, tmp_path):
    """The other half: a container that answers with anything at all is the
    ordinary case, and this check must never cost a run that would work."""
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.txt").write_text("x")
    fake = _FakeContainer(listing=b"/repo/a.txt\n")
    _fake_docker(monkeypatch, fake)

    with RunContainer(
        image="sha256:" + "0" * 64,
        repo_path=str(tmp_path / "repo"),
        base_sha="",
    ) as container:
        assert container._container is fake
    assert fake.removed, "the ordinary exit still removes the container"


def test_an_empty_host_directory_is_not_a_mount_failure(monkeypatch, tmp_path):
    """The post-condition is conditioned on the HOST side being non-empty,
    because an empty answer is only evidence of a stale mount when there was
    something to see. `oracle.py` and the grader both start a container over a
    directory they are about to populate, and refusing those would turn a
    defensive check into an outage."""
    (tmp_path / "repo").mkdir()
    fake = _FakeContainer(listing=b"")
    _fake_docker(monkeypatch, fake)

    with RunContainer(
        image="sha256:" + "0" * 64,
        repo_path=str(tmp_path / "repo"),
        base_sha="",
    ):
        pass


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


# --- HostSampler: the host block is measured, or says it was not -------------


def test_a_first_stream_frame_yields_no_percentage():
    """Measured against docker SDK 7.2.0 / daemon 29.5.2: the FIRST frame of a
    stats stream has `precpu_stats.cpu_usage.total_usage == 0` and NO
    `system_cpu_usage` key at all.

    That absence is the trap. `pre.get("system_cpu_usage", 0)` defaults the
    missing key to zero, so sys_delta becomes the ABSOLUTE system total --
    enormous and positive -- the guard passes, and a fabricated ~0.000003%
    reading enters the series on every run. Guard on the key, not on the sign.
    """
    frame = {
        "cpu_stats": {"cpu_usage": {"total_usage": 9_310_000},
                      "system_cpu_usage": 791_801_200_000_000, "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}},
    }
    assert HostSampler(container=None, client=None)._cpu_pct(frame) is None


def test_cpu_percent_is_scaled_by_online_cpus():
    """Docker reports a fraction of total system jiffies; multiplying by the
    core count is what turns it into the percentage `docker stats` prints. On
    this daemon online_cpus is 2 -- the VM's allocation, not the Mac's 18."""
    assert HostSampler(container=None, client=None)._cpu_pct({
        "cpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 8_000,
                      "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
    }) == pytest.approx(25.0)


def test_a_malformed_frame_is_skipped_not_counted_as_zero():
    """Docker has changed this payload before -- cgroup v2 already dropped
    max_usage. A KeyError must cost the sample, never the run, and must not
    enter the series as a zero."""
    assert HostSampler(container=None, client=None)._cpu_pct({"cpu_stats": {}}) is None


def test_the_p95_ignores_one_spike_and_catches_a_sustained_one():
    """Nearest-rank p95: index ceil(0.95*n)-1 of the sorted series. One outlier
    in twenty is below it by construction -- that is the point, a single
    scheduling blip is not contention -- while two in twenty is above it."""
    sampler = HostSampler(container=None, client=None)
    sampler._cpu = [10.0] * 19 + [400.0]
    assert sampler.metrics().cpu_pct_p95 == 10.0
    sampler._cpu = [10.0] * 18 + [400.0, 400.0]
    assert sampler.metrics().cpu_pct_p95 == 400.0


def test_an_unsampled_run_claims_nothing():
    """The defect this class exists for. `contention_flag: bool = False` put
    "this run had the host to itself" into every record ever written, while
    nothing called stats() at all -- one fabricated false beside two honest
    nulls."""
    metrics = HostSampler(container=None, client=None).metrics()
    assert metrics.contention_flag is None
    assert metrics.cpu_pct_p95 is None
    assert metrics.mem_peak_mb is None
    assert metrics.load_p95 is None
    assert metrics.vm_cpus is None
    assert metrics.samples == 0


def test_a_sampler_that_dies_names_it_and_does_not_raise():
    """Measured 2026-08-12: a raise from inside the run body unwound past the
    container and recorded CRASHED with zero turns for a run that worked. A CPU
    reading is supplementary; the trajectory is the product."""
    entered = threading.Event()

    class _Boom:
        def stats(self, **_kw):
            entered.set()
            raise RuntimeError("docker went away")

    sampler = HostSampler(container=_Boom(), client=None)
    sampler.start()
    # Join, then assert, THEN stop. Calling stop() first races the thread: with
    # _stop already set, `_run` swallows the error by design and this asserts
    # on an empty string -- measured 9 failures in 2000 iterations at
    # sys.setswitchinterval(1e-6). An Event set before the raise does not close
    # it either (3 in 5000): the raise and the _stop check are still ahead of
    # the waiter. The thread terminates on its own and `_run` catches
    # everything, so joining it is deterministic.
    assert entered.wait(5)
    sampler._thread.join(5)
    assert "docker went away" in sampler.metrics().error
    assert sampler.metrics().samples == 0
    sampler.stop()


def test_a_refused_thread_is_recorded_and_never_raised(monkeypatch):
    """`Thread.start()` raises RuntimeError at the OS thread limit. Outside a
    guard that reaches execute_run's catch-all and records CRASHED with zero
    turns for a run that would have worked."""
    def _refuse(_self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _refuse)
    sampler = HostSampler(container=None, client=None)
    sampler.start()

    assert "can't start new thread" in sampler.metrics().error
    assert sampler._thread is None


def test_stop_is_total_even_when_the_thread_never_started():
    """The most load-bearing line in this class. stop() is called from
    execute_run's OUTER finally, which is not inside a try -- so a raise there
    escapes execute_run and the run produces no record at all, not even
    record.unwritten.json. `join()` on a constructed-but-never-started thread
    raises "cannot join thread before it is started", which is precisely the
    state a refused start leaves behind."""
    sampler = HostSampler(container=None, client=None)
    sampler._thread = threading.Thread(target=lambda: None)  # never started
    sampler.stop()   # must not raise
    sampler.stop()   # idempotent
    assert sampler.metrics().samples == 0


def test_a_raise_after_stop_is_teardown_not_a_sampling_failure():
    """__exit__ kills and force-removes the container, so a thread blocked in
    stats(stream=True) raises as a matter of course. The lifecycle stops the
    sampler inside the `with` on every path, but stop() only sets a flag and
    joins with a 3 s timeout -- it cannot interrupt a blocking read -- so on a
    join timeout this thread is still streaming when the container goes away.
    Without the flag that lands in HostMetrics.error as a sampling failure,
    non-deterministically, on runs that sampled fine."""
    raised = threading.Event()

    class _DiesWhenRemoved:
        def __init__(self, stop_event):
            self._stop = stop_event

        def stats(self, **_kw):
            yield {"cpu_stats": {}, "memory_stats": {}}
            raised.set()
            # The EVENT, never sampler.stop(): stop() joins, and joining from
            # inside the sampler thread raises "cannot join current thread" --
            # which the sampler would then record, so the test would pass on
            # the wrong exception and prove nothing.
            self._stop.set()
            raise RuntimeError("container 4f2a is not running")

    sampler = HostSampler(container=None, client=None)
    sampler._container = _DiesWhenRemoved(sampler._stop)
    sampler.start()
    assert raised.wait(5)
    sampler._thread.join(5)
    assert sampler.metrics().error == ""
    sampler.stop()


def test_contention_is_flagged_when_another_eval_container_shares_the_host():
    """Deliberately narrow, and the docstring says so. The matrix is sequential
    by design, so this fires rarely -- and the alternatives were worse: a
    loadavg threshold needs host load above os.cpu_count() (18 here) while the
    agent's real budget is a 2-vCPU VM, so it can never fire, and mixing the
    two puts denominators from different machines in one boolean."""
    sampler = HostSampler(container=None, client=None)
    sampler._frames, sampler._peers = 2, [1, 2]
    assert sampler.metrics().contention_flag is True


def test_a_run_that_saw_only_itself_is_a_measured_false():
    sampler = HostSampler(container=None, client=None)
    sampler._frames, sampler._peers = 2, [1, 1]
    assert sampler.metrics().contention_flag is False


def test_frames_without_a_single_peer_observation_claim_nothing():
    """`any([])` is False, so gating this on frames rather than on peers would
    assert "no other eval container shared the host" from zero peer
    observations -- true of every run with no docker client and every run whose
    polls all failed. That is the fabricated false this class exists to remove,
    reintroduced one layer down. The poll failure gets named too."""
    sampler = HostSampler(container=None, client=None)
    sampler._frames, sampler._peers = 30, []
    sampler._peer_error = "peer poll: APIError: boom"
    metrics = sampler.metrics()
    assert metrics.contention_flag is None
    assert "peer poll" in metrics.error


def test_a_frame_with_no_memory_block_is_not_zero_megabytes():
    """The same rule _cpu_pct follows, on the memory axis. A frame that carried
    no `usage` must not report "the container used 0 MB"."""
    sampler = HostSampler(container=None, client=None)
    sampler._frames = 3
    assert sampler.metrics().mem_peak_mb is None
    sampler._mem_bytes = 5 * 1024 * 1024
    assert sampler.metrics().mem_peak_mb == 5


def test_load_is_recorded_as_a_number_and_never_as_a_verdict():
    """The harness measures and does not decide. load_p95 is host loadavg over
    host CPUs -- a different machine from cpu_pct_p95's VM -- so it is stored
    for a derived view to interpret and is NOT folded into contention_flag."""
    sampler = HostSampler(container=None, client=None)
    sampler._frames, sampler._peers, sampler._load = 2, [1, 1], [0.1, 9.0]
    metrics = sampler.metrics()
    assert metrics.load_p95 == 9.0
    assert metrics.contention_flag is False


@integration
def test_a_real_container_reports_cpu_and_memory(alpine_container):
    """The unit tests pin the arithmetic; this pins that the frames arrive at
    all and that the sampler survives a CONCURRENT exec_stream -- Container.stats
    and the agent's stream share one APIClient and requests.Session, and
    docker-py guarantees nothing about that. The sampler is written to fail
    quietly, so without this a payload change or a client collision would
    surface as a record full of honest nulls."""
    sampler = alpine_container.host_sampler()
    sampler.start()
    # exec_stream(cmd, on_stdout_line, env=None) -> ExecResult. It is NOT a
    # generator and the callback is required.
    alpine_container.exec_stream(
        ["sh", "-c", "i=0; while [ $i -lt 3000000 ]; do i=$((i+1)); done; echo done"],
        lambda _line: None,
    )
    # Poll rather than sizing the workload. Measured frame cadence is ~1 s, so
    # a fixed workload sits one scheduling hiccup away from a flaky failure.
    deadline = time.monotonic() + 30
    while sampler._frames < 2 and time.monotonic() < deadline:
        time.sleep(0.2)
    sampler.stop()

    metrics = sampler.metrics()
    assert metrics.error == ""
    assert metrics.samples >= 2, "one frame yields no percentage by construction"
    assert metrics.cpu_pct_p95 is not None
    assert metrics.mem_peak_mb is not None
    assert metrics.vm_cpus and metrics.vm_cpus > 0
    assert metrics.load_p95 is not None
    # Not `is False`. The peer count is over every running bakeoff.eval_agent
    # container, and this same label is how a container leaked by a crashed run
    # is found -- so a dirty machine would fail this on a correct
    # implementation. What is under test is that a peer observation was made.
    assert metrics.contention_flag in (False, True)


@integration
def test_the_eval_container_carries_the_label_the_sampler_counts(alpine_container):
    """The label is also what makes a container leaked by a crashed run
    identifiable: docker ps -a --filter label=bakeoff.eval_agent."""
    import docker

    running = docker.from_env().containers.list(
        filters={"label": "bakeoff.eval_agent=1"}
    )
    assert any(c.id == alpine_container._container.id for c in running)


def test_no_container_is_nothing_to_sample_not_a_sampling_failure():
    """The RunContainer doubles in test_fault_injection and test_smoke_test
    hand back a sampler with no container. Without the guard its thread raises
    AttributeError on None.stats, `_run` files that, and every record those
    tests write carries a fabricated `error` -- corrupting the one field whose
    job is to say that sampling broke.

    `samples: 0` with an empty error already says "nobody measured"."""
    sampler = HostSampler(container=None, client=None)
    sampler.start()
    sampler.stop()
    metrics = sampler.metrics()
    assert metrics.error == ""
    assert metrics.samples == 0
    assert metrics.contention_flag is None
