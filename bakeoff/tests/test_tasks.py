"""The task loader, which is the harness's only defence against a bad input.

Every case here is a way a task could be wrong such that the run still
completes and the record still looks ordinary. That is the whole class:
`git checkout --detach` onto a SHA that does not resolve leaves the tree
where it was, `git apply` of a re-cut patch changes what the agent was asked
to do, and a duplicate task_id collides in the event log only at the far end
of a matrix, after the tokens are spent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bakeoff.tasks import (
    TaskError,
    TaskGrading,
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
        # A raw YAML block appended verbatim, so a test can write a `grading:`
        # section -- including the malformed shapes a keyword-per-key helper
        # could not express.
        "extra_yaml": "",
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
    ]
    if data["extra_yaml"]:
        lines.append(data["extra_yaml"])
    lines.append("")
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


def test_an_undeclared_grading_section_is_not_configured_not_an_error(
    tmp_path, upstream
):
    """Most tasks declare no build, no typecheck and no linter, and that is
    not a defect in the task. The empty tuple is what the grader records as
    `not_configured`; a loader that raised would make the section mandatory
    on 80 harvested tasks that have nothing to put in it."""
    task_dir = _write_task(tmp_path / "set", upstream)

    task = load_task(task_dir)

    assert task.grading == TaskGrading()
    assert (task.grading.build, task.grading.typecheck, task.grading.lint) == (
        (), (), (),
    )


def test_a_declared_grading_section_parses_as_argv(tmp_path, upstream):
    """argv everywhere, matching `tests.runner`. A shell string would be run
    through a shell inside the image or split by the grader on whitespace --
    and a path with a space then becomes two arguments, so the check fails for
    a reason that has nothing to do with the submission."""
    task_dir = _write_task(
        tmp_path / "set",
        upstream,
        extra_yaml=(
            "grading:\n"
            '  build: ["python", "-m", "build"]\n'
            '  typecheck: ["mypy", "src"]\n'
            '  lint: ["ruff", "check", "."]\n'
        ),
    )

    task = load_task(task_dir)

    assert task.grading.build == ("python", "-m", "build")
    assert task.grading.typecheck == ("mypy", "src")
    assert task.grading.lint == ("ruff", "check", ".")


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ('grading:\n  lint: "ruff check ."', r"grading\.lint"),
        ("grading:\n  build: {make: all}", r"grading\.build"),
        # The whole section as a scalar, which no key-level check reaches.
        ("grading: ruff", "grading must be a mapping"),
    ],
)
def test_a_grading_key_that_is_not_argv_is_refused_at_load(
    tmp_path, upstream, block, match
):
    """A shell string is the shape an author reaches for, and it is the one
    that survives quietly: `"ruff check ."` is iterable, so a loader that only
    stored it hands the grader a three-element argv of `r`, `u`, `f` -- an
    exec failure recorded as a lint verdict against the submission, in a
    per-record grade nobody re-derives."""
    task_dir = _write_task(tmp_path / "set", upstream, extra_yaml=block)

    with pytest.raises(TaskError, match=match):
        load_task(task_dir)


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
    a host path leaked into a run that is supposed to be hermetic.

    The reflog is the second copy of that path and was missed: `git clone`
    records `clone: from <host cache path>` in `.git/logs/HEAD`, and the
    pruned mirror's name carries the cache layout AND `base_sha`."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"
    cache = tmp_path / "cache"

    materialize(task, repo, cache)

    assert _sh("git", "remote", cwd=repo) == ""
    logs = repo / ".git" / "logs"
    leaked = [
        path
        for path in logs.rglob("*")
        if path.is_file() and str(cache) in path.read_text()
    ]
    assert leaked == []


# --- the run tree does not contain the answer --------------------------------
#
# `git clone --local` hardlinks the whole object store, so before the pruned
# mirror the run tree carried every object the upstream mirror had -- including
# the merge commit of the PR the task was cut from. Measured on pallets/click:
# `refs/heads/main` 181 commits ahead of the start state, and `git log main
# --grep=3360` naming the fix. The tests below are stated over OBJECTS, because
# a ref-level guarantee is satisfied by a prune that leaves the oracle readable
# through `cat-file -p` or `fsck --lost-found`.


def test_the_reference_fix_is_not_in_the_run_tree(tmp_path, upstream):
    """The one assertion that carries the whole guarantee.

    `git log --all` equalling `git log` is nearly vacuous here -- `--all` means
    refs plus HEAD, and the fixture has one ref -- so it is checked for shape
    only. Object absence is the claim."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0, "the merged fix is readable in the agent's own tree"
    # Sets, not the raw output: `--all` walks the refs in a different order.
    assert set(_sh("git", "rev-list", "--all", cwd=repo).split()) == set(
        _sh("git", "rev-list", "HEAD", cwd=repo).split()
    )


