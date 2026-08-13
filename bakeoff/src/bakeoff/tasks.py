"""A task on disk: manifest, reference diff, and the start state they define.

Spec section 3.7 gives the task record; this module is the loader for it, and
the validation is the point of the module rather than a courtesy. `TaskSpec`
was a dataclass nothing read from disk, so the harness could only be pointed
at a task somebody constructed by hand in a script -- and the failure mode of
getting that wrong is the worst one available: `git checkout --detach` onto a
SHA that does not resolve leaves the tree wherever it was, every diff after it
is taken against a state nobody chose, and the record reads as an ordinary
quiet run. Every check here exists to turn one of those into a loud stop
before a container starts.

Three things are load-bearing and are argued where they happen:

  ONE reference diff, split at load. The merged PR is stored verbatim and
  split into a test half and a solution half by path. Two hand-maintained
  files would drift, and a drop from either would be invisible; a split is
  checkable, and it is checked.

  The start state is base_sha PLUS the test half, committed. A real bug-fix
  PR carries the test that proves the fix, so at base_sha the oracle does not
  exist yet and section 3.3's "runs tests, sees failures, self-corrects" loop
  has nothing to run -- the same truncation that invalidated every Phase 0c
  capability figure, arriving through the dataset instead of the image.

  That commit is deterministic. Fixed author, committer, date and message, so
  its SHA is a pure function of (base_sha, test half, gitignore_extra) and
  section 5.1's byte-identical world stays checkable rather than asserted.
  `start_sha` in the manifest is optional and, when present, verified: a
  re-cut patch or an edited manifest that moves the start state is exactly
  what it catches.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_NAME = "task.yaml"
REFERENCE_NAME = "reference.diff"

# The setup commit's fixed identity. Any of these varying moves the commit
# SHA without moving the tree, which would make `start_sha` unpinnable and
# turn a reproducibility check into noise.
_SETUP_ENV = {
    "GIT_AUTHOR_NAME": "bakeoff",
    "GIT_AUTHOR_EMAIL": "eval@pindrop.test",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "bakeoff",
    "GIT_COMMITTER_EMAIL": "eval@pindrop.test",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+0000",
}
_SETUP_MESSAGE = "bakeoff: task setup (test oracle)"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+) b/(?P<b>.+)$")


class TaskError(ValueError):
    """A task that cannot be loaded, materialized or trusted.

    One exception type on purpose: every caller's correct response is the
    same -- refuse to run, print the message, and change nothing.
    """


@dataclass(frozen=True)
class TaskTests:
    #: Path prefixes that make a file part of the ORACLE rather than the fix.
    #: This is what splits the reference diff, so it decides what the agent
    #: starts with and what it is graded against.
    paths: tuple[str, ...]
    #: The command, argv-style. Run inside the pinned image, never on the host.
    runner: tuple[str, ...]
    #: Fail-to-pass: red at the start state, green after the reference fix.
    f2p: tuple[str, ...]
    #: Pass-to-pass. Empty means "everything the runner collects except f2p",
    #: which is the honest default -- an explicit list would go stale against
    #: a pinned suite for no benefit.
    p2p: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskImage:
    apt: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    build: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskBudget:
    # Section 5.4: generous caps, measure actuals. These are placeholders
    # until the section 3.5 calibration pilot sets them from the
    # slowest-converging model's p95 -- tuning them to the incumbent is
    # exactly what that section forbids.
    max_turns: int = 40
    wall_clock_timeout_s: int = 900


@dataclass(frozen=True)
class TaskManifest:
    task_id: str
    task_version: int
    tier: str
    stratum: str
    repo_url: str
    base_sha: str
    prompt: str
    tests: TaskTests
    image: TaskImage
    budget: TaskBudget
    reference_diff: str
    test_diff: str
    solution_diff: str
    #: Files the test half touches. Passed to `scan_destructive` as the paths
    #: whose deletion is a HIGH-severity event, derived rather than restated:
    #: a hand-listed copy would drift from the diff that actually defines them.
    test_files: tuple[str, ...]
    root: Path
    declared_start_sha: str = ""
    gitignore_extra: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Git commit of the task-set repository, `-dirty` when the task set has
    #: uncommitted changes. This is what makes a stored record re-derivable
    #: against the task set it ran (spec section 6.1, `Versions`).
    task_set_commit: str = ""
    #: Content hash of (manifest bytes, reference diff bytes). Not provenance
    #: -- it keys the preflight cache, so an edited task re-validates and an
    #: untouched one does not pay for the check on every resume.
    manifest_digest: str = ""


# --- the reference diff ------------------------------------------------------


def diff_chunks(diff: str) -> list[tuple[str, str, str]]:
    """Split a git diff into (path_a, path_b, text) per file.

    Splits on `diff --git` headers, which is the only boundary git guarantees.
    Anything before the first header -- a commit message, a stray log line --
    is a loud error rather than silently dropped text, because a reference
    diff that is not exactly a diff is a reference nobody can reproduce.
    """
    if not diff.strip():
        return []
    lines = diff.splitlines(keepends=True)
    chunks: list[tuple[str, str, str]] = []
    current: list[str] = []
    header: tuple[str, str] | None = None
    for line in lines:
        match = _DIFF_HEADER.match(line.rstrip("\n"))
        if match:
            if header is not None:
                chunks.append((*header, "".join(current)))
            elif current and "".join(current).strip():
                raise TaskError(
                    "reference diff has content before its first `diff --git` "
                    "header; store it verbatim from `git diff`"
                )
            header = (match.group("a"), match.group("b"))
            current = [line]
            continue
        current.append(line)
    if header is None:
        raise TaskError("reference diff contains no `diff --git` header")
    chunks.append((*header, "".join(current)))
    return chunks


def split_reference_diff(
    diff: str, test_paths: tuple[str, ...]
) -> tuple[str, str, tuple[str, ...]]:
    """Partition the merged PR into its oracle half and its fix half.

    Returns (test_diff, solution_diff, test_files).

    A PARTITION, checked as one. Every chunk lands in exactly one half and
    the two halves together are the whole reference -- so nothing can be
    quietly dropped from the thing the offline grader compares against, which
    is the failure a pair of hand-maintained patch files invites.

    A rename that crosses the boundary raises. `tests/x.py -> src/x.py` is
    simultaneously oracle and fix, and guessing which half it belongs to
    would put the agent's oracle in its own submission or vice versa.
    """
    test_parts: list[str] = []
    solution_parts: list[str] = []
    test_files: list[str] = []
    chunks = diff_chunks(diff)

    for path_a, path_b, text in chunks:
        a_is_test = any(path_a.startswith(prefix) for prefix in test_paths)
        b_is_test = any(path_b.startswith(prefix) for prefix in test_paths)
        if a_is_test != b_is_test:
            raise TaskError(
                f"reference diff renames {path_a!r} to {path_b!r} across the "
                "test/solution boundary; the harness cannot tell whether that "
                "file is the agent's oracle or part of the fix"
            )
        if b_is_test:
            test_parts.append(text)
            test_files.extend({path_a, path_b})
        else:
            solution_parts.append(text)

    if len(test_parts) + len(solution_parts) != len(chunks):
        raise TaskError("reference diff split lost a file")
    if "".join(test_parts) + "".join(solution_parts) != "".join(
        text for _, _, text in chunks
    ) and sorted(test_parts + solution_parts) != sorted(
        text for _, _, text in chunks
    ):
        raise TaskError("reference diff split did not preserve its content")

    return "".join(test_parts), "".join(solution_parts), tuple(sorted(set(test_files)))


# --- loading -----------------------------------------------------------------


def _require(data: dict, key: str, kind: type, where: str) -> Any:
    if key not in data:
        raise TaskError(f"{where}: missing required key {key!r}")
    value = data[key]
    if not isinstance(value, kind):
        raise TaskError(
            f"{where}: {key!r} must be {kind.__name__}, got {type(value).__name__}"
        )
    return value


def _strs(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise TaskError(f"{where}: expected a list of strings, got {value!r}")
    return tuple(value)


def task_set_commit(root: Path) -> str:
    """Commit of the task-set repository, `-dirty` when the SET is modified.

    Scoped to the task-set path, not to the whole worktree. The task set lives
    inside the harness repo today, so a global `git status` would stamp
    `-dirty` on every record whenever any unrelated harness file was open in
    an editor -- a provenance marker that is always on says nothing.

    "" when the path is not in a git repository at all, which is the same
    honest blank the field carried before a dataset existed.

    RESOLVED first, and that is not a formality. `git status -- <pathspec>`
    interprets the pathspec relative to the cwd, so passing the relative path
    the caller used ("taskset") while running in that same directory asks
    about `taskset/taskset` -- which matches nothing, and git prints nothing
    and exits 0. Measured: a task set with every file untracked reported
    clean. The one job of the `-dirty` marker is to be visible when it should
    be, so a marker that is structurally always off is worse than no marker.
    """
    root = Path(root).resolve()
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", str(root)],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return ""
    return f"{sha}-dirty" if dirty else sha


def load_task(task_dir: Path, set_commit: str = "") -> TaskManifest:
    """Read and validate one task directory.

    Raises TaskError on anything that would make a run unattributable. The
    bar for raising is deliberately low: this runs before a container starts,
    so a false stop costs a message and a false pass costs a matrix.
    """
    import yaml

    task_dir = Path(task_dir)
    manifest_path = task_dir / MANIFEST_NAME
    reference_path = task_dir / REFERENCE_NAME
    where = str(manifest_path)

    if not manifest_path.exists():
        raise TaskError(f"{where}: no manifest")
    raw_manifest = manifest_path.read_bytes()
    data = yaml.safe_load(raw_manifest.decode("utf-8"))
    if not isinstance(data, dict):
        raise TaskError(f"{where}: manifest must be a mapping")

    task_id = _require(data, "task_id", str, where)
    if not task_id.strip():
        raise TaskError(f"{where}: task_id is empty")
    task_version = _require(data, "task_version", int, where)

    repo = _require(data, "repo", dict, where)
    repo_url = _require(repo, "url", str, f"{where}:repo")
    base_sha = _require(repo, "base_sha", str, f"{where}:repo")
    if not _SHA_RE.match(base_sha):
        # Abbreviations and branch names both resolve, and both resolve to
        # something that can move. A task set has to name one immutable
        # commit or section 5.1's pinning is decorative.
        raise TaskError(
            f"{where}: base_sha must be a full 40-character hex sha, got "
            f"{base_sha!r}"
        )
    declared_start = repo.get("start_sha") or ""
    if declared_start and not _SHA_RE.match(str(declared_start)):
        raise TaskError(f"{where}: start_sha must be a full 40-character hex sha")

    prompt = _require(data, "prompt", str, where)
    if not prompt.strip():
        raise TaskError(f"{where}: prompt is empty")

    tests_raw = _require(data, "tests", dict, where)
    test_paths = _strs(tests_raw.get("paths"), f"{where}:tests.paths")
    if not test_paths:
        raise TaskError(f"{where}: tests.paths is required; it splits the reference")
    runner = _strs(tests_raw.get("runner"), f"{where}:tests.runner")
    if not runner:
        raise TaskError(f"{where}: tests.runner is required")
    f2p = _strs(tests_raw.get("f2p"), f"{where}:tests.f2p")
    if not f2p:
        # Without a fail-to-pass set there is no way to show the task
        # discriminates, and a task that cannot be shown to discriminate
        # scores every arm on an unverifiable guess.
        raise TaskError(f"{where}: tests.f2p is required and must be non-empty")
    if len(set(f2p)) != len(f2p):
        raise TaskError(f"{where}: tests.f2p contains duplicates")
    p2p = _strs(tests_raw.get("p2p"), f"{where}:tests.p2p")
    overlap = set(p2p) & set(f2p)
    if overlap:
        # A test cannot be both the thing that must start red and the thing
        # that must never go red. Whichever check ran second would contradict
        # the first, and the task would be unsatisfiable by any submission.
        raise TaskError(
            f"{where}: {sorted(overlap)} are declared as both f2p and p2p"
        )

    image_raw = data.get("image") or {}
    if not isinstance(image_raw, dict):
        raise TaskError(f"{where}: image must be a mapping")
    budget_raw = data.get("budget") or {}
    if not isinstance(budget_raw, dict):
        raise TaskError(f"{where}: budget must be a mapping")

    if not reference_path.exists():
        raise TaskError(f"{reference_path}: no reference diff")
    raw_reference = reference_path.read_bytes()
    reference = raw_reference.decode("utf-8")
    test_diff, solution_diff, test_files = split_reference_diff(reference, test_paths)
    if not test_diff.strip():
        raise TaskError(
            f"{reference_path}: nothing under {list(test_paths)} -- the start "
            "state would carry no failing test, so the agent has nothing to "
            "run and no arm's result would be attributable"
        )
    if not solution_diff.strip():
        raise TaskError(
            f"{reference_path}: everything is under {list(test_paths)} -- there "
            "is no reference fix to validate the task against"
        )

    return TaskManifest(
        task_id=task_id,
        task_version=task_version,
        tier=str(data.get("tier", "")),
        stratum=str(data.get("stratum", "")),
        repo_url=repo_url,
        base_sha=base_sha,
        prompt=prompt,
        tests=TaskTests(paths=test_paths, runner=runner, f2p=f2p, p2p=p2p),
        image=TaskImage(
            apt=_strs(image_raw.get("apt"), f"{where}:image.apt"),
            pip=_strs(image_raw.get("pip"), f"{where}:image.pip"),
            build=_strs(image_raw.get("build"), f"{where}:image.build"),
        ),
        budget=TaskBudget(
            max_turns=int(budget_raw.get("max_turns", TaskBudget.max_turns)),
            wall_clock_timeout_s=int(
                budget_raw.get("wall_clock_timeout_s", TaskBudget.wall_clock_timeout_s)
            ),
        ),
        reference_diff=reference,
        test_diff=test_diff,
        solution_diff=solution_diff,
        test_files=test_files,
        root=task_dir,
        declared_start_sha=str(declared_start),
        gitignore_extra=_strs(data.get("gitignore_extra"), f"{where}:gitignore_extra"),
        provenance=dict(data.get("provenance") or {}),
        task_set_commit=set_commit,
        manifest_digest=hashlib.sha256(raw_manifest + raw_reference).hexdigest()[:16],
    )


def load_task_set(root: Path, only: list[str] | None = None) -> list[TaskManifest]:
    """Every task under `root`, validated, in a stable order.

    Duplicate task_ids raise. `run_id` is a hash of (task_id, model, sample,
    attempt), so two tasks sharing an id would collide in the event log and
    the second one's records would be refused as immutability violations --
    at the far end of a matrix, after the tokens were spent.
    """
    root = Path(root)
    if not root.is_dir():
        raise TaskError(f"{root}: no such task set")
    commit = task_set_commit(root)
    tasks: list[TaskManifest] = []
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (task_dir / MANIFEST_NAME).exists():
            continue
        tasks.append(load_task(task_dir, set_commit=commit))
    if only:
        wanted = set(only)
        known = {task.task_id for task in tasks}
        missing = wanted - known
        if missing:
            raise TaskError(
                f"no such task(s) in {root}: {', '.join(sorted(missing))}"
            )
        tasks = [task for task in tasks if task.task_id in wanted]
    seen: set[str] = set()
    for task in tasks:
        if task.task_id in seen:
            raise TaskError(f"duplicate task_id {task.task_id!r} in {root}")
        seen.add(task.task_id)
    if not tasks:
        raise TaskError(f"{root}: no tasks")
    return tasks


# --- materialization ---------------------------------------------------------


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    import os

    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=full_env
    )
    if check and result.returncode != 0:
        raise TaskError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def mirror_path(repo_url: str, cache_root: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", repo_url.rstrip("/"))
    return Path(cache_root) / "repos" / f"{slug}.git"


def ensure_mirror(repo_url: str, base_sha: str, cache_root: Path) -> Path:
    """A bare mirror holding `base_sha`, fetched at most once per matrix.

    The one network dependency in the whole task path, and it is deliberately
    here rather than inside a run: the container has no route off the host, so
    everything a run needs must exist before it starts.

    Resolution is CHECKED with `cat-file -e`. `git checkout --detach` onto a
    SHA the repository does not have leaves the working tree exactly where it
    was and the run proceeds against a state nobody chose -- which is the
    quiet-looking failure this whole module exists to make loud.
    """
    mirror = mirror_path(repo_url, cache_root)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not (mirror / "HEAD").exists():
        _git("clone", "--mirror", repo_url, str(mirror))

    if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=mirror,
            check=False).returncode == 0:
        return mirror

    _git("fetch", "--prune", "origin", cwd=mirror, check=False)
    if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=mirror,
            check=False).returncode == 0:
        return mirror

    # Last resort: a commit that no ref reaches (a PR head, a rewritten
    # branch). Servers may refuse; the point is to have tried before failing.
    _git("fetch", "origin", base_sha, cwd=mirror, check=False)
    if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=mirror,
            check=False).returncode != 0:
        raise TaskError(
            f"{repo_url} does not contain base_sha {base_sha}. The task cannot "
            "be materialized; running anyway would detach onto nothing and "
            "record the result as an ordinary run."
        )
    return mirror


def materialize(task: TaskManifest, dest: Path, cache_root: Path) -> str:
    """Build the run's start state on disk and return its commit SHA.

    `dest` must not exist. Every run gets its own tree: sharing one would let
    a later sample start from an earlier sample's dirty state and report a
    diff its own agent never made.

    The clone is `--local`, which hardlinks objects rather than copying them,
    so 2,400 materializations of a real repository cost neither time nor disk.
    `origin` is then removed -- it points at a host path that does not exist
    inside the container, which is both a confusing error surface for the
    agent and a host path leaked into the run.
    """
    dest = Path(dest)
    if dest.exists():
        raise TaskError(f"{dest}: already exists; every run needs its own tree")
    mirror = ensure_mirror(task.repo_url, task.base_sha, cache_root)

    dest.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--local", "--no-checkout", str(mirror), str(dest))
    _git("checkout", "--detach", task.base_sha, cwd=dest)
    _git("remote", "remove", "origin", cwd=dest, check=False)

    staged = False
    if task.test_diff.strip():
        patch = dest / ".bakeoff-test.patch"
        patch.write_text(task.test_diff)
        try:
            _git("apply", "--index", str(patch), cwd=dest)
        finally:
            patch.unlink(missing_ok=True)
        staged = True

    if task.gitignore_extra:
        gitignore = dest / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        gitignore.write_text(
            existing
            + "# added by the bakeoff task setup; see the task manifest\n"
            + "".join(f"{line}\n" for line in task.gitignore_extra)
        )
        _git("add", ".gitignore", cwd=dest)
        staged = True

    if staged:
        _git(
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", _SETUP_MESSAGE,
            cwd=dest, env=_SETUP_ENV,
        )
    start_sha = _git("rev-parse", "HEAD", cwd=dest).stdout.strip()
    _git("clean", "-xfd", cwd=dest)

    if task.declared_start_sha and task.declared_start_sha != start_sha:
        raise TaskError(
            f"{task.task_id}: start_sha is pinned to {task.declared_start_sha} "
            f"but materialization produced {start_sha}. The start state moved "
            "-- a re-cut patch, an edited manifest, or a different git. Every "
            "record written against the old SHA describes a different task."
        )
    return start_sha
