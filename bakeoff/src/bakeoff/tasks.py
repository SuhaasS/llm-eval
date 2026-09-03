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

from bakeoff.runners import for_framework

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
    #: Which runner adapter reads this suite. `pytest` -- the default -- keeps
    #: every existing manifest loading unchanged. See `_FRAMEWORKS` for why
    #: the set is closed, and `bakeoff/src/bakeoff/runners/` for what an
    #: adapter is and which pytest-specific judgement each method replaces.
    #:
    #: Last in the field order rather than beside `runner`, which is where it
    #: reads best, because a frozen dataclass needs every field after the
    #: first defaulted one to carry a default and `f2p` does not.
    framework: str = "pytest"


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

#: The base Dockerfile's own `ARG BASE_PYTHON_VERSION` default, restated here
#: as `TaskImage.python`'s default. Pinned equal by
#: `tests/test_images.py::test_the_dockerfile_default_matches_the_manifest_default`
#: -- two defaults that can drift means a manifest declaring nothing loads as a
#: task whose base was never built.
#:
#: It must also be a member of `_PYTHON_VERSIONS` below, and that is the one
#: relationship neither constant's own checks cover: `_python_version` returns
#: this value BEFORE the allowlist check, so a default outside the set is the
#: single value that reaches a build ungated -- and it reaches it from every
#: manifest that declares nothing, which is all of them today. Pinned by
#: `tests/test_tasks.py::test_the_default_is_itself_an_allowlisted_version`.
_DEFAULT_PYTHON = "3.12"

#: `TaskImage.node`'s default, and the node base Dockerfile's own
#: `ARG BASE_NODE_VERSION` default. Same pair, and the same failure, as
#: `_DEFAULT_PYTHON` one line up: two defaults that can drift means a manifest
#: declaring nothing loads as a task whose base was never built. It must also
#: be a member of `_NODE_VERSIONS`, for the reason stated there -- `_node_version`
#: returns this value BEFORE the allowlist check, so the default is the one
#: value that can reach a build ungated.
_DEFAULT_NODE = "22"


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
    #: The base image's Python. Every ARM of a task runs the same base, so
    #: this is a per-task property and not a section 5.4 divergence -- what
    #: that section holds identical is the environment two ARMS are compared
    #: in, and a task is compared against itself.
    #:
    #: Validated against `_PYTHON_VERSIONS` at load time. It selects which
    #: base image the drivers build and hand to `build_task_image`; preflight
    #: reads `python --version` back out of the finished container and refuses
    #: a mismatch, because the tag is mutable and local and a stale one leaves
    #: every unit test green while the suite runs under the wrong interpreter.
    python: str = _DEFAULT_PYTHON
    #: The node base image's major, for a `vitest` or `jest` task. Mirrors
    #: `python` above key for key -- validated against a closed set, quoted or
    #: refused, read back out of the finished container by preflight.
    #:
    #: DECLARING IT ON A PYTEST TASK IS A LOAD ERROR, and so is declaring
    #: `python` on a node task. The runtime is derived from
    #: `tests.framework` (see `task_runtime`); two keys that each imply one is
    #: two sources for one fact, and the failure of a disagreement between
    #: them is a task gated in one interpreter and run in another -- silently,
    #: because both halves build and both halves run.
    node: str = _DEFAULT_NODE


#: The keys `image:` accepts, derived from the dataclass for the same reason
#: `_GRADING_KEYS` is. This one guards the typo NOTHING downstream can see:
#: preflight reads `python --version` back out of the finished container and
#: compares it against `task.image.python`, so a misspelled `pyhton:` loads as
#: the default, builds the default base, and the read-back agrees with the
#: field it was compared to -- every gate green under an interpreter the author
#: did not ask for.
_IMAGE_KEYS = tuple(f.name for f in dataclass_fields(TaskImage))


@dataclass(frozen=True)
class TaskBudget:
    # Section 5.4: generous caps, measure actuals. These are placeholders
    # until the section 3.5 calibration pilot sets them from the
    # slowest-converging model's p95 -- tuning them to the incumbent is
    # exactly what that section forbids.
    # All three are positive integers, refused at load by name if not --
    # see `_positive_int`. No upper bound on max_turns: wall_clock_timeout_s
    # is the outer stop and section 3.5's pilot owns the number.
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
    #: timeout_s=600)`, `ensure_oracle(timeout_s=600)` and the grader's own
    #: per-check bound (`_check_command`/`_check_f2p`/`_check_p2p`, formerly
    #: `GRADE_TIMEOUT_S`). A manifest that does not declare the key therefore
    #: gates and grades byte-identically to before. `grader.SCAN_TIMEOUT_S`
    #: is not one of the three: it bounds only the host-side gitleaks scan,
    #: which has no `task` in scope and never ran in a container.
    suite_timeout_s: int = 600


@dataclass(frozen=True)
class Submodule:
    """One gitlink at `base_sha`, DERIVED rather than declared (see the
    module docstring's third load-bearing item, and spec section 3.7).

    Every field is inside the tree `base_sha` already pins: `path` and `sha`
    are a `160000` entry in `git ls-tree`, `name` and `url` are the section
    header and value in the `.gitmodules` blob. A manifest key restating them
    would be configuration reported as observation, and it could drift from
    the tree while every other check passed.

    `name` is not `path`: the `[submodule "NAME"]` header is what
    `submodule.<name>.url` keys on, and git does not require the two to match.

    `declared_unneeded` is the one field that is NOT derived. It is the
    manifest's `submodules_unneeded` list, joined onto the derived entry by
    path, and it is carried here rather than consulted separately so that every
    reader of a `Submodule` -- the initialiser, the image builder, the gate's
    evidence -- sees one answer instead of three lookups that can disagree. The
    entry is kept in the tuple rather than filtered out: a filtered submodule
    renders as a tree with no gitlink there, which is a different tree.
    """

    name: str
    path: str
    url: str
    sha: str
    declared_unneeded: bool = False


#: The only submodule url shape the eval can fetch. A relative url ("../x.git")
#: resolves against the superproject's remote, which `materialize` deletes and
#: the container cannot reach; ssh needs keys the eval does not carry; file://
#: points at the task author's laptop. Refused at derivation so the failure
#: names the manifest, not `git submodule update`'s clone error.
_SUBMODULE_URL_PREFIX = "https://"


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
    #: Submodule paths this task declares it does not need. The gitlink stays
    #: in the index and the tree exactly as at `base_sha`; the directory is
    #: never populated, its `.gitmodules` url is never checked -- nor is a
    #: readable `.gitmodules` required to exist for it at all -- and no pruned
    #: mirror is built for it. What lifts a repository's submodule floor
    #: without rewriting history: `tobymao/sqlglot` carries an ssh-url
    #: submodule from 2026-02-27 that closed every later `base_sha`, for
    #: suites that guard on its presence and never read it. Section 6.4
    #: confound: the humans who wrote the PR had the submodule.
    submodules_unneeded: tuple[str, ...] = ()
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
#:
#:   TZ  -- Date/time libraries are a large, well-shaped slice of the corpus
#:          (deterministic input, exact string output) and nearly all of them
#:          pin a zone in CI. Measured 2026-09-01
#:          (`~/.cache/bakeoff-probe/reports/d7-node.md`): `moment`/`luxon`
#:          were rejected as jest candidates purely because their suites
#:          assert against `TZ=America/New_York` (`test/zones/local.test.js`)
#:          and this allowlist had no way to grant it. `_env_map` holds TZ to
#:          an extra value shape the other two keys do not need
#:          (`_TZ_VALUE = re.compile(r"\A(?!/)[A-Za-z0-9_+\-/]+\Z")`): a `TZ`
#:          value is consumed by libc, not by this harness, and a leading `:`
#:          OR `/` makes glibc read the rest as a FILE PATH rather than a
#:          zone name -- the character-blacklist check above this constant
#:          does not catch a bare `/etc/localtime` (no colon at all, and `/`
#:          is a legal zone-name character), so TZ needs a positive allowlist
#:          of its own on top of it.
_IMAGE_ENV_ALLOWED = frozenset({"CI", "HYPOTHESIS_STORAGE_DIRECTORY", "TZ"})

