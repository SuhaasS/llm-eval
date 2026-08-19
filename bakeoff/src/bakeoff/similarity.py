"""Three facts about a candidate diff against the reference, and no verdict.

See `docs/superpowers/specs/2026-08-18-judge-design.md`, section
"`similarity.py` -- deterministic context, explicitly not a score".

§4.2.1 computes three things and hands them to the judge as CONTEXT: which
files both diffs touch, which symbols both diffs touch, and how the two change
sizes compare. Similarity to the reference is not correctness -- valid
solutions legitimately differ -- so these inform judgment and never contribute
to a score.

The failure mode this module is shaped against is a later edit, not a wrong
answer today: someone sums the three into a number, and a correct fix that
happened to touch a different file starts losing to a wrong one that touched
the right file. `SimilarityContext` therefore carries no `score` field, no
`__lt__` and no aggregate -- three independent fields, handed over as three
fields. `@dataclass(frozen=True)` without `order=True` is what keeps `<` from
existing; adding `order=True` would make the whole structure sortable and is
the one-word version of the same mistake.

Pure functions over two strings. No subprocess, no git, no filesystem, so this
is the only part of the judge that is verifiable without a model call -- which
is the reason it lives here rather than inside `judge.py`.

Two parsing hazards, both of which read as ordinary output when got wrong:

* `+++`/`---` and `@@` are not markers a diff reserves. A diff of a file that
  is itself a patch has hunk BODY lines reading `+++ b/decoy.py` and
  `+@@ -1 +1 @@`. Only the lines BEFORE a chunk's first `@@` are file headers,
  and only lines after it are changed lines, so both are read positionally
  rather than by prefix. Read by prefix, a patch fixture reports a phantom path
  and inflates its own changed-line count.
* `split("\\n")`, NEVER `splitlines()`, for the reason `tasks.diff_chunks`
  documents at length: Python also breaks on `\\x0c` and friends, so one
  content line carrying a form feed becomes two, and the second half can look
  like a header. Splitting into chunks is `diff_chunks`' job for exactly that
  reason and is not re-implemented here.

Paths are read lexically off the header lines rather than from git, which is
the price of being pure. Git C-quotes a path containing a control character or
a quote (`+++ "b/pa\\th.py"`), and this module reports that string as written.
The result degrades a CONTEXT SIGNAL on a pathological filename -- the judge
sees a file it cannot match rather than a wrong number -- which is a different
class of harm from a score that moves. `tasks.py` asks git for the paths it
binds to task halves, and that is where exactness is load-bearing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from bakeoff.tasks import diff_chunks

#: `diff --git a/<old> b/<new>`, the only path source a chunk with no hunks
#: has -- a pure rename or a mode change carries no `---`/`+++` pair at all.
#: Unanchored at the end and greedy, so it declines the quoted form
#: (`diff --git "a/x" "b/x"`) rather than guessing at an escaped path.
_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")

#: `@@ -1,4 +1,6 @@ def foo(self):` -- the trailing text is the enclosing
#: symbol git's own hunk-header heuristic guessed, and is empty as often as not.
_HUNK = re.compile(r"^@@ -\S+ \+\S+ @@ ?(.*)$")

_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class OverlapSet:
    """One three-way split of two sets of names, each side sorted and deduped.

    `candidate_only` and `reference_only` are kept apart rather than collapsed
    into a difference count because they answer different questions: the first
    is scope the candidate added, the second is work it may have skipped. A
    single "distance" would answer neither and would invite the sum.
    """

    common: tuple[str, ...]
    candidate_only: tuple[str, ...]
    reference_only: tuple[str, ...]


@dataclass(frozen=True)
class SimilarityContext:
    """Three independent facts about a candidate diff against a reference.

    NO score field, NO `__lt__`, NO aggregate -- §4.2.1: similarity informs
    judgment and never contributes to a score.
    """

    file_overlap: OverlapSet
    symbol_overlap: OverlapSet
    #: `candidate changed lines / reference changed lines`, and `None` when the
    #: reference changes zero lines. `None` rather than `inf` or `0.0`: both of
    #: those are numbers a judge would read as a measurement, and one of them
    #: reads as "the candidate changed nothing", which is the opposite claim.
    diff_size_ratio: float | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, on `GradeRecord.to_dict`'s precedent.

        Tuples become lists so the returned dict equals its own JSON round
        trip; a payload that compares unequal to what was serialized makes
        every downstream equality check quietly wrong.
        """

        def encode(value: Any) -> Any:
            if isinstance(value, (list, tuple)):
                return [encode(v) for v in value]
            if isinstance(value, dict):
                return {k: encode(v) for k, v in value.items()}
            return value

        return {k: encode(v) for k, v in asdict(self).items()}


def diff_files(diff: str) -> tuple[str, ...]:
    """Repo-relative paths the diff touches, sorted and deduped."""
    return _files(diff_chunks(diff))


