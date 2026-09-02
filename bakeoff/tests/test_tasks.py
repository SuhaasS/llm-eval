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

from bakeoff import tasks
from bakeoff.tasks import (
    TaskBudget,
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

# A chunk for a file the fix commit does not touch, appended to a reference so
# a test can exercise the three-class split. `git apply --numstat` PARSES a
# chunk rather than applying it -- the same property the `_chunks_of` tests
# further down already rely on.
CHANGELOG_CHUNK = (
    "diff --git a/CHANGES.md b/CHANGES.md\n"
    "--- a/CHANGES.md\n"
    "+++ b/CHANGES.md\n"
    "@@ -1 +1,2 @@\n"
    " changelog\n"
    "+- fixed add()\n"
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
    stops running. It also carries `CLAUDE.md`, `.claude/settings.json`,
    `vendor/dep.py` and `CHANGES.md` alongside `calc.py` and its test, because
    `strip_paths` exists for exactly those shapes -- an agent file, a
    vendored tree, and a changelog the fix commit does not touch.
    """
    repo = tmp_path / "upstream"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
    (repo / ".gitignore").write_text("__pycache__/\n")
    # A repository whose base_sha carries an agent file, a vendored tree and a
    # changelog is the shape `strip_paths` exists for, and all three were
    # measured on real repositories (sqlglot's CLAUDE.md, the internal repo's
    # committed venv, click's CHANGES.rst). None of them is touched by the fix
    # commit, so the reference diff below is unchanged and every other test in
    # this module sees exactly the halves it saw before.
    (repo / "CLAUDE.md").write_text("# project notes\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}\n")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "dep.py").write_text("VERSION = '1.0'\n")
    (repo / "CHANGES.md").write_text("changelog\n")
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


SUB_LIB = "VALUE = 1\n"
SUB_LIB_FUTURE = "VALUE = 999\n"


@pytest.fixture
def upstream_submodule(tmp_path):
    """A superproject with one submodule, whose upstream has moved PAST the pin.

    The future commit is the point. `git submodule update --init` against the
    real url clones the submodule's whole history (measured 2026-09-01, git
    2.50.1), so a fixture whose submodule has nothing after the gitlink cannot
    tell a pruned mirror from an unpruned one -- which is the guarantee Task 2
    exists to pin.

    `-c protocol.file.allow=always` on every submodule-touching command: git
    refuses the file transport for submodules by default since the CVE-2022-39253
    hardening, and a local path is a file transport. Production needs the same
    flag for the same reason (the pruned mirror is a local path), so this is not
    a fixture-only concession.
    """
    lib = tmp_path / "libdep"
    (lib / "libdep").mkdir(parents=True)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB)
    _sh("git", "init", "-q", cwd=lib)
    _sh("git", "config", "user.email", "t@t.test", cwd=lib)
    _sh("git", "config", "user.name", "t", cwd=lib)
    _sh("git", "add", "-A", cwd=lib)
    _sh("git", "commit", "-q", "-m", "libdep v1", cwd=lib)
    pinned = _sh("git", "rev-parse", "HEAD", cwd=lib)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB_FUTURE)
    _sh("git", "commit", "-q", "-am", "libdep FUTURE", cwd=lib)
    future = _sh("git", "rev-parse", "HEAD", cwd=lib)

    repo = tmp_path / "super"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
    _sh("git", "init", "-q", cwd=repo)
    _sh("git", "config", "user.email", "t@t.test", cwd=repo)
    _sh("git", "config", "user.name", "t", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "base", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(lib), "vendor/libdep", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "-C", "vendor/libdep",
        "checkout", "-q", pinned, cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "pin the submodule", cwd=repo)
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
    return {"path": repo, "base": base, "head": head, "reference": reference,
            "lib": lib, "pinned": pinned, "future": future,
            "sub_path": "vendor/libdep"}


@pytest.fixture
def local_urls(monkeypatch):
    """Accept the fixtures' local-path submodule urls.

    Empties `_SUBMODULE_URL_PREFIX` so `startswith` is vacuously true. The
    fixtures are local repositories on purpose -- the same reason `upstream`
    is -- so that the whole materialization path runs offline; a test that
    needed the network would be a test that stops running.
    """
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")


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


def _budget_task(tmp_path, upstream, body: str) -> Path:
    """A manifest whose `budget:` block is the literal YAML in `body`.

    `extra_yaml` is appended verbatim, which is what lets these tests state
    the malformed shapes a keyword-per-key helper could not express -- the
    same reason the `grading:` tests use it.
    """
    return _write_task(tmp_path / "set", upstream,
                       extra_yaml="budget:\n" + body)


def test_the_suite_timeout_defaults_to_the_constant_it_replaces(
    tmp_path, upstream
):
    """600 was `preflight(timeout_s=600)`, `ensure_oracle(timeout_s=600)` and
    the grader's own per-check bound (formerly `GRADE_TIMEOUT_S`, now read off
    this field directly), three copies of one number. `grader.SCAN_TIMEOUT_S`
    is a fourth, separate constant -- it bounds only the host-side gitleaks
    scan, not a copy of this default. A manifest that does not mention the
    key must gate and grade exactly as it did before, so the default is that
    number and not a rounder one."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    assert task.budget.suite_timeout_s == 600


def test_a_declared_suite_timeout_is_read(tmp_path, upstream):
    task = load_task(_budget_task(tmp_path, upstream, (
        "  max_turns: 40\n"
        "  wall_clock_timeout_s: 3600\n"
        "  suite_timeout_s: 1800\n"
    )))
    assert task.budget.suite_timeout_s == 1800