def test_a_tag_on_the_future_is_not_in_the_run_tree(tmp_path, upstream):
    """Tags are copied straight into `refs/tags/*` by a clone, so
    `git remote remove origin` never touched them. This is also the only test
    that exercises ref DELETION: the fixture's single branch is retargeted to
    `base_sha`, so without a future tag the delete list is empty and gc alone
    would prune the future."""
    _sh("git", "tag", "v9.9", upstream["head"], cwd=upstream["path"])
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert "v9.9" not in _sh("git", "tag", cwd=repo).split()


def test_history_before_the_start_state_survives(tmp_path, upstream):
    """The prune must not be satisfied by destroying history. Section 3.3's
    loop includes reading the repository, and a run tree with no past is as
    unlike the harvested workflow as one with the answer in it."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['base']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode == 0
    assert upstream["base"][:7] in _sh("git", "log", "--oneline", cwd=repo)


def test_an_annotated_tag_on_the_history_survives(tmp_path, upstream):
    """`git describe` is why ancestor tags are kept rather than all refs
    dropped: `setuptools_scm` and `hatch-vcs` derive a package version from it,
    and a repo the agent reinstalls would fail on a tagless tree.

    Annotated on purpose -- `git describe` shows only annotated tags by
    default, so a lightweight one fails this whether or not the prune exists."""
    _sh("git", "tag", "-a", "v1.0", "-m", "v1.0", upstream["base"],
        cwd=upstream["path"])
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert "v1.0" in _sh("git", "tag", cwd=repo).split()
    assert _sh("git", "describe", cwd=repo).startswith("v1.0")


@pytest.mark.parametrize(
    "layout",
    [["--reachable"], ["--reachable", "--split"]],
    ids=("single", "split"),
)
def test_an_inherited_commit_graph_does_not_reach_the_run_tree(
    tmp_path, upstream, layout
):
    """A commit-graph is HARDLINKED by `clone --mirror --local` and carries
    future commit ids verbatim. Under `gc.writeCommitGraph=false` gc exits 0
    and leaves the inherited copy -- measured: the object post-condition still
    reports zero outside commits and PASSES, while `git fsck` in the run tree
    exits non-zero printing `Could not read <the fix's sha>`.

    Both layouts, because `--split` writes a DIRECTORY,
    `objects/info/commit-graphs/`, which `clone --mirror --local` hardlinks
    through just the same -- a limb of the strip a single-file fixture never
    reaches. Verified against git 2.50.1.

    The `.keep` is a real `pack-<hash>.keep`. Measured: a .keep matching no
    existing pack is ignored by git entirely, so the earlier `stale.keep`
    fixture exercised the glob and nothing else. A real one makes gc refuse the
    pack and the fix survives EVEN under `repack.packKeptObjects=true`, which
    leaves `_strip_derived`'s unlink as the only thing that saves that case.
    It needs a pack to match, hence the repack: `ensure_mirror` clones from a
    local path and hardlinks the fixture's loose objects, leaving zero packs.

    The object sweep is blind to all of this -- emptying `_DERIVED_PATHS`
    empties `_verify_pruned`'s leftover list too, so nothing raises and the
    inherited graph reaches the run tree, where the assertions below catch it.
    """
    from bakeoff.tasks import ensure_mirror

    cache = tmp_path / "cache"
    mirror = ensure_mirror(str(upstream["path"]), upstream["base"], cache)
    _sh("git", "repack", "-adq", cwd=mirror)
    _sh("git", "commit-graph", "write", *layout, cwd=mirror)
    pack = next((mirror / "objects" / "pack").glob("pack-*.pack"))
    pack.with_suffix(".keep").write_text("")
    # Asserted, not assumed: a git that declined to split so small a history
    # would make this param a silent duplicate of the other one.
    info = mirror / "objects" / "info"
    assert (info / "commit-graphs").is_dir() if "--split" in layout else (
        info / "commit-graph"
    ).exists()

    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"
    materialize(task, repo, cache)

    run_info = repo / ".git" / "objects" / "info"
    assert not (run_info / "commit-graph").exists()
    assert not (run_info / "commit-graphs").exists()
    assert subprocess.run(
        ["git", "fsck", "--no-progress"], cwd=repo, capture_output=True
    ).returncode == 0


def test_a_prune_that_left_the_future_behind_is_refused(tmp_path, upstream, monkeypatch):
    """The post-condition is what makes the prune trustworthy, so it is checked
    rather than assumed. Every bypass found -- `gc.bigPackThreshold`, a cruft
    pack, a `.keep`, an operator reflog -- leaves the refs gone and the objects
    readable, which is indistinguishable from success at every other layer."""
    import bakeoff.tasks as tasks_module

    real_git = tasks_module._git

    def skip_gc(*args, **kwargs):
        # The invocation begins with `-c`, so match anywhere in argv.
        if "gc" in args:
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(tasks_module, "_git", skip_gc)
    task = load_task(_write_task(tmp_path / "set", upstream))

    with pytest.raises(TaskError, match="survived the prune"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


@pytest.mark.parametrize(
    "marker_body",
    ["0 stale stale\n", "corrupt\n"],
    ids=("older_revision", "unparseable"),
)
def test_a_cached_prune_from_an_older_revision_is_rebuilt(
    tmp_path, upstream, marker_body
):
    """A pruned mirror built by an earlier revision of the prune must not be
    served forever -- that is the silent-wrong-cache failure `ensure_mirror`
    re-checks on every call to avoid. The rebuild has to survive the old mirror
    still being there: `os.replace` onto a non-empty directory raises.

    Both shapes a marker can be wrong in. `corrupt` is the one that unpacks to
    the wrong number of fields, which a truncated write leaves behind -- and
    which must fall through to a rebuild rather than raise `ValueError` out of
    a cache read."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    first = materialize(task, tmp_path / "a" / "repo", cache)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    (pruned / "bakeoff-prune-version").write_text(marker_body)

    assert materialize(task, tmp_path / "b" / "repo", cache) == first


