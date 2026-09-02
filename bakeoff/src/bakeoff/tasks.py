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
  its SHA is a pure function of (base_sha, strip_paths, test half,
  gitignore_extra) and section 5.1's byte-identical world stays checkable
  rather than asserted.
  `start_sha` in the manifest is optional and, when present, verified: a
  re-cut patch or an edited manifest that moves the start state is exactly
  what it catches.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

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
class TaskGrading:
    """What the offline grader runs beside the suite, argv-style and DECLARED.

    Declared rather than detected, because "the repo's own mypy config if
    present" asks the loader to divine tool presence and config precedence,
    and that detection fails silently in BOTH directions: an absent tool
    reads as a clean typecheck, and a config the repo does not actually use
    reads as a dirty one. Either way the guess is stamped into a per-record
    verdict that outlives the run and that nobody re-derives.

    Every key is optional and most tasks declare none of them. An empty tuple
    is recorded by the grader as `not_configured`, never as a pass -- "this
    task has no linter" and "the linter found nothing" are different claims,
    and collapsing them is the same defect one layer down.

    argv, like `tests.runner`, so nothing is split on whitespace by the
    grader or handed to a shell inside the image. Whether a declared command
    exists is not asked here and cannot be: this loader runs on the host,
    where the answer describes the operator's laptop. Preflight asks it in
    the pinned image, so a typo is a NO-GO before the proxy starts rather
    than a permanent verdict against every submission.
    """

    build: tuple[str, ...] = ()
    typecheck: tuple[str, ...] = ()
    lint: tuple[str, ...] = ()


#: The keys `grading:` accepts, derived from the dataclass so the two cannot
#: drift. A hand-listed copy would go stale the first time a check is added,
#: and the failure of a stale list is the silent one: the new key is refused
#: on a manifest that is correct.
_GRADING_KEYS = tuple(f.name for f in dataclass_fields(TaskGrading))


@dataclass(frozen=True)
class TaskImage:
    apt: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    build: tuple[str, ...] = ()
    #: Environment baked into the task image as Dockerfile ENV lines, so it
    #: reaches EVERY process in the container -- preflight's runner, the
    #: oracle's, the grader's, the agent's `claude` -- and the commands the
    #: agent invents. That last one is the whole reason it is here and not in
    #: `tests.runner`: the agent is never told the runner argv (it gets
    #: `task.prompt` and nothing else), so a flag in the runner would leave
    #: the gate measuring one suite and section 3.3's self-correction loop
    #: running another. Same shape, and the same argument, as the base image's
    #: PYTHONDONTWRITEBYTECODE.
    #:
    #: Keys are restricted to `_IMAGE_ENV_ALLOWED`. See its comment: an
    #: unrestricted map re-opens the CLAUDE_CODE_USE_BEDROCK hole that
    #: `claude_runner`'s env allowlist exists to close.
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskBudget:
    # Section 5.4: generous caps, measure actuals. These are placeholders
    # until the section 3.5 calibration pilot sets them from the
    # slowest-converging model's p95 -- tuning them to the incumbent is
    # exactly what that section forbids.
    max_turns: int = 40
    wall_clock_timeout_s: int = 900
    #: The coreutils `timeout` bound on every command preflight, the oracle
    #: and the offline grader run INSIDE the container -- each suite
    #: invocation and each declared `grading.*` argv alike. "Suite" is the
    #: short name; the breadth is the point, because the gate and the grader
    #: run the SAME commands and a second key for the grading argvs would be
    #: a second number that can diverge between them.
    #:
    #: It does NOT bound the agent: the agent's own suite runs happen inside
    #: `wall_clock_timeout_s` with no per-command bound (`claude_runner`
    #: wraps the whole `claude -p`). That independence is why the two keys
    #: are separate, and the `>` refusal in `load_task` is the one place
    #: they are compared.
    #:
    #: 600 is the constant this replaced, in three copies: `preflight(
    #: timeout_s=600)`, `ensure_oracle(timeout_s=600)` and
    #: `grader.GRADE_TIMEOUT_S`. A manifest that does not declare the key
    #: therefore gates and grades byte-identically to before.
    suite_timeout_s: int = 600


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
    #: Paths removed from the start state in the same setup commit that
    #: applies the test half. What lifts a repository's agent-file or
    #: vendored-tree date floor without hand-rewriting `base_sha`: the
    #: modification is in the manifest, so it is in `manifest_digest` and in
    #: `start_sha`, rather than in a rewritten history a reader diffing
    #: against upstream would not find there.
    strip_paths: tuple[str, ...] = ()
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
    #: The offline grader's build/typecheck/lint commands, empty by default.
    #: Defaulted so a manifest written before this section existed keeps
    #: loading, and so the absent case is one value rather than a `None` every
    #: caller has to re-decide.
    grading: TaskGrading = field(default_factory=TaskGrading)


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


#: Pathspec magic `git rm` would honour and this key does not accept. See
#: `_validate_strip_paths`.
_PATHSPEC_MAGIC = ("*", "?", "[", "]")

#: Where `RunContainer` binds the run tree. Spelled here rather than imported
#: from `container.REPO_MOUNT`, because container.py imports `docker` at
#: module level and this loader deliberately runs without a daemon.
#: `test_the_repo_mount_constant_matches_the_container_it_describes` is where
#: the drift is caught.
_REPO_MOUNT = "/repo"