@pytest.mark.parametrize("yaml_value", ['"600"', "600.0", "null", "0", "-1"])
def test_a_suite_timeout_that_is_not_a_positive_int_is_a_load_error(
    tmp_path, upstream, yaml_value
):
    """`int("600")` and `int(600.0)` both SUCCEED, so the bare `int(...)` the
    other two budget keys use accepts a quoted or floated value silently and
    the manifest stops being a faithful record. An explicit `null` is refused
    rather than defaulted: the author WROTE the key, so reading it as "never
    written" is the wrong repair -- which is why `_positive_int` takes an
    `_ABSENT` sentinel and not a `None` default.

    Parametrized over YAML SOURCE. `'"600"'` reaches the file with its quotes;
    written as a Python `"600"` it would render unquoted, parse as the int
    600, and this case would silently stop testing anything."""
    with pytest.raises(TaskError, match="budget.suite_timeout_s"):
        load_task(_budget_task(
            tmp_path, upstream, f"  suite_timeout_s: {yaml_value}\n"))


def test_a_boolean_suite_timeout_is_refused_rather_than_read_as_one_second(
    tmp_path, upstream
):
    """Its own test, because the failure mode differs from every value above:
    those are visibly wrong, and this one is ACCEPTED. `bool` IS an `int` in
    Python, so `int(True)` is 1 and `suite_timeout_s: true` kills every gated
    and graded command after one second -- a NO-GO at the gate and, past it,
    `timed_out` stamped on every arm -- for a YAML typo. `isinstance(value,
    bool)` must be tested BEFORE `isinstance(value, int)`; folded into it the
    bool branch is dead code a mutation cannot catch."""
    with pytest.raises(TaskError, match="budget.suite_timeout_s"):
        load_task(_budget_task(tmp_path, upstream, "  suite_timeout_s: true\n"))


def test_a_suite_timeout_over_the_agents_wall_clock_is_refused_with_the_sum(
    tmp_path, upstream
):
    """The agent re-runs this suite INSIDE `wall_clock_timeout_s` and there is
    no per-command bound in its container, so a suite the author says may need
    longer than the agent's whole run is a task no arm can verify even once --
    the run is SIGTERMed mid-suite and the diff is unchecked. Spec section 3.3
    measures a loop that ends in "runs tests, sees failures, self-corrects";
    this is that loop truncated, and it is settled by arithmetic over two
    manifest numbers, so it is a LOAD error rather than a preflight one.

    The message must carry both numbers: an author told only "too large" has
    to guess which of the two to move."""
    with pytest.raises(TaskError) as exc:
        load_task(_budget_task(tmp_path, upstream, (
            "  wall_clock_timeout_s: 900\n"
            "  suite_timeout_s: 1800\n"
        )))
    message = str(exc.value)
    assert "1800" in message and "900" in message
    assert "wall_clock_timeout_s" in message


def test_a_suite_timeout_equal_to_the_wall_clock_loads(tmp_path, upstream):
    """Strictly `>`, not `>=`. Equality leaves the agent exactly one suite run
    and no editing time, which is degenerate -- but "degenerate" is a judgement
    about how much slack an agent needs, and this rule asserts only what
    arithmetic settles. Pinned so a later tightening is a deliberate change
    rather than an unnoticed one."""
    task = load_task(_budget_task(tmp_path, upstream, (
        "  wall_clock_timeout_s: 900\n"
        "  suite_timeout_s: 900\n"
    )))
    assert task.budget.suite_timeout_s == 900


def test_the_default_budget_pair_is_self_consistent():
    """The defaults must not be a pair the loader would refuse: 600 <= 900."""
    assert TaskBudget().suite_timeout_s <= TaskBudget().wall_clock_timeout_s


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
    ],
)
def test_a_grading_key_that_is_not_argv_is_refused_at_load(
    tmp_path, upstream, block, match
):
    """A shell string is the shape an author reaches for, and it is the one
    that survives quietly: `"ruff check ."` is iterable, so a loader that
    only stored it hands the grader `tuple("ruff check .")` -- one argv
    element per CHARACTER. That is an exec failure recorded as a lint verdict
    against the submission, in a per-record grade nobody re-derives."""
    task_dir = _write_task(tmp_path / "set", upstream, extra_yaml=block)

    with pytest.raises(TaskError, match=match):
        load_task(task_dir)


@pytest.mark.parametrize(
    "block",
    [
        "grading: ruff",
        # Falsy non-mappings, and the reason `or {}` is not good enough: each
        # of these is a section the author wrote and the loader would read as
        # one they never wrote.
        "grading: []",
        "grading: ''",
        "grading: 0",
    ],
)
def test_a_grading_section_that_is_not_a_mapping_is_refused_at_load(
    tmp_path, upstream, block
):
    """No key-level check reaches these -- there are no keys. `data.get(k) or
    {}` cannot tell them from absent, so `grading: []` (the shape an author
    who started a list and never wrote the keys leaves behind) would load as
    a task declaring nothing and grade `not_configured` on all three checks,
    forever, in an append-only store."""
    task_dir = _write_task(tmp_path / "set", upstream, extra_yaml=block)

    with pytest.raises(TaskError, match="grading must be a mapping"):
        load_task(task_dir)


