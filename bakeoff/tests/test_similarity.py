"""Unit tests for the deterministic similarity context.

Two halves. The first is ordinary parsing: paths, symbols and changed-line
counts read out of unified diffs, including the shapes that read wrongly under
a naive scan -- a deletion whose `+++` side is `/dev/null`, a rename that
carries no hunk at all, and a hunk BODY line that looks exactly like a file
header because the file under diff is itself a patch.

The second half is the one the spec asks for by name (§4.2.1): a correct fix
sharing no file with the reference must come out as an empty `common` and
nothing else -- no penalty, no ranking, no number. The structural assertions
(`score`, `__lt__`) are there because the failure mode is a later edit, not a
wrong result today.
"""

from __future__ import annotations

import json

from bakeoff.similarity import (
    SimilarityContext,
    changed_line_count,
    diff_files,
    diff_symbols,
    similarity_context,
)

CANDIDATE_CALC = (
    "diff --git a/calc.py b/calc.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@ def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)

REFERENCE_TOTALS = (
    "diff --git a/math_utils.py b/math_utils.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/math_utils.py\n"
    "+++ b/math_utils.py\n"
    "@@ -10,3 +10,3 @@ def total(values):\n"
    "-    return sum(values) - 1\n"
    "+    return sum(values)\n"
)

# Both sides touch the changelog `allow_extra_paths` excluded from the task,
# and they touch it by different amounts, so dropping it moves the ratio.
CANDIDATE_WITH_CHANGELOG = CANDIDATE_CALC + (
    "diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
    "index 3333333..4444444 100644\n"
    "--- a/CHANGELOG.md\n"
    "+++ b/CHANGELOG.md\n"
    "@@ -1,2 +1,5 @@ # Changelog\n"
    "+- fixed add\n"
    "+- tidied imports\n"
    "+- bumped version\n"
)

REFERENCE_WITH_CHANGELOG = (
    "diff --git a/calc.py b/calc.py\n"
    "index 5555555..6666666 100644\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@ def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
    "diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
    "index 7777777..8888888 100644\n"
    "--- a/CHANGELOG.md\n"
    "+++ b/CHANGELOG.md\n"
    "@@ -1,2 +1,3 @@ # Changelog\n"
    "+- fixed add\n"
)

RENAME_ONLY = (
    "diff --git a/old_name.py b/new_name.py\n"
    "similarity index 100%\n"
    "rename from old_name.py\n"
    "rename to new_name.py\n"
)

TWO_HUNKS_ONE_SYMBOL = (
    "diff --git a/svc.py b/svc.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/svc.py\n"
    "+++ b/svc.py\n"
    "@@ -10,4 +10,4 @@ def foo(self, request):\n"
    "-    return None\n"
    "+    return self.handle(request)\n"
    "@@ -40,3 +40,4 @@ def foo(self, request):\n"
    "-    pass\n"
    "+    raise NotImplementedError\n"
    "+    # unreachable\n"
    "@@ -60,2 +60,2 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)

DELETED_FILE = (
    "diff --git a/gone.py b/gone.py\n"
    "deleted file mode 100644\n"
    "index 1111111..0000000\n"
    "--- a/gone.py\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    '-print("x")\n'
    '-print("y")\n'
)

ADDED_FILE = (
    "diff --git a/new.py b/new.py\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/new.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+import os\n"
    "+\n"
    "+print(os.getcwd())\n"
)

# One added line carrying a form feed, then a second. Under `splitlines` each
# becomes TWO lines and the halves read as a file header and a hunk header.
FORM_FEED_BODY = (
    "diff --git a/notes.txt b/notes.txt\n"
    "index 1111111..2222222 100644\n"
    "--- a/notes.txt\n"
    "+++ b/notes.txt\n"
    "@@ -1 +1,2 @@ def real():\n"
    " keep\n"
    "+\x0c+++ b/ghost.py\n"
    "+\x0c@@ -1 +1 @@ def ghost():\n"
)

# A diff of a file that is itself a patch. Every line after the hunk header is
# CONTENT that happens to look like a diff header.
PATCH_FIXTURE_EDIT = (
    "diff --git a/fixtures/sample.patch b/fixtures/sample.patch\n"
    "index 1111111..2222222 100644\n"
    "--- a/fixtures/sample.patch\n"
    "+++ b/fixtures/sample.patch\n"
    "@@ -1,3 +1,4 @@ context line\n"
    "+diff --git a/decoy.py b/decoy.py\n"
    "+--- a/decoy.py\n"
    "+++ b/decoy.py\n"
    "+@@ -1 +1 @@\n"
)


# Git terminates a `---`/`+++` path with a TAB when the path contains a space
# and needs no C-quoting. Verified against git 2.50.1 (Apple Git-155) on a
# throwaway repo: `--- a/CHANGES 3360.rst\t`, and the `diff --git` line carries
# no such terminator. The TAB is what an `allow_extra_paths` changelog named
# `CHANGES 3360.rst` arrives with, and it never equals the manifest's path.
SPACE_IN_PATH = (
    "diff --git a/CHANGES 3360.rst b/CHANGES 3360.rst\n"
    "index 1111111..2222222 100644\n"
    "--- a/CHANGES 3360.rst\t\n"
    "+++ b/CHANGES 3360.rst\t\n"
    "@@ -1,2 +1,4 @@\n"
    "+- fixed the pager\n"
    "+- bumped version\n"
)

# Git C-quotes a path holding a non-ASCII byte, a control character, a quote or
# a backslash, and the quotes wrap the `a/`/`b/` prefix too. Verified against
# git 2.50.1: `café.py` -> `"a/caf\303\251.py"` (octal, exactly three digits,
# one escape per UTF-8 byte) under the default `core.quotepath=true`.
QUOTED_NON_ASCII = (
    'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
    "index 1111111..2222222 100644\n"
    '--- "a/caf\\303\\251.py"\n'
    '+++ "b/caf\\303\\251.py"\n'
    "@@ -1,2 +1,3 @@\n"
    "+accent\n"
)

# Both at once: a path with a space AND a non-ASCII byte is quoted AND TAB
# terminated. Verified against git 2.50.1 -- `--- "a/caf\303\251 x.py"\t` --
# which is why the terminator has to come off before the unquote, not after.
QUOTED_AND_TAB_TERMINATED = (
    'diff --git "a/caf\\303\\251 x.py" "b/caf\\303\\251 x.py"\n'
    "index 1111111..2222222 100644\n"
    '--- "a/caf\\303\\251 x.py"\t\n'
    '+++ "b/caf\\303\\251 x.py"\t\n'
    "@@ -1,2 +1,3 @@\n"
    "+accent\n"
)

# A mode change carries no `---`/`+++` pair at all, so the `diff --git` line is
# the only path source -- and this path contains ` b/`, so every split of the
# line is a candidate. Verified against git 2.50.1: chmod +x on `we b/ird.py`
# yields exactly this header plus `old mode`/`new mode`.
MODE_CHANGE_AMBIGUOUS = (
    "diff --git a/we b/ird.py b/we b/ird.py\n"
    "old mode 100644\n"
    "new mode 100755\n"
)

# Git quotes per side, not per line. Verified against git 2.50.1: renaming
# `café.py` to `plain.py` yields one quoted endpoint and one bare one.
RENAME_MIXED_QUOTING = (
    'diff --git "a/caf\\303\\251.py" b/plain.py\n'
    "similarity index 100%\n"
    'rename from "caf\\303\\251.py"\n'
    "rename to plain.py\n"
)


def test_a_tab_terminated_path_is_dropped_when_the_manifest_names_it():
    # `tasks.py` records `extra_files` from `git apply --numstat -z`, which
    # emits raw paths -- no quoting, no terminator. A drop filter comparing
    # against the header verbatim never matches, and the excluded changelog
    # goes on inflating both facts it was recorded to be removed from.
    assert diff_files(SPACE_IN_PATH) == ("CHANGES 3360.rst",)

    dropped = similarity_context(
        CANDIDATE_CALC + SPACE_IN_PATH,
        REFERENCE_WITH_CHANGELOG,
        drop_paths=("CHANGES 3360.rst", "CHANGELOG.md"),
    )
    assert dropped.file_overlap.common == ("calc.py",)
    assert dropped.file_overlap.candidate_only == ()
    assert dropped.diff_size_ratio == 1.0


def test_a_c_quoted_path_is_unquoted_to_the_bytes_git_plumbing_reports():
    assert diff_files(QUOTED_NON_ASCII) == ("café.py",)
    assert diff_files(QUOTED_AND_TAB_TERMINATED) == ("café x.py",)

    dropped = similarity_context(
        CANDIDATE_CALC + QUOTED_NON_ASCII,
        CANDIDATE_CALC,
        drop_paths=("café.py",),
    )
    assert dropped.file_overlap.common == ("calc.py",)
    assert dropped.file_overlap.candidate_only == ()
    assert dropped.diff_size_ratio == 1.0


def test_every_c_escape_git_emits_round_trips():
    for encoded, decoded in (
        (r"be\all.py", "be\all.py"),
        (r"bac\bk.py", "bac\bk.py"),
        (r"f\ff.py", "f\ff.py"),
        (r"n\nl.py", "n\nl.py"),
        (r"c\rr.py", "c\rr.py"),
        (r"pa\tth.py", "pa\tth.py"),
        (r"v\vt.py", "v\vt.py"),
        (r"esc\033.py", "esc\x1b.py"),
        (r"del\177.py", "del\x7f.py"),
        (r"back\\slash.py", "back\\slash.py"),
        ('qu\\"ote.py', 'qu"ote.py'),
    ):
        chunk = f'diff --git "a/{encoded}" "b/{encoded}"\n--- "a/{encoded}"\n+++ "b/{encoded}"\n@@ -1 +1 @@\n+x\n'
        assert diff_files(chunk) == (decoded,), encoded


def test_a_quoted_form_that_is_not_gits_output_degrades_instead_of_guessing():
    # An unterminated escape and a two-digit octal are shapes `quote_c_style`
    # never emits. Reporting a half-decoded path would be a wrong path with the
    # provenance of a right one, so the raw string is kept -- it matches no
    # manifest entry, which is the same visible outcome as an unknown file.
    for encoded in ("trailing\\", r"sh\77rt.py", r"unknown\qescape.py"):
        chunk = f'diff --git "a/{encoded}" "b/{encoded}"\n--- "a/{encoded}"\n+++ "b/{encoded}"\n@@ -1 +1 @@\n+x\n'
        assert diff_files(chunk) == (f'"b/{encoded}"',), encoded


def test_a_mode_change_whose_path_contains_b_slash_resolves_both_endpoints():
    # Greedy `^diff --git a/(.+) b/(.+)$` splits on the LAST ` b/` and reports
    # ("we b/ird.py b/we", "ird.py") -- two paths, neither of which exists.
    assert diff_files(MODE_CHANGE_AMBIGUOUS) == ("we b/ird.py",)

    dropped = similarity_context(
        CANDIDATE_CALC + MODE_CHANGE_AMBIGUOUS,
        CANDIDATE_CALC,
        drop_paths=("we b/ird.py",),
    )
    assert dropped.file_overlap.common == ("calc.py",)
    assert dropped.file_overlap.candidate_only == ()


def test_a_diff_git_line_quoted_on_one_side_only_still_names_both_endpoints():
    assert diff_files(RENAME_MIXED_QUOTING) == ("plain.py",)

    dropped = similarity_context(
        CANDIDATE_CALC + RENAME_MIXED_QUOTING,
        CANDIDATE_CALC,
        drop_paths=("café.py",),
    )
    # The manifest recorded the rename SOURCE; the chunk goes anyway, because a
    # rename touched both names.
    assert dropped.file_overlap.common == ("calc.py",)
    assert dropped.file_overlap.candidate_only == ()


def test_a_correct_fix_with_zero_file_overlap_produces_empty_common_and_carries_no_penalty():
    context = similarity_context(CANDIDATE_CALC, REFERENCE_TOTALS)

    assert context.file_overlap.common == ()
    assert context.file_overlap.candidate_only == ("calc.py",)
    assert context.file_overlap.reference_only == ("math_utils.py",)
    assert context.symbol_overlap.common == ()
    assert context.symbol_overlap.candidate_only == ("def add(a, b):",)
    assert context.symbol_overlap.reference_only == ("def total(values):",)

    # The whole point of the unit (§4.2.1): three facts, no verdict. If either
    # of these ever holds, someone has started summing context into a score.
    assert not hasattr(context, "score")
    assert "__lt__" not in SimilarityContext.__dict__


def test_extra_files_are_dropped_from_both_sides_before_overlap():
    kept = similarity_context(
        CANDIDATE_WITH_CHANGELOG, REFERENCE_WITH_CHANGELOG
    )
    assert kept.file_overlap.common == ("CHANGELOG.md", "calc.py")
    assert kept.diff_size_ratio == 5 / 3

    dropped = similarity_context(
        CANDIDATE_WITH_CHANGELOG,
        REFERENCE_WITH_CHANGELOG,
        drop_paths=("CHANGELOG.md",),
    )
    assert dropped.file_overlap.common == ("calc.py",)
    assert dropped.file_overlap.candidate_only == ()
    assert dropped.file_overlap.reference_only == ()
    assert "# Changelog" not in dropped.symbol_overlap.common
    # 2 changed lines each once the changelog's 3 and 1 are gone, not 5/3.
    assert dropped.diff_size_ratio == 1.0


def test_diff_size_ratio_is_none_when_the_reference_changes_no_lines():
    context = similarity_context(CANDIDATE_CALC, RENAME_ONLY)

    assert changed_line_count(RENAME_ONLY) == 0
    # None, never `inf` and never a fabricated 0.0 -- an undefined ratio has to
    # read as undefined to a judge.
    assert context.diff_size_ratio is None


def test_symbols_come_from_hunk_headers_and_dedupe():
    # Two hunks in the same function, plus one hunk git gave no context at all.
    assert diff_symbols(TWO_HUNKS_ONE_SYMBOL) == ("def foo(self, request):",)


def test_deleted_file_paths_are_still_counted():
    assert diff_files(DELETED_FILE) == ("gone.py",)
    assert changed_line_count(DELETED_FILE) == 2


def test_changed_line_count_excludes_file_headers():
    # `--- /dev/null` and `+++ b/new.py` both start with the change markers; a
    # naive count reports 5 here and 4 on the deletion.
    assert changed_line_count(ADDED_FILE) == 3
    assert changed_line_count(DELETED_FILE) == 2
    assert changed_line_count(ADDED_FILE + DELETED_FILE) == 5


def test_a_hunk_body_line_that_looks_like_a_file_header_is_not_read_as_a_path():
    assert diff_files(PATCH_FIXTURE_EDIT) == ("fixtures/sample.patch",)
    assert changed_line_count(PATCH_FIXTURE_EDIT) == 4
    assert diff_symbols(PATCH_FIXTURE_EDIT) == ("context line",)


def test_a_form_feed_in_a_hunk_body_does_not_manufacture_a_header_or_a_symbol():
    # Measured: `splitlines` splits both added lines and yields
    # `+++ b/ghost.py` and `@@ -1 +1 @@ def ghost():` as if they were headers.
    assert "+++ b/ghost.py" in FORM_FEED_BODY.splitlines()

    assert diff_files(FORM_FEED_BODY) == ("notes.txt",)
    assert diff_symbols(FORM_FEED_BODY) == ("def real():",)
    assert changed_line_count(FORM_FEED_BODY) == 2


def test_a_rename_with_no_content_change_still_names_its_destination_file():
    assert diff_files(RENAME_ONLY) == ("new_name.py",)
    assert diff_symbols(RENAME_ONLY) == ()


def test_an_empty_diff_produces_no_files_no_symbols_and_no_changed_lines():
    assert diff_files("") == ()
    assert diff_symbols("") == ()
    assert changed_line_count("") == 0

    context = similarity_context("", "")
    assert context.file_overlap.common == ()
    assert context.file_overlap.candidate_only == ()
    assert context.file_overlap.reference_only == ()
    assert context.diff_size_ratio is None


def test_to_dict_survives_a_json_round_trip_unchanged():
    context = similarity_context(CANDIDATE_WITH_CHANGELOG, REFERENCE_WITH_CHANGELOG)
    payload = context.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["file_overlap"]["common"] == ["CHANGELOG.md", "calc.py"]
    assert "score" not in payload
