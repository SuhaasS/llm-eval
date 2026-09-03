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
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any, Callable

import docker

from bakeoff.schema import SUBMODULE_UNINITIALISED_CONTENT, HostMetrics

REPO_MOUNT = "/repo"

# How old a leaf under a `fresh_tree` parent has to be before the sweep may
# take it. A graded record's tree lives minutes and an eval cell's is capped
# by `wall_clock_timeout_s`, so a day is ~1000x the longest legitimate hold --
# and the margin is what keeps the sweep from deleting a tree out from under
# a concurrent `run_matrix` or `grade.py` running against the same cache.
STALE_TREE_AGE_S = 86_400

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


def fresh_tree(parent: Path) -> Path:
    """A host directory under `parent` that NO container has ever mounted.

    The Docker VM caches the directory it serves for a bind-mount source. A
    host path that is `rmtree`d and re-created underneath that cache is served
    from it and arrives EMPTY inside the container -- measured 2026-09-02,
    four cycles on one path gave `[files], [], [files], []`. An empty mount
    then RESOLVES: an empty `/repo` is `No test files found` at exit 1 under
    vitest and jest, the same exit a red suite gives, and `git apply` fails,
    which the grader reads as APPLY_FAILED -> `resolved: False`; an empty
    CLAUDE_CONFIG_DIR is a run that parses to zero turns, zero tokens and zero
    cost with the tokens already spent. So the failure is not flakiness, it is
    a passing measurement of nothing, an accusation against a model, or a lost
    cell. `RunContainer._assert_repo_mounted` is the post-condition for the
    `/repo` case; this is the cause, for all of them.

    THE RULE THIS FUNCTION EXISTS TO KEEP: a host path that has been
    bind-mounted once is never bind-mounted again. Callers may `rmtree` a tree
    this returned -- that is safe precisely because the allocator will not hand
    the same name out a second time -- but they may never write into a path
    they removed.

    The stable key (`run_id`, `task_id`, the cell label, the artifacts root)
    stays in the path as the PARENT, so an operator can still find a tree by
    grepping the cache layout; only the leaf is unique. The key-level directory
    survives cleanup as an empty husk -- see `_sweep_stale_trees` for what that
    costs and what is and is not collected.

    `uuid4`, not `tempfile.mkdtemp`: mkdtemp hard-codes mode 0o700, and the
    container runs as uid 1000 while the host directory is owned by the
    operator, so on a Linux host (no ownership remapping) the mount would be
    unreadable to the agent. `materialize` creates its trees with the default
    mode and this stays beside it.
    """
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_trees(parent)
    tree = parent / uuid.uuid4().hex
    # `exist_ok=False`, deliberately: a collision here is a bug and has to be
    # loud. Papering over it by reusing the directory is the defect itself.
    tree.mkdir()
    return tree


def _sweep_stale_trees(parent: Path, max_age_s: float = STALE_TREE_AGE_S) -> None:
    """Remove leaves under `parent` that no live process can still be using.

    Husks are the price of `fresh_tree`'s rule: a leaf is removed by the
    process that allocated it, so a process killed before that leaves one
    behind, and nothing deletes it later -- deleting it later, BY NAME, is the
    defect this whole mechanism removes.

    AGE-BASED, never "every sibling". `run_matrix` and `grade.py` run against
    one cache at the same time on purpose, and deleting a tree out from under
    a live container is worse than leaking an inode. The leaf's own `st_mtime`
    is effectively its ALLOCATION time -- everything a run writes goes into a
    child of it and never touches the leaf's own mtime -- so a 24 h bound is
    ~1000x the longest legitimate hold (a graded record's tree lives minutes,
    an eval cell's is capped by `wall_clock_timeout_s`) rather than a bet on
    what a long run does to its directory.

    LEAF LEVEL ONLY, so this collects a husk only under a key that is
    allocated under again -- true of `preflight-tree/<task_id>` and
    `grade-preflight-tree/<task_id>` on every invocation, and not true of a
    `grade-tree/<run_id>` or a per-cell tree. Sweeping the key level too would
    collect those, and would `rmtree` a directory a concurrent allocator has
    just created and is about to put a leaf under, whose `mkdir` then raises
    `FileNotFoundError`.

    Never raises. A sweep failure must not cost the run it is allocating for
    -- the rule `HostSampler.start` and `checkpoints.maybe_capture` keep.
    """
    cutoff = time.time() - max_age_s
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