def test_a_misspelled_grading_key_is_refused_rather_than_dropped(
    tmp_path, upstream
):
    """The one failure preflight structurally cannot catch. Preflight asserts
    the DECLARED argvs run in the image, and a key typo declares nothing --
    `linter:` yields `TaskGrading((), (), ())`, which is byte-identical to a
    task with no linter. So the check goes NOT_CONFIGURED on every arm, every
    sample, and the manifest says otherwise in plain sight. A manifest asking
    for something the loader does not know is a load error, not a silent
    skip, and the message has to name the allowed set or the author's next
    guess is another typo."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='grading:\n  linter: ["ruff"]'
    )

    with pytest.raises(TaskError, match="unknown grading key") as excinfo:
        load_task(task_dir)
    assert "linter" in str(excinfo.value)
    assert "typecheck" in str(excinfo.value), "the message must name the allowed set"


def test_a_misspelled_image_key_is_refused_rather_than_dropped(
    tmp_path, upstream
):
    """The `image:` twin of the check above, and the one typo NOTHING
    downstream can see. Preflight reads `python --version` back out of the
    finished container and compares it against `task.image.python` -- so a
    manifest asking for `pyhton: "3.11"` loads as the 3.12 default, builds a
    3.12 base, and the read-back agrees with the field it was compared to.
    Every gate goes green while the suite runs under an interpreter the author
    did not ask for. `grading:` has had this guard since it shipped; the same
    four lines were simply never written for `image:`."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  pyhton: "3.11"'
    )

    with pytest.raises(TaskError, match="unknown image key") as excinfo:
        load_task(task_dir)
    assert "pyhton" in str(excinfo.value)
    assert "python" in str(excinfo.value), "the message must name the allowed set"


def test_strip_paths_loads_and_is_exposed(tmp_path, upstream):
    """A manifest key nothing can read is configuration nobody can report.
    `materialize`, `build_task_image` and preflight all need this list."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml='strip_paths: ["CLAUDE.md", ".claude"]',
    )

    task = load_task(task_dir)

    assert task.strip_paths == ("CLAUDE.md", ".claude")


def test_a_manifest_with_no_strip_paths_still_loads(tmp_path, upstream):
    """Every manifest written before this key existed keeps loading, and the
    absent case is one value rather than a None every caller re-decides."""
    assert load_task(_write_task(tmp_path / "set", upstream)).strip_paths == ()


@pytest.mark.parametrize(
    "bad, match",
    [
        ("", "empty or padded"),
        (" CLAUDE.md", "empty or padded"),
        ("/etc/passwd", "relative and free of"),
        ("../outside", "relative and free of"),
        (".", "names the whole tree"),
        ("./", "names the whole tree"),
        (".git", "own .git"),
        (".git/hooks", "own .git"),
        ("*.log", "pathspec magic"),
        ("docs/*", "pathspec magic"),
        (":(glob)**/x", "pathspec magic"),
    ],
)
def test_a_strip_path_that_would_remove_the_wrong_thing_is_refused(
    tmp_path, upstream, bad, match
):
    """This key's effect is a DELETE, so the validation is stricter than
    `_validate_prefixes` alone.

    `.` and `./` pass every check that function makes -- measured,
    `PurePosixPath(".").parts` is `()` and `is_relative_to(".")` is True for
    every path -- and name the whole tree, so the start state would be emptied
    and `start_sha` would still be a pure function of the manifest. `.git`
    would take the repository the submission diff is computed against. Glob
    and pathspec magic would make what gets removed a property of the tree
    rather than of the manifest, and the existence check in `materialize`
    would then pass on one accidental match.

    `match` pins the specific refusal, not just the `where` prefix every
    message carries -- a generic `match="strip_paths"` would pass even if
    every branch below raised the same message."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml=f"strip_paths: [{bad!r}]",
    )

    with pytest.raises(TaskError, match=match):
        load_task(task_dir)


def test_a_stripped_path_in_the_solution_half_is_refused(tmp_path, upstream):
    """Strip does NOT imply exclusion, and this is why.

    Dropping the chunk silently would make `solution_diff` something other
    than the merged PR (section 3.2's verbatim reference), and preflight would
    only notice when the missing hunk happened to be one the f2p tests need.
    The surviving case is a reference that is no longer a reference, with
    every gate green."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: ["calc.py"]',
    )

    with pytest.raises(TaskError, match=r"strip_paths.*calc\.py.*solution"):
        load_task(task_dir)


def test_a_stripped_path_in_the_test_half_is_refused(tmp_path, upstream):
    """The worse direction: the oracle shrinks, and every arm is then graded
    against less than the task says it is graded against."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: ["tests"]',
    )

    with pytest.raises(TaskError, match=r"test_calc\.py.*test half"):
        load_task(task_dir)