#: The test frameworks this harness can classify. Closed, and it must equal
#: `bakeoff.runners.FRAMEWORKS` -- pinned from both files, because a reader of
#: either constant has to be told it is half of a pair. `for_framework` raises
#: KeyError rather than defaulting, and this allowlist is the only thing that
#: keeps that raise unreachable; a default in either place classifies a jest
#: run with pytest's exit codes, which means exit 1 for a BROKEN CONFIG read as
#: a test failure and stamped on the model, permanently, in an append-only
#: store.
#:
#: Measured 2026-09-01 in node:22-bookworm-slim, vitest 3.2.7 and jest 30.5.0:
#: both exit 1 for a failing test, an unresolvable import, a syntax error, a
#: nonexistent file argument AND a broken config, and BOTH exit 0 when a `-t`
#: pattern matches nothing. That is why each entry needs an adapter that reads
#: a JSON report rather than a row in a table of exit codes.
_FRAMEWORKS = frozenset({"pytest", "vitest", "jest"})

#: The frameworks that need the node base image. Derived from the framework
#: rather than declared, so there is one source for the runtime.
_NODE_FRAMEWORKS = frozenset({"vitest", "jest"})


def _framework(value: Any, where: str) -> str:
    """`tests.framework`, validated. `"pytest"` when absent.

    Absent means pytest and not "unknown", because every manifest that exists
    predates the key -- backwards compatibility is a constraint of this
    broadening, not a hope.
    """
    if value is None:
        return "pytest"
    if not isinstance(value, str) or value not in _FRAMEWORKS:
        raise TaskError(
            f"{where}: {value!r} is not a test framework this harness can "
            f"classify. Allowed: {sorted(_FRAMEWORKS)}. The gate tells 'the "
            "bug is present' from 'the environment is broken' by reading what "
            "the runner reported, and each framework reports it differently -- "
            "pytest through its exit code, vitest and jest through a JSON "
            "report, because both of those exit 1 for a test failure and for a "
            "broken config alike"
        )
    return value


#: Python versions the base Dockerfile is KNOWN to build, because someone
#: built it. A closed set rather than a free string, for two reasons that are
#: each silent without it:
#:
#:   A floating tag is a moving base. `python:3-slim-bookworm` and
#:   `python:3.13-slim-bookworm` are both republished, so two collections
#:   months apart run different interpreters under one manifest and nothing in
#:   the record says which -- the defect `image.pip`'s "pinned, not floored"
#:   rule exists to prevent, one key over.
#:
#:   An unbuildable string fails LATE. Measured 2026-09-01, Docker 29.5.2:
#:   `--build-arg BASE_PYTHON_VERSION=3.99` gets `failed to resolve reference
#:   "docker.io/library/python:3.99-slim-bookworm": ... not found` at the FROM,
#:   after a build has started and over the network. Here it is a TaskError
#:   with the manifest path in it, before any daemon is touched.
#:
#: Each entry was measured by building `docker/eval-agent.Dockerfile` at that
#: version and confirming BOTH in-Dockerfile pin assertions fired -- `pytest
#: 9.1.1 pinned` and `claude 2.1.220 pinned`. Verified 2026-09-01 for all
#: three; 3.11 gave Python 3.11.16, 3.12 gave 3.12.13, 3.13 gave 3.13.15.
#:
#: TO ADD A VERSION, all three steps: build the real base at it and confirm
#: both assertions fire; add the string here with the date; add the row to
#: taskset/HARVESTING.md's table and the note in docs/BUILDING-A-TASK-SET.md.
#: `python:3.14-slim-bookworm` exists and is deliberately absent -- nobody has
#: built the eval image on it, and an unmeasured entry is this constant
#: claiming something it does not know.
#:
#: `_DEFAULT_PYTHON` above must stay a member of this set. Removing a version
#: that happens to be the default leaves every manifest that declares nothing
#: pointing at a base nobody builds, and no refusal fires -- the default is
#: returned before this set is consulted.
_PYTHON_VERSIONS = frozenset({"3.11", "3.12", "3.13"})


def _python_version(value: Any, where: str) -> str:
    """`image.python`, validated. The default when absent.

    A `str` is REQUIRED, never coerced. YAML parses an unquoted `3.11` as the
    float 3.11 and an unquoted `3.10` as `3.1` -- so `str(value)` would turn a
    manifest asking for 3.10 into one asking for a version that is not in the
    allowlist at all, or (had 3.1 been listed) into a silently different
    interpreter. Same class as `_ENV_VALUE_REFUSED`'s `$`: a value that is a
    property of the parser rather than of the manifest.
    """
    if value is None:
        return _DEFAULT_PYTHON
    if not isinstance(value, str):
        raise TaskError(
            f"{where}: {value!r} is {type(value).__name__}, not a string -- "
            "quote it (`python: \"3.11\"`). YAML reads an unquoted 3.11 as a "
            "float and an unquoted 3.10 as 3.1, so the version that reaches "
            "the build is not the one the manifest names"
        )
    if value not in _PYTHON_VERSIONS:
        raise TaskError(
            f"{where}: {value!r} is not a Python version this base image is "
            f"known to build. Allowed: {sorted(_PYTHON_VERSIONS)}. A version "
            "outside the set is either a floating tag -- two collections "
            "months apart running different interpreters under one manifest, "
            "with nothing in the record saying so -- or one that fails at the "
            "FROM with a registry error mid-build. To add one, build "
            "docker/eval-agent.Dockerfile with "
            "`--build-arg BASE_PYTHON_VERSION=<v>`, confirm the pytest and "
            "claude pin assertions fire, then add it to _PYTHON_VERSIONS and "
            "to taskset/HARVESTING.md"
        )
    return value


#: Node majors the node base Dockerfile is KNOWN to build, because someone
#: built it. The same closed-set argument `_PYTHON_VERSIONS` makes, both halves
#: of it: `node:22-bookworm-slim` is republished, so an unpinned entry means
#: two collections months apart run different runtimes under one manifest with
#: nothing in the record saying so; and an unbuildable string fails at the FROM
#: with a registry error, over the network, mid-build.
#:
#: Verified 2026-09-01: node v22.23.2, npm 10.9.8, vitest 3.2.7, jest 30.5.0,
#: Claude Code 2.1.220, uid 1000 (after `userdel -r node` -- the base image
#: ships a `node` user at that uid and `useradd --uid 1000` exits 4 without
#: it).
#:
#: TO ADD A VERSION, all three steps: build docker/eval-agent-node.Dockerfile
#: with `--build-arg BASE_NODE_VERSION=<v>` and confirm the claude and runner
#: pin assertions fire; add the string here with the date; add the row to
#: taskset/HARVESTING.md. `20` and `24` exist as tags and are deliberately
#: absent -- an unmeasured entry is this constant claiming what it does not
#: know.
#:
#: `_DEFAULT_NODE` must stay a member of this set, for the reason stated
#: beside it: `_node_version` returns the default BEFORE this check.
_NODE_VERSIONS = frozenset({"22"})


def _node_version(value: Any, where: str) -> str:
    """`image.node`, validated. The default when absent.

    A `str` is REQUIRED, never coerced, for `_python_version`'s reason and not
    for a weaker one. `str(22)` happens to be right today; the refusal is here
    because the next version to be added is `22.1`, and YAML reads a bare
    `22.10` as the float `22.1` -- a value that is a property of the parser
    rather than of the manifest.
    """
    if value is None:
        return _DEFAULT_NODE
    if not isinstance(value, str):
        raise TaskError(
            f"{where}: {value!r} is {type(value).__name__}, not a string -- "
            'quote it (`node: "22"`). YAML reads an unquoted 22.10 as the '
            "float 22.1, so the version that reaches the build is not the one "
            "the manifest names"
        )
    if value not in _NODE_VERSIONS:
        raise TaskError(
            f"{where}: {value!r} is not a Node version this base image is "
            f"known to build. Allowed: {sorted(_NODE_VERSIONS)}. To add one, "
            "build docker/eval-agent-node.Dockerfile with `--build-arg "
            "BASE_NODE_VERSION=<v>`, confirm the claude and runner pin "
            "assertions fire, then add it to _NODE_VERSIONS and to "
            "taskset/HARVESTING.md"
        )
    return value


