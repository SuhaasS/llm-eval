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

import time
from dataclasses import dataclass
from typing import Any, Callable

import docker

from bakeoff.schema import HostMetrics

REPO_MOUNT = "/repo"

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
        self._peak_mem_mb = 0

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

    def _checked_exec(self, cmd: list[str], env: dict[str, str] | None = None) -> ExecResult:
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
        """
        env = {"GIT_INDEX_FILE": SNAPSHOT_INDEX}
        self._checked_exec(["git", "add", "-A"], env=env)
        diff = self._checked_exec(["git", "diff", "--cached", base_sha], env=env)
        names = self._checked_exec(
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

    def stats(self) -> HostMetrics:
        if self._container is None:
            return HostMetrics()
        raw = self._container.stats(stream=False)
        mem_bytes = raw.get("memory_stats", {}).get("max_usage") or raw.get(
            "memory_stats", {}
        ).get("usage", 0)
        self._peak_mem_mb = max(self._peak_mem_mb, int(mem_bytes / (1024 * 1024)))
        return HostMetrics(mem_peak_mb=self._peak_mem_mb)