def test_a_stripped_path_excluded_from_both_halves_loads(tmp_path, upstream):
    """The sanctioned combination, and the one HARVESTING.md already sends an
    author to: `allow_extra_paths` puts the file in neither half, so nothing
    tries to apply a patch onto a path the strip removed -- and `extra_files`
    still names it, so the combination is visible rather than inferred from
    two keys that never mention each other.

    Load-only here on purpose: the refusal this task adds is a load-time one,
    and the strip that makes the combination true end to end lands in Task 3.
    `test_a_stripped_extra_path_is_gone_from_the_start_state` there is the
    other half."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            '  allow_extra_paths: ["CHANGES.md"]\n'
            'strip_paths: ["CHANGES.md"]'
        ),
    )
    (task_dir / "reference.diff").write_text(
        upstream["reference"] + CHANGELOG_CHUNK
    )

    task = load_task(task_dir)

    assert task.extra_files == ("CHANGES.md",)
    assert "CHANGES.md" not in task.solution_diff
    assert "CHANGES.md" not in task.test_diff


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


def test_strip_paths_removes_the_path_in_the_setup_commit(tmp_path, upstream):
    """One commit onto base, not two.

    `start_sha` is pinned in the manifest and verified on every
    materialization; its job is that one manifest names one tree. A second
    commit would still be deterministic but would make `start_sha` describe a
    two-step history for some tasks and a one-step history for others."""
    task = load_task(_write_task(
        tmp_path / "set", upstream,
        extra_yaml='strip_paths: ["CLAUDE.md", ".claude", "vendor"]',
    ))
    repo = tmp_path / "run" / "repo"

    start = materialize(task, repo, tmp_path / "cache")

    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".claude").exists()
    assert not (repo / "vendor").exists()
    tracked = _sh("git", "ls-tree", "-r", "--name-only", start, cwd=repo)
    assert "CLAUDE.md" not in tracked
    assert "vendor/dep.py" not in tracked
    assert "calc.py" in tracked, "the strip must remove only what it names"
    assert not _sh("git", "status", "--porcelain", cwd=repo), \
        "committed, not left dirty"
    assert _sh("git", "rev-list", "--count", f"{upstream['base']}..{start}",
               cwd=repo) == "1"


def test_declaring_strip_paths_moves_the_start_state(tmp_path, upstream):
    """The modification is in the manifest, so it has to be in the sha every
    record names. A strip invisible to `start_sha` would be a change to what
    every arm was asked to do that no stored record could distinguish."""
    plain = load_task(_write_task(tmp_path / "a", upstream))
    stripped = load_task(_write_task(
        tmp_path / "b", upstream, extra_yaml='strip_paths: ["CLAUDE.md"]',
    ))

    assert materialize(plain, tmp_path / "ra" / "repo", tmp_path / "cache") \
        != materialize(stripped, tmp_path / "rb" / "repo", tmp_path / "cache")


def test_an_empty_strip_paths_does_not_move_the_start_state(tmp_path, upstream):
    """Backwards compatibility, stated as a property. Every manifest written
    before this key existed -- the click task among them, whose `start_sha` is
    pinned in its task.yaml -- must materialize to exactly what it did
    before, so an absent key and an empty list have to be the same state and
    neither may run a git call."""
    absent = load_task(_write_task(tmp_path / "a", upstream))
    empty = load_task(_write_task(
        tmp_path / "b", upstream, extra_yaml="strip_paths: []",
    ))

    assert materialize(absent, tmp_path / "ra" / "repo", tmp_path / "cache") \
        == materialize(empty, tmp_path / "rb" / "repo", tmp_path / "cache")


def test_a_declared_strip_path_that_is_not_in_the_tree_is_refused(
    tmp_path, upstream
):
    """The failure this key must not introduce. A typo strips nothing, the
    file stays in the start state, and nothing downstream says so: preflight's
    context-file check knows four names, so a mistyped vendored tree or a
    fifth agent-file spelling passes every gate and the confound is permanent
    in an append-only log. `--ignore-unmatch` is deliberately absent."""
    task = load_task(_write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: [".cluade"]',
    ))

    with pytest.raises(TaskError, match=r"strip_paths.*\.cluade"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


def test_an_untracked_path_is_not_something_a_strip_can_remove(
    tmp_path, upstream
):
    """The existence check asks git about TRACKED content, because only
    tracked content is in the tree `start_sha` names. It also reads
    `ls-files`'s OUTPUT rather than its exit code: measured,
    `git ls-files -z -- nope` exits 0 with empty stdout, which is the silent
    zero `container._checked_exec` exists to refuse.

    The same rule is why `strip_paths` cannot name a file the PR CREATES,
    even when `allow_extra_paths` also names it."""
    (upstream["path"] / "scratch.txt").write_text("untracked\n")
    task = load_task(_write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: ["scratch.txt"]',
    ))

    with pytest.raises(TaskError, match=r"strip_paths.*scratch\.txt"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


def test_a_stripped_extra_path_is_gone_from_the_start_state(tmp_path, upstream):
    """The other half of Task 2's `..._loads`: the exclusion and the strip are
    enforced in two different places, and only a materialization shows they
    agree -- `allow_extra_paths` keeps the file out of both halves, the strip
    removes it, and nothing tries to apply a patch onto a path that is gone.

    It also demonstrates the constraint task authors have to know about: the
    strip needs the path to be TRACKED at base_sha, so this combination works
    for a changelog the PR edits and not for one it creates."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            '  allow_extra_paths: ["CHANGES.md"]\n'
            'strip_paths: ["CHANGES.md"]'
        ),
    )
    (task_dir / "reference.diff").write_text(
        upstream["reference"] + CHANGELOG_CHUNK
    )
    task = load_task(task_dir)
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert not (repo / "CHANGES.md").exists()


def test_the_strip_runs_before_the_gitignore_is_written(tmp_path, upstream):
    """Order: strip, then the test half, then `gitignore_extra`, then one
    commit. A stripped path the TEST half re-creates cannot occur -- the
    loader refuses that overlap -- so `.gitignore` is the only path a later
    step can legitimately re-create under a strip prefix, and strip-first is
    what makes that deterministic. Reversed, the strip would delete the file
    the manifest just asked for."""
    task = load_task(_write_task(
        tmp_path / "set", upstream,
        extra_yaml='strip_paths: [".gitignore"]\ngitignore_extra: ["*.log"]',
    ))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    text = (repo / ".gitignore").read_text()
    assert "*.log" in text
    assert "__pycache__/" not in text, "upstream's .gitignore was stripped"


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


# --- image.env ---------------------------------------------------------------


def test_image_env_defaults_to_empty_and_an_old_manifest_still_loads(
    tmp_path, upstream
):
    """Every manifest written before this key existed must load unchanged, and
    the absent case has to be ONE value rather than a None every caller
    re-decides -- the reason `grading` is defaulted the same way."""
    task_dir = _write_task(tmp_path / "set", upstream)  # no image: block

    assert load_task(task_dir).image.env == {}


def test_image_env_is_carried_verbatim(tmp_path, upstream):
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            "image:\n"
            '  env:\n'
            '    CI: "1"\n'
            '    HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"\n'
        ),
    )

    assert load_task(task_dir).image.env == {
        "CI": "1",
        "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/bakeoff-hypothesis",
    }


