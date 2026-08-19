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
