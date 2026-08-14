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
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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
#: A chunk boundary, and deliberately NOT a path parser. Paths come from git
#: itself (see `_chunk_path`); five drafts of a hand-rolled header parser each
#: shipped a different silent-wrong-path bug, because this line is not an
#: unambiguous encoding: git appends a TAB when a path holds a space, C-quotes
#: the whole side when it holds a byte >= 0x80 or a backslash/quote/control
#: char, and a greedy `a/(.+) b/(.+)` fabricates two paths for any file whose
#: name contains " b/".
#:
#: The `a/` lookahead is load-bearing in BOTH directions. Without it,
#: `--no-prefix` output is admitted and `git apply`'s `-p1` then strips a REAL
#: path component: measured on a tree with `src/tests/conftest.py` and
#: `tests/test_main.py`, git reports `tests/conftest.py` and `test_main.py`, so
#: under `tests.paths: ["tests/"]` a source file becomes the oracle and the
#: oracle becomes part of the fix -- with every other check here passing. With
#: a stricter form that demands ` b/` too, every C-quoted header is absorbed
#: into the previous chunk instead.
_DIFF_HEADER = re.compile(r'^diff --git (?=a/|"a/)')
#: Matched to tell a genuine header we cannot accept from an ordinary body
#: line, so the refusal names the cause instead of surfacing as a chunk/entry
#: disagreement several steps later.
_ANY_DIFF_HEADER = re.compile(r"^diff --git ")
#: Combined diffs, both spellings git emits from one call site: `--cc` when the
#: output is dense (the `git show` default on a merge) and `--combined` when it
#: is not (`git show -c`). Neither is a `diff --git` header, so a MIXED diff
#: absorbs one into the previous chunk -- and measured, git's own parser
#: silently ignores it too, so the per-chunk entry count still comes back 1 and
#: does not catch it. This raise is the only thing that does.
_COMBINED_HEADER = re.compile(r"^diff --(cc|combined) ")


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
    #: Paths that belong to NEITHER half -- a changelog entry citing the issue
    #: number is the motivating case. Explicit, because deciding "not part of
    #: the fix" needs a source-tree oracle the harness does not have; an
    #: unlisted file still lands in the solution half.
    allow_extra_paths: tuple[str, ...] = ()


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
    #: Files the solution half touches, derived the same way `test_files` is.
    #: What a human scans when reviewing 80 harvested tasks -- a forgotten
    #: changelog shows up here, which is how it gets caught, since no rule
    #: available at load time can tell a changelog from a source file.
    solution_files: tuple[str, ...] = ()
    #: Files `allow_extra_paths` excluded from both halves. Recorded so the
    #: offline grader can drop them from any diff-similarity metric.
    extra_files: tuple[str, ...] = ()
    #: Content hash of (manifest bytes, reference diff bytes). Not provenance
    #: -- it keys the preflight cache, so an edited task re-validates and an
    #: untouched one does not pay for the check on every resume.
    manifest_digest: str = ""


# --- the reference diff ------------------------------------------------------