def test_a_key_outside_the_allowlist_is_refused(tmp_path, upstream):
    """An unrestricted image.env re-opens the hole the env ALLOWLIST exists to
    close from the other side: CLAUDE_CODE_USE_BEDROCK is kept out of the host
    environment by PASSTHROUGH_ENV and is not set by container_env, so an image
    ENV carrying it would reach the agent, make the CLI ignore
    ANTHROPIC_BASE_URL, bypass the proxy, and leave the mandatory wire log
    empty with the run still looking normal."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml='image:\n  env:\n    CLAUDE_CODE_USE_BEDROCK: "1"\n',
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "CLAUDE_CODE_USE_BEDROCK" in str(exc.value)
    assert "HYPOTHESIS_STORAGE_DIRECTORY" in str(exc.value)  # names the set


def test_an_image_env_that_is_not_a_mapping_is_refused(tmp_path, upstream):
    """`image: {env: []}` is what an author who started a list and never wrote
    the keys leaves behind. `or {}` cannot tell it from absent, so it would
    load as a task declaring no environment -- and a hypothesis suite whose
    determinism lever silently never applied is a suite that sometimes
    passes."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml="image:\n  env: []\n"
    )

    with pytest.raises(TaskError, match="expected a mapping"):
        load_task(task_dir)


def test_the_allowlist_and_the_harness_pinned_keys_are_disjoint():
    """Checked as a property of the two sets, not of one manifest.

    An allowlist entry naming a key `_eval_env` also sets would be silently
    overridden on the AGENT's exec while still applying to preflight's and the
    grader's -- two environments for one task, with nothing in the record
    saying which. This is what makes adding a careless allowlist entry a red
    suite rather than a bad eval.

    It does NOT cover the proxy-bypass hole, and the second assertion is what
    says so. `CLAUDE_CODE_USE_BEDROCK` / `_USE_VERTEX` are pinned by ABSENCE
    -- `PASSTHROUGH_ENV` keeps them out and `_eval_env` never sets them -- so
    `pinned_env_keys()` cannot see them and this disjointness holds vacuously
    for exactly the two keys that matter most. They are named literally
    instead, because an image `ENV CLAUDE_CODE_USE_BEDROCK=1` would make the
    CLI ignore `ANTHROPIC_BASE_URL`, bypass the proxy, and leave the mandatory
    wire log empty with the run still looking normal."""
    from bakeoff.claude_runner import pinned_env_keys

    assert not (tasks._IMAGE_ENV_ALLOWED & pinned_env_keys())
    assert not (tasks._IMAGE_ENV_ALLOWED
                & {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"})


def test_a_pinned_key_is_refused_even_if_someone_allowlists_it(
    tmp_path, upstream, monkeypatch
):
    """Belt and braces, and the braces are the half that survives a future
    edit: the allowlist is what an author reads, and this refusal is what
    catches an entry added to it without reading decision 6."""
    monkeypatch.setattr(
        tasks, "_IMAGE_ENV_ALLOWED",
        tasks._IMAGE_ENV_ALLOWED | {"CLAUDE_CONFIG_DIR"},
    )
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml='image:\n  env:\n    CLAUDE_CONFIG_DIR: "/elsewhere"\n',
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "the harness sets" in str(exc.value)


@pytest.mark.parametrize("literal", [
    '"/tmp/a\\nb"',        # a newline would end the ENV line early
    "'/tmp/a\"b'",         # the value is emitted double-quoted
    "'/tmp/a\\\\b'",       # a backslash continues a Dockerfile line
    '"$HOME/hyp"',         # Docker EXPANDS this against the build environment
])
def test_a_value_that_would_not_survive_a_dockerfile_line_is_refused(
    tmp_path, upstream, literal
):
    """The `$` case is the one that is not about syntax. Docker expands $VAR
    in an ENV value against the BUILD environment, so the recorded value would
    be a property of the builder rather than of the manifest -- the same
    argument that refuses pathspec magic in strip_paths.

    Written as YAML literals rather than Python strings because the loader
    reads YAML: `"a\\nb"` in double quotes is a real newline to the parser,
    which is the shape that has to be refused."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            f"image:\n  env:\n    HYPOTHESIS_STORAGE_DIRECTORY: {literal}\n"
        ),
    )

    with pytest.raises(TaskError):
        load_task(task_dir)


@pytest.mark.parametrize("extra_yaml", [
    "image:\n  env:\n    CI: 1\n",      # a YAML int, not a string
    'image:\n  env:\n    CI: ""\n',     # a string, but empty
])
def test_a_non_string_or_empty_env_value_is_refused(
    tmp_path, upstream, extra_yaml
):
    """The fourth `_env_map` refusal has no coverage elsewhere: a YAML scalar
    that is not a string (`CI: 1` parses as an int) and a value that is a
    string but empty (`CI: ""`) both fail `isinstance(raw_value, str) and
    raw_value` and must be refused before the char-level checks above ever
    run."""
    task_dir = _write_task(tmp_path / "set", upstream, extra_yaml=extra_yaml)

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "non-empty string" in str(exc.value)


@pytest.mark.parametrize("value", ["relative/path", "/repo", "/repo/.hyp"])
def test_a_storage_directory_inside_the_repo_is_refused(
    tmp_path, upstream, value
):
    """The key exists to keep hypothesis's writes out of the tree the §5.6
    submission diff is taken against. Pointed back into /repo it undoes
    exactly that, and what lands in the tree becomes a property of a string an
    author typed."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            f'image:\n  env:\n    HYPOTHESIS_STORAGE_DIRECTORY: "{value}"\n'
        ),
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "/repo" in str(exc.value)


def test_the_repo_mount_constant_matches_the_container_it_describes():
    """tasks.py spells /repo itself rather than importing REPO_MOUNT, because
    container.py imports `docker` at module level and the loader deliberately
    does not depend on a daemon. The drift belongs here."""
    from bakeoff.container import REPO_MOUNT

    assert tasks._REPO_MOUNT == REPO_MOUNT


# --- image.python -------------------------------------------------------------


def test_image_python_defaults_to_the_base_images_version(tmp_path, upstream):
    """Every manifest written before this key existed must load unchanged, and
    the default has to be the version the base Dockerfile's own ARG default
    builds -- two defaults that can drift is one manifest loading as a task
    whose image nobody built."""
    task_dir = _write_task(tmp_path / "set", upstream)  # no image: block

    assert load_task(task_dir).image.python == "3.12"


def test_the_default_is_itself_an_allowlisted_version():
    """`_python_version` returns the default BEFORE the allowlist check, so a
    default outside the set is the one value that reaches a build ungated --
    every manifest that declares nothing, which is all of them today."""
    assert tasks._DEFAULT_PYTHON in tasks._PYTHON_VERSIONS


def test_an_allowlisted_version_is_carried_verbatim(tmp_path, upstream):
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  python: "3.11"\n'
    )

    assert load_task(task_dir).image.python == "3.11"


