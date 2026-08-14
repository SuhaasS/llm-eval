"""The task loader, which is the harness's only defence against a bad input.

Every case here is a way a task could be wrong such that the run still
completes and the record still looks ordinary. That is the whole class:
`git checkout --detach` onto a SHA that does not resolve leaves the tree
where it was, `git apply` of a re-cut patch changes what the agent was asked
to do, and a duplicate task_id collides in the event log only at the far end
of a matrix, after the tokens are spent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bakeoff.tasks import (
    TaskError,
    diff_chunks,
    load_task,
    load_task_set,
    materialize,
    split_reference_diff,
    task_set_commit,
)

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
OLD_TEST = "from calc import add\n\n\ndef test_old():\n    assert add(0, 0) == 0\n"
NEW_TEST = (
    "from calc import add\n\n\ndef test_old():\n    assert add(0, 0) == 0\n\n\n"
    "def test_new():\n    assert add(2, 3) == 5\n"
)


def _sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A real git repository with a real bug-fix commit.

    Local rather than remote on purpose: `materialize` clones with
    `--mirror`, which works against a path, so the whole materialization path
    is exercised offline. A test that needed the network would be a test that
    stops running.
    """
    repo = tmp_path / "upstream"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
    (repo / ".gitignore").write_text("__pycache__/\n")
    _sh("git", "init", "-q", cwd=repo)
    _sh("git", "config", "user.email", "t@t.test", cwd=repo)
    _sh("git", "config", "user.name", "t", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "base", cwd=repo)
    base = _sh("git", "rev-parse", "HEAD", cwd=repo)

    (repo / "calc.py").write_text(FIXED)
    (repo / "tests" / "test_calc.py").write_text(NEW_TEST)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "fix", cwd=repo)
    head = _sh("git", "rev-parse", "HEAD", cwd=repo)

    reference = subprocess.run(
        ["git", "diff", base, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    return {"path": repo, "base": base, "head": head, "reference": reference}


def _manifest(**overrides) -> str:
    data = {
        "task_id": "t-001",
        "task_version": 1,
        "url": "REPO",
        "base_sha": "0" * 40,
        "prompt": "fix the bug",
        "paths": '["tests/"]',
        "runner": '["python", "-m", "pytest", "-q"]',
        "f2p": '["tests/test_calc.py::test_new"]',
        "start_sha": "",
    }
    data.update(overrides)
    lines = [
        f"task_id: {data['task_id']}",
        f"task_version: {data['task_version']}",
        "repo:",
        f"  url: {data['url']}",
        f"  base_sha: {data['base_sha']}",
    ]
    if data["start_sha"]:
        lines.append(f"  start_sha: {data['start_sha']}")
    lines += [
        "prompt: |",
        f"  {data['prompt']}",
        "tests:",
        f"  paths: {data['paths']}",
        f"  runner: {data['runner']}",
        f"  f2p: {data['f2p']}",
        "",
    ]
    return "\n".join(lines)


def _write_task(root: Path, upstream, name="t-001", **overrides) -> Path:
    task_dir = root / name
    task_dir.mkdir(parents=True)
    fields = {"url": str(upstream["path"]), "base_sha": upstream["base"]}
    fields.update(overrides)
    (task_dir / "task.yaml").write_text(_manifest(**fields))
    (task_dir / "reference.diff").write_text(upstream["reference"])
    return task_dir


# --- the split ---------------------------------------------------------------


def test_the_reference_diff_is_partitioned_not_filtered(upstream):
    """Both halves together are the whole reference, exactly.

    Two hand-maintained patch files would drift, and a chunk dropped from
    either one is invisible: the agent would start without part of its
    oracle, or the offline grader would compare against a reference that is
    missing part of the fix. A partition can be checked, so it is."""
    test_half, solution_half, files, solution_files, extra_files = (
        split_reference_diff(upstream["reference"], ("tests/",))
    )

    chunks = diff_chunks(upstream["reference"])
    assert len(diff_chunks(test_half)) + len(diff_chunks(solution_half)) == len(chunks)
    assert extra_files == (), "this fixture declares no allow_extra_paths"
    assert solution_files == ("calc.py",)
    assert "tests/test_calc.py" in test_half
    assert "tests/test_calc.py" not in solution_half
    assert "calc.py" in solution_half
    assert files == ("tests/test_calc.py",)


def test_content_before_the_first_header_is_refused():
    """A reference that is not exactly `git diff` output cannot be reproduced,
    and the text would be silently dropped rather than applied."""
    with pytest.raises(TaskError, match="before its first"):
        diff_chunks("commit abc123\nAuthor: someone\n\ndiff --git a/x b/x\n")


def test_a_rename_across_the_boundary_is_refused():
    """`tests/x.py -> src/x.py` is simultaneously the agent's oracle and part
    of the fix. Guessing a half would either hand the agent its own
    submission or delete the test it is measured by."""
    # Real `git diff` output. The synthetic form this test used before -- a
    # `similarity index` line with no `rename from`/`rename to` -- is not a
    # shape git emits, and git rejects it outright ("header lacks filename
    # information"), so the test used to pass only because a hand-rolled greedy
    # regex happened to accept it.
    diff = (
        "diff --git a/tests/x.py b/src/x.py\n"
        "similarity index 100%\n"
        "rename from tests/x.py\n"
        "rename to src/x.py\n"
    )
    with pytest.raises(TaskError, match="across the test/solution boundary"):
        split_reference_diff(diff, ("tests/",))


# --- validation --------------------------------------------------------------


def test_a_task_loads(tmp_path, upstream):
    task_dir = _write_task(tmp_path / "set", upstream)
    task = load_task(task_dir)

    assert task.task_id == "t-001"
    assert task.base_sha == upstream["base"]
    assert task.test_files == ("tests/test_calc.py",)
    assert task.manifest_digest


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"base_sha": "abc123"}, "40-character hex"),
        ({"base_sha": "main"}, "40-character hex"),
        ({"f2p": "[]"}, "f2p is required"),
        ({"prompt": " "}, "prompt is empty"),
    ],
)
def test_a_manifest_that_could_run_the_wrong_thing_is_refused(
    tmp_path, upstream, override, match
):
    """An abbreviated SHA and a branch name both resolve, and both resolve to
    something that can move -- so pinning would be decorative. An empty f2p
    set means the task can never be shown to discriminate, which scores every
    arm on evidence that cannot tell a solved run from an idle one."""
    task_dir = _write_task(tmp_path / "set", upstream, **override)
    with pytest.raises(TaskError, match=match):
        load_task(task_dir)