def diff_chunks(diff: str) -> list[str]:
    """Split a git diff into one text per file, in diff order.

    Texts only -- paths come from `_chunk_path`, which asks git. Splitting and
    naming are separate jobs and only one of them is safely done with a regex.

    `split("\n")`, NOT `splitlines()`. Python also breaks on \v \f \x1c \x1d
    \x1e \x85 \u2028 \u2029, and a form feed is ordinary in Emacs-era Python
    and C sources. Measured: one line of git output reading
    `+\x0cdiff --git a/evil.py b/evil.py` becomes TWO under `splitlines`, so a
    phantom chunk appears and half a real test file's diff moves into the fix
    half. Every in-file line counter is fooled the same way on both sides, so
    the disagreement is invisible; only git's own parse is immune.

    Anything before the first header -- a commit message, a stray log line --
    is a loud error rather than silently dropped text, because a reference diff
    that is not exactly a diff is a reference nobody can reproduce.
    """
    if not diff.strip():
        return []
    # Separators re-added exactly. The naive `[l + "\n" for l in split("\n")]`
    # appends a phantom byte to a diff that does not end in a newline --
    # measured +1 on the real reference -- and `test_diff`'s bytes feed the
    # setup commit, so one byte moves `start_sha` and breaks its pin.
    raw = diff.split("\n")
    lines = [piece + "\n" for piece in raw[:-1]]
    if raw[-1]:
        lines.append(raw[-1])

    chunks: list[str] = []
    current: list[str] = []
    started = False
    for line in lines:
        stripped = line.rstrip("\n")
        if _COMBINED_HEADER.match(stripped):
            raise TaskError(
                f"reference diff contains a combined-diff header ({stripped!r}). "
                "That is `git show` on a merge commit; cut the reference with the "
                "command in taskset/HARVESTING.md instead. Git's own parser "
                "ignores these silently, so nothing else here would catch it."
            )
        if _ANY_DIFF_HEADER.match(stripped) and not _DIFF_HEADER.match(stripped):
            raise TaskError(
                f"reference diff header has no `a/` prefix ({stripped!r}); it was "
                "cut with --no-prefix or a custom --src-prefix. `git apply` would "
                "then strip a real path component and the test/solution split "
                "would be silently wrong. Re-cut with `-c diff.noprefix=false`."
            )
        if _DIFF_HEADER.match(stripped):
            if started:
                chunks.append("".join(current))
            elif current and "".join(current).strip():
                raise TaskError(
                    "reference diff has content before its first `diff --git` "
                    "header; store it verbatim from `git diff`"
                )
            started = True
            current = [line]
            continue
        current.append(line)
    if not started:
        raise TaskError("reference diff contains no `diff --git` header")
    chunks.append("".join(current))

    # The one residue git's per-chunk parse cannot witness: text this function
    # dropped rather than misfiled. `raise`, never `assert` -- `python -O`
    # strips asserts, and an AssertionError escapes both this module's
    # single-exception contract and run_matrix's `except TaskError`.
    if "".join(chunks) != diff:
        raise TaskError(
            "reference diff did not survive chunking byte for byte; the usual "
            "cause is leading blank lines before the first header, which are "
            "dropped rather than refused"
        )
    return chunks


def _numstat(chunk: str, workdir: Path, reverse: bool) -> list[str]:
    """Paths git reports for one chunk, from git's own header parser.

    `-z` gives raw, unquoted, NUL-terminated paths -- no C-quoting to undo, no
    TAB terminator to strip, no ambiguity on a name containing " b/". `-R`
    swaps old and new during the parse, so the same command yields the SOURCE
    of a rename or copy. `git apply --summary` also reports sources and must
    not be used: it ignores `-z`, does not quote, brace-compresses via git's
    own `pprint_rename`, is ambiguous on ` => `, is not 1:1 with `--numstat`,
    and -- fatally -- ZERO summary lines is legitimate output for an
    all-modification diff, so a failure is indistinguishable from "no renames".

    Bytes in, bytes out. Git paths are bytes and `-z` emits them raw, including
    a literal newline inside a filename; `text=True` would raise
    UnicodeEncodeError or UnicodeDecodeError out of `load_task`.

    `cwd` must be outside any repository, and the caller owns that. Measured:
    run from a repository SUBDIRECTORY this command filters the patch to the
    cwd prefix and reports zero entries, exit 0, empty stderr -- the silent
    zero `container._checked_exec` exists to refuse.
    """
    argv = ["git", "apply"] + (["-R"] if reverse else []) + ["--numstat", "-z"]
    try:
        proc = subprocess.run(
            argv, input=chunk.encode("utf-8", "surrogateescape"),
            cwd=workdir, capture_output=True,
        )
    except (OSError, FileNotFoundError) as exc:
        # `load_task` promises one exception type; run_matrix catches only that.
        raise TaskError(f"could not run {' '.join(argv)}: {exc}") from exc
    if proc.returncode != 0:
        raise TaskError(
            "git refused a chunk of the reference diff: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return [
        field.decode("utf-8", "surrogateescape").split("\t")[-1]
        for field in proc.stdout.split(b"\0")
        if field.strip()
    ]