def test_a_version_nobody_built_is_refused_at_load_time(tmp_path, upstream):
    """Measured 2026-09-01: `--build-arg BASE_PYTHON_VERSION=3.99` fails with
    `failed to resolve reference "docker.io/library/python:3.99-slim-bookworm"
    ... not found` -- a registry round-trip, mid-build, on a machine that may
    be offline. The allowlist turns that into a message naming the manifest."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  python: "3.99"\n'
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "3.99" in str(excinfo.value)
    assert "3.12" in str(excinfo.value)  # the allowlist is in the message


def test_a_floating_major_version_is_refused(tmp_path, upstream):
    """`python:3-slim-bookworm` resolves and is republished, so two collections
    months apart run different interpreters under one manifest and no record
    says so. Same defect `image.pip`'s "pinned, not floored" rule prevents."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  python: "3"\n'
    )

    with pytest.raises(TaskError):
        load_task(task_dir)


def test_an_unquoted_version_is_refused_rather_than_coerced(tmp_path, upstream):
    """YAML parses bare `3.11` as a float and bare `3.10` as `3.1`. Coercing
    with str() would turn the second into a version nobody named; the refusal
    names the quotes."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml="image:\n  python: 3.11\n"
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "quote" in str(excinfo.value).lower()


def test_the_worked_example_task_still_loads_on_the_default(tmp_path):
    """Backwards compatibility, stated over the real manifest rather than a
    fixture: click declares no `python:` and must keep the base it has."""
    task = load_task(
        Path(__file__).resolve().parent.parent
        / "taskset" / "click-3360-write-usage-empty-args"
    )

    assert task.image.python == "3.12"


def test_declaring_a_python_version_moves_the_digest_but_not_the_start_state(
    tmp_path, upstream
):
    """`manifest_digest` hashes the manifest BYTES, so the key participates
    with no term of its own and every preflight and oracle cache keyed on it
    invalidates. `start_sha` must NOT move: the key takes no part in the setup
    commit, and a task whose start state moved is a different task."""
    plain = load_task(_write_task(tmp_path / "a", upstream))
    pinned = load_task(
        _write_task(tmp_path / "b", upstream,
                    extra_yaml='image:\n  python: "3.11"\n')
    )

    assert pinned.manifest_digest != plain.manifest_digest
    assert materialize(pinned, tmp_path / "tb" / "repo", tmp_path / "cb") == \
        materialize(plain, tmp_path / "ta" / "repo", tmp_path / "ca")


# --- submodules --------------------------------------------------------------


def _sub_task(tmp_path, up, **overrides):
    return load_task(_write_task(tmp_path / "set", up, **overrides))


def test_a_repository_with_no_submodules_derives_an_empty_tuple(tmp_path, upstream):
    task = _sub_task(tmp_path, upstream)
    mirror = tasks.ensure_mirror(str(upstream["path"]), upstream["base"],
                                 tmp_path / "cache")

    assert tasks.derive_submodules(task, mirror) == ()


def test_the_gitlink_and_the_gitmodules_blob_are_both_read(tmp_path,
                                                           upstream_submodule,
                                                           local_urls):
    """Path and sha come from `ls-tree`, url from `.gitmodules`, name from the
    section header -- all four from git's own parsers over NUL-delimited output.
    """
    up = upstream_submodule
    task = _sub_task(tmp_path, up)
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    (sub,) = tasks.derive_submodules(task, mirror)

    assert sub.path == "vendor/libdep"
    assert sub.name == "vendor/libdep"
    assert sub.sha == up["pinned"]
    assert sub.url == str(up["lib"])


def test_a_gitlink_with_no_gitmodules_url_is_refused(tmp_path,
                                                     upstream_submodule,
                                                     local_urls):
    """The quiet shape: no url, so the directory would simply stay empty and
    the suite would fail to collect on every arm -- with `git status
    --porcelain` reporting the tree as clean throughout.
    """
    up = upstream_submodule
    _sh("git", "rm", "-q", "--cached", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "drop .gitmodules", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base})
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    with pytest.raises(TaskError, match="vendor/libdep"):
        tasks.derive_submodules(task, mirror)


def test_a_gitmodules_stanza_with_a_path_and_no_url_is_refused(
        tmp_path, upstream_submodule):
    """A THIRD shape, between the two neighbouring tests: `.gitmodules` is
    readable and the stanza names this exact path, so both the set difference
    and the "no readable .gitmodules" refusal are satisfied and neither fires.
    What is missing is the url alone, which leaves `Submodule.url` an empty
    string -- and an empty string is not a url, it is a stanza someone
    hand-edited or a `git submodule add` that never finished.

    Deliberately WITHOUT the `local_urls` fixture. That fixture empties
    `_SUBMODULE_URL_PREFIX` so `startswith` is vacuously true, and `""` starts
    with `""` -- so under it this refusal cannot fire at all, and a test that
    used it would pass with the check deleted.
    """
    up = upstream_submodule
    (up["path"] / ".gitmodules").write_text(
        '[submodule "vendor/libdep"]\n\tpath = vendor/libdep\n'
    )
    _sh("git", "add", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "drop the url", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base})
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    with pytest.raises(TaskError, match="declares no url") as excinfo:
        tasks.derive_submodules(task, mirror)

    # `no url`, never `url ''`: an empty value reads as a url that is present
    # and strange rather than one that was never written.
    assert "vendor/libdep" in str(excinfo.value)


def test_a_gitmodules_entry_with_no_gitlink_is_NOT_refused(tmp_path,
                                                           upstream_submodule,
                                                           local_urls):
    """The other direction is inert, and refusing it would refuse a working
    task. Measured 2026-09-01, git 2.50.1: git drives everything off the
    index, so an orphaned stanza is not listed by `git submodule status`, is
    not fetched by `update --init` (exit 0), creates no directory and leaves
    the tree clean. The shape is real -- a submodule `git rm --cached`'d with
    its .gitmodules stanza left behind. Preflight records the name as
    `submodules_orphaned` instead.
    """
    up = upstream_submodule
    gitmodules = up["path"] / ".gitmodules"
    gitmodules.write_text(
        gitmodules.read_text()
        + '\n[submodule "vendor/gone"]\n\tpath = vendor/gone\n'
        '\turl = https://example.invalid/gone.git\n'
    )
    _sh("git", "add", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "orphan stanza", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base})
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    assert [s.path for s in tasks.derive_submodules(task, mirror)] \
        == ["vendor/libdep"]


def test_a_non_https_submodule_url_is_refused(tmp_path, upstream_submodule):
    """The fixture's own url is a local path, which is exactly the shape that
    must be refused in production -- so this test needs no extra setup, while
    every OTHER test in this section relaxes the constant (see `local_urls`).
    """
    up = upstream_submodule
    task = _sub_task(tmp_path, up)
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    with pytest.raises(TaskError, match="https://"):
        tasks.derive_submodules(task, mirror)


def test_a_strip_path_covering_a_submodule_is_refused(tmp_path,
                                                      upstream_submodule,
                                                      local_urls):
    up = upstream_submodule
    task = _sub_task(tmp_path, up, extra_yaml='strip_paths: ["vendor/libdep"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    with pytest.raises(TaskError, match="strip_paths"):
        tasks.derive_submodules(task, mirror)


def test_a_reference_diff_touching_a_submodule_path_is_refused(
        tmp_path, upstream_submodule, local_urls):
    """Measured 2026-09-01: `git apply` WITHOUT `--index` applies such a patch
    exit 0 and edits submodule content, which is how preflight's green-after
    check would pass on a fix no submission diff can ever contain.
    """
    up = upstream_submodule
    reference = up["reference"] + (
        "diff --git a/vendor/libdep/libdep/__init__.py"
        " b/vendor/libdep/libdep/__init__.py\n"
        "--- a/vendor/libdep/libdep/__init__.py\n"
        "+++ b/vendor/libdep/libdep/__init__.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )
    task = _sub_task(tmp_path, {**up, "reference": reference})
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    with pytest.raises(TaskError, match="reference diff"):
        tasks.derive_submodules(task, mirror)


def test_a_gitlink_whose_only_gitmodules_stanza_names_another_path_is_refused(
        tmp_path, upstream_submodule, local_urls):
    """The set difference, with a READABLE `.gitmodules` on the other side.

    The neighbouring refusal covers "no .gitmodules at all"; this one covers
    the shape that actually happens -- a stanza edited to a path the tree does
    not carry, leaving the real gitlink with nothing to fetch from while every
    other check still passes. Both directions are exercised at once: the
    orphaned name is inert (it is not what the message names) and the
    url-less gitlink is fatal.
    """
    up = upstream_submodule
    (up["path"] / ".gitmodules").write_text(
        '[submodule "vendor/gone"]\n\tpath = vendor/gone\n'
        '\turl = https://example.invalid/gone.git\n'
    )
    _sh("git", "add", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "point the only stanza elsewhere", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base})
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    with pytest.raises(TaskError, match="no .gitmodules url") as excinfo:
        tasks.derive_submodules(task, mirror)
    assert "vendor/libdep" in str(excinfo.value)
    assert "vendor/gone" not in str(excinfo.value)


def _materialize_sub(tmp_path, up, **overrides):
    """Materialize a submodule task. Requires the `local_urls` fixture."""
    task = _sub_task(tmp_path, up, **overrides)
    start = materialize(task, tmp_path / "run", tmp_path / "cache")
    return task, tmp_path / "run", start


def test_materialize_populates_the_submodule_at_the_gitlink(
        tmp_path, upstream_submodule, local_urls):
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)

    assert (run / "vendor" / "libdep" / "libdep" / "__init__.py").read_text() \
        == SUB_LIB
    assert _sh("git", "-C", "vendor/libdep", "rev-parse", "HEAD", cwd=run) \
        == up["pinned"]


def test_the_run_trees_submodule_cannot_reach_the_future(
        tmp_path, upstream_submodule, local_urls):
    """The leak argument one level down. Measured 2026-09-01: `git submodule
    update --init` against the REAL url clones the whole submodule history,
    so without the pruned mirror `git -C vendor/libdep log main` hands the
    agent content newer than the pin -- differentially, since only an arm
    that looks collects it.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)
    sub = run / "vendor" / "libdep"

    found = subprocess.run(["git", "cat-file", "-e", up["future"]],
                           cwd=sub, capture_output=True)

    assert found.returncode != 0