def test_a_reference_with_no_test_half_is_refused(tmp_path, upstream):
    """Without it the start state carries no failing test, section 3.3's
    "runs tests, sees failures, self-corrects" loop has nothing to run, and
    the arm is scored on one unverified guess -- the Phase 0c failure,
    arriving through the dataset instead of the image."""
    task_dir = _write_task(tmp_path / "set", upstream, paths='["nowhere/"]')
    with pytest.raises(TaskError, match="nothing under"):
        load_task(task_dir)


def test_a_duplicate_task_id_is_refused_at_load(tmp_path, upstream):
    """`run_id` hashes (task_id, model, sample, attempt), so two tasks sharing
    an id collide in the event log -- and `write_run` opens mode "x", so the
    second one's records are refused at the far end of a matrix, after the
    tokens are spent."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="a")
    _write_task(root, upstream, name="b")
    with pytest.raises(TaskError, match="duplicate task_id"):
        load_task_set(root)


def test_an_unknown_task_id_is_refused_rather_than_silently_dropped(
    tmp_path, upstream
):
    """`--tasks typo` running zero cells looks exactly like a matrix that had
    nothing left to do."""
    root = tmp_path / "set"
    _write_task(root, upstream)
    with pytest.raises(TaskError, match="no such task"):
        load_task_set(root, only=["t-002"])


# --- provenance --------------------------------------------------------------


def test_task_set_commit_marks_a_modified_set_dirty(tmp_path, upstream):
    """The `-dirty` marker's only job is to be visible when it should be.

    It was structurally never set at first: `git status --porcelain -- <path>`
    reads the pathspec relative to the cwd, so passing the caller's relative
    path while running inside that directory asked about `set/set`, matched
    nothing, printed nothing and exited 0. A set with every file untracked
    reported clean."""
    root = tmp_path / "set"
    _write_task(root, upstream)
    _sh("git", "init", "-q", cwd=tmp_path)
    _sh("git", "config", "user.email", "t@t.test", cwd=tmp_path)
    _sh("git", "config", "user.name", "t", cwd=tmp_path)
    _sh("git", "add", "-A", cwd=tmp_path)
    _sh("git", "commit", "-q", "-m", "set", cwd=tmp_path)

    assert not task_set_commit(root).endswith("-dirty")

    (root / "t-001" / "task.yaml").write_text(
        (root / "t-001" / "task.yaml").read_text() + "\n# edited\n"
    )
    assert task_set_commit(root).endswith("-dirty")


def test_task_set_commit_is_blank_outside_a_repository(tmp_path, upstream):
    """The same honest blank the field carried before a dataset existed --
    never a guess, and never the harness's own commit."""
    root = tmp_path / "set"
    _write_task(root, upstream)
    assert task_set_commit(root) == ""