#: The only keys `image.env` may set. An ALLOWLIST, not a denylist, for
#: exactly the reason `claude_runner`'s module docstring gives: a denylist has
#: to anticipate every contaminant, and the one that matters most --
#: CLAUDE_CODE_USE_BEDROCK / _USE_VERTEX -- makes the CLI ignore
#: ANTHROPIC_BASE_URL, bypass the proxy, and leave the mandatory wire log
#: empty with the run still looking normal. Those two are kept out of the HOST
#: environment by PASSTHROUGH_ENV and are not set by `container_env`, so an
#: image ENV is precisely the route that is otherwise open.
#:
#: Every entry is argued once, here:
#:
#:   CI  -- Hypothesis registers a built-in `ci` profile at import time
#:          (derandomize=True, database=None, deadline=None) and auto-loads it
#:          whenever any of twelve CI variables is present; `"CI": None` in
#:          its `_CI_VARS` means presence alone, any value. Measured
#:          2026-09-01 against hypothesis 6.167.1: a property-based suite that
#:          gives `0 0 0 0 1 1 1 1 0 0` over ten fresh runs gives `1 1 1 1 1 1`
#:          under CI=1. It is also INERT where it does not apply -- in an
#:          image without hypothesis, `CI=1 pytest` is exit 0 and writes
#:          nothing, where any `--hypothesis-*` flag is exit 4 before
#:          collection.
#:
#:   HYPOTHESIS_STORAGE_DIRECTORY -- Hypothesis's storage root is
#:          `Path.cwd() / ".hypothesis"` fixed at import time
#:          (configuration.py:20), so with workdir=/repo it lands in the tree
#:          the section 5.6 submission diff is taken against. CI=1 stops the
#:          `examples/` database but not the `constants/` cache, and an agent
#:          running pytest from a subdirectory gets a second copy in a
#:          directory whose self-written .gitignore guard never fired. This is
#:          the only lever that keeps all of it out.
_IMAGE_ENV_ALLOWED = frozenset({"CI", "HYPOTHESIS_STORAGE_DIRECTORY"})

#: Characters that do not survive a generated `ENV KEY="value"` line. `$` is
#: the one that is not about syntax: Docker EXPANDS it against the build
#: environment, which would make the value a property of the builder rather
#: than of the manifest -- the argument that refuses pathspec magic in
#: strip_paths, one key over.
_ENV_VALUE_REFUSED = ('\n', '\r', '"', '\\', '$')

_ENV_KEY = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _env_map(value: Any, where: str) -> dict[str, str]:
    """`image.env`, validated. Empty when absent.

    Four refusals, each naming a failure that is silent without it:

    * a key outside `_IMAGE_ENV_ALLOWED` -- see that constant;
    * a key the harness itself sets -- Docker's exec env wins over the image's
      (measured), so the value would apply to preflight and the grader and NOT
      to the agent, which is two environments for one task;
    * a value carrying `_ENV_VALUE_REFUSED`;
    * HYPOTHESIS_STORAGE_DIRECTORY inside /repo, which undoes the only thing
      that key is for.
    """
    from bakeoff.claude_runner import pinned_env_keys

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TaskError(f"{where}: expected a mapping, got {value!r}")

    pinned = pinned_env_keys()
    env: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if not _ENV_KEY.match(key):
            raise TaskError(
                f"{where}: {key!r} is not a usable environment variable name"
            )
        if key in pinned:
            raise TaskError(
                f"{where}: {key!r} is a key the harness sets itself. Docker "
                "merges an exec's environment into the image's with the "
                "exec's keys winning, so this would apply to preflight and "
                "the grader and be overridden on the agent's own process -- "
                "two environments for one task, with nothing recording which"
            )
        if key not in _IMAGE_ENV_ALLOWED:
            raise TaskError(
                f"{where}: {key!r} is not an allowed image.env key. The "
                "allowed set is "
                f"{sorted(_IMAGE_ENV_ALLOWED)}; it is an allowlist because a "
                "denylist would have to anticipate CLAUDE_CODE_USE_BEDROCK, "
                "which bypasses the proxy and leaves the wire log empty with "
                "the run still looking normal"
            )
        if not isinstance(raw_value, str) or not raw_value:
            raise TaskError(
                f"{where}: {key!r} must be a non-empty string, got "
                f"{raw_value!r}"
            )
        bad = [ch for ch in _ENV_VALUE_REFUSED if ch in raw_value]
        if bad:
            raise TaskError(
                f"{where}: {key!r} carries {bad!r}, which would not survive "
                'a generated `ENV KEY="value"` line ("$" is expanded by the '
                "builder, so the value would be a property of the build "
                "rather than of the manifest)"
            )
        if key == "HYPOTHESIS_STORAGE_DIRECTORY":
            path = PurePosixPath(raw_value)
            # `is_relative_to` covers the equal case too -- measured,
            # `PurePosixPath("/repo").is_relative_to("/repo")` is True -- so
            # `/repo` itself and anything under it are one check, and the
            # `is_absolute` clause is what catches a relative entry before
            # `is_relative_to` is asked a question about a path with no root.
            if not path.is_absolute() or path.is_relative_to(_REPO_MOUNT):
                raise TaskError(
                    f"{where}: {raw_value!r} must be an absolute path outside "
                    f"{_REPO_MOUNT}. This key exists to keep hypothesis's "
                    "writes out of the tree the section 5.6 submission diff "
                    "is taken against; pointed back inside it, it undoes "
                    "exactly that"
                )
        env[key] = raw_value
    return env


