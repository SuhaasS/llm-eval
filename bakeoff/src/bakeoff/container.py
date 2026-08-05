"""Docker lifecycle with digest pinning and git state control.

Spec section 5.1: image pinned by digest, repo detached at base_sha,
network disabled, fresh container per sample.

Note on install_git: with network_mode="none" a package manager cannot
reach a mirror, so the image must already ship git. The flag survives only
to short-circuit on `command -v git`; it cannot install anything at run
time. A container without git makes snapshot_diff return empty output,
which reads as a clean tree rather than as a broken container -- so the
image, not this flag, is what guarantees git is present.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import docker

from bakeoff.schema import HostMetrics

REPO_MOUNT = "/repo"


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
    ) -> None:
        if "@sha256:" not in image:
            raise ContainerError(
                f"image must pin a digest, got {image!r} (spec section 5.1)"
            )
        self.image = image
        self.repo_path = repo_path
        self.base_sha = base_sha
        self.install_git = install_git
        self.mem_limit = mem_limit
        self._client: Any = None
        self._container: Any = None
        self._peak_mem_mb = 0

    def __enter__(self) -> RunContainer:
        self._client = docker.from_env()
        self._container = self._client.containers.run(
            self.image,
            # Override whatever the image declares -- task images carry their
            # own entrypoints, and an image with ENTRYPOINT ["git"] would run
            # `git sleep infinity` and exit immediately.
            entrypoint=["sleep"],
            command=["infinity"],
            volumes={self.repo_path: {"bind": REPO_MOUNT, "mode": "rw"}},
            working_dir=REPO_MOUNT,
            network_mode="none",  # spec section 5.1
            mem_limit=self.mem_limit,
            detach=True,
            auto_remove=False,
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

    def exec(self, cmd: list[str]) -> ExecResult:
        if self._container is None:
            raise ContainerError("container not started")
        started = time.monotonic()
        result = self._container.exec_run(cmd, demux=True, workdir=REPO_MOUNT)
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_raw, stderr_raw = result.output
        return ExecResult(
            exit_code=result.exit_code,
            stdout=(stdout_raw or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_raw or b"").decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
        )

    def snapshot_diff(self, base_sha: str) -> tuple[str, list[str]]:
        """Stage everything, then diff against base. Staging first captures
        untracked files and normalizes over whether the agent committed
        (spec section 5.6)."""
        self.exec(["git", "add", "-A"])
        diff = self.exec(["git", "diff", "--cached", base_sha])
        names = self.exec(["git", "diff", "--cached", "--name-only", base_sha])
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