def task_runtime(task: TaskManifest) -> tuple[str, str]:
    """Which base image this task needs: `("python", "3.12")` or `("node", "22")`.

    Derived from `tests.framework`, never declared. Two manifest keys that can
    each imply a runtime is two sources for one fact, and a disagreement
    between them is a task gated in one interpreter and run in another -- the
    silent shape, because both halves build and both halves run. `load_task`
    therefore refuses a manifest that declares the key belonging to the other
    runtime, which is what leaves this function total.
    """
    if task.tests.framework in _NODE_FRAMEWORKS:
        return ("node", task.image.node)
    return ("python", task.image.python)


#: Characters that do not survive a generated `ENV KEY="value"` line. `$` is
#: the one that is not about syntax: Docker EXPANDS it against the build
#: environment, which would make the value a property of the builder rather
#: than of the manifest -- the argument that refuses pathspec magic in
#: strip_paths, one key over.
_ENV_VALUE_REFUSED = ('\n', '\r', '"', '\\', '$')

_ENV_KEY = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

#: The extra shape a TZ value must have, on top of `_ENV_VALUE_REFUSED`. An
#: IANA zone name (`America/New_York`) or `UTC`/`Etc/GMT+N` -- letters,
#: digits, `_`, `+`, `-`, `/`. glibc's `tzset` reads a `TZ` value starting
#: with `:` OR `/` as a FILE PATH rather than a zone name: a leading `:` is
#: already refused by the character class (`:` was never in it), but a
#: leading `/` was not, and `/` IS in the class for `America/New_York` and
#: `Etc/GMT+5`. Measured 2026-09-02 in the eval image (glibc 2.36):
#: `TZ=/usr/share/zoneinfo/Asia/Tokyo` resolves identically to
#: `TZ=:/usr/share/zoneinfo/Asia/Tokyo`, and `TZ=/etc/localtime` resolves
#: too -- so a bare leading `/`, with no colon anywhere, hands libc a path
#: into the image just as effectively, including a path an agent's own edits
#: could reach (`/repo/tzfile`, say). The negative lookahead below refuses
#: only a LEADING `/`; the character elsewhere in the value is unaffected.
_TZ_VALUE = re.compile(r"\A(?!/)[A-Za-z0-9_+\-/]+\Z")