def _validate_strip_paths(paths: tuple[str, ...], where: str) -> None:
    """Refuse a strip entry that would remove something nobody asked for.

    `_validate_prefixes` covers empty, padded, absolute and `..`-bearing
    entries. The three refusals here are specific to a key whose effect is a
    DELETE rather than a classification.

    `.` and `./` pass every one of those checks and name the whole tree:
    measured, `PurePosixPath(".").parts` is `()` and
    `PurePosixPath("a/b").is_relative_to(PurePosixPath("."))` is True. The
    start state would be emptied and `start_sha` would still be a pure
    function of the manifest -- perfectly reproducible and completely wrong.

    `.git` is the same shape one level worse: the delete runs in the run tree,
    so it would destroy the repository the section 5.6 submission diff is
    taken against.

    Pathspec magic is refused because `git rm` takes PATHSPECS, not paths.
    `strip_paths: ["*.log"]` would glob, and `_strip_paths_from_tree`'s
    existence check would then pass on one accidental match while the author
    meant something else -- a manifest key whose meaning is a property of the
    tree it is applied to, inside the one field that has to be a pure function
    of the manifest.
    """
    _validate_prefixes(paths, where)
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts:
            raise TaskError(
                f"{where}: {path!r} names the whole tree; strip_paths removes "
                "what it names, so this would empty the start state"
            )
        if parts[0] == ".git":
            raise TaskError(
                f"{where}: {path!r} is inside the repository's own .git; the "
                "strip runs in the run tree and would destroy the repository "
                "the submission diff is taken against"
            )
        if path.startswith(":") or any(ch in path for ch in _PATHSPEC_MAGIC):
            raise TaskError(
                f"{where}: {path!r} carries pathspec magic; this key names "
                "paths, and a pattern would make what is removed a property "
                "of the tree rather than of the manifest"
            )


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


def _refuse_stripped_halves(
    test_files: tuple[str, ...],
    solution_files: tuple[str, ...],
    strip_paths: tuple[str, ...],
    where: str,
) -> None:
    """A path the strip removes and the reference diff changes is refused.

    STRIP DOES NOT IMPLY EXCLUSION, deliberately. Silently dropping the chunk
    would make `solution_diff` something other than the merged PR that section
    3.2 requires verbatim, and preflight would only notice when the missing
    hunk happened to be one the f2p tests need -- so the case that survives
    every gate is a reference that is no longer a reference. From the TEST
    half it is worse: the oracle shrinks and every arm is graded against less
    than the manifest says.

    `allow_extra_paths` is the declared way to say "neither half", it is what
    taskset/HARVESTING.md already tells an author to reach for here, and it
    leaves the file named in `extra_files` -- so the combination stays visible
    instead of being inferred from two keys that never mention each other.

    Called from `load_task` rather than from `split_reference_diff`: that
    function is a three-class partition of a diff by path and takes no
    manifest, and a fourth prefix argument would invite exactly the reading
    this refusal rejects. Renames need no special case, because
    `split_reference_diff` puts both endpoints into the file lists.
    """
    for half, files in (("test", test_files), ("solution", solution_files)):
        caught = sorted(path for path in files if _under(path, strip_paths))
        if caught:
            raise TaskError(
                f"{where}: strip_paths removes {', '.join(caught)}, which the "
                f"reference diff's {half} half also changes -- the patch would "
                "be applied onto a path that no longer exists. List those "
                "paths in tests.allow_extra_paths to keep them out of both "
                "halves, or narrow the strip."
            )


# --- loading -----------------------------------------------------------------


#: "the key is not in the manifest", which is not the same as `null`. A `None`
#: default would collapse the two, and `key: null` is a key the author WROTE
#: -- reading it as "never written" is the wrong repair.
_ABSENT = object()


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