def test_an_abandoned_build_is_reclaimed_but_a_live_one_is_not(tmp_path, upstream):
    """The sweep reclaims DISK, and an earlier docstring here -- and the comment
    in the code -- claimed it prevented a wedge: that a leftover tmp made
    `git clone --mirror --local` exit 128 "for this task forever". That was
    wrong. `tmp` comes from `mkdtemp`, so its name is unique on every call
    (measured: 200 calls, 200 distinct names, never colliding with a planted
    leftover) and the clone can never fail into one. What a leftover costs is a
    whole pruned mirror of disk per abandoned build.

    Which makes the age floor the load-bearing half, and it is asserted in both
    directions: an old leftover must go, and a FRESH one must survive, because
    a sweep that deleted recent directories would delete the live build of a
    concurrent invocation -- the sweep runs in a cache directory shared by every
    repo and every base_sha.

    The legacy name is used deliberately. The scoped `prune-<dest>-*` pattern
    matches none of the names the previous revision wrote (measured: 0 of 201),
    so sweeping only the new one would strand every existing leftover."""
    from bakeoff.tasks import _STALE_TMP_AGE_S, pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)

    abandoned = pruned.parent / "prune-leftover.tmp"
    abandoned.mkdir(parents=True)
    (abandoned / "junk").write_text("x")
    old = time.time() - _STALE_TMP_AGE_S - 60
    os.utime(abandoned, (old, old))

    # An interrupted publish renames the old mirror aside into this same
    # namespace, and `dest` is allowed to be a file -- so the sweep has to
    # reclaim a FILE too. `shutil.rmtree` does nothing to one.
    orphan = pruned.parent / f"prune-{pruned.name}-999-deadbeef.tmp"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("a publish that died between the two renames\n")
    os.utime(orphan, (old, old))

    live = pruned.parent / "prune-someone-elses-build.tmp"
    live.mkdir()
    (live / "junk").write_text("x")

    materialize(task, tmp_path / "run" / "repo", cache)

    assert not abandoned.exists(), "an abandoned build was not reclaimed"
    assert not orphan.exists(), "an interrupted publish left a file nothing reclaims"
    assert live.exists(), "the sweep deleted a build that may still be running"


def test_a_stale_entry_that_vanishes_mid_sweep_is_skipped(tmp_path, upstream, monkeypatch):
    """The sweep stats entries it did not create, in a cache directory shared
    by every repo -- so an entry can vanish (or become unstatable) between the
    glob and the stat when another invocation reclaims it first. That OSError
    must mean "skip this entry", never "fail the build": the sweep only
    reclaims disk, and a build that dies on someone else's leftover turns a
    janitor into a single point of failure."""
    from bakeoff.tasks import _STALE_TMP_AGE_S, pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)

    ghost = pruned.parent / "prune-vanishing.tmp"
    ghost.mkdir(parents=True)
    old = time.time() - _STALE_TMP_AGE_S - 60
    os.utime(ghost, (old, old))

    real_stat = Path.stat

    def stat_that_loses_the_race(self, **kwargs):
        if self.name == "prune-vanishing.tmp":
            raise OSError("stale file handle")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_that_loses_the_race)

    materialize(task, tmp_path / "run" / "repo", cache)  # must not raise
    # os.listdir, not ghost.exists(): Path.exists() routes through the
    # patched stat, and an OSError with errno=None is outside pathlib's
    # _ignore_error set, so exists() re-raises it instead of returning False.
    assert "prune-vanishing.tmp" in os.listdir(ghost.parent), \
        "an unstatable entry must be skipped, not deleted"