# --- materialization ---------------------------------------------------------


def test_materialize_commits_the_test_half_onto_base(tmp_path, upstream):
    """The start state is base PLUS the oracle, and the oracle is COMMITTED.

    Committed rather than left in the worktree so `git diff --cached <start>`
    -- section 5.6's submission -- does not carry the test patch, and so
    `restore_paths` restores the patched tests rather than deleting them."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"

    start = materialize(task, repo, tmp_path / "cache")

    assert start != upstream["base"]
    assert (repo / "tests" / "test_calc.py").read_text() == NEW_TEST
    assert (repo / "calc.py").read_text() == BUGGY, "the fix must NOT be applied"
    assert not _sh("git", "status", "--porcelain", cwd=repo)
    assert _sh("git", "rev-parse", "HEAD", cwd=repo) == start


def test_the_start_state_is_reproducible(tmp_path, upstream):
    """A fixed author, committer, date and message make the setup commit a
    pure function of (base_sha, test half, gitignore_extra). Without that,
    `start_sha` could not be pinned and section 5.1's byte-identical world
    would be an assertion rather than a check."""
    task = load_task(_write_task(tmp_path / "set", upstream))

    first = materialize(task, tmp_path / "a" / "repo", tmp_path / "cache")
    second = materialize(task, tmp_path / "b" / "repo", tmp_path / "cache")

    assert first == second


def test_a_start_state_that_moved_is_refused(tmp_path, upstream):
    """What catches a re-cut patch or an edited manifest. Every record
    already written against the old SHA describes a different task, and
    nothing downstream could tell."""
    task_dir = _write_task(tmp_path / "set", upstream, start_sha="f" * 40)
    task = load_task(task_dir)

    with pytest.raises(TaskError, match="start_sha is pinned"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


def test_an_unresolvable_base_sha_is_refused_before_a_container_starts(
    tmp_path, upstream
):
    """The failure this module exists for. `git checkout --detach` onto a SHA
    the repository does not have leaves the tree exactly where it was, every
    diff after it is taken against a state nobody chose, and the record reads
    as an ordinary quiet run."""
    task_dir = _write_task(tmp_path / "set", upstream, base_sha="a" * 40)
    task = load_task(task_dir)

    with pytest.raises(TaskError, match="does not contain base_sha"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


def test_gitignore_extra_lands_in_the_start_state(tmp_path, upstream):
    """The remedy when a suite dirties the tree. Section 5.6 stages
    everything, so anything the tests drop is in every submission diff and
    diff size measures the interpreter rather than the agent. It goes into
    the setup commit, so it is visible in `start_sha` rather than applied
    invisibly at run time."""
    task_dir = tmp_path / "set" / "t-001"
    _write_task(tmp_path / "set", upstream)
    (task_dir / "task.yaml").write_text(
        (task_dir / "task.yaml").read_text() + 'gitignore_extra: ["*.log"]\n'
    )
    task = load_task(task_dir)
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert "*.log" in (repo / ".gitignore").read_text()
    assert "__pycache__/" in (repo / ".gitignore").read_text(), "upstream's kept"


def test_materialize_refuses_to_reuse_a_tree(tmp_path, upstream):
    """Every run gets its own. Sharing one would let a later sample start
    from an earlier sample's dirty state and report a diff its own agent
    never made."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"
    materialize(task, repo, tmp_path / "cache")

    with pytest.raises(TaskError, match="already exists"):
        materialize(task, repo, tmp_path / "cache")