def _positive_int(value: Any, where: str, default: int) -> int:
    """A budget number, or a `TaskError` that names the key.

    Three things a bare `int(...)` gets wrong, and the third is the reason
    this exists at all:

    * `int("forty")` raises a bare `ValueError` out of `load_task`, with no
      manifest path in the traceback -- which is how `max_turns` and
      `wall_clock_timeout_s` behave today.
    * `int("600")` and `int(600.0)` SUCCEED, so a quoted or floated value is
      accepted silently and the manifest stops being a faithful record.
    * `bool` IS an `int` in Python: `int(True)` is 1. `suite_timeout_s: true`
      would kill every gated and graded command after one second, which reads
      as a task whose tests hang -- a NO-GO at the gate and `timed_out`
      stamped on every arm past it -- for a YAML typo. So the bool test comes
      FIRST; folded into the int test it is dead code a mutation cannot catch.

    `_ABSENT` rather than `None` for "not declared", so an explicit `key: null`
    falls through to the refusal. Applied to `suite_timeout_s` only:
    `max_turns` and `wall_clock_timeout_s` keep their bare `int(...)` because
    tightening them could refuse a manifest that loads today, which belongs in
    its own change (`TASKS.md`).
    """
    if value is _ABSENT:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskError(f"{where}: must be a positive integer, got {value!r}")
    return value


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
    # Optional: absent is `not_configured`, not an error. Whether the declared
    # commands RUN is preflight's question, asked inside the image rather than
    # on the host. `manifest_digest` needs no change: it hashes the raw
    # manifest bytes, so declaring or editing this section already invalidates
    # the task's preflight cache entry.
    #
    # `is None`, NOT `or {}`: `grading: []` is what an author who started a
    # list and never wrote the keys leaves behind, and `or {}` reads it as a
    # section they never wrote -- three checks recorded `not_configured`
    # against a manifest that plainly asks for them.
    grading_raw = data.get("grading")
    if grading_raw is None:
        grading_raw = {}
    if not isinstance(grading_raw, dict):
        raise TaskError(f"{where}: grading must be a mapping")
    # A misspelled key is the one shape preflight cannot catch: its assertion
    # covers the DECLARED argvs, and `linter:` declares nothing, so the task
    # grades `not_configured` on every arm and every sample while the manifest
    # says otherwise. `str(k)` because a YAML key need not be a string and
    # sorting a mixed set raises TypeError from inside the error path.
    unknown = sorted(str(k) for k in set(grading_raw) - set(_GRADING_KEYS))
    if unknown:
        raise TaskError(
            f"{where}: unknown grading key(s) {unknown}; allowed: "
            f"{list(_GRADING_KEYS)}"
        )

    if not reference_path.exists():
        raise TaskError(f"{reference_path}: no reference diff")
    raw_reference = reference_path.read_bytes()
    reference = raw_reference.decode("utf-8")
    extra_paths = _strs(
        tests_raw.get("allow_extra_paths"), f"{where}:tests.allow_extra_paths"
    )
    # Validated before the split so a malformed entry is reported without
    # first paying for git's per-chunk parse of the whole reference.
    # `manifest_digest` needs no term for this key: it hashes the raw manifest
    # bytes, so declaring or editing a strip already invalidates the task's
    # preflight cache entry.
    strip_paths = _strs(data.get("strip_paths"), f"{where}:strip_paths")
    _validate_strip_paths(strip_paths, f"{where}:strip_paths")
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
    _refuse_stripped_halves(test_files, solution_files, strip_paths, where)

    budget = TaskBudget(
        max_turns=int(budget_raw.get("max_turns", TaskBudget.max_turns)),
        wall_clock_timeout_s=int(
            budget_raw.get("wall_clock_timeout_s", TaskBudget.wall_clock_timeout_s)
        ),
        suite_timeout_s=_positive_int(
            budget_raw.get("suite_timeout_s", _ABSENT),
            f"{where}:budget.suite_timeout_s",
            TaskBudget.suite_timeout_s,
        ),
    )
    # The agent re-runs this suite INSIDE its wall clock and nothing bounds it
    # per command there, so a suite the author says may need longer than the
    # agent's whole run is a task no arm can verify even once: section 3.3
    # measures a loop that ends in "runs tests, sees failures, self-corrects",
    # and this is that loop truncated by a SIGTERM mid-suite, with a diff
    # nobody checked. Settled by arithmetic over two manifest numbers, so it
    # is refused HERE rather than in preflight -- an image build is a high
    # price for a contradiction visible in the YAML. Strictly `>`: equality is
    # degenerate but is a judgement about slack, not something arithmetic
    # settles.
    if budget.suite_timeout_s > budget.wall_clock_timeout_s:
        raise TaskError(
            f"{where}: budget.suite_timeout_s ({budget.suite_timeout_s}) "
            "exceeds budget.wall_clock_timeout_s "
            f"({budget.wall_clock_timeout_s}) by "
            f"{budget.suite_timeout_s - budget.wall_clock_timeout_s}s. The "
            "agent re-runs this suite inside its wall clock, so it could not "
            "verify its own work even once -- the run would be terminated "
            "mid-suite with an unchecked diff, and that is indistinguishable "
            "from a model that simply ran out of time. Raise "
            "wall_clock_timeout_s, or lower suite_timeout_s to what the suite "
            "actually needs."
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
            env=_env_map(image_raw.get("env"), f"{where}:image.env"),
        ),
        grading=TaskGrading(
            build=_strs(grading_raw.get("build"), f"{where}:grading.build"),
            typecheck=_strs(
                grading_raw.get("typecheck"), f"{where}:grading.typecheck"
            ),
            lint=_strs(grading_raw.get("lint"), f"{where}:grading.lint"),
        ),
        budget=budget,
        reference_diff=reference,
        test_diff=test_diff,
        solution_diff=solution_diff,
        test_files=test_files,
        solution_files=solution_files,
        extra_files=extra_files,
        root=task_dir,
        declared_start_sha=str(declared_start),
        gitignore_extra=_strs(data.get("gitignore_extra"), f"{where}:gitignore_extra"),
        strip_paths=strip_paths,
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
        # utf-8 with replacement, never the locale: text=True made one
        # non-UTF-8 byte in git output -- or LC_ALL=C in the harness's own
        # environment -- raise UnicodeDecodeError out of any git call,
        # naming neither the repo nor the task. A replaced ref name is NOT
        # deleted by the ref sweep (update-ref --stdin exits 0 on a
        # nonexistent name, measured); it survives to _verify_pruned's
        # object sweep, which refuses the mirror -- loud, not leaked.
        ["git", *args], cwd=cwd, capture_output=True,
        encoding="utf-8", errors="replace", env=full_env, input=input,
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


@contextlib.contextmanager
def _repo_lock(repo_url: str, cache_root: Path):
    """One exclusive flock per repo slug, held across every cache build and
    every read that a concurrent build could tear.

    Three critical sections take it: `ensure_mirror` (git clone writes HEAD
    early, so a second process's HEAD-exists check passes on a half-populated
    clone and `fetch --prune`s into it), the prune build-and-publish (two
    processes pruning one (repo, base_sha) -- the second's publish renames
    the first's live mirror aside), and `materialize`'s run-tree clone (the
    publish rmtree's the directory that clone is reading). Neither race was
    ever observed as corruption; both were derived from the code -- at
    f67ccc2, `grep -rE 'flock|fcntl|O_EXCL'` over src/ found zero hits, and
    this function is the first.

    Per SLUG, not per (repo, base_sha): both keys share one source mirror
    that `ensure_mirror` may fetch into, and one dest.parent sweep namespace,
    so a per-key lock would let a fetch run during another key's prune.

    The acquisitions MUST NOT nest: flock self-conflicts across two fds in
    one process (measured: LOCK_EX|LOCK_NB on a fresh fd raises
    BlockingIOError while the first is held), so `ensure_pruned_mirror`
    calls `ensure_mirror` -- which locks and releases -- BEFORE taking the
    lock itself.

    The lock file is separate from the mirror because the publish
    `os.replace`s the mirror away -- a lock on a renamed path guards nothing
    -- and it is never unlinked: removing a lock file another process holds
    open silently breaks the exclusion for every later acquirer.
    """
    lock_path = mirror_path(repo_url, cache_root).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


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
    with _repo_lock(repo_url, cache_root):
        if not (mirror / "HEAD").exists():
            # Deliberately NOT `--dissociate` here, though a borrowing
            # `repo_url` does propagate its alternates into this mirror. It is
            # a no-op for a remote url (a clone with no `--reference` writes no
            # alternates file), so it would be dead in production; the case it
            # would cover is a local borrowing path, and
            # `ensure_pruned_mirror`'s clone already dissociates -- measured,
            # dropping the flag there fails a test while dropping it here fails
            # nothing. An unanchored flag in this module is how the claims it
            # cannot check accumulate.
            _git("clone", "--mirror", repo_url, str(mirror))

        if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=mirror,
                check=False).returncode == 0:
            return mirror

        _git("fetch", "--prune", "origin", cwd=mirror, check=False)
        if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=mirror,
                check=False).returncode == 0:
            return mirror

        # Last resort: a commit that no ref reaches (a PR head, a rewritten
        # branch). Servers may refuse; the point is to have tried before
        # failing.
        _git("fetch", "origin", base_sha, cwd=mirror, check=False)
        if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=mirror,
                check=False).returncode != 0:
            raise TaskError(
                f"{repo_url} does not contain base_sha {base_sha}. The task "
                "cannot be materialized; running anyway would detach onto "
                "nothing and record the result as an ordinary run."
            )
        return mirror