def test_a_failed_publish_raises_a_named_taskerror(tmp_path, upstream, monkeypatch):
    """The publish rename is the one step allowed to fail after a verified
    build, and its TaskError must not point the operator at a path that is
    already gone: the finally-block rmtree reclaims `tmp` while the exception
    propagates, and on the second os.replace `dest` has itself been renamed
    aside -- so an earlier message saying the build "is at {tmp}" named a
    directory the caller could never find."""
    import bakeoff.tasks as tasks_mod
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)

    real_replace = os.replace

    def replace_that_hits_a_full_disk(src, dst):
        if Path(dst) == pruned:
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(tasks_mod.os, "replace", replace_that_hits_a_full_disk)

    with pytest.raises(TaskError, match="could not publish the pruned mirror") as exc:
        materialize(task, tmp_path / "run" / "repo", cache)
    assert "prune-" not in str(exc.value), \
        "the message names a build directory the finally block already reclaimed"
    assert not list(pruned.parent.glob("prune-*")), \
        "a failed publish stranded a build the finally block should reclaim"


def _pack_a_latin1_ref(repo: Path, sha: str) -> None:
    # packed-refs is a plain file, so it takes ref-name bytes APFS refuses
    # as a loose-ref filename (measured: `git tag caf\xe9` exits 128
    # "Illegal byte sequence" on APFS, while this file round-trips).
    with (repo / ".git" / "packed-refs").open("ab") as refs:
        refs.write(f"{sha} refs/tags/caf".encode() + b"\xe9\n")


def test_git_output_survives_bytes_the_locale_cannot_decode(tmp_path, upstream):
    """`text=True` decodes with the harness process's locale encoding and
    errors='strict', so one non-UTF-8 byte in git output -- a latin-1 ref
    name here -- turned any git call into a raw UnicodeDecodeError naming
    neither the repo nor the task. (A CI runner whose own LC_ALL=C makes the
    same crash out of plain UTF-8 output; pinning the encoding closes both.)
    """
    from bakeoff.tasks import _git

    _pack_a_latin1_ref(upstream["path"], upstream["base"])

    out = _git("for-each-ref", "--format=%(refname)", cwd=upstream["path"])
    assert "caf�" in out.stdout