def diff_symbols(diff: str) -> tuple[str, ...]:
    """Enclosing symbols git named in the hunk headers, sorted and deduped.

    Unqualified by file on purpose: this is a context signal, and `def run` in
    two files reading as one entry costs the judge nothing it can act on. The
    headers are a heuristic of git's, so an empty context is common and is
    dropped rather than recorded as an empty symbol.
    """
    return _symbols(diff_chunks(diff))


def changed_line_count(diff: str) -> int:
    """Added plus removed lines, excluding the `+++`/`---` file headers.

    Excluded positionally -- only lines after a chunk's first `@@` are counted
    -- since a hunk body line may itself begin `+++`. `\\ No newline at end of
    file` begins with a backslash and is correctly not a change.
    """
    return _changed_lines(diff_chunks(diff))


def similarity_context(
    candidate_diff: str,
    reference_diff: str,
    drop_paths: Iterable[str] = (),
) -> SimilarityContext:
    """The three facts, computed after `drop_paths` is removed from both sides.

    `drop_paths` takes `TaskManifest.extra_files` -- the files
    `allow_extra_paths` excluded from both task halves, recorded there (see
    `tasks.py:214`) precisely "so the offline grader can drop them from any
    diff-similarity metric". Dropping happens BEFORE all three facts, not after
    file overlap: a changelog touched by both sides otherwise inflates the
    overlap and the line counts with work neither side was asked to do.

    A chunk is dropped when either of the paths it names is listed, so a
    renamed extra file goes whichever of its two names the manifest recorded.
    """
    dropped = frozenset(drop_paths)
    candidate = _kept_chunks(candidate_diff, dropped)
    reference = _kept_chunks(reference_diff, dropped)

    reference_lines = _changed_lines(reference)
    return SimilarityContext(
        file_overlap=_overlap(_files(candidate), _files(reference)),
        symbol_overlap=_overlap(_symbols(candidate), _symbols(reference)),
        diff_size_ratio=(
            None
            if reference_lines == 0
            else _changed_lines(candidate) / reference_lines
        ),
    )


def _kept_chunks(diff: str, dropped: frozenset[str]) -> list[str]:
    if not dropped:
        return diff_chunks(diff)
    return [
        chunk
        for chunk in diff_chunks(diff)
        if not dropped.intersection(p for p in _chunk_endpoints(chunk) if p)
    ]


def _chunk_endpoints(chunk: str) -> tuple[str | None, str | None]:
    """`(old, new)` for one chunk; `None` for `/dev/null` or for absent.

    Scanning stops at the first `@@`: everything after it is content, and a
    diff of a patch file has content that looks exactly like a header. A chunk
    with no `---`/`+++` pair at all -- a pure rename, a mode change -- falls
    back to its `diff --git` line, since a rename did touch both names.
    """
    old = new = None
    header: re.Match[str] | None = None
    for line in chunk.split("\n"):
        if _HUNK.match(line):
            break
        if line.startswith("--- "):
            old = _header_path(line[4:])
        elif line.startswith("+++ "):
            new = _header_path(line[4:])
        elif header is None and line.startswith("diff --git "):
            header = _DIFF_GIT.match(line)
    if old is None and new is None and header is not None:
        return header.group(1), header.group(2)
    return old, new


def _header_path(value: str) -> str | None:
    """The repo-relative path out of a `--- a/x` / `+++ b/x` header tail."""
    if value == _DEV_NULL:
        return None
    if value[:2] in ("a/", "b/"):
        return value[2:] or None
    return value or None


def _chunk_file(chunk: str) -> str | None:
    """The file a chunk is about: its destination, or its source on a delete.

    A deletion's `+++` side is `/dev/null`, and a deletion still touched that
    file -- reporting nothing there would hide the largest edit a diff can make.
    """
    old, new = _chunk_endpoints(chunk)
    return new or old


def _files(chunks: list[str]) -> tuple[str, ...]:
    return tuple(sorted({path for path in map(_chunk_file, chunks) if path}))


def _symbols(chunks: list[str]) -> tuple[str, ...]:
    found: set[str] = set()
    for chunk in chunks:
        for line in chunk.split("\n"):
            match = _HUNK.match(line)
            symbol = match.group(1).strip() if match else ""
            if symbol:
                found.add(symbol)
    return tuple(sorted(found))


def _changed_lines(chunks: list[str]) -> int:
    count = 0
    for chunk in chunks:
        in_hunk = False
        for line in chunk.split("\n"):
            if _HUNK.match(line):
                in_hunk = True
            elif in_hunk and line[:1] in ("+", "-"):
                count += 1
    return count


def _overlap(
    candidate: tuple[str, ...], reference: tuple[str, ...]
) -> OverlapSet:
    left, right = set(candidate), set(reference)
    return OverlapSet(
        common=tuple(sorted(left & right)),
        candidate_only=tuple(sorted(left - right)),
        reference_only=tuple(sorted(right - left)),
    )