# Bumping this invalidates every cached pruned mirror. Bump it whenever the
# prune changes what it removes, or an older revision's output is served
# forever -- which is the silent-wrong-cache failure `ensure_mirror` re-checks
# on every call to avoid.
#
# 3: the 0700 publish. Mode is not something the fast path checks, so without
# the bump every mirror built before the rmtree fix stays at the umask
# forever -- found live: the first post-fix collection served a cached 755
# mirror while every new build came out 0700.
_PRUNE_VERSION = "3"
_PRUNE_MARKER = "bakeoff-prune-version"

# What `git clone --mirror --local` HARDLINKS from the source and `gc` does not
# reliably rewrite, and what is therefore safe to UNLINK -- every entry here is
# derived and regenerable. (`objects/info/alternates` is neither hardlinked nor
# safe to unlink; it is rewritten and absolutized by clone's `copy_alternates`,
# and it lives in `_FORBIDDEN_PATHS` below.)
# Measured: the commit-graph carries future commit OIDs
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

# What a cached mirror must NOT contain, which is a SUPERSET of what is stripped.
# `objects/info/alternates` is here and deliberately NOT in `_DERIVED_PATHS`,
# because the two sets answer different questions and unlinking this one is
# destructive: measured on a true borrower (`git clone --shared --bare`, which
# has ZERO local objects), deleting the file and running the gc below exits 128
# with `fatal: bad object refs/heads/main / failed to run repack` and leaves
# `base_sha` unresolvable -- a harder wedge than the leak. `--dissociate` on the
# clone is what removes it safely, by absorbing the borrowed objects first.
#
# It has to be checked at all because borrowed objects are reachable, survive
# `gc` (they are not in this repository to prune), and are invisible to
# `_pack_fingerprint`, which counts only local packs and loose objects.
# Measured: writing this file into a cached pruned mirror leaves the digest
# byte-identical, the fast path serves it, and `git cat-file -p <the merged
# fix>` then works in the run tree.
#
# Dead on the build path once `--dissociate` is in place, live on the fast path,
# where the mirror is whatever an earlier invocation or an operator left behind.
_FORBIDDEN_PATHS = _DERIVED_PATHS + ("objects/info/alternates",)

# How long an abandoned build directory must sit before the sweep reclaims it.
# Generous on purpose: the sweep only reclaims disk, and an age floor that fires
# early would delete a slow prune out from under the process still running it.
_STALE_TMP_AGE_S = 6 * 60 * 60