def test_a_ref_the_decode_mangled_is_refused_not_leaked(tmp_path, upstream):
    """Replacement is survivable only because the object sweep backstops it,
    and this pins the chain. The ref sweep deletes by DECODED name, and
    `git update-ref --stdin` exits 0 deleting a ref that does not exist
    (measured, git 2.50.1) -- so a latin-1 ref pointing outside base_sha's
    history survives the sweep, keeps the future alive through the gc, and
    `_verify_pruned` refuses the mirror. An earlier claim that the mangled
    ref "gets deleted, which is the safe direction" was measured false; the
    safe direction is this loud refusal.

    Verified end-to-end 2026-08-14, darwin/APFS, git 2.50.1: `git clone
    --mirror` writes the refs PACKED -- zero loose ref files, so the latin-1
    name never touches an APFS filename -- the pruned clone inherits the
    ref, the decoded-name delete exits 0 touching nothing, the gc keeps the
    head commit, and `_verify_pruned` raises `1 commit(s) outside ...
    survived the prune`."""
    _pack_a_latin1_ref(upstream["path"], upstream["head"])

    task = load_task(_write_task(tmp_path / "set", upstream))

    with pytest.raises(TaskError, match="survived the prune"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


def test_the_object_sweep_stops_reading_after_the_evidence(tmp_path, upstream):
    """`_commits_outside` buffered the entire `--batch-all-objects` listing --
    659 KB on pruned click, ~229 MB extrapolated to a 5M-object monorepo --
    then split it, for an error message that only ever prints three names.
    Pinned by counting what it returns: one past the sample, on a repo with
    more than that outside, so the message can say "more than 3" without
    claiming a count it never took.

    The MEMORY half is deliberately unanchored: a rewrite that buffers the
    listing and slices it passes this test. What the test pins is the
    contract the message depends on -- the cap and the one-past read."""
    from bakeoff.tasks import _OUTSIDE_SAMPLE, _ancestors, _commits_outside

    repo_path = upstream["path"]
    for i in range(5):
        (repo_path / f"extra{i}.txt").write_text(f"{i}\n")
        _sh("git", "add", "-A", cwd=repo_path)
        _sh("git", "commit", "-q", "-m", f"extra {i}", cwd=repo_path)

    outside = _commits_outside(
        repo_path / ".git", _ancestors(repo_path / ".git", upstream["base"])
    )
    assert len(outside) == _OUTSIDE_SAMPLE + 1


def _assert_flock_held(lock_path: Path) -> None:
    import fcntl
    with open(lock_path) as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(probe, fcntl.LOCK_UN)
    raise AssertionError(f"{lock_path} was not held")


def test_the_prune_and_the_run_tree_clone_run_under_the_repo_lock(
    tmp_path, upstream, monkeypatch
):
    """Two invocations on the same (repo, base_sha) race twice: both prune
    at once, and one publishes -- rename aside, rmtree -- while the other's
    materialize is cloning from the mirror being reclaimed. The lock is
    asserted from INSIDE each critical section, with a probe fd taking
    LOCK_EX|LOCK_NB and expecting to be refused, because a lock tested only
    by its file existing is a lock nothing proves is taken."""
    import bakeoff.tasks as tasks_mod
    from bakeoff.tasks import mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    lock_path = mirror_path(str(upstream["path"]), cache).with_suffix(".lock")

    real_strip = tasks_mod._strip_derived
    real_git = tasks_mod._git
    probed = []

    def strip_while_probing(repo):
        _assert_flock_held(lock_path)
        probed.append("prune")
        return real_strip(repo)

    def git_while_probing(*args, **kwargs):
        if args and args[0] == "clone" and "--local" in args and "--mirror" not in args:
            _assert_flock_held(lock_path)
            probed.append("run-tree clone")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(tasks_mod, "_strip_derived", strip_while_probing)
    monkeypatch.setattr(tasks_mod, "_git", git_while_probing)
    materialize(task, tmp_path / "run" / "repo", cache)
    assert probed == ["prune", "run-tree clone"], \
        f"a probe never fired: {probed}"


def test_the_mirror_fetch_path_runs_under_the_repo_lock(tmp_path, upstream, monkeypatch):
    """`ensure_mirror`'s `HEAD`-exists check is the same race one level up:
    `git clone` writes HEAD early, so a second process can `fetch --prune`
    into a half-populated clone. The probe rides the mirror clone itself."""
    import bakeoff.tasks as tasks_mod
    from bakeoff.tasks import ensure_mirror, mirror_path

    cache = tmp_path / "cache"
    lock_path = mirror_path(str(upstream["path"]), cache).with_suffix(".lock")

    real_git = tasks_mod._git
    probed = []

    def git_while_probing(*args, **kwargs):
        if args and args[0] == "clone" and "--mirror" in args and "--local" not in args:
            _assert_flock_held(lock_path)
            probed.append("mirror clone")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(tasks_mod, "_git", git_while_probing)
    ensure_mirror(str(upstream["path"]), upstream["base"], cache)
    assert probed == ["mirror clone"], \
        "the probe never fired; the clone path was not exercised"


def test_the_published_prune_is_not_world_readable(tmp_path, upstream):
    """`mkdtemp` gives 0700, but the build rmtree'd that directory so `git
    clone` could create it fresh -- under the umask -- and `os.replace`
    published THAT as the permanent cache. The exposure is the whole cached
    repository, forever, not the build window; it matters the moment the
    task repos are private.

    The umask is pinned to 0 so the verdict is a property of this repository
    rather than of the laptop -- under a 077 umask the defect is invisible.
    os.umask is process-global, so this test is not xdist-safe; the suite
    runs serially today, and this line is the notice if that changes.

    WHAT THIS DOES NOT COVER: `ensure_mirror`'s full mirror sits in the same
    cache directory at the umask's mercy and holds a superset of these
    objects, including the merged fix. That exposure is recorded in
    HANDOFF.md's open list, not silently closed here."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    old_umask = os.umask(0)
    try:
        materialize(task, tmp_path / "run" / "repo", cache)
    finally:
        os.umask(old_umask)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    assert (pruned.stat().st_mode & 0o777) == 0o700


def test_a_base_sha_off_the_default_branch_materializes(tmp_path, upstream):
    """`base_sha` is often not on the default branch -- a release branch, a
    merge parent. Retargeting `main` at it anyway would show the agent history
    that never was, so the prune leaves HEAD detached instead. That limb is the
    newer one and depends on `clone --local` propagating a detached HEAD, so it
    is pinned here rather than left to the branch path every other test takes."""
    repo_path = upstream["path"]
    _sh("git", "checkout", "-q", "-b", "sidebranch", upstream["base"], cwd=repo_path)
    (repo_path / "side.txt").write_text("side\n")
    _sh("git", "add", "-A", cwd=repo_path)
    _sh("git", "commit", "-q", "-m", "side", cwd=repo_path)
    side = _sh("git", "rev-parse", "HEAD", cwd=repo_path)
    _sh("git", "checkout", "-q", "master" if _sh(
        "git", "branch", "--list", "master", cwd=repo_path
    ) else "main", cwd=repo_path)

    task = load_task(_write_task(tmp_path / "set", upstream, base_sha=side))
    repo = tmp_path / "run" / "repo"

    start = materialize(task, repo, tmp_path / "cache")

    assert _sh("git", "rev-parse", "HEAD", cwd=repo) == start
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0


# --- the pruned mirror is a CACHE, and a cache is not evidence ----------------


def test_a_fetch_into_the_cache_does_not_reach_the_run_tree(tmp_path, upstream):
    """The pruned mirror is kept across invocations and trusted on a
    fingerprint of its `*.idx` names and sizes -- deliberately, so a run tree
    staging a file cannot invalidate it.

    Measured against git 2.50.1: a fetch into that cache lands its objects
    LOOSE, because under `transfer.unpackLimit` no pack is written. No `.idx`
    moves, the fingerprint is byte-identical, the fast path returns the mirror
    unchecked, and `git cat-file -p <the merged fix>` works in the next run
    tree -- same `start_sha`, no error, nothing recorded. The leak this module
    exists to close, reintroduced one layer up. `_pack_fingerprint` counts
    loose objects for exactly this."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    first = materialize(task, tmp_path / "a" / "repo", cache)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    _sh("git", "fetch", str(upstream["path"]), "+refs/heads/*:refs/future/*",
        cwd=pruned)

    repo = tmp_path / "b" / "repo"

    assert materialize(task, repo, cache) == first
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0, "a fetch into the cache reached the agent's own tree"


def _borrow_the_future(pruned: Path, upstream) -> None:
    (pruned / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (pruned / "objects" / "info" / "alternates").write_text(
        f"{(upstream['path'] / '.git' / 'objects').resolve()}\n"
    )


def _regrow_a_commit_graph(pruned: Path, upstream) -> None:
    _sh("git", "commit-graph", "write", "--reachable", cwd=pruned)


def _plant_a_real_keep(pruned: Path, upstream) -> None:
    idx = next((pruned / "objects" / "pack").glob("*.idx"))
    (pruned / "objects" / "pack" / f"{idx.stem}.keep").write_text("")


@pytest.mark.parametrize(
    "damage",
    [_borrow_the_future, _regrow_a_commit_graph, _plant_a_real_keep],
    ids=("alternates", "commit_graph", "pack_keep"),
)
def test_a_cache_that_stopped_being_pruned_is_not_served(tmp_path, upstream, damage):
    """`_pack_fingerprint` detects changes to the LOCAL PACK SET. The fast path
    used it as proof the mirror still satisfied `_verify_pruned`, which asserts
    four things -- and the fingerprint can observe one of them.

    Measured against the previous revision, all three: the digest is
    byte-identical after the damage, the fast path serves the mirror, and for
    `alternates` the merged fix is then readable in the agent's own run tree
    (28459dbfe8a3b533 both sides). Borrowed objects are not local packs and not
    loose objects, so no term of the digest moves; `gc` cannot prune them either,
    because they are not in this repository.

    So the cheap half of `_verify_pruned` -- three `exists()` calls and one glob
    -- runs on the fast path too, and anything short of usable rebuilds. The
    expensive half (the object sweep) stays rebuild-only: running it per cell is
    `cat-file --batch-all-objects` over the whole store ~3,200 times, which is
    the cost the cache exists to avoid."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    first = materialize(task, tmp_path / "a" / "repo", cache)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    damage(pruned, upstream)

    repo = tmp_path / "b" / "repo"
    assert materialize(task, repo, cache) == first
    # The leak assertion, which only `alternates` can actually violate -- a
    # commit-graph over ancestor-only history is not itself an oracle.
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0, "a cached mirror that stopped being pruned reached the agent"
    # The assertion that carries the other two params: the mirror was REBUILT,
    # not served. Without it they pass against a fast path that ignores them.
    assert not (pruned / "objects" / "info" / "alternates").exists()
    assert not (pruned / "objects" / "info" / "commit-graph").exists()
    assert not any((pruned / "objects" / "pack").glob("*.keep"))


def test_an_upstream_that_borrows_objects_materializes(tmp_path, upstream):
    """A repository that legitimately borrows -- `git clone --shared`, a
    `--reference` clone -- must work, and the obvious fix breaks it.

    Unlinking `objects/info/alternates` before the gc was tried and measured:
    on a true borrower (zero local objects) gc exits 128 with `fatal: bad object
    refs/heads/main / failed to run repack` and `base_sha` stops resolving,
    which is a harder wedge than the leak. `--dissociate` absorbs the borrowed
    objects instead, which is why the file is in `_FORBIDDEN_PATHS` (assert its
    absence) and NOT in `_DERIVED_PATHS` (never unlink it)."""
    from bakeoff.tasks import pruned_mirror_path

    borrower = tmp_path / "borrower"
    _sh("git", "clone", "-q", "--shared", str(upstream["path"]), str(borrower),
        cwd=tmp_path)
    _sh("git", "checkout", "-q", "--detach", upstream["base"], cwd=borrower)
    assert (borrower / ".git" / "objects" / "info" / "alternates").exists()

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream, url=str(borrower)))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, cache)

    pruned = pruned_mirror_path(str(borrower), upstream["base"], cache)
    assert not (pruned / "objects" / "info" / "alternates").exists()
    assert not (repo / ".git" / "objects" / "info" / "alternates").exists()
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['base']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode == 0, "--dissociate must absorb the history, not drop it"


def test_the_pruned_clone_dissociates_too(tmp_path, upstream):
    """`--dissociate` appears on BOTH clones, and only this test covers the
    second one.

    Measured: with a borrowing upstream, `ensure_mirror`'s clone absorbs the
    alternates, so by the time `ensure_pruned_mirror` clones there is nothing
    left to dissociate -- deleting the flag there passes the whole suite. The
    channel that reaches it is a full mirror that GAINS alternates after it was
    cached, which `ensure_mirror` cannot notice: it re-checks `base_sha` with
    `cat-file -e` and nothing else."""
    from bakeoff.tasks import ensure_mirror, pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    mirror = ensure_mirror(str(upstream["path"]), upstream["base"], cache)
    (mirror / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (mirror / "objects" / "info" / "alternates").write_text(
        f"{(upstream['path'] / '.git' / 'objects').resolve()}\n"
    )

    repo = tmp_path / "run" / "repo"
    materialize(task, repo, cache)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    assert not (pruned / "objects" / "info" / "alternates").exists()
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0, "the pruned mirror inherited a borrowed object store"


def _dest_is_a_file(pruned: Path) -> None:
    shutil.rmtree(pruned)
    pruned.write_text("not a directory\n")


def _marker_is_a_directory(pruned: Path) -> None:
    marker = pruned / "bakeoff-prune-version"
    marker.unlink()
    marker.mkdir()


@pytest.mark.parametrize(
    "damage", [_dest_is_a_file, _marker_is_a_directory],
    ids=("dest_is_a_file", "marker_is_a_directory"),
)
def test_an_unreadable_cache_rebuilds_instead_of_wedging(tmp_path, upstream, damage):
    """Two states that were permanent, both measured, both the failure class the
    previous revision set out to eliminate and reached one statement short of.

    `dest` as a file: `shutil.rmtree(dest, ignore_errors=True)` silently removes
    NOTHING from a file, and `os.replace` then raises `NotADirectoryError` on
    this and every later invocation. Fixed by renaming the old mirror aside
    rather than deleting it in place -- a rename is atomic and cannot
    half-succeed.

    The marker as a directory: `read_text()` raises `IsADirectoryError`, an
    `OSError` and not the `ValueError` the guard caught, so it escaped ahead of
    the rebuild block. (Invalid UTF-8 was already survivable, because
    `UnicodeDecodeError` subclasses `ValueError` -- which is exactly why the
    narrow catch looked sufficient.) Fixed by computing the whole cache
    predicate inside the try and catching `OSError` beside `ValueError`."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    first = materialize(task, tmp_path / "a" / "repo", cache)

    damage(pruned_mirror_path(str(upstream["path"]), upstream["base"], cache))

    assert materialize(task, tmp_path / "b" / "repo", cache) == first


def test_a_damaged_prune_cache_heals_itself(tmp_path, upstream):
    """A pruned mirror that cannot be shown to be pruned must be REBUILT, not
    refused. An earlier revision re-verified it and raised -- before the
    rebuild block, so the damaged entry stayed on disk and every cell of every
    task on that (repo, base_sha) died on every re-invocation, with a message
    that reads like a prune bug and no instruction to delete anything.
    Measured: three identical TaskErrors in a row.

    Emptying `objects/pack` empties the `.idx` set, so the marker's recorded
    fingerprint mismatches the recomputed one on its own -- the genuine
    stale-but-parseable shape. An earlier revision rewrote the marker "to
    reach the guard at all", which was wrong twice over: `_verify_pruned` has
    one call site, on the freshly built tmp, so there is no guard on the
    cache path to reach; and the fast path's own structural checks catch the
    fingerprint-invisible shapes whatever the marker says (pinned in
    test_a_cache_that_stopped_being_pruned_is_not_served).

    Unlinking `objects/pack/*` rather than `objects/` on purpose: the first
    leaves a valid bare repository with an empty object store (`cat-file -e`
    exits 128 "Not a valid object name", `rev-parse --is-bare-repository`
    still true), which is the truncated-mirror shape. Removing `objects/`
    outright stops git recognising the directory at all."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    first = materialize(task, tmp_path / "a" / "repo", cache)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    for path in (pruned / "objects" / "pack").glob("*"):
        path.unlink()

    repo = tmp_path / "b" / "repo"

    assert materialize(task, repo, cache) == first
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0


def test_a_pruned_mirror_missing_its_base_sha_is_refused(tmp_path):
    """Belt and braces, and called directly because nothing else can reach it:
    the one caller passes a mirror it just cloned from a source `ensure_mirror`
    resolved `base_sha` in, so on any passing path the commit is present by
    construction.

    Kept for the MESSAGE, not to stop a vacuous pass -- an earlier revision of
    this docstring claimed `_commits_outside` over an empty store would return
    `[]` and pass, but `_ancestors` raises out of `rev-list base_sha`'s own
    check=True before the sweep runs. What the guard buys is a failure that
    names the mirror and calls it a cache defect, instead of _git's generic
    exit-128 line from the wrong depth."""
    from bakeoff.tasks import _verify_pruned

    repo = tmp_path / "empty.git"
    _sh("git", "init", "-q", "--bare", str(repo), cwd=tmp_path)

    with pytest.raises(TaskError, match="pruned mirror does not contain base_sha"):
        _verify_pruned(repo, "0" * 40)


def test_a_pruned_mirror_that_kept_a_derived_file_is_refused(tmp_path, upstream):
    """`git gc` writes a commit-graph BY DEFAULT, so this guard fires on gc's
    own output whenever `-c gc.writeCommitGraph=false` is missing -- it is not
    only about what `clone --mirror --local` inherits.

    It matters because a commit-graph carries future commit ids verbatim,
    passes the object sweep untouched, and then makes `git fsck` in the run
    tree print the reference fix's SHA at the agent. Checked BEFORE the sweep,
    which is why a mirror that was never pruned at all lands here rather than
    in the outside-commits branch."""
    from bakeoff.tasks import _verify_pruned

    mirror = tmp_path / "mirror.git"
    _sh("git", "clone", "-q", "--bare", str(upstream["path"]), str(mirror),
        cwd=tmp_path)
    _sh("git", "commit-graph", "write", "--reachable", cwd=mirror)

    with pytest.raises(TaskError, match="carries future commit ids"):
        _verify_pruned(mirror, upstream["base"])


def test_the_prune_holds_under_a_hostile_gitconfig(tmp_path, upstream, monkeypatch):
    """`_GC_CONFIG`'s `-c` overrides exist because the operator's own
    `~/.gitconfig` otherwise decides whether a prune prunes. Nothing that runs
    in CI exercised them, so a later edit could trim them as noise and stay
    green.

    The failure pinned here is NOT a silent leak -- `_verify_pruned` catches
    those and would raise. It is that an operator's config makes every task
    refuse to materialize, which is the whole task set down until someone
    finds the setting.

    `GIT_CONFIG_GLOBAL` replaces the operator's global entirely, so the verdict
    is a property of this repository rather than of the laptop. Two of the
    seven overrides are load-bearing, measured leave-one-out:
    `gc.bigPackThreshold` (the fix survives with every ref gone) and
    `gc.writeCommitGraph` (gc re-creates the file `_strip_derived` removed).
    The other five are shadowed by an explicit argument elsewhere.

    No `[user]` block on purpose: `materialize` commits the test half and
    survives an identity-less config only because `_SETUP_ENV` supplies
    `GIT_{AUTHOR,COMMITTER}_{NAME,EMAIL,DATE}` and the commit is
    `-c commit.gpgsign=false`."""
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text(
        "[gc]\n"
        "\tbigPackThreshold = 1\n"
        "\twriteCommitGraph = true\n"
        "\tcruftPacks = true\n"
        "\tpruneExpire = never\n"
        "\treflogExpire = never\n"
        "\treflogExpireUnreachable = never\n"
        "[core]\n"
        "\tlogAllRefUpdates = true\n"
        "[repack]\n"
        "\tpackKeptObjects = false\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Loose objects would leave no pack for `gc.bigPackThreshold` to keep, so
    # the hostile setting could not bite and this would pass whether or not the
    # override existed.
    _sh("git", "repack", "-adq", cwd=upstream["path"])
    assert _sh("git", "config", "--get", "gc.bigPackThreshold",
               cwd=upstream["path"]) == "1", "the hostile config is not being read"

    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"
    cache = tmp_path / "cache"

    materialize(task, repo, cache)

    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0
    assert subprocess.run(
        ["git", "fsck", "--no-progress"], cwd=repo, capture_output=True
    ).returncode == 0
    # `core.logAllRefUpdates=true` makes the run tree keep reflogs it normally
    # would not; `materialize`'s explicit `--expire=now` beats
    # `gc.reflogExpire=never` because an argument outranks config.
    logs = repo / ".git" / "logs"
    assert [
        path
        for path in logs.rglob("*")
        if path.is_file() and str(cache) in path.read_text()
    ] == []


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