#: The submodule-state read, word for word. Every token is load-bearing and
#: each was measured on 2026-09-02 (git 2.50.1); see `submodule_states`.
_SUBMODULE_STATUS_ARGV = [
    "git", "--no-optional-locks", "status",
    "--porcelain=v2", "--ignore-submodules=none", "-z",
]

#: The gitlink path set, same rule and same reason as
#: `preflight._gitlink_paths`: paths come from git, NUL-delimited, never from
#: a display line. It is also the short-circuit -- no gitlinks, no probe.
_GITLINK_ARGV = ["git", "ls-files", "-s", "-z"]

#: Which declared gitlink directories hold content, for the paths passed as
#: "$@". POSIX sh, no bashisms, every branch measured 2026-09-02 (M7).
#:
#: `[ -e "$p/.git" ]` is the INITIALISED test: a modern submodule has a `.git`
#: FILE there and an older one a directory, so `-e` covers both. It also
#: excludes an agent who ran `git init` in the directory -- that shape stages a
#: `160000` chunk and `grader._gitlinks_touched` already owns it.
#:
#: `find -mindepth 1 -maxdepth 1 -print -quit` rather than `ls -A`: it stops at
#: the first entry, and its output is empty-or-not rather than a list that has
#: to be parsed. Command substitution strips trailing newlines, which is why
#: the test is `-n` on ONE printed entry and never a count of lines. A missing
#: directory writes to stderr, which is discarded, and prints nothing.
#:
#: `if`/`then` blocks and a closing `exit 0`, never `[ ... ] && continue`: the
#: loop's last command decides the script's exit status, so a trailing false
#: test makes `checked_exec` raise on a healthy tree.
_UNINITIALISED_CONTENT_SH = '''
for p in "$@"; do
  if [ -e "$p/.git" ]; then
    continue
  fi
  if [ -n "$(find "$p" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    printf '%s\\0' "$p"
  fi
done
exit 0
'''


def _parse_status_v2(out: str) -> dict[str, str]:
    """Submodule sub-states out of `git status --porcelain=v2 -z`.

    Records are NUL-terminated, and the count of items per record is NOT
    uniform: a `2` (rename/copy) record occupies TWO -- the new path, then the
    original. Measured 2026-09-02 (git 2.50.1); the five shapes are in round 2
    item 17's plan under M3.

    ADVANCING BY TWO AFTER A `2` IS LOAD-BEARING, not hygiene. The second item
    is a PATH, which is repository-authored text, and a path may be named so
    that it reads as a record: a file named

        `1 .M S.M. 160000 160000 160000 aaaaaaa bbbbbbb sneakysub`

    renamed to `ordinary.py` puts exactly that string in the second slot, and a
    parser that reads it as a record files a submodule state for `sneakysub` on
    a repository that has no submodule at all. Same rule as `_GITLINK_MODE`'s:
    content must never reach column zero of a record.

    Fields are split by COUNT (`split(" ", 8)` / `split(" ", 9)`), so a path
    containing spaces survives -- verified against `a dir/f.txt` and against
    the forged name above. A record too short to index contributes nothing
    rather than raising: this is called from the agent's stdout loop, and
    `preflight._gitlink_paths`'s `partition` comment records what an
    unadvertised `IndexError` costs there.
    """
    items = out.split("\0")
    states: dict[str, str] = {}
    index = 0
    while index < len(items):
        item = items[index]
        if item.startswith("2 "):
            index += 2
            fields = item.split(" ", 9)
            path_at = 9
        elif item.startswith("1 "):
            index += 1
            fields = item.split(" ", 8)
            path_at = 8
        else:
            # `u`, `?`, `!`, a `#` header this argv never asks for, and the
            # trailing empty item after the final NUL. One item, no entry.
            index += 1
            continue
        if len(fields) > path_at and fields[2].startswith("S"):
            states[fields[path_at]] = fields[2]
    return states