def test_initialising_a_submodule_does_not_move_start_sha(
        tmp_path, upstream_submodule, local_urls):
    """`materialize` initialises AFTER `start_sha` is settled, so the pin is a
    property of the ordering rather than of `git add -A` happening not to
    stage a gitlink.
    """
    up = upstream_submodule
    task, run, start = _materialize_sub(tmp_path, up)
    head_tree = _sh("git", "rev-parse", "HEAD^{tree}", cwd=run)

    assert start == _sh("git", "rev-parse", "HEAD", cwd=run)
    assert head_tree == _sh("git", "rev-parse", f"{start}^{{tree}}", cwd=run)


def test_the_materialized_tree_is_clean_after_initialisation(
        tmp_path, upstream_submodule, local_urls):
    """The leading SPACE is the whole assertion. Measured: `-` is
    uninitialised (and is what a transient `-c submodule.<n>.url=` leaves
    behind), `+` is initialised at the wrong commit, and a space is the one
    acceptable state. Preflight gates on the same character in Task 4.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)
    # NOT `_sh`, which strips -- and the leading space is the whole assertion,
    # so a stripped read cannot tell the acceptable state from the two
    # unacceptable ones.
    status = subprocess.run(["git", "submodule", "status"], cwd=run,
                            check=True, capture_output=True, text=True).stdout

    assert _sh("git", "status", "--porcelain", cwd=run) == ""
    assert status.startswith(f" {up['pinned']}")


def test_the_run_tree_carries_no_host_cache_path_for_the_submodule(
        tmp_path, upstream_submodule, local_urls):
    """The WHOLE `.git/modules` subtree, not the config files.

    Measured 2026-09-01, git 2.50.1: after `remote remove origin` and the url
    rewrite the config files are already clean, and the cache path survives in
    `logs/HEAD` and `logs/refs/heads/main` as `clone: from /…/cache/repos/…`.
    A config-only assertion therefore passes with the leak present -- which is
    exactly what the first draft of this test did. The `reflog expire` in
    `_init_submodules` is what clears both, so this test is what makes that
    call load-bearing rather than deletable.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)
    cache = str(tmp_path / "cache")

    leaking = [
        path for path in (run / ".git" / "modules").rglob("*")
        if path.is_file() and cache in path.read_text(errors="replace")
    ]

    assert leaking == []
    assert cache not in (run / ".git" / "config").read_text()
    assert str(up["lib"]) in (run / ".git" / "config").read_text()