def test_the_host_path_does_not_travel_into_the_run(tmp_path, upstream):
    """`origin` points at a mirror under the harness's cache, which does not
    exist inside the container: a confusing error surface for the agent, and
    a host path leaked into a run that is supposed to be hermetic."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert _sh("git", "remote", cwd=repo) == ""


def test_a_test_declared_as_both_f2p_and_p2p_is_refused(tmp_path, upstream):
    """Unsatisfiable by any submission: one check requires it to start red,
    the other requires it never to be red, and whichever ran second would
    contradict the first."""
    task_dir = _write_task(tmp_path / "set", upstream)
    (task_dir / "task.yaml").write_text(
        (task_dir / "task.yaml").read_text()
        + '  p2p: ["tests/test_calc.py::test_new"]\n'
    )
    with pytest.raises(TaskError, match="both f2p and p2p"):
        load_task(task_dir)


# --- RC4: the reference diff is parsed by git, not by a regex ----------------
#
# Five drafts of a hand-rolled header parser each shipped a different
# silent-wrong-path bug. Every case below is one of those, plus the three
# raises that replaced them.


def _chunks_of(*headers: str) -> str:
    """A minimal well-formed multi-file diff, one modification per header."""
    body = "index 1111111..2222222 100644\n@@ -1 +1,2 @@\n a\n+b\n"
    return "".join(f"{h}\n{body}" for h in headers)


def test_chunking_is_byte_exact():
    """`test_diff`'s bytes feed the setup commit, so one byte moves `start_sha`
    and breaks its pin. The naive `[l + "\\n" for l in split("\\n")]` re-add is
    off by exactly +1 on a diff that ends with a newline."""
    diff = _chunks_of("diff --git a/src/x.py b/src/x.py")

    assert "".join(diff_chunks(diff)) == diff
    assert "".join(diff_chunks(diff.rstrip("\n"))) == diff.rstrip("\n")


def test_a_form_feed_in_a_hunk_does_not_create_a_phantom_chunk():
    """`str.splitlines()` also breaks on \\v \\f \\x1c \\x1d \\x1e \\x85 \\u2028
    \\u2029. A form feed is ordinary in Emacs-era Python and C sources, and one
    line of git output carrying one becomes TWO -- so a phantom chunk appears
    and half a real test file's diff moves into the fix half. Every in-file
    line counter is fooled identically on both sides, which is why the witness
    has to be git."""
    diff = (
        "diff --git a/tests/t.py b/tests/t.py\n"
        "index 1111111..2222222 100644\n"
        "@@ -1 +1,2 @@\n"
        " a\n"
        "+\x0cdiff --git a/evil.py b/evil.py\n"
    )

    assert len(diff_chunks(diff)) == 1


def test_a_quoted_header_is_not_absorbed():
    """Git C-quotes a header whenever a path holds a byte >= 0x80, a
    backslash, a quote or a control char. A boundary demanding ` b/` misses it
    and the whole file is absorbed into the previous chunk."""
    diff = _chunks_of(
        "diff --git a/src/plain.py b/src/plain.py",
        r'diff --git "a/src/caf\303\251.py" "b/src/caf\303\251.py"',
    )

    assert len(diff_chunks(diff)) == 2


def test_a_no_prefix_reference_is_refused():
    """`git apply`'s -p1 strips a REAL component from a --no-prefix diff.
    Measured on a tree with `src/tests/conftest.py` and `tests/test_main.py`,
    git reports `tests/conftest.py` and `test_main.py` -- so a source file
    becomes the oracle and the oracle becomes part of the fix, with every other
    check here passing. Asserted on THIS message, not on the empty-half one,
    which does not fire on a layout with a nested `tests/`."""
    diff = _chunks_of("diff --git src/tests/conftest.py src/tests/conftest.py")

    with pytest.raises(TaskError, match="no `a/` prefix"):
        diff_chunks(diff)


@pytest.mark.parametrize("prefix", ["diff --cc ", "diff --combined "])
def test_a_combined_diff_header_is_refused(prefix):
    """Two spellings from one call site: `--cc` when the output is dense (the
    `git show` default on a merge) and `--combined` when it is not.

    Measured: mixed after a real chunk, git's own parser silently ignores the
    combined patch and still reports exactly one entry -- so the per-chunk
    entry check does NOT catch it and this raise is the only thing that does.
    """
    diff = _chunks_of("diff --git a/src/x.py b/src/x.py") + f"{prefix}src/y.py\n"

    with pytest.raises(TaskError, match="combined-diff header"):
        diff_chunks(diff)


def test_a_smuggled_second_file_is_refused():
    """The case a whole-diff count cannot see.

    2 chunks, 2 forward entries, 2 reverse entries -- all agreeing -- and the
    zip is still wrong: chunk 0 carries a traditional hunk for a second file
    and pairs with `tests/t.py`, so the `src/other.py` change is classified as
    oracle and rides into the START STATE. The fix, pre-applied, on every arm.
    Per chunk the same diff yields entry counts [2, 0].
    """
    diff = (
        "diff --git a/tests/t.py b/tests/t.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/t.py\n+++ b/tests/t.py\n@@ -1 +1,2 @@\n y\n+w\n"
        "--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1,2 @@\n o\n+p\n"
        "diff --git a/src/ghost.py b/src/ghost.py\n"
    )

    with pytest.raises(TaskError, match="expected\n?\\s*exactly one of each|exactly one"):
        split_reference_diff(diff, ("tests/",))


def test_paths_are_matched_by_component_not_by_prefix_string():
    """`str.startswith` over-claims on a first segment that merely starts with
    the prefix: under `paths: ["tests"]` it also takes `tests_helper.py`,
    `testsuite/x.py` and `tests2/x.py`. Slash-terminated prefixes behave
    identically under both, so no existing manifest changes."""
    diff = _chunks_of(
        "diff --git a/tests/real.py b/tests/real.py",
        "diff --git a/tests_helper.py b/tests_helper.py",
    )

    test_half, solution_half, files, solution_files, _ = split_reference_diff(
        diff, ("tests",)
    )

    assert files == ("tests/real.py",)
    assert solution_files == ("tests_helper.py",)


def test_an_extra_path_leaves_both_halves():
    """A changelog citing the issue number: no agent writes one, and leaving it
    in `solution_diff` makes preflight apply documentation it does not need."""
    diff = _chunks_of(
        "diff --git a/tests/t.py b/tests/t.py",
        "diff --git a/src/x.py b/src/x.py",
        "diff --git a/CHANGES.rst b/CHANGES.rst",
    )

    test_half, solution_half, _, solution_files, extra_files = split_reference_diff(
        diff, ("tests/",), extra_paths=("CHANGES.rst",)
    )

    assert extra_files == ("CHANGES.rst",)
    assert solution_files == ("src/x.py",)
    assert "CHANGES.rst" not in solution_half
    assert "CHANGES.rst" not in test_half


def test_an_unlisted_extra_still_lands_in_the_fix_half():
    """Deliberate scope limit. Deciding "not part of the fix" needs a
    source-tree oracle the harness does not have, so the Gate-1 plan's "a file
    that is neither is a load error" cannot be implemented -- an unlisted file
    behaves exactly as it did before."""
    diff = _chunks_of(
        "diff --git a/tests/t.py b/tests/t.py",
        "diff --git a/CHANGES.rst b/CHANGES.rst",
    )

    _, solution_half, _, solution_files, extra_files = split_reference_diff(
        diff, ("tests/",)
    )

    assert extra_files == ()
    assert solution_files == ("CHANGES.rst",)
    assert "CHANGES.rst" in solution_half


def test_a_rename_out_of_the_extra_class_is_refused():
    """The three-class boundary check. `a_is_test != b_is_test` misses this:
    both endpoints are non-test, so a two-class comparison stays silent while
    an excluded file is renamed into the fix half."""
    diff = (
        "diff --git a/CHANGES.rst b/docs/CHANGES.rst\n"
        "similarity index 100%\n"
        "rename from CHANGES.rst\n"
        "rename to docs/CHANGES.rst\n"
    )

    with pytest.raises(TaskError, match="across the test/solution boundary"):
        split_reference_diff(diff, ("tests/",), extra_paths=("CHANGES.rst",))


def test_overlapping_test_and_extra_paths_are_refused():
    """Containment both ways, not set intersection: `tests/` and
    `tests/fixtures/x.rst` share no element, and with tests.paths applied first
    the extra entry is a silent no-op -- a manifest key that looks like it
    excludes a file and does nothing."""
    diff = _chunks_of("diff --git a/tests/t.py b/tests/t.py")

    with pytest.raises(TaskError, match="overlaps tests.paths"):
        split_reference_diff(
            diff, ("tests/",), extra_paths=("tests/fixtures/x.rst",)
        )


@pytest.mark.parametrize("bad", ["", "/abs/path", "../escape"])
def test_a_malformed_path_prefix_is_refused(bad):
    diff = _chunks_of("diff --git a/tests/t.py b/tests/t.py")

    with pytest.raises(TaskError):
        split_reference_diff(diff, (bad,))


def test_a_missing_git_is_a_task_error_not_a_traceback(monkeypatch):
    """`load_task` promises one exception type and `run_matrix` catches only
    that, so a subprocess failure must not escape as a traceback from a module
    that previously ran no subprocesses at all."""
    import bakeoff.tasks as tasks_module

    def no_git(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(tasks_module.subprocess, "run", no_git)
    diff = _chunks_of("diff --git a/tests/t.py b/tests/t.py")

    with pytest.raises(TaskError, match="could not run"):
        split_reference_diff(diff, ("tests/",))


def test_the_oracle_does_not_inherit_the_callers_cwd(tmp_path, monkeypatch):
    """Measured: run from a repository SUBDIRECTORY, `git apply --numstat`
    filters the patch to the cwd prefix and reports zero entries, exit 0, empty
    stderr -- byte-identical to a patch that touches nothing. The temp
    directory is what makes that unreachable; without this test it is one
    refactor away."""
    import subprocess as sp

    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    sp.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo / "sub")

    diff = _chunks_of("diff --git a/tests/t.py b/tests/t.py")
    _, _, files, _, _ = split_reference_diff(diff, ("tests/",))

    assert files == ("tests/t.py",), "the oracle must not see the caller's cwd"


def test_leading_blank_lines_are_refused_rather_than_dropped():
    """The one live trigger of the byte-exactness guard.

    Leading blank lines are not "content before the first header" -- the
    existing check tests `.strip()`, so they fall through and are silently
    discarded with the chunk that replaces them. A test asserting only that
    the round trip HOLDS cannot see that: it holds for every well-formed
    input. This exercises the raise.
    """
    diff = "\n\n" + _chunks_of("diff --git a/src/x.py b/src/x.py")

    with pytest.raises(TaskError, match="byte for byte"):
        diff_chunks(diff)