def _gitlinks_from_ls_files(out: str) -> tuple[str, ...]:
    """Every 160000 entry's path, from `git ls-files -s -z`.

    `partition`, never `split("\\t", 1)[1]`: a record beginning `160000 ` with
    no TAB makes the indexed form raise `IndexError`, and this is called from
    the agent's stdout loop. A record that yields no path contributes none.
    """
    paths = []
    for record in out.split("\0"):
        if record.startswith("160000 "):
            _meta, _tab, path = record.partition("\t")
            if path:
                paths.append(path)
    return tuple(paths)


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
        self._assert_repo_mounted()
        if self.install_git:
            self.exec(["sh", "-c", "command -v git || apk add --no-cache git"])
        if self.base_sha:
            self.exec(["git", "config", "--global", "--add", "safe.directory", REPO_MOUNT])
        return self

    def _assert_repo_mounted(self) -> None:
        """Refuse a container whose bind mount did not land.

        The trap is the Docker VM's mount cache, and it is NOT the macOS
        `/var/folders` one (which the harness's `--basetemp` rule covers). A
        host path the VM DOES share, removed and re-materialized underneath it
        between containers, is served from a stale cache and comes up EMPTY:
        measured 2026-09-02, four cycles of rmtree -> materialize -> new
        container on one path gave `[files], [], [files], []`. Six production
        call sites reused a path that way until 2026-09-03; each now allocates
        through `fresh_tree`, so no container mounts a path an earlier one did.

        THE CHECK STAYS, because the cause it caught is one of several. A path
        the Docker VM does not share at all (`/var/folders`, the `--basetemp`
        convention) is untouched by that fix; a new call site that builds a
        path by hand instead of calling `fresh_tree` is not covered by it; and
        a mount can fail to land for reasons that have nothing to do with the
        path. This is the post-condition, `fresh_tree` is the cause.

        It covers `REPO_MOUNT` and only `REPO_MOUNT`. The `CLAUDE_CONFIG_DIR`
        mount is fresh by allocation and has NO mount post-condition of its
        own: a freshly allocated config directory is supposed to be empty on
        the host, so there is nothing here for a mount-time check to compare,
        and `runner.py`'s own read-back answers for the allocator rather than
        for the mount.

        It is here because an empty `/repo` RESOLVES rather than failing. Both
        node frameworks answer it with `No test files found` at exit **1**, the
        same exit a failing test gives, so a gate passes while measuring
        nothing; on the offline grader the same emptiness is `APPLY_FAILED`,
        which stamps `resolved: False` -- an accusation that the model's patch
        did not work -- over an environment difference the model never saw.
        That is `_checked_exec`'s rule one layer up: an empty answer that is
        byte-identical to a legitimate one is the failure this class refuses to
        pass along quietly.

        CONDITIONED ON THE HOST SIDE, because an empty answer is evidence only
        when there was something to see: `oracle.py` and the grader both start
        a container over a directory they are about to populate, and refusing
        those would turn a defensive check into an outage.

        THE REMOVAL IS HALF THE GUARANTEE. `__exit__` is not invoked when
        `__enter__` raises, and this container carries `bakeoff.eval_agent` --
        the label `HostSampler` counts peers by, and the one that makes a
        leaked container findable -- so a refusal that left it running would
        file `contention_flag: true` against every concurrent run for as long
        as it survived.

        The raise reaches `execute_run` inside its `try` (the `with
        RunContainer` sits there), so a run refused here still produces a
        CRASHED record naming this in `crash_error`, and never no record.

        `-print -quit` rather than a bare listing: the question is whether
        ANYTHING is there, and stopping at the first entry keeps the cost flat
        on a large tree.
        """
        try:
            host_empty = not any(os.scandir(self.repo_path))
        except OSError:
            # An unreadable host path is not this check's finding to make --
            # whatever comes next will fail on it loudly and with the right
            # message. Refusing here would rename that failure.
            return
        if host_empty:
            return
        seen = self.exec(
            ["find", REPO_MOUNT, "-mindepth", "1", "-maxdepth", "1",
             "-print", "-quit"]
        )
        if seen.stdout.strip():
            return
        try:
            self._container.remove(force=True)
        except Exception:  # noqa: BLE001 - the raise below is the real signal
            pass
        finally:
            self._container = None
        raise ContainerError(
            f"{REPO_MOUNT} is empty inside the container but {self.repo_path} "
            "is not: the bind mount did not land (the Docker VM serves a "
            "reused host path from a stale cache -- measured, every other "
            "container on one path). Nothing downstream can see this: an "
            "empty /repo is byte-identical to a red suite on both node "
            "frameworks (`No test files found`, exit 1) and to APPLY_FAILED "
            "on the grader. Use a fresh path per container."
        )

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

        THE SCRATCH INDEX IS SEEDED FROM `base_sha` FIRST, and it is not an
        optimisation. `GIT_INDEX_FILE` starts EMPTY, and `git add -A` does not
        descend into a gitlink path -- so for a submodule left uninitialised on
        purpose (`submodules_unneeded`) the staged index has no entry and the
        diff reports the gitlink as DELETED. Measured 2026-09-02, git 2.50.1:
        199 bytes on a clean fixture tree and 239 on `tobymao/sqlglot` at
        `05eed63b...`, `deleted file mode 160000`, with the agent having done
        nothing. `grader._GITLINK_MODE` matches that line, so every such
        submission would be refused as SUBMODULE_GITLINK_UNGRADABLE and every
        checkpoint would carry a phantom the section 5.5 curve cannot tell
        from a real deletion. `git read-tree <base_sha>` makes the index start
        as the tree the diff is about to be taken against, so `add -A` UPDATES
        it rather than rebuilding it from a scan that cannot see the gitlink.
        The seed and the diff read the SAME parameter, so they can never
        disagree -- and the parameter named `base_sha` holds `start_sha` at run
        time (`matrix` -> `runner` -> `checkpoints`), which is why the
        committed test half does not appear as additions: `add -A` reconciles
        every worktree-visible path anyway, so the seed survives only where git
        is blind, which is the gitlink and nothing else. Measured 0 bytes
        seeding from either sha. Unconditional, because this class has no
        manifest and must not gain one: measured, a repository with no
        submodule diffs BYTE-IDENTICALLY either way (384 bytes,
        `cmp`-identical, over a modification, an addition, a deletion and an
        ignored untracked file). It also removes a second phantom that predates
        any of this -- a file tracked at `base_sha` that also matches a
        `.gitignore` pattern is skipped by `add -A` from an empty index and was
        reported DELETED (460 bytes vs 135, measured).

        `checked_exec`, so a failed seed RAISES rather than falling through:
        the fallback for a silent failure here is the phantom deletion, which
        is the accusation this paragraph exists to prevent. Containment
        already lives one layer up in `checkpoints.maybe_capture`.

        The seed is not free: `read-tree` discards the scratch index's stat
        cache, so the `add -A` that follows re-hashes every tracked file
        rather than trusting an unchanged mtime. Measured 2026-09-02 on a
        358-file, 70 MB `sqlglot` worktree, three reps each: unseeded 0.01 s,
        seeded 0.04 s -- a 4x multiplier, ~30 ms absolute. The cost scales
        with the tracked bytes in the worktree, not with the size of the
        change (`trucking-2`'s `base_sha` tracks 2,902 site-packages files on
        top of its source tree); if that ever binds, the remedy is a narrower
        base for the read-tree, never a conditional seed.
        """
        env = {"GIT_INDEX_FILE": SNAPSHOT_INDEX}
        self.checked_exec(["git", "read-tree", base_sha], env=env)
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

    def submodule_states(self) -> dict[str, str]:
        """What the submission diff cannot carry: per-gitlink dirt.

        `snapshot_diff` normalizes over everything the agent did OUTSIDE a
        gitlink boundary and nothing inside one. Measured 2026-09-02 (git
        2.50.1) through this class's own command sequence -- scratch-index
        `git add -A`, then `git diff --cached <base>`: an uncommitted edit to a
        tracked file inside an INITIALISED submodule stages **0 bytes**, an
        untracked file inside one stages 0 bytes, and an `rm` of tracked
        content inside one stages 0 bytes. A commit inside one stages 245
        bytes (an `index <old>..<new> 160000` chunk plus `Subproject commit`),
        and a gitlink whose directory has been REMOVED stages a `deleted file
        mode 160000` chunk, 199 bytes per path. So the two states the diff
        carries are exactly the two `grader._gitlinks_touched` already refuses,
        and the states it does not carry are recorded here instead. Without
        this field a run that edited a submodule is byte-identical to one that
        did nothing, and the offline grader stamps `EMPTY_PATCH` --
        `resolved: False`, an accusation -- on it, permanently.

        EVERY TOKEN OF THE ARGV IS LOAD-BEARING.

        `--no-optional-locks`, because a plain `git status` REWRITES the index
        -- and the submodule's index too. Both were stamped to `1577865600`
        and the command re-run: after this argv both are still `1577865600`,
        after a plain `status` both read the wall clock. `SNAPSHOT_INDEX`'s own
        comment states the rule this protects -- *nothing here writes to
        .git/index at all* -- and it is not hygiene: this runs from inside the
        agent's stdout loop, concurrently with the agent's own git.

        `--ignore-submodules=none`, because three config sites silence the
        entry completely without it, measured each in isolation:
        `submodule.<name>.ignore=all` in `.git/config`, `diff.ignoreSubmodules
        =all` in `.git/config`, and `submodule.<name>.ignore=all` in
        **`.gitmodules`** -- which is repository-authored and ships in the
        upstream tree the task was cut from. Without the flag a file the task
        author never wrote disarms the record.

        `-z`, the same rule `preflight._gitlink_paths` and `tasks._numstat`
        follow: the path field is C-quoted under `core.quotePath` and
        space-delimited otherwise, so paths come from git's own delimiter and
        never from a regex over a display line.

        `git submodule status` is REJECTED, not merely unused: it reports a
        **space** marker for all three states this method exists for, because
        it compares the submodule's HEAD against the index gitlink and says
        nothing about the submodule's working tree. Its `+` fires only on the
        one state the diff already carries. It also costs ~91 ms against this
        argv's ~14 ms (100 iterations, warm), so the cheaper read is also the
        strictly more informative one.

        A SECOND READER SITS BESIDE IT, because git sees only half of this.
        With the scratch index seeded from `base_sha`, `git add -A` does not
        descend through a gitlink boundary -- so content an agent writes into
        an UNINITIALISED submodule directory is invisible to the diff, to
        `git status` and to the v2 stream alike: recorded nowhere. So each
        `160000` path from `git ls-files -s -z` whose directory carries no
        `.git` is probed with `find -mindepth 1 -maxdepth 1 -print -quit`, and
        one that prints anything is filed under
        `schema.SUBMODULE_UNINITIALISED_CONTENT`.

        The two readers are measured disjoint, and the rule is a DIRECTORY
        test rather than a `.git` test: on a path with no `.git`, v2 fires only
        when the directory is **absent** (it emits `1 .D S... ...` for a removed
        gitlink) and the probe fires only when it is **non-empty**. Absent and
        non-empty are mutually exclusive, so no path can be filed twice. The
        `.git` test is what makes the probe CHEAP -- an initialised path is
        never probed at all -- not what keeps the two apart.

        The value space of the first reader is git's own `S<c><m><u>` grammar,
        each slot its letter or `.`: eight values, of which seven are measured
        (`S...`, `S.M.`, `S..U`, `SC..`, `SCM.`, `S.MU`, `SCMU`; `SC.U` follows
        from the grammar and was not constructed). Recorded raw and
        unenumerated, for the reason `_parse_submodule_status` keeps `marker`
        beside `initialised`: the record is an observation and the verdict is
        derived from it elsewhere. Here the marker also says WHICH READER
        spoke -- git's is always four characters beginning `S`, the
        filesystem's is one character.

        `checked_exec`, never `exec`: an empty stdout from a failed
        `git status` is byte-identical to a tree with no dirty submodule, and
        reading that silence as "clean" is the fabricated measurement
        `checked_exec` exists to prevent.

        ONE METHOD, ONE CONTAINMENT, ONE RESULT. `{}` is a measurement --
        "read, nothing dirty", which includes "no submodules" and an
        uninitialised submodule whose directory is genuinely empty. This
        method never returns `None`: it either returns a complete mapping or
        raises, so a half-read can never present itself as a whole one. The
        containment is `checkpoints._capture`'s, and `None` there means "not
        read".
        """
        states = _parse_status_v2(
            self.checked_exec(list(_SUBMODULE_STATUS_ARGV)).stdout
        )
        gitlinks = _gitlinks_from_ls_files(
            self.checked_exec(list(_GITLINK_ARGV)).stdout
        )
        for path in self._uninitialised_with_content(gitlinks):
            states[path] = SUBMODULE_UNINITIALISED_CONTENT
        return states

    def _uninitialised_with_content(
        self, gitlinks: tuple[str, ...]
    ) -> tuple[str, ...]:
        """The declared gitlink directories that hold content git will not
        report. Empty tuple without an exec when the tree has no gitlink at
        all, which is every task in today's corpus but one.

        `sh -c <script> sh <paths...>` -- the paths go through `"$@"` and never
        through string interpolation, so a gitlink path containing a space, a
        quote or a `$` is passed verbatim (the space is measured). `"sh"` in
        the `$0` slot is what makes `"$@"` start at the first real path.
        """
        if not gitlinks:
            return ()
        result = self.checked_exec(
            ["sh", "-c", _UNINITIALISED_CONTENT_SH, "sh", *gitlinks]
        )
        return tuple(path for path in result.stdout.split("\0") if path)

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
        # No container is not a failure -- it is nothing to sample. Without
        # this the thread raises AttributeError on None.stats, the except
        # below files it, and every record written by a RunContainer double
        # carries a fabricated `error` -- which is the one field that is
        # supposed to mean sampling broke. `samples: 0` with an empty error
        # already says "nobody measured", and it says it truthfully.
        if self._container is None:
            return
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