# How many outside commits the object sweep names before it stops reading.
# It reads ONE PAST this: the message can then distinguish "exactly N" from
# "more than N", instead of rendering a ref-sweep miss (a handful) and a gc
# that did nothing (tens of thousands) identically.
_OUTSIDE_SAMPLE = 3

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
    against git 2.50.1: `git fetch <upstream> +refs/heads/*:refs/future/*` in a cached
    pruned mirror lands 5 objects LOOSE -- under `transfer.unpackLimit` no pack
    is written -- so no `.idx` moves, the digest is byte-identical, the fast
    path returns, and `git cat-file -p <the merged fix>` works in the next run
    tree. That is the whole leak this module exists to close, reintroduced one
    layer up and silently.

    The safety property is not "the count is zero" but that the count cannot
    return to its recorded value without also moving the `.idx` set: packing
    those objects rewrites an index. It does not reintroduce the freshening
    problem above -- the mirror's digest is unchanged across a run tree's
    `git add -A`, `commit`, `gc --prune=now` and `repack -adq`, because a run
    tree writes into its own `.git/objects`.

    WHAT THIS CANNOT SEE, and the reason it is not the fast path's only check.
    An earlier revision of this docstring said a run tree has "no
    `objects/info/alternates` (`clone --local` hardlinks, so there is no write
    channel back)". The parenthetical is false: `clone` calls
    `copy_alternates`, which REWRITES the file with absolutized entries rather
    than hardlinking it. That sentence is why a borrowed object store went
    unconsidered for two revisions. Borrowed objects are not local packs and
    not loose objects, so they move nothing here; neither does a commit-graph,
    a multi-pack-index, or a `pack-<hash>.keep`. Measured: each leaves the
    digest byte-identical. This function detects changes to the LOCAL PACK SET
    and nothing else, which is why `ensure_pruned_mirror` checks
    `_FORBIDDEN_PATHS` and `*.keep` beside it rather than trusting it alone.
    """
    packs = sorted(
        # `pack-*.idx`, not `*.idx`: Path.glob matches dotfiles (measured:
        # `glob("*.idx")` returns an in-flight `.tmp-1-pack-abc.idx`;
        # `glob("pack-*.idx")` does not). Safe direction -- a spurious full
        # re-prune, never staleness -- but a fingerprint that can flap under
        # a concurrent repack is noise this module can avoid.
        (p.name, p.stat().st_size)
        for p in (repo / "objects" / "pack").glob("pack-*.idx")
    )
    # Two hex characters exactly, so `objects/info` and `objects/pack` -- four
    # characters each -- are not matched.
    loose = sum(1 for _ in (repo / "objects").glob("[0-9a-f][0-9a-f]/*"))
    return hashlib.sha256(repr((packs, loose)).encode("utf-8")).hexdigest()[:16]


def _commits_outside(repo: Path, ancestors: set[str]) -> list[str]:
    """Commit objects present in `repo` that `ancestors` does not contain --
    at most `_OUTSIDE_SAMPLE + 1` of them, because the error message this
    feeds prints `_OUTSIDE_SAMPLE` names plus whether there were more, and
    the caller branches on emptiness alone.

    THE post-condition, and it is stated over objects rather than refs because
    the leak is an object-level one: `clone --local` hardlinks the whole store,
    so the merged fix is readable through `cat-file -p`, `--batch-all-objects`
    and `fsck --lost-found` with no ref pointing at it. A ref-level check is
    satisfied by a prune that leaves the oracle sitting there.

    `--batch-all-objects` lists cruft-pack objects too, which is what makes
    this catch the `gc.cruftPacks` and `gc.bigPackThreshold` bypasses rather
    than merely surviving them. `--unordered` because the sort is pure cost.

    STREAMED, not buffered: the listing is one line per object, 659 KB on
    pruned click and ~229 MB extrapolated to a 5M-object monorepo, and
    `.splitlines()` plus the comprehension held three copies of it at once.
    stderr is not drained while stdout streams, so a git that filled the
    stderr pipe (64 KB -- measured to the byte on darwin 25.5.0, and the
    linux default) mid-listing would deadlock -- accepted rather than paid
    for with a drain thread, and recorded here. That git stays under it is
    inference from the command's shape (`--batch-check` has nothing to
    narrate), not a measurement.
    """
    with subprocess.Popen(
        ["git", "cat-file", "--batch-all-objects", "--unordered",
         "--batch-check=%(objecttype) %(objectname)"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        outside: list[str] = []
        try:
            for line in proc.stdout:
                kind, _, name = line.rstrip("\n").partition(" ")
                if kind == "commit" and name not in ancestors:
                    outside.append(name)
                    if len(outside) > _OUTSIDE_SAMPLE:
                        # Enough evidence; stop paying for the rest of the
                        # listing. The kill makes the exit code meaningless,
                        # which is fine -- the hits are already a harder
                        # failure than any exit code.
                        proc.kill()
                        break
            else:
                stderr_text = proc.stderr.read()
                if proc.wait() != 0:
                    raise TaskError(
                        f"git cat-file --batch-all-objects failed "
                        f"(exit {proc.returncode}) in {repo}: "
                        f"{stderr_text.strip()}"
                    )
        finally:
            proc.kill()
    return outside


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
            "Checked first for the message: with this guard gone, _ancestors "
            "raises out of rev-list's own check=True before the object sweep "
            "runs, naming neither the mirror nor that this is a cache defect."
        )
    leftover = [rel for rel in _FORBIDDEN_PATHS if (repo / rel).exists()]
    leftover += [p.name for p in (repo / "objects" / "pack").glob("*.keep")]
    if leftover:
        raise TaskError(
            f"{repo}: inherited {', '.join(leftover)} survived the prune. A "
            "commit-graph carries future commit ids verbatim, a "
            "pack-<hash>.keep matching an existing pack makes gc refuse that "
            "pack, and an alternates file borrows an object store gc cannot "
            "prune at all -- so the run tree would still hand the agent the "
            "reference fix. An alternates hit means the clone needs "
            "--dissociate; do not unlink it, which leaves base_sha unresolvable."
        )
    outside = _commits_outside(repo, _ancestors(repo, base_sha))
    if outside:
        count = (str(len(outside)) if len(outside) <= _OUTSIDE_SAMPLE
                 else f"more than {_OUTSIDE_SAMPLE}")
        raise TaskError(
            f"{repo}: {count} commit(s) outside {base_sha}'s history "
            f"survived the prune, e.g. "
            f"{', '.join(sorted(outside)[:_OUTSIDE_SAMPLE])}. The run tree "
            "would contain the merged fix the task was cut from."
        )


def _build_pruned_mirror(source: Path, dest: Path, base_sha: str) -> Path:
    """The body of `ensure_pruned_mirror`, split out as a SEAM, not a
    decomposition: the lock had to wrap this whole region, and wrapping it
    in-place would re-indent every line -- six of which are exact-substring
    mutation anchors in `mutation_check.py`, indentation included. Inlining
    this back "for tidiness" stales all six. Runs entirely under the caller's
    `_repo_lock`; see `ensure_pruned_mirror` for why the fast path is inside
    it too."""
    marker = dest / _PRUNE_MARKER
    if marker.exists():
        # The WHOLE predicate goes inside the try, not just the unpack. An
        # earlier revision caught only `ValueError` around `.split()`, which
        # left `_pack_fingerprint` and the `cat-file` fork outside it: measured,
        # a marker that is a DIRECTORY raises `IsADirectoryError` -- an
        # `OSError`, not a `ValueError` -- before the rebuild block below is
        # reached, so the task was unmaterializable forever. (Invalid UTF-8 was
        # already survivable, because `UnicodeDecodeError` subclasses
        # `ValueError`.) Any unreadable cache state must mean "rebuild", never
        # "raise": the rebuild is always available and a cache defect must not
        # be terminal.
        #
        # `OSError` and not `Exception`: swallowing a `NameError` from a future
        # edit would turn a code defect into a silent rebuild-every-time loop,
        # which is the plausible-looking zero this module refuses.
        try:
            version, marked_sha, fingerprint = marker.read_text().split()
            # Ordered by cost. The structural checks are `exists()` calls and
            # one directory glob; `_pack_fingerprint` scans `objects/pack` AND
            # every `objects/xx/`; `cat-file` forks. They are also the three
            # `_verify_pruned` assertions the fingerprint cannot make -- it
            # detects changes to the local pack set, and a commit-graph, a
            # `.keep` and an alternates file all leave it byte-identical
            # (measured). A cached mirror is trusted only when it still
            # satisfies what the rebuild proved about it.
            usable = (
                version == _PRUNE_VERSION
                and marked_sha == base_sha
                and not any((dest / rel).exists() for rel in _FORBIDDEN_PATHS)
                and not any((dest / "objects" / "pack").glob("*.keep"))
                and fingerprint == _pack_fingerprint(dest)
                and _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=dest,
                         check=False).returncode == 0
            )
        except (ValueError, OSError):
            usable = False
        # Anything short of usable falls through to the rebuild. An earlier
        # revision re-verified the mirror on a fingerprint mismatch and RAISED
        # when that failed -- before the rebuild block, so the bad entry stayed
        # on disk and the task was unmaterializable on every future invocation
        # until an operator rm -rf'd it. Measured: three identical TaskErrors in
        # a row.
        #
        # Nothing is lost by rebuilding. The re-verify limb existed to skip a
        # rebuild when a mismatch is benign, but the only benign trigger is an
        # IDEMPOTENT repack of an already-fully-packed store with no loose
        # objects, which leaves the digest identical and so never reaches here.
        # (With loose objects present -- the state a fetch produces -- gc packs
        # them and the digest moves, correctly.) The rebuild ends in
        # `_verify_pruned(tmp, ...)`, which still raises: refusing a prune this
        # function just built is a code defect, refusing one it found on disk is
        # a cache defect, and only the second is recoverable.
        if usable:
            return dest

    # Rebuild. Sweep is DISK RECLAIM, not wedge prevention -- an earlier comment
    # here claimed a leftover tmp made the task "unmaterializable forever", and
    # that was wrong: `tmp` comes from `mkdtemp`, so its name is unique on every
    # call (measured: 200 calls, 200 distinct names, never colliding with a
    # planted leftover) and `git clone` can never fail into one. What a leftover
    # does cost is a whole pruned mirror of disk, per abandoned build.
    #
    # Two patterns on purpose. The new name carries `dest.name`, which already
    # contains both the repo slug and `base_sha`, so a sweep is scoped to THIS
    # task instead of deleting every in-flight prune in a cache directory shared
    # by every repo. The legacy `prune-*.tmp` pattern is swept as well because
    # the new glob matches none of the names the deployed code left behind
    # (measured: 0 of 201) and each of those is a full mirror stranded forever.
    #
    # Age floor, not pid liveness: this only reclaims disk, the cache may be on
    # a shared filesystem where a pid means nothing, and after the scoping above
    # the sole remaining hazard is two processes on the SAME key -- which needs
    # the lock this module still does not have, tracked as its own change.
    dest.parent.mkdir(parents=True, exist_ok=True)
    scoped = f"prune-{dest.name}-"
    cutoff = time.time() - _STALE_TMP_AGE_S
    for pattern in (f"{scoped}*", "prune-*.tmp"):
        for stale in dest.parent.glob(pattern):
            try:
                if stale.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(stale, ignore_errors=True)
            if stale.exists():
                stale.unlink(missing_ok=True)
    tmp = Path(tempfile.mkdtemp(
        dir=dest.parent, prefix=f"{scoped}{os.getpid()}-", suffix=".tmp"
    ))
    try:
        # The clone goes INTO mkdtemp's directory, keeping its 0700: an
        # earlier revision rmtree'd it first on the claim that clone
        # "tolerates an empty target only if it does not exist" -- measured
        # false (git 2.50.1 clones into an existing empty directory, rc=0,
        # mode kept) -- so the clone recreated it under the umask and
        # os.replace published a world-readable cache, permanently.
        #
        # `--dissociate` absorbs any borrowed object store instead of inheriting
        # the pointer to it. Measured, git 2.50.1: accepted without
        # `--reference`; 0.02 s and the pack still hardlinked (nlink 2) when
        # there is nothing to absorb; a full `repack -a -d` when there is, which
        # also breaks the hardlink to that source. Without it a borrowing
        # upstream raises here on every invocation forever, because every
        # rebuild re-inherits from the same place.
        _git("clone", "--mirror", "--local", "--dissociate", str(source), str(tmp))
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
        # Rename the old mirror ASIDE, never delete in place. `os.replace` onto
        # a non-empty directory raises ENOTEMPTY, so something has to clear
        # `dest` first -- but `shutil.rmtree(dest, ignore_errors=True)` was the
        # wrong something twice over. It silently removes NOTHING when `dest` is
        # a file (measured), after which `os.replace` raises NotADirectoryError
        # on this and every later invocation; and `ignore_errors=True` discards
        # a partial failure, leaving half a repository that ENOTEMPTYs forever.
        # A rename is atomic, fails loudly, and cannot half-succeed.
        #
        # `doomed` is named into the sweep's own namespace so an interrupted
        # publish is reclaimed rather than stranded -- it is not a `mkdtemp`
        # name, so it carries its own randomness.
        try:
            if dest.exists() or dest.is_symlink():
                doomed = dest.parent / f"{scoped}{os.getpid()}-{uuid4().hex}.tmp"
                os.replace(dest, doomed)
                shutil.rmtree(doomed, ignore_errors=True)
                # rmtree does nothing to a file; `dest` being one is exactly
                # the case that used to wedge.
                if doomed.exists():
                    doomed.unlink(missing_ok=True)
            os.replace(tmp, dest)
        except OSError as exc:
            raise TaskError(
                f"{dest}: could not publish the pruned mirror ({exc}). The "
                "build was verified and is discarded on the way out; whatever "
                f"this failure left under {dest.parent} is swept or rebuilt "
                "on the next invocation."
            ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


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
    # `ensure_mirror` locks and releases inside itself; taking the lock only
    # after it returns is what keeps the two acquisitions from nesting, which
    # flock punishes with a same-process deadlock (see _repo_lock). The whole
    # fast path sits inside the lock because the publish has a window where
    # `dest` does not exist, and an unlocked reader there starts a redundant
    # rebuild. The body lives in _build_pruned_mirror so this wrapper could
    # be added without re-indenting the six mutation anchors inside it.
    source = ensure_mirror(repo_url, base_sha, cache_root)
    dest = pruned_mirror_path(repo_url, base_sha, cache_root)
    with _repo_lock(repo_url, cache_root):
        return _build_pruned_mirror(source, dest, base_sha)


def _strip_paths_from_tree(
    repo: Path, strip_paths: tuple[str, ...], task_id: str
) -> bool:
    """Remove the declared paths from index AND worktree. True if anything went.

    `git rm -r`, not `git rm -r --cached`: the point of the key is that the
    agent does not read a stripped CLAUDE.md and `pip install -e .` does not
    resolve against a stripped vendored tree, and both need the file gone from
    disk rather than only from the index. The deletions are staged, so the
    caller's existing setup commit carries them and `start_sha` moves.

    A path that matches nothing is a TaskError, and `--ignore-unmatch` is
    deliberately absent. A typo (`.cluade/`) strips nothing and leaves the
    file in the start state -- and nothing downstream says so, because
    preflight's `_CONTEXT_FILES` check knows four names, so a mistyped
    vendored tree or a fifth agent-file spelling passes every gate and the
    confound is permanent in an append-only log. That is the failure this key
    exists to prevent, so it cannot also be the failure the key introduces.

    The existence check reads `git ls-files`'s OUTPUT, not its exit code:
    measured 2026-09-01, `git ls-files -z -- nope` exits 0 with an empty
    stdout, which is the silent zero `container._checked_exec` exists to
    refuse. It asks about TRACKED content, because only tracked content is in
    the tree `start_sha` names -- which is also why a strip cannot name a file
    the reference diff CREATES, even one `allow_extra_paths` legitimately
    lists. (`git rm` also refuses an unmatched pathspec, exit 128; this check
    is kept because it names the manifest key and reports every missing entry
    at once instead of the first.)
    """
    if not strip_paths:
        return False
    missing = [
        path for path in strip_paths
        if not _git("ls-files", "-z", "--", path, cwd=repo).stdout
    ]
    if missing:
        raise TaskError(
            f"{task_id}: strip_paths names {', '.join(missing)}, which no "
            "tracked file is at or under in the start state. A typo strips "
            "nothing and silently leaves behind the file the task was cut to "
            "remove."
        )
    _git("rm", "-r", "-q", "--", *strip_paths, cwd=repo)
    return True


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

    `strip_paths` is applied first and lands in the same setup commit, so the
    commit's SHA stays a pure function of (base_sha, strip_paths, test half,
    gitignore_extra) and `start_sha` remains pinnable. A strip that did not
    move `start_sha` would be a change to what every arm was asked to do that
    no stored record could distinguish.
    """
    dest = Path(dest)
    if dest.exists():
        raise TaskError(f"{dest}: already exists; every run needs its own tree")
    mirror = ensure_pruned_mirror(task.repo_url, task.base_sha, cache_root)

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Under the repo lock: a concurrent invocation's publish renames the
    # mirror aside and rmtree's it, and a clone reading it at that moment
    # survives only until the rmtree wins. The clone is the last reader of
    # shared state; everything after it touches only this run's tree.
    with _repo_lock(task.repo_url, cache_root):
        _git("clone", "--local", "--no-checkout", str(mirror), str(dest))
    _git("checkout", "--detach", task.base_sha, cwd=dest)
    _git("remote", "remove", "origin", cwd=dest, check=False)
    _git("reflog", "expire", "--expire=now", "--all", cwd=dest, check=False)

    # Every other check in the object-leak path is a check on the CACHE. This
    # one is on the artifact the agent actually receives, and it is the only
    # one that is. `git clone --local` does not hardlink an alternates file --
    # it calls `copy_alternates`, which rewrites each entry as an ABSOLUTE path
    # into the clone, so the run tree would both reach objects outside
    # `base_sha`'s history and carry a host cache path that does not resolve
    # inside the container.
    #
    # Raise, do not unlink: unlinking trades a loud stop for a silently
    # incomplete tree. Raising is affordable because `materialize` runs before
    # the container -- it costs setup time and zero tokens, and `run_matrix`
    # catches per cell.
    #
    # DELIBERATELY UNANCHORED. Measured: deleting this check fails no test,
    # because reaching it requires the clone's `--dissociate` and the fast
    # path's `_FORBIDDEN_PATHS` check to fail at the same time. That is the
    # definition of belt-and-braces, and it is kept rather than dropped because
    # "expected unreachable" is what every defect in this subsystem has been.
    borrowed = dest / ".git" / "objects" / "info" / "alternates"
    if borrowed.exists():
        raise TaskError(
            f"{dest}: the run tree borrows objects via {borrowed}, which points "
            "outside base_sha's history and at a host path the container cannot "
            "resolve. The pruned mirror it was cloned from needs --dissociate."
        )

    # FIRST, before the test half and before `gitignore_extra`. The loader
    # refuses a strip that covers either half of the reference, so nothing the
    # test patch writes can land under a stripped prefix; `.gitignore` is the
    # only path a later step can legitimately re-create there, and running the
    # strip first is what makes that deterministic rather than an order the
    # reader has to infer.
    staged = _strip_paths_from_tree(dest, task.strip_paths, task.task_id)
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
