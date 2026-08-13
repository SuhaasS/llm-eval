"""Docker lifecycle with digest pinning and git state control.

Spec section 5.1: image pinned by digest, repo detached at base_sha,
network isolated, fresh container per sample.

Network. The default is network_mode="none". The agent itself runs in this
container (Task 10) and has to reach the LiteLLM proxy, which "none" makes
impossible -- so section 5.1's other branch applies: "network off during
the run, OR through a recording proxy". Pass `network` to attach an
internal Docker network carrying only the proxy. Internal networks have no
route off the host, so the proxy stays the single reachable endpoint, and
it is the endpoint already being recorded (section 6.2).

Note on install_git: with no route to a mirror a package manager cannot
install anything, so the image must already ship git. The flag survives
only to short-circuit on `command -v git`. A container without git makes
snapshot_diff return empty output, which reads as a clean tree rather than
as a broken container -- so the image, not this flag, is what guarantees
git is present.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from math import ceil
from typing import Any, Callable

import docker

from bakeoff.schema import HostMetrics

REPO_MOUNT = "/repo"

# How many times `git add -A` may lose the race against the agent's own
# atomic writes before the failure is treated as real. Three, with a short
# pause: the window is a single rename, so an attempt that fails twice more
# is not a race.
_ADD_ATTEMPTS = 3
_ADD_RETRY_DELAY_S = 0.2

# Snapshots stage into their own index rather than the repo's. Checkpoints
# are captured while the agent is working, and `git add -A` against the
# real index would stage the agent's in-progress work under it: a later
# commit by the agent would sweep in files it never staged, and its
# `git status` would disagree with reality. Nothing here writes to
# .git/index at all.
SNAPSHOT_INDEX = "/tmp/bakeoff-snapshot-index"


class ContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class RunContainer:
    def __init__(
        self,
        image: str,
        repo_path: str,
        base_sha: str,
        install_git: bool = False,
        mem_limit: str = "4g",
        network: str | None = None,
        extra_mounts: dict[str, str] | None = None,
    ) -> None:
        # Two forms of content pin are accepted. "repo@sha256:..." is the
        # registry digest and travels between machines. A bare "sha256:..."
        # is an image ID -- the digest of the image config, which
        # transitively pins every layer. A locally built image has no
        # registry digest until it is pushed, so rejecting the ID form would
        # mean either not testing against purpose-built images or loosening
        # the check to accept tags, which is what section 5.1 forbids.
        if "@sha256:" not in image and not image.startswith("sha256:"):
            raise ContainerError(
                f"image must pin a digest, got {image!r} (spec section 5.1)"
            )
        self.image = image
        self.repo_path = repo_path
        self.base_sha = base_sha
        self.install_git = install_git
        self.mem_limit = mem_limit
        self.network = network
        self.extra_mounts = dict(extra_mounts or {})
        self._client: Any = None
        self._container: Any = None

    def __enter__(self) -> RunContainer:
        self._client = docker.from_env()
        volumes = {self.repo_path: {"bind": REPO_MOUNT, "mode": "rw"}}
        # Anything the agent needs that must NOT appear in the diff lives
        # here rather than under /repo -- CLAUDE_CONFIG_DIR above all, since
        # `git add -A` would otherwise sweep the whole config tree and every
        # transcript into each checkpoint.
        for host_path, bind in self.extra_mounts.items():
            volumes[host_path] = {"bind": bind, "mode": "rw"}

        network_kwargs: dict[str, Any] = (
            {"network": self.network}
            if self.network
            else {"network_mode": "none"}  # spec section 5.1
        )

        self._container = self._client.containers.run(
            self.image,
            # Override whatever the image declares -- task images carry their
            # own entrypoints, and an image with ENTRYPOINT ["git"] would run
            # `git sleep infinity` and exit immediately.
            entrypoint=["sleep"],
            command=["infinity"],
            volumes=volumes,
            working_dir=REPO_MOUNT,
            mem_limit=self.mem_limit,
            detach=True,
            auto_remove=False,
            # What HostSampler counts peers by, and what makes a container
            # leaked by a crashed run identifiable:
            #   docker ps -a --filter label=bakeoff.eval_agent
            # The proxy must NOT carry it -- it is a peer of every cell by
            # design, and counting it would flag contention on every run.
            # proxy.py builds its containers with client.containers.run
            # directly, so it does not.
            #
            # preflight.py also constructs a RunContainer, so it carries this
            # too. That is correct for the peer count -- a preflight in a
            # second terminal IS contention -- but it means the label marks
            # every container this harness starts, not only eval agents.
            labels={"bakeoff.eval_agent": "1"},
            **network_kwargs,
        )
        if self.install_git:
            self.exec(["sh", "-c", "command -v git || apk add --no-cache git"])
        if self.base_sha:
            self.exec(["git", "config", "--global", "--add", "safe.directory", REPO_MOUNT])
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._container is not None:
            try:
                self._container.kill()
            except Exception:  # noqa: BLE001 - teardown must never mask errors
                pass
            self._container.remove(force=True)

    def exec(self, cmd: list[str], env: dict[str, str] | None = None) -> ExecResult:
        if self._container is None:
            raise ContainerError("container not started")
        started = time.monotonic()
        result = self._container.exec_run(
            cmd, demux=True, workdir=REPO_MOUNT, environment=env
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_raw, stderr_raw = result.output
        return ExecResult(
            exit_code=result.exit_code,
            stdout=(stdout_raw or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_raw or b"").decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
        )

    def exec_stream(
        self,
        cmd: list[str],
        on_stdout_line: Callable[[str], None],
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run a command, delivering stdout lines as they arrive.

        The agent's turn boundaries are only observable live: a checkpoint
        taken after the process exits records the same end state under every
        turn number. Callers use on_stdout_line to snapshot mid-run.

        Enforce timeouts by wrapping cmd in `timeout` rather than by
        abandoning the read loop -- Docker has no API to kill a running
        exec, so a caller that simply stopped reading would leave the agent
        running inside the container it is about to tear down.
        """
        if self._container is None:
            raise ContainerError("container not started")

        api = self._client.api
        started = time.monotonic()
        handle = api.exec_create(
            self._container.id,
            cmd,
            workdir=REPO_MOUNT,
            environment=env,
            stdout=True,
            stderr=True,
        )
        pending = b""
        stderr_chunks: list[bytes] = []
        stdout_chunks: list[bytes] = []

        for out_chunk, err_chunk in api.exec_start(
            handle["Id"], stream=True, demux=True
        ):
            if err_chunk:
                stderr_chunks.append(err_chunk)
            if not out_chunk:
                continue
            stdout_chunks.append(out_chunk)
            pending += out_chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                on_stdout_line(line.decode("utf-8", errors="replace"))

        if pending.strip():
            on_stdout_line(pending.decode("utf-8", errors="replace"))

        return ExecResult(
            exit_code=api.exec_inspect(handle["Id"]).get("ExitCode") or 0,
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def checked_exec(self, cmd: list[str], env: dict[str, str] | None = None) -> ExecResult:
        """Run a command that must succeed, or say so.

        git writes failures ("not a git repository", a bad base_sha, an
        unreadable object) to stderr and exits non-zero, while this class
        returns stdout -- so an unchecked failure yields empty output that is
        byte-identical to a clean tree. Downstream that becomes a Checkpoint
        claiming the agent changed nothing, which is a fabricated measurement
        rather than a visible error.
        """
        result = self.exec(cmd, env=env)
        if result.exit_code != 0:
            raise ContainerError(
                f"{' '.join(cmd)} failed (exit {result.exit_code}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return result

    def snapshot_diff(self, base_sha: str) -> tuple[str, list[str]]:
        """Stage everything, then diff against base. Staging first captures
        untracked files and normalizes over whether the agent committed
        (spec section 5.6).

        `git diff` is run without --exit-code, so it returns 0 whether or not
        differences exist; a non-zero code is unambiguously a failure and
        never means "there were changes".

        Staging goes to SNAPSHOT_INDEX, never .git/index, because this runs
        concurrently with the agent. `git add -A` keeps that index in sync
        with the worktree on every call -- additions, modifications,
        deletions and untracked files alike -- so the diff is complete
        without the agent's own staging area being touched.

        `git add -A` IS RETRIED, and only for the one failure that concurrency
        causes. Checkpoints are captured while the agent is editing, and
        Claude Code's Write tool is atomic: it creates `<name>.tmpXXXX` and
        renames it. If the rename lands between git's readdir and its stat,
        git exits 128 with "unable to stat" -- measured on 2026-08-12, once
        in seven offline arms, and it took the whole run down with it (0
        turns, 0 tools, no diff, recorded as CRASHED).

        Retried rather than ignored, because `--ignore-errors` would drop the
        file from the snapshot and report success. Retried rather than merely
        contained, because a checkpoint that is missing is still evidence
        lost from the section 5.5 curve. The failure is preserved and raised
        if it survives every attempt: a stat error that is not a race is a
        broken container and must not be smoothed over.
        """
        env = {"GIT_INDEX_FILE": SNAPSHOT_INDEX}
        for attempt in range(_ADD_ATTEMPTS):
            result = self.exec(["git", "add", "-A"], env=env)
            if result.exit_code == 0:
                break
            message = (result.stderr or result.stdout).strip()
            if attempt == _ADD_ATTEMPTS - 1 or "unable to stat" not in message:
                raise ContainerError(f"git add -A failed (exit {result.exit_code}): {message}")
            time.sleep(_ADD_RETRY_DELAY_S)
        diff = self.checked_exec(["git", "diff", "--cached", base_sha], env=env)
        names = self.checked_exec(
            ["git", "diff", "--cached", "--name-only", base_sha], env=env
        )
        files = [line for line in names.stdout.splitlines() if line.strip()]
        return diff.stdout, files

    def restore_paths(self, base_sha: str, paths: list[str]) -> None:
        """Overwrite paths with their base_sha contents. Used to restore
        test files before grading (spec section 4.2.1, check 2)."""
        if not paths:
            return
        self.exec(["git", "checkout", base_sha, "--", *paths])

    def network_isolation(self) -> tuple[bool | None, str]:
        """Whether this container has a route off the host, MEASURED.

        Spec section 5.1's guarantee was previously reported as `bool(network)`
        -- the argument the caller passed, not a property anything checked. That
        is the one place runner.py's own rule (configuration is never reported
        as observation) did not hold, and it would have gone on saying `True`
        through both of the ways it can actually be false: a `network` naming a
        non-internal network, and a container that also joined the default
        bridge.

        Read from Docker rather than probed from inside. `Internal: true` IS
        the property section 5.1 wants -- Docker's own semantics for a network
        with no external routing -- so inspecting it observes the real thing at
        no runtime cost. The alternative, opening a socket to an off-host
        address and waiting for it to fail, buys nothing and pays for it in
        timeouts on every run.

        Returns (isolated, evidence). `None` means the inspection failed, so
        nobody measured -- distinct from `False`, which is a finding.
        """
        if self._container is None:
            return None, "container not started"
        try:
            self._container.reload()
            attached = (
                (self._container.attrs.get("NetworkSettings") or {}).get("Networks")
                or {}
            )
            if not attached:
                return False, "no networks attached: no recording proxy"

            verdicts: list[tuple[str, str]] = []
            for name in sorted(attached):
                attrs = self._client.networks.get(name).attrs
                if attrs.get("Driver") == "null":
                    # network_mode="none". Docker reports this as membership of
                    # a network literally named `none`, whose Internal flag is
                    # FALSE -- so reading that flag alone calls the one
                    # configuration with no connectivity at all "routable". The
                    # driver is what distinguishes it, and the distinction is
                    # not cosmetic: the evidence string is the whole point of
                    # this field, and a plausible wrong reason in it is worse
                    # than no reason.
                    verdicts.append((name, "null-driver"))
                elif attrs.get("Internal", False):
                    verdicts.append((name, "internal"))
                else:
                    verdicts.append((name, "ROUTABLE"))

            evidence = "; ".join(f"{name}: {kind}" for name, kind in verdicts)
            if all(kind == "null-driver" for _, kind in verdicts):
                # Trivially unroutable and deliberately NOT isolated: section
                # 5.1's property is "no route off the host EXCEPT the recording
                # proxy", and with no network there is no proxy either. The run
                # happened outside the topology the eval is about -- the same
                # answer the old `bool(network)` gave, now for the stated reason
                # rather than by accident.
                return False, f"{evidence} (no route anywhere, and no recording proxy)"
            return all(kind == "internal" for _, kind in verdicts), evidence
        except Exception as exc:  # noqa: BLE001 - never cost a run a record
            return None, f"isolation check failed: {type(exc).__name__}: {exc}"

    def host_sampler(self) -> HostSampler:
        return HostSampler(self._container, self._client)


# Frames between peer-container polls. `Container.stats` is one long-lived
# request for the whole run, but `containers.list()` is a fresh call each time
# -- ~300 of them on a 300 s cell, on the same APIClient and requests.Session
# the agent's exec_stream is using. docker-py guarantees nothing about
# concurrent APIClient use, so the poll is thinned rather than run per frame.
PEER_EVERY = 15


class HostSampler:
    """Container CPU and memory for the life of a run, on its own thread.

    A held `stats(stream=True)` generator rather than repeated one-shot calls:
    the stream pushes a frame about once a second for one request, while
    polling would issue a call per sample on the client the agent is streaming
    through. The one-shot read is NOT degenerate -- measured against docker SDK
    7.2.0 / daemon 29.5.2, `stream=False` returns a fully populated
    `precpu_stats`; it is the STREAM's first frame that omits
    `precpu_stats.system_cpu_usage`, which is why `_cpu_pct` guards on that key.

    Nothing here may cost the run. Measured 2026-08-12, a raise from inside the
    run body unwound past the container and recorded CRASHED with zero turns
    for a run that worked -- and a CPU reading is worth less than the checkpoint
    that taught that lesson. The thread is a daemon, catches everything, and
    reports through `HostMetrics.error`.
    """

    def __init__(self, container: Any, client: Any) -> None:
        self._container = container
        self._client = client
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames = 0
        self._cpu: list[float] = []
        self._load: list[float] = []
        self._peers: list[int] = []
        self._vm_cpus: int | None = None
        # None until a frame actually carried `memory_stats.usage`. 0 would
        # report "the container used 0 MB" from a frame that carried no memory
        # block at all -- the same rule _cpu_pct follows on its own axis.
        self._mem_bytes: int | None = None
        self._error = ""
        self._peer_error = ""

    @staticmethod
    def _cpu_pct(frame: dict[str, Any]) -> float | None:
        """Container CPU percent for one frame, or None if it is not a
        measurement.

        The `"system_cpu_usage" not in pre` guard is the load-bearing line.
        Measured: the first frame of a stream carries `precpu_stats` with
        `cpu_usage` present and `system_cpu_usage` ABSENT. The `.get(..., 0)`
        below turns that into a delta against the absolute system total --
        positive, enormous, and passing any `sys_delta <= 0` check -- so
        without the guard a fabricated ~0% reading enters the series on every
        run.

        The `.get` and the guard are a pair. Subscripting `pre` instead would
        make the guard dead code: the KeyError lands in the except below and
        returns None anyway, so nothing would fail if the guard were deleted.
        """
        try:
            cpu = frame["cpu_stats"]
            pre = frame["precpu_stats"]
            if "system_cpu_usage" not in pre:
                return None
            cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
            sys_delta = cpu["system_cpu_usage"] - pre.get("system_cpu_usage", 0)
            online = cpu.get("online_cpus") or len(
                cpu["cpu_usage"].get("percpu_usage") or []
            )
        except (KeyError, TypeError):
            return None
        if sys_delta <= 0 or not online:
            return None
        return (cpu_delta / sys_delta) * online * 100.0

    def _run(self) -> None:
        try:
            for frame in self._container.stats(stream=True, decode=True):
                if self._stop.is_set():
                    break
                self._frames += 1
                pct = self._cpu_pct(frame)
                if pct is not None:
                    self._cpu.append(pct)
                online = (frame.get("cpu_stats") or {}).get("online_cpus")
                if isinstance(online, int) and online:
                    self._vm_cpus = online
                memory = frame.get("memory_stats") or {}
                # `usage` only: cgroup v2 exposes no max_usage (measured -- the
                # keys are exactly limit/stats/usage), so this is the peak of
                # what was sampled, and HostMetrics says so. A frame with no
                # usage leaves it None rather than contributing a zero.
                usage = memory.get("usage")
                if isinstance(usage, int):
                    self._mem_bytes = max(self._mem_bytes or 0, usage)
                cpus = os.cpu_count() or 1
                self._load.append(os.getloadavg()[0] / cpus)
                if self._client is not None and self._frames % PEER_EVERY == 1:
                    try:
                        self._peers.append(len(self._client.containers.list(
                            filters={"label": "bakeoff.eval_agent=1"}
                        )))
                    except Exception as exc:  # noqa: BLE001 - not worth the run
                        # Named, not swallowed. contention_flag is None when
                        # `_peers` is empty, and this is why it was empty --
                        # otherwise the one field that says "nobody counted"
                        # cannot say who failed to count.
                        self._peer_error = f"peer poll: {type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            # Expected once stop() has been called: __exit__ kills and
            # force-removes the container, and a thread parked in the stream
            # raises when it disappears. stop() only sets a flag and joins with
            # a timeout -- it cannot interrupt a blocking read -- so on a join
            # timeout this thread is still streaming when the container is
            # removed. Without the flag every such run records an error
            # describing teardown rather than sampling.
            if not self._stop.is_set():
                self._error = f"{type(exc).__name__}: {exc}"

    def start(self) -> None:
        """Never raises. `Thread.start()` raises RuntimeError when the OS
        refuses a thread, and `execute_run`'s catch-all would turn that into
        CRASHED with zero turns for a run that would have worked -- the
        2026-08-12 failure this class's containment rule exists for,
        reintroduced in the one line the rule did not cover.
        """
        thread = threading.Thread(target=self._run, daemon=True)
        try:
            thread.start()
        except RuntimeError as exc:  # thread limit, interpreter shutting down
            self._error = f"{type(exc).__name__}: {exc}"
            return
        self._thread = thread

    def stop(self) -> None:
        """Idempotent, and it MUST NOT RAISE. Called from three places: right
        after `runner.run` returns, from the `finally` that backstops it, and
        from the record-assembly block -- and that last one sits in
        `execute_run`'s outer `finally`, which is not itself inside a `try`.
        A raise there escapes `execute_run` entirely: no record, and not even
        `record.unwritten.json`, because `_write_or_strand` is never reached.
        That breaks the one invariant everything else here is subordinate to.

        `_thread` is None when `start()` was refused, and `join()` on a thread
        that was constructed but never started raises "cannot join thread
        before it is started" -- which is exactly the state a thread-limit
        failure leaves behind, so the guard and the except are both live.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        try:
            # Bounded, and a daemon either way. A teardown that hung would cost
            # the record this sampler is only decorating.
            thread.join(timeout=3)
        except RuntimeError as exc:
            self._error = self._error or f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _p95(values: list[float]) -> float | None:
        """Nearest-rank p95, or None on an empty series -- never 0.0."""
        if not values:
            return None
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, ceil(0.95 * len(ordered)) - 1)]

    def metrics(self) -> HostMetrics:
        return HostMetrics(
            cpu_pct_p95=self._p95(self._cpu),
            mem_peak_mb=(
                int(self._mem_bytes / (1024 * 1024))
                if self._mem_bytes is not None
                else None
            ),
            # Gated on `_peers`, NOT on `_frames`. `any([])` is False, so
            # gating on frames would assert "no other eval container shared the
            # host" from zero peer observations -- which is every run with no
            # docker client and every run whose polls all failed. That is the
            # fabricated-false this whole change exists to remove, reintroduced
            # one layer down.
            contention_flag=(any(n > 1 for n in self._peers) if self._peers else None),
            load_p95=self._p95(self._load),
            vm_cpus=self._vm_cpus,
            samples=self._frames,
            error="; ".join(e for e in (self._error, self._peer_error) if e),
        )