def test_an_unreachable_submodule_mirror_is_a_task_error(
        tmp_path, upstream_submodule, local_urls):
    """Loud, not empty. Measured: `git submodule update --init` exits 1 both
    when the url does not resolve and when the file transport is refused, and
    an empty submodule directory leaves `git status --porcelain` clean.
    """
    up = upstream_submodule
    shutil.rmtree(up["lib"])

    with pytest.raises(TaskError):
        _materialize_sub(tmp_path, up)


def test_a_nested_submodule_is_refused(tmp_path, upstream_submodule,
                                       local_urls):
    """`submodule update --init` without `--recursive` leaves the inner one
    empty, which is the same silence one level further down.

    Two details this test got wrong once and is now pinned against. The
    superproject is detached back to `up["base"]` before the gitlink moves, so
    the new base still carries the pre-fix tree and the reference diff still
    applies -- otherwise `materialize` raises out of `git apply --index` long
    before a submodule is touched. And `match=` names the message rather than
    the word "nested": `pytest.raises(match=...)` searches the whole exception
    string, this test's own tmp_path contains "nested", and the loose pattern
    therefore matched that `git apply` failure -- green against a tree with no
    refusal in it at all.
    """
    up = upstream_submodule
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "x.py").write_text("X = 1\n")
    _sh("git", "init", "-q", cwd=inner)
    _sh("git", "config", "user.email", "t@t.test", cwd=inner)
    _sh("git", "config", "user.name", "t", cwd=inner)
    _sh("git", "add", "-A", cwd=inner)
    _sh("git", "commit", "-q", "-m", "inner", cwd=inner)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(inner), "inner", cwd=up["lib"])
    _sh("git", "commit", "-q", "-m", "nest", cwd=up["lib"])
    nested = _sh("git", "rev-parse", "HEAD", cwd=up["lib"])
    _sh("git", "checkout", "-q", "--detach", up["base"], cwd=up["path"])
    _sh("git", "-c", "protocol.file.allow=always", "-C", "vendor/libdep",
        "fetch", "-q", "origin", cwd=up["path"])
    _sh("git", "-C", "vendor/libdep", "checkout", "-q", nested, cwd=up["path"])
    _sh("git", "add", "-A", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "repin at the nested commit", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])

    with pytest.raises(TaskError, match="submodules of its own"):
        _materialize_sub(tmp_path, {**up, "base": base})


def test_a_submodule_the_update_left_unpopulated_is_refused(
        tmp_path, upstream_submodule, local_urls, monkeypatch):
    """The post-condition, against a `submodule update` that exits 0 and does
    nothing -- which is the shape the whole function exists for, because an
    empty submodule directory leaves `git status --porcelain` clean and reads
    downstream as a suite that cannot import.

    The sha comparison is what catches it, and the emptiness check never runs.
    Measured 2026-09-01: `git -C vendor/libdep rev-parse HEAD` inside an EMPTY
    gitlink directory succeeds and answers with the SUPERPROJECT's HEAD --
    git walks up to the enclosing repository. So `head.returncode != 0` alone
    would pass here; comparing against `sub.sha` is the load-bearing half.
    """
    up = upstream_submodule
    real_git = tasks._git

    def a_git_whose_update_does_nothing(*args, **kwargs):
        if "submodule" in args and "update" in args:
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(tasks, "_git", a_git_whose_update_does_nothing)

    with pytest.raises(TaskError, match="not at its gitlink") as excinfo:
        _materialize_sub(tmp_path, up)
    assert up["pinned"] in str(excinfo.value)