def _chunk_path(chunk: str, workdir: Path) -> tuple[str, str]:
    """(source, destination) for one chunk, as git names them.

    EXACTLY ONE ENTRY EACH WAY, and that is the check that binds a path to its
    chunk. A whole-diff count agreeing is not enough -- measured, this diff has
    2 chunks, 2 forward entries and 2 reverse entries, all agreeing, and the
    zip is still wrong:

        diff --git a/tests/t.py b/tests/t.py      <- chunk 0
        --- a/tests/t.py            ... its own hunk ...
        --- a/src/other.py          <- a TRADITIONAL hunk smuggled into chunk 0
        diff --git a/src/ghost.py b/src/ghost.py  <- chunk 1, header only

    Positionally chunk 0 pairs with `tests/t.py`, is classified as the oracle,
    and the `src/other.py` change rides into the start state -- the fix
    pre-applied, on every arm, silently. Per chunk the same diff yields entry
    counts [2, 0] and raises.
    """
    forward = _numstat(chunk, workdir, reverse=False)
    reverse = _numstat(chunk, workdir, reverse=True)
    if len(forward) != 1 or len(reverse) != 1:
        first = chunk.split("\n", 1)[0]
        raise TaskError(
            f"git reads {len(forward)} file(s) forward and {len(reverse)} in "
            f"reverse from one chunk of the reference diff ({first!r}); expected "
            "exactly one of each. A chunk carrying a second file's hunk, or a "
            "header with no body, splits wrongly however the halves are counted."
        )
    return reverse[0], forward[0]


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether `path` sits under any of `prefixes`.

    `PurePosixPath.is_relative_to`, never `str.startswith`. Measured, the two
    differ on exactly one class -- a first segment that merely starts with the
    prefix -- and the difference is a silent over-claim: under
    `paths: ["tests"]`, `startswith` also claims `tests_helper.py`,
    `testsuite/x.py` and `tests2/x.py`. Slash-terminated prefixes behave
    identically under both, so no manifest has to change.
    """
    candidate = PurePosixPath(path)
    return any(candidate.is_relative_to(PurePosixPath(p)) for p in prefixes)


def _validate_prefixes(prefixes: tuple[str, ...], where: str) -> None:
    for prefix in prefixes:
        if not prefix or prefix != prefix.strip():
            raise TaskError(f"{where}: {prefix!r} is empty or padded")
        if prefix.startswith("/") or ".." in PurePosixPath(prefix).parts:
            raise TaskError(f"{where}: {prefix!r} must be relative and free of '..'")


def split_reference_diff(
    diff: str, test_paths: tuple[str, ...], *, extra_paths: tuple[str, ...] = ()
) -> tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Partition the merged PR by path.

    Returns (test_diff, solution_diff, test_files, solution_files, extra_files).

    THREE CLASSES, not two. `extra_paths` names files that belong to neither
    half -- a changelog entry citing the issue number is the motivating case:
    no agent writes one, and leaving it in `solution_diff` means preflight
    applies documentation it does not need and any later diff-similarity metric
    measures changelog authorship. Solution stays the `else`: deciding "not
    part of the fix" needs a source-tree oracle the harness does not have, so
    an UNLISTED extra file still lands in the fix half exactly as before.

    THERE IS NO PARTITION SELF-CHECK, deliberately. Four were written and all
    four were circular -- a length check, an `assignment[i] is None` over an
    exhaustive branch, and two recomposition checks -- because any property
    computed from the assignment restates the assignment. The external witness
    is `_chunk_path`: git parses each chunk independently and must find exactly
    one file in it.

    A rename that crosses ANY class boundary raises. `tests/x.py -> src/x.py`
    is simultaneously oracle and fix, and guessing a half would either hand the
    agent its own submission or delete the test it is measured by. Checked over
    the full class of both endpoints, not `a_is_test != b_is_test`, so a
    listed-extra file renamed into the fix half is caught too.
    """
    _validate_prefixes(test_paths, "tests.paths")
    _validate_prefixes(extra_paths, "allow_extra_paths")
    # Containment BOTH ways, not set intersection: `tests/` and
    # `tests/fixtures/CHANGES.rst` share no element, and with test evaluated
    # first the extra entry is a silent no-op -- a manifest key that looks like
    # it excludes a file and does nothing.
    for extra in extra_paths:
        for test in test_paths:
            if _under(extra, (test,)) or _under(test, (extra,)):
                raise TaskError(
                    f"allow_extra_paths {extra!r} overlaps tests.paths {test!r}; "
                    "tests.paths is applied first, so the entry would silently "
                    "do nothing"
                )

    chunks = diff_chunks(diff)
    test_parts: list[str] = []
    solution_parts: list[str] = []
    test_files: list[str] = []
    solution_files: list[str] = []
    extra_files: list[str] = []

    def classify(path: str) -> str:
        if _under(path, test_paths):
            return "test"
        if _under(path, extra_paths):
            return "extra"
        return "solution"

    # One temp directory for the whole diff, and it must be outside any git
    # repository -- see `_numstat`. TemporaryDirectory honours TMPDIR, so a
    # TMPDIR inside a worktree re-arms the silent zero; the per-chunk entry
    # check is what converts that into a raise.
    with tempfile.TemporaryDirectory(prefix="bakeoff-diff-") as tmp:
        workdir = Path(tmp)
        for text in chunks:
            source, destination = _chunk_path(text, workdir)
            kind_a, kind_b = classify(source), classify(destination)
            if kind_a != kind_b:
                raise TaskError(
                    f"reference diff renames {source!r} ({kind_a}) to "
                    f"{destination!r} ({kind_b}), across the test/solution "
                    "boundary; the harness cannot tell whether that file is the "
                    "agent's oracle or part of the fix"
                )
            if kind_b == "test":
                test_parts.append(text)
                test_files.extend({source, destination})
            elif kind_b == "extra":
                extra_files.extend({source, destination})
            else:
                solution_parts.append(text)
                solution_files.extend({source, destination})

    return (
        "".join(test_parts),
        "".join(solution_parts),
        tuple(sorted(set(test_files))),
        tuple(sorted(set(solution_files))),
        tuple(sorted(set(extra_files))),
    )


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
    extra_paths = _strs(
        tests_raw.get("allow_extra_paths"), f"{where}:tests.allow_extra_paths"
    )
    test_diff, solution_diff, test_files, solution_files, extra_files = (
        split_reference_diff(reference, test_paths, extra_paths=extra_paths)
    )
    if not test_diff.strip():
        raise TaskError(
            f"{reference_path}: nothing under {list(test_paths)} -- the start "
            "state would carry no failing test, so the agent has nothing to "
            "run and no arm's result would be attributable"
        )
    if not solution_diff.strip():
        # Reachable two ways since allow_extra_paths exists, so the message
        # names both keys rather than sending the author to the wrong one.
        raise TaskError(
            f"{reference_path}: nothing is left for the fix -- every file is "
            f"under tests.paths {list(test_paths)} or "
            f"allow_extra_paths {list(extra_paths)}"
        )

    return TaskManifest(
        task_id=task_id,
        task_version=task_version,
        tier=str(data.get("tier", "")),
        stratum=str(data.get("stratum", "")),
        repo_url=repo_url,
        base_sha=base_sha,
        prompt=prompt,
        tests=TaskTests(
            paths=test_paths, runner=runner, f2p=f2p, p2p=p2p,
            allow_extra_paths=extra_paths,
        ),
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
        solution_files=solution_files,
        extra_files=extra_files,
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
         check: bool = True,
         input: str | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=full_env,
        input=input,
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


# Bumping this invalidates every cached pruned mirror. Bump it whenever the
# prune changes what it removes, or an older revision's output is served
# forever -- which is the silent-wrong-cache failure `ensure_mirror` re-checks
# on every call to avoid.
_PRUNE_VERSION = "1"
_PRUNE_MARKER = "bakeoff-prune-version"

# What `git clone --mirror --local` HARDLINKS from the source and `gc` does not
# reliably rewrite. Measured: the commit-graph carries future commit OIDs
# verbatim, and under `gc.writeCommitGraph=false` gc exits 0 and leaves the
# inherited copy -- the object sweep below still passes, and `git fsck` in the
# run tree then prints the reference fix's SHA at the agent.
#
# A tuple because emptying it is the mutation anchor for that guarantee: there
# is no `if` here to neutralise.
_DERIVED_PATHS = (
    "objects/info/commit-graph",
    "objects/info/commit-graphs",
    "objects/pack/multi-pack-index",
)

# The operator's ~/.gitconfig decides whether a prune prunes, so none of this
# is left to it. TWO of these are load-bearing, measured leave-one-out against
# a hostile global config on a packed repository:
#   gc.bigPackThreshold below the pack size keeps the pack wholesale, and the
#     fix survives with every ref gone;
#   gc.writeCommitGraph -- gc writes a commit-graph by DEFAULT, so without the
#     override it re-creates, after _strip_derived, the very file that carries
#     future commit ids into the run tree.
# The other five are shadowed by an explicit argument elsewhere and are kept to
# state intent, not because removing one was measured to break anything:
# gc.pruneExpire and gc.cruftPacks lose to `--prune=now` on the same command
# line, gc.reflogExpire* lose to the explicit `reflog expire --expire=now
# --all` below, and repack.packKeptObjects is shadowed by _strip_derived's
# .keep unlink -- with a pack-<hash>.keep present at gc time the fix survives
# EVEN WITH the override set, so the unlink is what saves that case. A .keep
# whose name matches no existing pack is ignored by git entirely (measured,
# git 2.50.1); an earlier revision of this comment claimed otherwise.
# The reflog written by the update-ref calls below is a reachability root and
# gc's default expiry keeps entries for 90d/30d, which is what the reflog
# flags are for -- but the explicit expire below already covers it.
# `gc.writeMultiPackIndex` is deliberately absent -- it is not a real config
# name (checked against `git help -c`, git 2.50.1); the MIDX is handled by
# _DERIVED_PATHS instead.
_GC_CONFIG = (
    "-c", "gc.bigPackThreshold=0",
    "-c", "gc.cruftPacks=false",
    "-c", "gc.pruneExpire=now",
    "-c", "gc.reflogExpire=now",
    "-c", "gc.reflogExpireUnreachable=now",
    "-c", "gc.writeCommitGraph=false",
    "-c", "repack.packKeptObjects=true",
)


def pruned_mirror_path(repo_url: str, base_sha: str, cache_root: Path) -> Path:
    """Keyed on base_sha as well as the URL.

    Two tasks on one repository at different base_shas need different pruned
    mirrors -- each is pruned to its own history.
    """
    return mirror_path(repo_url, cache_root).with_name(
        f"{mirror_path(repo_url, cache_root).name[:-4]}-{base_sha}.git"
    )


def _strip_derived(repo: Path) -> None:
    for rel in _DERIVED_PATHS:
        target = repo / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    for keep in (repo / "objects" / "pack").glob("*.keep"):
        keep.unlink(missing_ok=True)


def _pack_fingerprint(repo: Path) -> str:
    """A digest of the object store that a run tree cannot invalidate.

    `*.idx` names and sizes, never `*.pack` and never mtimes. `git clone
    --local` hardlinks the pack into every run tree, and git FRESHENS (utimes)
    a pack when objects are written -- so `container.py`'s checkpoint
    `git add -A` moves the mtime of the shared pack in every real cell.
    Measured: after one run tree staged a file, only `pack-*.pack` changed;
    `.idx`, `.rev` and `.bitmap` did not. A pack-and-mtime fingerprint would
    therefore mismatch forever after the first cell and never re-converge,
    turning the cheap path into the expensive one it exists to bound.

    An `.idx` name is content-addressed and its size changes when the store is
    repacked, which is the only change that matters here.

    LOOSE OBJECTS ARE COUNTED BESIDE THE PACKS, and an earlier revision's
    docstring claiming nothing writes to a cached mirror was wrong. Measured
    against git 2.50.1: `git fetch <upstream> +refs/*:refs/future/*` in a cached
    pruned mirror lands 5 objects LOOSE -- under `transfer.unpackLimit` no pack
    is written -- so no `.idx` moves, the digest is byte-identical, the fast
    path returns, and `git cat-file -p <the merged fix>` works in the next run
    tree. That is the whole leak this module exists to close, reintroduced one
    layer up and silently.

    The safety property is not "the count is zero" but that the count cannot
    return to its recorded value without also moving the `.idx` set: packing
    those objects rewrites an index. It does not reintroduce the freshening
    problem above -- a run tree has its own `.git/objects` and no
    `objects/info/alternates` (`clone --local` hardlinks, so there is no write
    channel back), and the mirror's digest is unchanged across a run tree's
    `git add -A`, `commit`, `gc --prune=now` and `repack -adq`.
    """
    packs = sorted(
        (p.name, p.stat().st_size) for p in (repo / "objects" / "pack").glob("*.idx")
    )
    # Two hex characters exactly, so `objects/info` and `objects/pack` -- four
    # characters each -- are not matched.
    loose = sum(1 for _ in (repo / "objects").glob("[0-9a-f][0-9a-f]/*"))
    return hashlib.sha256(repr((packs, loose)).encode("utf-8")).hexdigest()[:16]


def _commits_outside(repo: Path, ancestors: set[str]) -> list[str]:
    """Commit objects present in `repo` that `ancestors` does not contain.

    THE post-condition, and it is stated over objects rather than refs because
    the leak is an object-level one: `clone --local` hardlinks the whole store,
    so the merged fix is readable through `cat-file -p`, `--batch-all-objects`
    and `fsck --lost-found` with no ref pointing at it. A ref-level check is
    satisfied by a prune that leaves the oracle sitting there.

    `--batch-all-objects` lists cruft-pack objects too, which is what makes
    this catch the `gc.cruftPacks` and `gc.bigPackThreshold` bypasses rather
    than merely surviving them. `--unordered` because the sort is pure cost.
    """
    listing = _git(
        "cat-file", "--batch-all-objects", "--unordered",
        "--batch-check=%(objecttype) %(objectname)", cwd=repo,
    ).stdout
    return [
        name
        for kind, _, name in (line.partition(" ") for line in listing.splitlines())
        if kind == "commit" and name not in ancestors
    ]


def _ancestors(repo: Path, base_sha: str) -> set[str]:
    return set(_git("rev-list", base_sha, cwd=repo).stdout.split())


def _verify_pruned(repo: Path, base_sha: str) -> None:
    """Refuse a pruned mirror that cannot be shown to be pruned.

    Not inferred from three exit codes. `ensure_mirror` re-checks `base_sha`
    with `cat-file -e` on every call for the same reason: in this module the
    expensive failures are the quiet ones, and a prune that silently did
    nothing produces a start state indistinguishable from a correct one.
    """
    if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=repo,
            check=False).returncode != 0:
        raise TaskError(
            f"{repo}: pruned mirror does not contain base_sha {base_sha}. "
            "Without this the object check below passes vacuously on an empty "
            "or truncated mirror."
        )
    leftover = [rel for rel in _DERIVED_PATHS if (repo / rel).exists()]
    leftover += [p.name for p in (repo / "objects" / "pack").glob("*.keep")]
    if leftover:
        raise TaskError(
            f"{repo}: inherited {', '.join(leftover)} survived the prune. A "
            "commit-graph carries future commit ids verbatim and a .keep makes "
            "gc refuse the pack, so the run tree would still hand the agent the "
            "reference fix."
        )
    outside = _commits_outside(repo, _ancestors(repo, base_sha))
    if outside:
        raise TaskError(
            f"{repo}: {len(outside)} commit(s) outside {base_sha}'s history "
            f"survived the prune, e.g. {', '.join(sorted(outside)[:3])}. The "
            "run tree would contain the merged fix the task was cut from."
        )