def _env_map(value: Any, where: str) -> dict[str, str]:
    """`image.env`, validated. Empty when absent.

    Five refusals, each naming a failure that is silent without it:

    * a key outside `_IMAGE_ENV_ALLOWED` -- see that constant;
    * a key the harness itself sets -- Docker's exec env wins over the image's
      (measured), so the value would apply to preflight and the grader and NOT
      to the agent, which is two environments for one task;
    * a value carrying `_ENV_VALUE_REFUSED`;
    * HYPOTHESIS_STORAGE_DIRECTORY inside /repo, which undoes the only thing
      that key is for;
    * a TZ value outside `_TZ_VALUE` -- see that constant.
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
        if key == "TZ" and not _TZ_VALUE.match(raw_value):
            raise TaskError(
                f"{where}: {key!r} value {raw_value!r} is not an IANA zone "
                "name or UTC/Etc/GMT+N. A TZ value is consumed by libc, not "
                "by this harness -- a leading `:` OR `/` makes glibc read "
                "the rest as a file path rather than a zone name, which the "
                "character blacklist above does not catch on its own"
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


def _validate_unneeded_submodules(paths: tuple[str, ...], where: str) -> None:
    """The SHAPE half of `submodules_unneeded`, which is all that can run here.

    `load_task` runs on the host, offline, with no repository and no cache
    root, so the question that matters most -- is this path a gitlink at
    `base_sha`? -- cannot be asked at load time at all. It is asked in
    `derive_submodules`, which holds the mirror, exactly as `strip_paths`'
    "names nothing tracked" check lives in `_strip_paths_from_tree` rather
    than here. What is left for this function is the shape.

    `_validate_prefixes` covers empty, padded, absolute and `..`-bearing
    entries. The three after it are the same three `_validate_strip_paths`
    refuses and for the same reasons one key over: `.`/`./` names the whole
    tree and a tree is not a gitlink, a `.git`-rooted path is inside the
    repository's own metadata where no tree entry lives, and pathspec magic
    would make which submodules are left unpopulated a property of the tree
    rather than of the manifest.

    The DUPLICATE refusal is the one `strip_paths` does not have, and it is
    here because this key is consumed as a SET -- `set(task.submodules_unneeded)`
    in `derive_submodules`, a `frozenset` in `preflight`. A repeat is
    therefore invisible to every downstream comparison: it cannot change any
    behaviour, so a manifest carrying one means something the key cannot
    express, and saying nothing is how an author's actual intent gets lost.
    """
    _validate_prefixes(paths, where)
    seen: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts:
            raise TaskError(
                f"{where}: {path!r} names the whole tree; this key names one "
                "submodule path per entry, and a tree is not a gitlink"
            )
        if parts[0] == ".git":
            raise TaskError(
                f"{where}: {path!r} is inside the repository's own .git; a "
                "gitlink is a tree entry, and nothing under .git is one"
            )
        if path.startswith(":") or any(ch in path for ch in _PATHSPEC_MAGIC):
            raise TaskError(
                f"{where}: {path!r} carries pathspec magic; this key names "
                "paths, and a pattern would make which submodules are left "
                "unpopulated a property of the tree rather than of the "
                "manifest"
            )
        if path in seen:
            raise TaskError(
                f"{where}: {path!r} is listed twice; the second entry cannot "
                "change anything, so a manifest carrying it means something "
                "the key cannot express"
            )
        seen.add(path)


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
      manifest path in the traceback -- and `int(None)` a bare `TypeError`,
      which is how `max_turns` and `wall_clock_timeout_s` behaved before
      2026-09-02. An author is handed a stack trace naming this module
      instead of a message naming their file and their key.
    * `int("600")` and `int(600.0)` SUCCEED, so a quoted or floated value is
      accepted silently and the manifest stops being a faithful record.
    * `bool` IS an `int` in Python: `int(True)` is 1. `suite_timeout_s: true`
      would kill every gated and graded command after one second, which reads
      as a task whose tests hang -- a NO-GO at the gate and `timed_out`
      stamped on every arm past it -- for a YAML typo. So the bool test comes
      FIRST; folded into the int test it is dead code a mutation cannot catch.

    `_ABSENT` rather than `None` for "not declared", so an explicit `key: null`
    falls through to the refusal.

    Applied to all three `budget:` keys. `max_turns` and `wall_clock_timeout_s`
    kept a bare `int(...)` until 2026-09-02, and the cost was not the shapes
    that raised -- it was the shapes that did NOT. `wall_clock_timeout_s: true`
    loaded as 1 second, and the `>` refusal below then reported the manifest's
    defect as a `suite_timeout_s` the author had never declared, ending in
    "lower suite_timeout_s", which is advice about the wrong key. `: 0.5`
    loaded as 0 the same way. Measured across the nine manifests that existed
    that day, extending this validator refused none of them.

    No UPPER bound on `max_turns`, deliberately: `wall_clock_timeout_s` is the
    outer stop whatever this says, section 5.4 leaves the number to the
    section 3.5 calibration pilot, and there is no downstream limit to mirror
    AT THE VERSION MEASURED -- verified against claude 2.1.258 on the host,
    where `--max-turns` is absent from `--help` entirely and `--max-turns 0`
    starts a session and calls the API rather than being refused. The eval
    image pins 2.1.220 and was not measured; a stricter check there would
    only move the failure earlier, never make this refusal wrong. The bottom
    is what the container cannot survive, and `value <= 0` is what refuses
    it.
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
    framework = _framework(tests_raw.get("framework"), f"{where}:tests.framework")
    adapter = for_framework(framework)
    # Cross-checked rather than derived from each other, in either direction:
    # each key catches the other's typo. A runner read by the wrong adapter is
    # classified by the wrong rules -- and on the node side there is no exit
    # code that would say so, because vitest and jest exit 1 for a failing
    # test and for a broken config alike.
    if not any(adapter.runner_marker in part for part in runner):
        raise TaskError(
            f"{where}: tests.framework is {framework!r} but no element of "
            f"tests.runner contains {adapter.runner_marker!r} "
            f"({list(runner)!r}). Each key catches the other's typo, which is "
            "why neither is derived from the other -- and a runner read by the "
            "wrong adapter is classified by the wrong rules."
        )
    # Per-framework, and EMPTY for pytest: pytest selects by the whole node id,
    # path included, so a shape rule added here now could refuse a manifest
    # that loads today.
    for node_id in (*f2p, *p2p):
        adapter.validate_node_id(node_id, test_paths, where)
    adapter.validate_id_set((*f2p, *p2p), where)

    # `is None`, NOT `or {}`, for the reason given at `budget_raw` above and
    # at `grading_raw` below -- and this section is the one where a silently
    # discarded body costs the most. Measured 2026-09-02: `image: []`, `: 0`
    # and `: ""` all loaded as the default python with EMPTY apt, pip and
    # build, so one stray bracket throws away `build: ["pip install -e ."]`
    # (imports then resolve to site-packages and nothing the agent writes
    # takes effect) and `apt: ["less"]` (189 tests error on a closed stdout
    # in the pager test). Preflight catches both, which is the only reason
    # this was ever a hygiene defect rather than a P0.
    image_raw = data.get("image")
    if image_raw is None:
        image_raw = {}
    if not isinstance(image_raw, dict):
        raise TaskError(f"{where}: image must be a mapping")
    # The same guard `grading:` has below, needed here for a sharper reason: a
    # misspelled `image:` key is the one failure preflight's read-back cannot
    # see. It asks the container which interpreter it runs and compares that
    # against `task.image.python` -- but `pyhton: "3.11"` loads as the default,
    # so the base that was built and the field it is compared to are the same
    # wrong value, and they agree.
    unknown = sorted(str(k) for k in set(image_raw) - set(_IMAGE_KEYS))
    if unknown:
        raise TaskError(
            f"{where}: unknown image key(s) {unknown}; allowed: "
            f"{list(_IMAGE_KEYS)}"
        )
    # The runtime is DERIVED from `tests.framework` -- see `task_runtime`. The
    # key belonging to the other runtime is refused rather than ignored,
    # because ignoring it is the silent half: a manifest declaring
    # `python: "3.11"` beside `framework: vitest` would build a node base and
    # say nothing, and the author would have no way to learn that the version
    # they pinned was never consulted.
    declared_node = image_raw.get("node")
    declared_python = image_raw.get("python")
    is_node = framework in _NODE_FRAMEWORKS
    if is_node and declared_python is not None:
        raise TaskError(
            f"{where}: image.python is declared but tests.framework is "
            f"{framework!r}, which runs on the node base image. The runtime "
            "comes from the framework; declaring both is two sources for one "
            "fact, and a disagreement gates the task in one interpreter and "
            "runs it in another."
        )
    if not is_node and declared_node is not None:
        raise TaskError(
            f"{where}: image.node is declared but tests.framework is "
            f"{framework!r}, which runs on the python base image. The runtime "
            "comes from the framework; declaring both is two sources for one "
            "fact."
        )
    # `is None`, NOT `or {}` -- for the reason the `grading:` block below
    # gives in the same words: `budget: []` is what an author who started a
    # list and never wrote the keys leaves behind, and `or {}` reads it as a
    # section they never wrote, applying all three defaults to a manifest
    # that visibly asked for something else. Measured 2026-09-02: `[]`, `0`
    # and `""` all loaded as 40/900/600. An explicit `budget: null` still
    # takes the defaults, which is a commented-out block and is what the
    # defaults are for -- unlike a null KEY, which is an author reaching for
    # one number and writing none, and is refused by `_positive_int`.
    budget_raw = data.get("budget")
    if budget_raw is None:
        budget_raw = {}
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
    submodules_unneeded = _strs(
        data.get("submodules_unneeded"), f"{where}:submodules_unneeded"
    )
    _validate_unneeded_submodules(
        submodules_unneeded, f"{where}:submodules_unneeded"
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
    _refuse_stripped_halves(test_files, solution_files, strip_paths, where)

    # All three through one validator, and the two below the comparison
    # matter most: a bare `int(...)` accepted `wall_clock_timeout_s: true`
    # as 1 and `: 0.5` as 0, and the `>` refusal further down then reported
    # the manifest's problem as a `suite_timeout_s` the author never
    # declared -- advising them to lower the one number that was correct.
    # `_ABSENT` rather than the dataclass default, so `key: null` is refused
    # instead of read as a key nobody wrote.
    #
    # Keyword arguments evaluate left to right, so a manifest with two bad
    # keys is refused by the FIRST in this order. Deterministic, and it is
    # the order the keys appear in the dataclass and in every manifest.
    budget = TaskBudget(
        max_turns=_positive_int(
            budget_raw.get("max_turns", _ABSENT),
            f"{where}:budget.max_turns",
            TaskBudget.max_turns,
        ),
        wall_clock_timeout_s=_positive_int(
            budget_raw.get("wall_clock_timeout_s", _ABSENT),
            f"{where}:budget.wall_clock_timeout_s",
            TaskBudget.wall_clock_timeout_s,
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
    # Runs after all three keys are validated above, which is what lets this
    # message be trusted: on a bare `int(...)` it fired for a boolean
    # `wall_clock_timeout_s` and blamed `suite_timeout_s`.
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
            allow_extra_paths=extra_paths, framework=framework,
        ),
        image=TaskImage(
            apt=_strs(image_raw.get("apt"), f"{where}:image.apt"),
            pip=_strs(image_raw.get("pip"), f"{where}:image.pip"),
            build=_strs(image_raw.get("build"), f"{where}:image.build"),
            env=_env_map(image_raw.get("env"), f"{where}:image.env"),
            python=_python_version(declared_python, f"{where}:image.python"),
            node=_node_version(declared_node, f"{where}:image.node"),
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
        submodules_unneeded=submodules_unneeded,
        provenance=dict(data.get("provenance") or {}),
        task_set_commit=set_commit,
        manifest_digest=hashlib.sha256(raw_manifest + raw_reference).hexdigest()[:16],
    )


@dataclass(frozen=True)
class RefusedManifest:
    """One task directory under a task-set root whose manifest did not load.

    `error` is the message the load raised. A `TaskError` already opens with
    the absolute path of the offending `task.yaml` (`load_task`'s `where`), so
    a caller printing these never has to reconstruct which file is meant; the
    collector prefixes the path itself for the exception types that do not
    carry one.

    `committed` is the whole reason a refusal can ever be downgraded to a
    warning, and it is stated per DIRECTORY rather than per set: it means
    "this directory is part of the revision a record would name". A directory
    that is untracked, ignored, or in no repository at all is work in progress
    -- `docs/BUILDING-A-TASK-SET.md` section 1.5 says to commit the task set
    before every collection, so an uncommitted one is by definition not the
    thing a collection runs against. A TRACKED manifest that does not load is
    a different animal: the set's own revision is broken, every record written
    against it names a sha that promises a set which does not load whole, and
    `grade.py` -- which has no `--tasks` and loads the set whole -- grades
    every record naming that task as TASK_NOT_FOUND, at exit code 0. So such a
    refusal is fatal whatever the selection says, and the drafting affordance
    cannot be used to carry a broken task set through a collection.
    """

    directory: Path
    error: str
    committed: bool


def _manifest_committed(root: Path, task_dir: Path) -> bool:
    """True when this task directory is part of the revision a record names.

    TWO facts, in this order, because one of them alone answers a different
    question. `git status --porcelain -- <dir>` reports what the enclosing
    repository has to SAY about a path, and it says nothing about an IGNORED
    path -- measured, git 2.50.1: a scratch task set inside a repo whose
    `.gitignore` holds `scratch/` gets exit 0 and empty stdout, while `git
    ls-files --error-unmatch` on the same path exits non-zero because nothing
    there is tracked. Status alone therefore calls an untracked drafting
    directory "committed" and hard-refuses it under a message asserting the
    opposite of the truth -- which re-breaks the exact workflow the warning
    path exists to allow. So: tracked decides whether the path is in the
    index at all, and only then does clean decide whether it is modified.

    Pathspec discipline, learned the way `task_set_commit` learned it: `git
    status -- <pathspec>` resolves the pathspec against the CWD, so a relative
    path plus `cwd=root` asks about `<root>/<root>/...`, matches nothing, and
    git prints nothing and exits 0 -- every directory would read as committed.
    Here that is not cosmetic: it would flip every warning back into the
    refusal this function exists to lift. Both paths are resolved first, for
    both calls.

    The status half fails CLOSED. Once a path is known to be tracked, a `git
    status` that cannot run leaves us unable to prove the manifest is work in
    progress, and guessing permissively is how a committed task set that does
    not load whole reaches the end of a collection.

    "Committed" means committed to whatever repository ENCLOSES this path.
    `task_set_commit` names the enclosing repo, which for the in-repo task set
    (`bakeoff/taskset/`) is the harness repo rather than a task-set repo. That
    is pre-existing, and this is the first code to turn it into a refuse/warn
    decision.

    The caller does not call this when the set has no commit at all -- an
    empty `task_set_commit` means not a git repository, or git unusable, or no
    commits yet -- and that guard is NOT what makes the non-repo case correct.
    Measured, git 2.50.1: called on a root outside any repository this returns
    `False` on its own, because `ls-files` exits non-zero and the path reads as
    untracked before the status half is reached. The guard is kept because
    asking git twice about a directory in no repository spends two subprocesses
    to learn nothing, and because the caller is the layer that already knows
    the set has no revision. Do not read it as the thing that covers a non-repo
    root: under the SUPERSEDED one-term predicate it was, and removing the
    `ls-files` term on that reading reintroduces the ignored-directory refusal
    above.

    TOTAL: returns a bool for every input and raises for none. It is called
    from inside the collector's `except` block, where a raise would chain onto
    the manifest's own error and escape `load_task_set_with_refusals` as an
    exception no driver catches -- the traceback this change exists to remove,
    reintroduced one layer over. Both `resolve()` calls are inside the same
    guard as the `ls-files` call, not ahead of it: `resolve()` is non-strict
    and will not raise on a missing path, but it can raise `OSError` on a
    pathological one, and the totality claim above covers that too.
    """
    try:
        root = Path(root).resolve()
        task_dir = Path(task_dir).resolve()
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(task_dir)],
            cwd=root, capture_output=True, text=True,
        ).returncode == 0
    except OSError:
        return True
    if not tracked:
        return False
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", str(task_dir)],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return True
    return not status


def _fatal_reason(refusal: RefusedManifest, wanted: set[str] | None) -> str:
    """Why THIS refusal cannot be skipped. Order is precedence, not taste:
    a directory that is both selected and committed is refused because it was
    asked for, which is the fact the operator can act on.

    The third branch names the SELECTION, not just the sibling. `TASKS.md`
    states the remedy as "the error should name the offending sibling AND say
    explicitly that it is not the selected task", and the committed-unselected
    branch is the one a curated task set hits every time -- the branch this
    change creates. Without the selection in the text it reproduces exactly
    the operator experience the probe reported: a message about a sibling,
    with no mention of what was actually asked for.
    """
    if wanted is not None and refusal.directory.name in wanted:
        return f"SELECTED by --tasks as {refusal.directory.name!r}"
    if wanted is None:
        return "no --tasks selection was given, so the whole set is required"
    return (
        f"committed, and NOT the task you selected "
        f"({', '.join(sorted(wanted))}): this manifest is not work in "
        "progress, so the task set's own revision does not load and no "
        "selection can skip it"
    )


def _refusal_report(root: Path, fatal: list[RefusedManifest],
                    wanted: set[str] | None, skippable: int) -> str:
    """The refusal text. `skippable` is the count of refusals that were NOT
    fatal, and it is stated when non-zero because the header claims every
    listed manifest is required -- a reader who knows there were four
    refusals and sees one listed would otherwise be reading a report that
    silently dropped three."""
    lines = [
        f"{root}: {len(fatal)} manifest(s) in this task set did not load, "
        "and every one of them is required here:"
    ]
    for refusal in fatal:
        lines.append(f"  - [{_fatal_reason(refusal, wanted)}]")
        lines.append(f"    {refusal.error}")
    if skippable:
        lines.append(
            f"({skippable} further manifest(s) here also did not load and "
            "would have been skippable under this selection; they are listed "
            "only when the load is allowed to proceed.)"
        )
    lines.append(
        "Every record stamps task_set_commit -- the revision of this whole "
        "directory -- so a set that does not load whole is not a state to "
        "record against, and grade.py, which has no --tasks and loads the set "
        "whole, cannot tell that a manifest it cannot read is not the one a "
        "record names. Fix the manifest, or move it out of this directory. "
        "Only work in progress can be skipped -- a manifest not tracked in "
        "this directory's revision, or in a directory that has none -- and "
        "only by a --tasks selection that does not name it "
        "(docs/BUILDING-A-TASK-SET.md section 1.5: commit the task set before "
        "every collection)."
    )
    return "\n".join(lines)


def refusal_warnings(refusals: list[RefusedManifest], *, root: Path,
                     selected: set[str]) -> list[str]:
    """The lines a driver prints for refusals it was allowed to skip.

    A list of lines rather than a print, because `tasks.py` is a library and a
    module that prints cannot be asserted against. Returned as text rather
    than as the refusals themselves so that two drivers cannot word the same
    finding differently -- the reason this is a function at all.

    The middle clause claims only what was measured. "Uncommitted work in
    progress" would be an overclaim about a directory in a tree that is not a
    git repository at all, where nothing is known about work in progress; what
    IS known is that no such refusal is committed in this task set's revision
    -- because the directory is in no repository, because the manifest is
    untracked or ignored there, or because it is tracked but modified since
    the last commit (the ordinary drafting loop: editing an existing tracked
    task rather than adding a new one). All three collapse to the same
    observable fact -- `_manifest_committed` returned False -- and the
    parenthetical below states exactly those three, not just the first two.

    Empty whenever `refusals` is: `load_task_set_with_refusals` returns a
    non-empty list only on the branch that was allowed to proceed.
    """
    if not refusals:
        return []
    lines = [
        f"WARNING: {len(refusals)} manifest(s) under {root} did not load and "
        f"were skipped. None of them is a selected task "
        f"({', '.join(sorted(selected))}), and none is committed in this task "
        "set's revision (this directory is in no repository, the manifest is "
        "untracked or ignored there, or it is modified since the last "
        "commit):"
    ]
    lines += [f"  - {refusal.error}" for refusal in refusals]
    lines.append(
        "WARNING: these are refusals, not skips. A run without --tasks, and "
        "any grade.py run over this directory, refuses until each one is "
        "fixed or moved out. This warning is the only place the skip is "
        "recorded -- no record field carries it."
    )
    return lines


def load_task_set_with_refusals(
    root: Path, only: list[str] | None = None
) -> tuple[list[TaskManifest], list[RefusedManifest]]:
    """Every task under `root`, validated, in a stable order -- and the
    directories that would not load, when the caller is allowed to skip them.

    Duplicate task_ids raise. `run_id` is a hash of (task_id, model, sample,
    attempt), so two tasks sharing an id would collide in the event log and
    the second one's records would be refused as immutability violations --
    at the far end of a matrix, after the tokens were spent.

    A refusal is NOT raised where it is found. Measured 2026-09-02 in a shared
    drafting directory: one worker's in-progress `image.env` typo blocked a
    different worker's `--tasks <unrelated>` gate, and the printed error named
    only the sibling. Refusals are collected and judged once, against three
    facts -- was a selection given, does it name this directory, is this
    manifest tracked in the enclosing revision -- because each answers a
    different question. The selection says what the operator asked for;
    tracked-ness says whether the task set's own revision is broken or whether
    this is work in progress in a tree that has no revision for a record to
    name (`task_set_commit` is then `""`, never `"<sha>-dirty"` -- a scratch
    task set is typically not a git repository at all).

    A refused directory is matched against `only` by DIRECTORY NAME, because a
    manifest that did not load has no readable task_id. `load_task` does not
    require the two to agree, so a refused `foo/` could declare `task_id: bar`
    and a `--tasks bar` selection would proceed past it. That cannot pass
    quietly: `bar` is then missing from the loaded ids and the "no such task"
    refusal below names every surviving refusal as a directory it could not
    check.

    `only` is read for truthiness, not for `is not None`, exactly as before:
    an empty selection has always meant "no selection", and `run_matrix.py`
    passes `None` for an absent `--tasks`.
    """
    root = Path(root)
    if not root.is_dir():
        raise TaskError(f"{root}: no such task set")
    commit = task_set_commit(root)
    tasks: list[TaskManifest] = []
    refusals: list[RefusedManifest] = []
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (task_dir / MANIFEST_NAME).exists():
            continue
        try:
            tasks.append(load_task(task_dir, set_commit=commit))
        except Exception as exc:  # noqa: BLE001 - one manifest, not the set
            # Broad on purpose. `load_task` raises TaskError for everything it
            # checks, but `yaml.safe_load` raises `yaml.YAMLError` and an
            # unreadable file raises OSError -- neither is a TaskError, so a
            # sibling with malformed YAML used to reach the operator as a
            # traceback straight through both drivers' `except TaskError`.
            # Nothing gets quieter: every collected refusal is re-raised as a
            # TaskError below unless it is provably skippable.
            refusals.append(RefusedManifest(
                directory=task_dir,
                error=(str(exc) if isinstance(exc, TaskError) else
                       f"{task_dir / MANIFEST_NAME}: "
                       f"{type(exc).__module__}.{type(exc).__name__}: {exc}"),
                # Only asked when the set has a revision at all -- see
                # `_manifest_committed`'s docstring for why the empty-commit
                # case must not reach it.
                committed=(bool(commit)
                           and _manifest_committed(root, task_dir)),
            ))

    wanted = set(only) if only else None
    fatal = [r for r in refusals
             if wanted is None or r.committed or r.directory.name in wanted]
    if fatal:
        raise TaskError(
            _refusal_report(root, fatal, wanted, len(refusals) - len(fatal))
        )

    if wanted is not None:
        known = {task.task_id for task in tasks}
        missing = wanted - known
        if missing:
            unchecked = ""
            if refusals:
                unchecked = (
                    f"; {len(refusals)} manifest(s) here did not load and were "
                    "matched against the selection by DIRECTORY NAME only, so "
                    "one of them may be the task you asked for: "
                    + ", ".join(str(r.directory) for r in refusals)
                )
            raise TaskError(
                f"no such task(s) in {root}: "
                f"{', '.join(sorted(missing))}{unchecked}"
            )
        tasks = [task for task in tasks if task.task_id in wanted]

    seen: set[str] = set()
    for task in tasks:
        if task.task_id in seen:
            raise TaskError(f"duplicate task_id {task.task_id!r} in {root}")
        seen.add(task.task_id)
    if not tasks:
        raise TaskError(f"{root}: no tasks")
    return tasks, refusals


def load_task_set(root: Path, only: list[str] | None = None
                  ) -> list[TaskManifest]:
    """`load_task_set_with_refusals` for a caller with nothing to skip.

    Signature and return type are unchanged, so `scripts/grade.py` and
    `scripts/judge.py` keep their call sites and inherit the refusal text
    without an edit. Both pass no selection, which is the branch on which
    `refusals` is empty by construction: a caller that named no subset is
    asking for the whole set, and every manifest in it is required.
    """
    tasks, _ = load_task_set_with_refusals(root, only)
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


def derive_submodules(task: TaskManifest, mirror: Path) -> tuple[Submodule, ...]:
    """The submodules of `task.base_sha`, from git's own parsers, cross-checked.

    TWO independent readers, and their agreement is the guarantee. `ls-tree`
    gives the paths that have a gitlink; `.gitmodules` gives the paths that
    have a url. A path in one set and not the other is a QUIET failure in both
    directions -- a url with no gitlink makes the init fail with git's message
    instead of one naming the task, and a gitlink with no url leaves the
    directory empty, which is byte-identical to a suite that cannot import and
    is scored as capability on every arm (measured 2026-09-01, git 2.50.1:
    `git status --porcelain` is EMPTY in a run tree whose submodule directory
    has zero entries).

    Both reads are NUL-delimited, for the reason `_chunk_path`'s docstring
    gives: paths come from git, never from a regex over a header line.

    The url rule is enforced through the module constant
    `_SUBMODULE_URL_PREFIX` rather than a parameter, so the test fixtures --
    local repositories, so the whole materialization path runs offline -- relax
    it by monkeypatching one name, and no shipping signature carries a flag
    that only tests ever set.
    """
    entries = _git("ls-tree", "-r", "-z", task.base_sha, cwd=mirror).stdout
    gitlinks: dict[str, str] = {}
    for record in entries.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, _, rest = meta.partition(" ")
        if mode != "160000":
            continue
        gitlinks[path] = rest.split(" ")[-1]

    # FIRST, before anything reads `.gitmodules`. Both of the refusals below
    # are derived from that file, and an author who misspells the path of the
    # very submodule they are exempting must be told about the typo rather
    # than about a url or an unreadable blob. The position is the message.
    unknown = sorted(set(task.submodules_unneeded) - set(gitlinks))
    if unknown:
        raise TaskError(
            f"{task.task_id}: submodules_unneeded names "
            f"{', '.join(unknown)}, which the tree at {task.base_sha} has no "
            "gitlink at. A typo declares nothing: the submodule it was meant "
            "to name is still populated (or still refused for its url), and "
            "nothing downstream would say the key did not apply."
        )
    # ONE derived set for BOTH `.gitmodules` refusals, not two subtractions,
    # because they are the same claim: nothing is fetched for a declared path,
    # so neither a url nor a readable `.gitmodules` is needed for it. One line
    # carries the exemption and one mutation can revert both.
    needed_gitlinks = set(gitlinks) - set(task.submodules_unneeded)

    declared: dict[str, dict[str, str]] = {}
    if gitlinks or _has_gitmodules(mirror, task.base_sha):
        listing = _git("config", "--blob", f"{task.base_sha}:.gitmodules",
                       "--list", "-z", cwd=mirror, check=False)
        if listing.returncode != 0 and needed_gitlinks:
            raise TaskError(
                f"{task.task_id}: {task.base_sha} carries gitlinks "
                f"({', '.join(sorted(needed_gitlinks))}) but no readable "
                ".gitmodules, so no url exists to fetch them from and the "
                "directories would arrive empty."
            )
        for record in listing.stdout.split("\0"):
            key, _, value = record.partition("\n")
            if not key.startswith("submodule."):
                continue
            name, _, field = key[len("submodule."):].rpartition(".")
            if field in ("path", "url"):
                declared.setdefault(name, {})[field] = value

    by_path = {v["path"]: (name, v) for name, v in declared.items()
               if "path" in v}
    # ONE-DIRECTIONAL, and the direction is the decision. A gitlink with no
    # url has nothing to fetch from and leaves a directory `git status
    # --porcelain` reports as CLEAN -- quiet, and fatal. The reverse is inert:
    # measured 2026-09-01, a `.gitmodules` entry naming a path with no gitlink
    # is not listed by `git submodule status`, is not fetched by `update
    # --init` (exit 0) and creates no directory, because git drives everything
    # off the index. Refusing it would refuse a real, working shape -- a
    # submodule `git rm --cached`'d with its stanza left behind. Preflight
    # records those names as `submodules_orphaned` instead -- a key with no
    # producer until Task 4 computes it inside the container, where it is an
    # observation of the tree rather than a re-derivation on the host.
    unfetchable = sorted(needed_gitlinks - set(by_path))
    if unfetchable:
        raise TaskError(
            f"{task.task_id}: the tree at {task.base_sha} carries gitlinks "
            f"with no .gitmodules url: {', '.join(unfetchable)}. There is "
            "nothing to fetch them from, so each directory would arrive empty "
            "-- and an empty submodule directory leaves `git status "
            "--porcelain` clean, so nothing downstream would say so."
        )

    # `by_path.get(path, (path, {}))` is what lets a DECLARED-UNNEEDED gitlink
    # construct an entry with no stanza at all, and even with no readable
    # `.gitmodules`: `name` falls back to the path and `url` to `""`. That
    # combination is unreachable for a needed submodule, because the two
    # refusals above rule it out.
    unneeded = set(task.submodules_unneeded)
    subs = tuple(
        Submodule(name=by_path.get(path, (path, {}))[0], path=path,
                  url=by_path.get(path, (path, {}))[1].get("url", ""), sha=sha,
                  declared_unneeded=path in unneeded)
        for path, sha in sorted(gitlinks.items())
    )
    _refuse_submodule_conflicts(task, subs, mirrors={})
    return subs


def _has_gitmodules(mirror: Path, sha: str) -> bool:
    return _git("cat-file", "-e", f"{sha}:.gitmodules", cwd=mirror,
                check=False).returncode == 0


def _refuse_submodule_conflicts(task: TaskManifest, subs: tuple[Submodule, ...],
                                mirrors: dict[str, Path]) -> None:
    """The four refusals that are not about the two readers agreeing -- two of
    which a `submodules_unneeded` declaration skips, and two of which it does
    NOT.

    SKIPPED for a declared path: the url check (nothing is fetched for it, so
    no url is needed) and the nested-submodule check (no pruned mirror is
    built for it, so `mirrors.get` returns `None` and the check already
    no-ops; the guard makes that explicit rather than leaving it to the
    coincidence).

    KEPT for a declared path, and both are OUTSIDE the guard on purpose:

    - a `strip_paths` entry covering the path. Unrelated to population --
      `_strip_paths_from_tree`'s `git rm -r` still removes the gitlink, still
      leaves `.gitmodules` naming a path that no longer exists, and still
      moves `start_sha` while doing it.
    - a reference diff touching the path. `git add -A` stages NOTHING for a
      gitlink path in either state, so a task whose fix lives there is
      ungradable by construction however the manifest declares it.

    `mutation_check.py` anchors the PLACEMENT of both, not only the guard: the
    edit a later editor reaches for is tidying them inside it, which would
    silently drop the two refusals the key promises to keep.

    `mirrors` maps a submodule path to its pruned mirror, and is `{}` wherever
    no mirror exists yet -- `derive_submodules` runs with no cache root, and
    `images.build_task_image` reaches it through `task_submodules`, which has
    none either. So the one refusal that needs a
    clone (nested submodules) is skipped there and runs again from
    `_init_submodules`, which calls this function a SECOND time with the real
    mapping once the mirrors exist -- where a clone was going to happen anyway.
    The second call is a strict superset of the first: the cheap refusals run
    wherever the derivation runs, and re-running them costs three comparisons
    over tuples already in hand. One function owning every refusal beats
    splitting them across two so that a reader has to know which half ran
    where.

    Four refusals here, not five: the gitlink-with-no-url check is
    `derive_submodules`' own, because it is a property of the pair of readers
    rather than of one entry.

    Each is a shape that would otherwise fail LATE and describe the wrong
    thing: a bad url as `git submodule update`'s clone error, a nested
    submodule as an empty directory one level further down, a strip as a
    `git rm` against a path the manifest never meant, and a reference diff
    touching submodule content as a preflight green-after that passes on a fix
    no submission diff can contain (measured 2026-09-01: plain `git apply`
    edits inside a submodule at exit 0, and `git add -A` then stages NOTHING
    for it -- the checkpoint diff is zero bytes).
    """
    for sub in subs:
        # NOT a `continue` at the top of the loop. That is the shape a later
        # editor reaches for, and it would silently drop the two refusals
        # below -- which are the two this key promises to keep.
        if not sub.declared_unneeded:
            # `no url` rather than `url ''`: an empty value is what a
            # `.gitmodules` stanza carrying a `path` and nothing else yields,
            # and quoting the empty string reads as a url that is present and
            # strange rather than as a field the author never wrote.
            stated = f"url {sub.url!r}" if sub.url else "no url"
            if not sub.url.startswith(_SUBMODULE_URL_PREFIX):
                raise TaskError(
                    f"{task.task_id}: submodule {sub.path} declares {stated}; "
                    f"only {_SUBMODULE_URL_PREFIX} urls can be fetched by this "
                    "eval. A relative url resolves against a remote the run "
                    "tree does not have, and ssh/file urls cannot be fetched "
                    "at all."
                )
            # FIRST after the url check, and the only refusal that needs an
            # artifact rather than a comparison -- hence `mirrors`, and hence
            # the skip wherever no mirror exists.
            mirror_for = mirrors.get(sub.path)
            if mirror_for is not None and _has_gitmodules(mirror_for, sub.sha):
                raise TaskError(
                    f"{task.task_id}: submodule {sub.path} at {sub.sha} "
                    "declares submodules of its own. `git submodule update "
                    "--init` does not recurse, so the inner directory would "
                    "arrive empty -- and an empty submodule directory leaves "
                    "`git status --porcelain` clean, so nothing downstream "
                    "would say so."
                )
        # BOTH DIRECTIONS, and the ancestor one is the half that used to fall
        # through. `_under` is `PurePosixPath.is_relative_to`, so the first
        # conjunct already matches the path itself and an `or p == sub.path`
        # would be dead; the second is a strip that covers the submodule from
        # ABOVE (`strip_paths: ["vendor"]`, gitlink `vendor/libdep`). That
        # shape was accepted, and it does not fail late in a way that names
        # the task: `_strip_paths_from_tree`'s `git rm -r` removes the
        # gitlink, so `_init_submodules`' `git submodule update --init` writes
        # nothing and the very next command runs with `cwd=dest/vendor/libdep`
        # -- a directory that does not exist. That is a bare `FileNotFoundError`
        # out of `subprocess.run`, not a `TaskError` carrying `task_id`, which
        # is the loader refusing a manifest wearing the costume of a harness
        # crash.
        stripped = [p for p in task.strip_paths
                    if _under(p, (sub.path,)) or _under(sub.path, (p,))]
        if stripped:
            raise TaskError(
                f"{task.task_id}: strip_paths names {', '.join(stripped)}, "
                f"which covers the submodule {sub.path} from above or below. "
                "The strip runs against a start state where the submodule is "
                "not initialised, so it would remove the gitlink and leave "
                ".gitmodules naming a path that no longer exists."
            )
        touched = [p for p in (*task.test_files, *task.solution_files,
                               *task.extra_files)
                   if _under(p, (sub.path,))]
        if touched:
            raise TaskError(
                f"{task.task_id}: the reference diff touches "
                f"{', '.join(sorted(touched))}, which is at or under the "
                f"submodule {sub.path}. A submission diff cannot carry an edit "
                "inside a submodule -- `git add -A` stages nothing for it -- so "
                "a task whose fix lives there is ungradable by construction."
            )


def task_submodules(task: TaskManifest, cache_root: Path) -> tuple[Submodule, ...]:
    """`ensure_mirror` then `derive_submodules`. THE entry point.

    One caller, which does not hold a mirror at the point it needs the answer:
    `images.build_task_image`. `materialize` deliberately does NOT come
    through here -- it already holds the PRUNED superproject mirror, which
    carries `base_sha`'s history and answers both reads, so it calls
    `derive_submodules` against that and takes no second trip through
    `ensure_mirror`.

    `grader.grade_run` was the second caller and is NOT one any more. Its
    gitlink refusal reads the submission's own chunks instead, because the
    DECLARED set is empty for every task in today's corpus while an agent can
    create a gitlink with `git init` in any tracked subdirectory of any of
    them -- so the declared set was the wrong authority, and asking for it
    cost the grader a mirror clone per record for a question the diff already
    answers.

    Deriving per caller costs two git reads against a warm cache; threading
    one value through three signatures would make each caller depend on a
    neighbour having run the refusals in `derive_submodules`, which is the
    failure this module's every other check is shaped to avoid.
    """
    mirror = ensure_mirror(task.repo_url, task.base_sha, cache_root)
    return derive_submodules(task, mirror)


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


def _init_submodules(task: TaskManifest, dest: Path,
                     subs: tuple[Submodule, ...], cache_root: Path) -> None:
    """Populate every submodule from a PRUNED mirror, with no network.

    Three things are load-bearing and each was measured on 2026-09-01 against
    git 2.50.1.

    THE MIRROR IS PRUNED. `git submodule update --init` against the declared
    url clones the submodule's whole history, `remotes/origin/main` included,
    so the run tree would carry submodule content NEWER than the gitlink --
    `ensure_pruned_mirror`'s founding leak, one level down, and differential in
    the same way, since only an arm that looks inside `vendor/.../.git`
    collects it. `ensure_pruned_mirror` is reused UNCHANGED: it is already
    generic over (url, sha), so the submodule gets its own cache entry, its own
    repo lock, and its own `_verify_pruned` post-condition. Do not add a term
    to `_pack_fingerprint` for this; see HANDOFF.md.

    THE REFLOG EXPIRE IS A LEAK GUARD. `logs/HEAD` and `logs/refs/heads/main`
    both record `clone: from <host cache path>`, and nothing else removes them
    -- the config files are clean without it, so a check that greps only those
    passes with the leak in place. It runs `check=True`.

    THE URL IS PERSISTED, THEN REWRITTEN. A transient `-c submodule.<n>.url=`
    populates the tree but leaves `git submodule status` reading `-<sha>` --
    uninitialised -- because `submodule init` skips its registration step when
    the value is already visible, so `.git/config` never gets it. Preflight
    reads exactly that character. The value is then rewritten to the
    `.gitmodules` url, and `origin` removed from the submodule, for the same
    reason `materialize` removes the superproject's: a host cache path inside
    the container is both an unresolvable error surface for the agent and a
    leak of the operator's cache layout.

    That first sentence is stated as the reason for the shape and is NOT
    independently anchored, which is worth saying rather than leaving for the
    next reader to discover. Measured 2026-09-01 against this function: with
    the trailing rewrite in place, a transient `-c` reaches the SAME end state
    -- the rewrite is itself a `git config` write, so it performs the
    registration `submodule init` skipped, and `git submodule status` reads a
    leading space either way. The measurement the sentence comes from was
    taken with no trailing write. The persisted form is kept because it puts
    the url git clones from and the url the run tree carries in one place, in
    that order; it is a legibility choice here, not a behavioural one.

    `protocol.file.allow=always` stays TRANSIENT and is REQUIRED in production,
    not only in tests: git has refused the file transport for submodules since
    the CVE-2022-39253 hardening, and a local pruned mirror is a file
    transport. Without it the update exits 1 with `transport 'file' not
    allowed`.

    The post-condition is the point of the function. An empty submodule
    directory leaves `git status --porcelain` EMPTY -- byte-identical to a
    healthy tree -- so a silent failure here reads as a suite that cannot
    import, on every arm, and is scored as capability. Its existence-and-
    non-empty half runs IMMEDIATELY after the update and before anything
    takes `dest/sub.path` as a working directory, so a directory that was
    never written is a `TaskError` naming the task rather than a
    `FileNotFoundError` out of `subprocess.run`; the HEAD comparison stays
    below, where it has a repository to ask.

    A submodule the manifest declared UNNEEDED is skipped here entirely, and
    the `if not needed: return` above the mirror comprehension is what makes
    "no pruned mirror is built for it" a property of the code rather than of
    an empty comprehension. Its url is never read, so an ssh or relative url
    costs nothing; its directory stays as `git checkout` left it, which is
    present and empty (measured 2026-09-02, git 2.50.1: `git clean -xfd` does
    not remove it, `git status --porcelain` reports the tree clean, and `git
    submodule status` reads `-<sha>`). The refusals still run over the FULL
    tuple, because two of them -- a strip covering the path, and a reference
    diff touching it -- are unrelated to whether the directory is populated.
    """
    if not subs:
        return
    needed = tuple(sub for sub in subs if not sub.declared_unneeded)
    if not needed:
        return
    # `materialize`'s own `_repo_lock` critical section has long exited by the
    # time this runs (it wraps the clone only), so `ensure_pruned_mirror`'s
    # acquisitions here are never nested inside it -- which flock would punish
    # with a same-process deadlock on the same slug.
    mirrors = {
        sub.path: ensure_pruned_mirror(sub.url, sub.sha, cache_root)
        for sub in needed
    }
    _refuse_submodule_conflicts(task, subs, mirrors=mirrors)
    for sub in needed:
        key = f"submodule.{sub.name}.url"
        _git("config", key, str(mirrors[sub.path]), cwd=dest)
        _git("-c", "protocol.file.allow=always",
             "submodule", "update", "--init", "--", sub.path, cwd=dest)
        _git("config", key, sub.url, cwd=dest)

        # BEFORE the two commands that run with `cwd=dest/sub.path`, and that
        # order is the whole point. Everything below this guard -- `remote
        # remove`, `reflog expire`, the HEAD check -- names the submodule
        # directory as its working directory, and `subprocess.run` against a
        # cwd that does not exist raises `FileNotFoundError` from the C
        # library, not `TaskError`. So any residual way of reaching this loop
        # with no directory (the loader's strip refusal is the one measured
        # shape, but it is not the only way an `update --init` can write
        # nothing) used to surface as a bare OSError naming a path, with no
        # task_id and nothing saying which of a task set's manifests was at
        # fault. `iterdir()` raises the same way, so the emptiness half has to
        # move with the existence half rather than stay below.
        checked = dest / sub.path
        if not checked.is_dir() or not any(checked.iterdir()):
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} is missing or empty "
                "after initialisation. An empty submodule directory leaves "
                "`git status --porcelain` clean, so the suite would simply "
                "fail to collect and every arm would be scored on an "
                "environment defect."
            )

        _git("remote", "remove", "origin", cwd=dest / sub.path, check=False)
        # check=True (the default). This is a LEAK GUARD, not tidiness:
        # measured 2026-09-01, the submodule's `logs/HEAD` AND
        # `logs/refs/heads/main` each carry `clone: from <host cache path>`
        # after the two rewrites above -- the pruned mirror publishes
        # `refs/heads/main`, so the clone creates a local branch and logs the
        # source twice -- and this expire is the only thing that removes them.
        # `.git/modules/<name>/config` is clean either way, which is why a
        # config-only check sees nothing.
        _git("reflog", "expire", "--expire=now", "--all", cwd=dest / sub.path)

        head = _git("rev-parse", "HEAD", cwd=checked, check=False)
        if head.returncode != 0 or head.stdout.strip() != sub.sha:
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} is not at its gitlink "
                f"{sub.sha} after initialisation (got "
                f"{head.stdout.strip() or 'nothing'}). An empty or wrong "
                "submodule leaves `git status --porcelain` clean, so the suite "
                "would simply fail to collect and every arm would be scored on "
                "an environment defect."
            )


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

    Submodules are initialised LAST, after `start_sha` is settled and compared
    against its pin, and each from its own PRUNED mirror rather than from the
    url `.gitmodules` declares (spec section 5.1; see `_init_submodules`). The
    ordering is the guarantee, not an observation about what `git add -A`
    stages: the tree `start_sha` is computed over has never seen a submodule
    checkout, so initialising cannot move it. Deriving here rather than through
    `task_submodules` reuses the pruned mirror this function already holds --
    it carries `base_sha`'s history, so both reads answer from it and no second
    lock is taken.
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
    # LAST. `start_sha` is computed from a tree that has never seen a submodule
    # checkout, which is what makes "initialising cannot move the pin" a
    # property of the ordering rather than a coincidence about what `git add -A`
    # happens to stage. Failing fast on a moved start state also avoids paying a
    # clone per submodule for a task that is already refused. Measured: `git
    # clean -xfd` above does NOT wipe an initialised submodule (that needs
    # `-ffd`), so the order is safe in the other direction too.
    _init_submodules(task, dest, derive_submodules(task, mirror), cache_root)
    return start_sha