def ensure_pruned_mirror(repo_url: str, base_sha: str, cache_root: Path) -> Path:
    """A bare mirror holding `base_sha`'s history and NOTHING after it.

    Run trees clone from this rather than from the full mirror, because
    `git clone --local` hardlinks the entire object store: measured on
    `pallets/click`, the run tree carried all 30,766 objects of the mirror,
    including the merge commit of the very PR the task was cut from, reachable
    as `refs/heads/main` 181 commits ahead of the start state. `git log --all`
    or `git show main` hands the model the answer it is being scored on, and it
    does so unevenly across arms -- a section 6.4 confound recorded as
    capability. `git remote remove origin` did not cover it: that deletes
    `refs/remotes/origin/*`, not the local branch a clone creates, and not tags.

    ONCE PER (repo, base_sha), cached across invocations, because the prune
    ends in a `gc` that repacks the whole store. Doing it per run tree would
    cost that on every one of ~3,200 cells and break the hardlink sharing
    `materialize` depends on.

    `ensure_mirror` FIRST: an unresolvable `base_sha` has to fail there, with
    the message that names it, rather than deeper in here.
    """
    source = ensure_mirror(repo_url, base_sha, cache_root)
    dest = pruned_mirror_path(repo_url, base_sha, cache_root)

    marker = dest / _PRUNE_MARKER
    if marker.exists():
        try:
            version, marked_sha, fingerprint = marker.read_text().split()
        except ValueError:
            version = marked_sha = fingerprint = ""
        # ONE condition, and everything else falls through to the rebuild.
        # An earlier revision re-verified the mirror on a fingerprint mismatch
        # and RAISED when that failed -- before the rebuild block, so the bad
        # entry stayed on disk and the task was unmaterializable on every future
        # invocation until an operator rm -rf'd it. Measured: three identical
        # TaskErrors in a row. That is the same failure class the stale-tmp
        # sweep below refuses, handled the opposite way.
        #
        # Nothing is lost by rebuilding instead. The re-verify limb existed to
        # skip a rebuild when a mismatch is benign, but the only benign trigger
        # is an operator repack -- and a repack leaves the digest identical
        # (measured), so it does not reach here at all. Every mismatch that does
        # fire is one that should be rebuilt. The rebuild ends in
        # `_verify_pruned(tmp, ...)`, which still raises: refusing a prune this
        # function just built is a code defect, refusing one it found on disk is
        # a cache defect, and only the second is recoverable.
        if (version == _PRUNE_VERSION and marked_sha == base_sha
                and fingerprint == _pack_fingerprint(dest)
                and _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=dest,
                         check=False).returncode == 0):
            return dest

    # Rebuild. Sweep first: a tmp left behind by a killed process would make
    # `git clone --mirror --local` exit 128 ("destination path already exists
    # and is not an empty directory") for this task forever.
    for stale in dest.parent.glob("prune-*.tmp"):
        shutil.rmtree(stale, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix="prune-", suffix=".tmp"))
    try:
        # mkdtemp created it; clone refuses a non-empty target and tolerates an
        # empty one only if it does not exist.
        shutil.rmtree(tmp)
        _git("clone", "--mirror", "--local", str(source), str(tmp))
        # `--mirror` leaves remote.origin.fetch=+refs/*:refs/* and
        # remote.origin.mirror=true behind. One `git fetch` in this directory
        # would restore the entire future and nothing downstream would notice.
        _git("remote", "remove", "origin", cwd=tmp, check=False)
        _strip_derived(tmp)

        # The default branch, but only where pointing it at `base_sha` is TRUE.
        # `base_sha` is often not on it (a release branch, a merge parent), and
        # a `main` retargeted anyway would show the agent history that never
        # was. A harness-invented branch name is a tell. Detached is honest.
        #
        # check=False on both: `symbolic-ref` exits 128 on a detached bare
        # repo, and it returns 0 with a branch name even when that ref does not
        # exist, in which case `merge-base` exits 128 rather than 1.
        head_ref = _git("symbolic-ref", "HEAD", cwd=tmp, check=False)
        branch = head_ref.stdout.strip() if head_ref.returncode == 0 else ""
        on_branch = branch and _git(
            "merge-base", "--is-ancestor", base_sha, branch, cwd=tmp, check=False
        ).returncode == 0
        if on_branch:
            _git("update-ref", branch, base_sha, cwd=tmp)
            _git("symbolic-ref", "HEAD", branch, cwd=tmp)
        else:
            _git("update-ref", "--no-deref", "HEAD", base_sha, cwd=tmp)

        ancestors = _ancestors(tmp, base_sha)
        refs = _git(
            "for-each-ref",
            "--format=%(refname) %(objectname) %(*objectname)", cwd=tmp,
        ).stdout
        # `%(*objectname)` is the peeled target and is empty for a lightweight
        # tag, so the last field is the commit for both tag kinds. Ref names
        # cannot contain spaces (git check-ref-format rule 4).
        deletions = [
            f"delete {line.split()[0]}\n"
            for line in refs.splitlines()
            if line.split()[-1] not in ancestors
        ]
        if deletions:
            _git("update-ref", "--stdin", cwd=tmp, input="".join(deletions))
        _git("reflog", "expire", "--expire=now", "--all", cwd=tmp, check=False)
        _git(*_GC_CONFIG, "gc", "--prune=now", "--quiet", cwd=tmp)

        _verify_pruned(tmp, base_sha)
        (tmp / _PRUNE_MARKER).write_text(
            f"{_PRUNE_VERSION} {base_sha} {_pack_fingerprint(tmp)}\n"
        )
        # rmtree first: os.replace onto a non-empty directory raises
        # ENOTEMPTY, and the marker-mismatch path arrives here with the old
        # mirror still in place -- which is the very case the marker exists for.
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def materialize(task: TaskManifest, dest: Path, cache_root: Path) -> str:
    """Build the run's start state on disk and return its commit SHA.

    `dest` must not exist. Every run gets its own tree: sharing one would let
    a later sample start from an earlier sample's dirty state and report a
    diff its own agent never made.

    The clone is `--local`, which hardlinks objects rather than copying them,
    so 2,400 materializations of a real repository cost neither time nor disk
    -- and it is cloned from the PRUNED mirror, which is what keeps that true
    while still removing the future. Pruning here instead would repack the
    whole store per run and break the hardlink.

    `origin` is then removed -- it points at a host path that does not exist
    inside the container, which is both a confusing error surface for the
    agent and a host path leaked into the run. The reflog is expired for the
    same reason: the clone records `clone: from <host cache path>` in
    `.git/logs/HEAD`, which now carries the cache layout and `base_sha`.
    """
    dest = Path(dest)
    if dest.exists():
        raise TaskError(f"{dest}: already exists; every run needs its own tree")
    mirror = ensure_pruned_mirror(task.repo_url, task.base_sha, cache_root)

    dest.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--local", "--no-checkout", str(mirror), str(dest))
    _git("checkout", "--detach", task.base_sha, cwd=dest)
    _git("remote", "remove", "origin", cwd=dest, check=False)
    _git("reflog", "expire", "--expire=now", "--all", cwd=dest, check=False)

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
