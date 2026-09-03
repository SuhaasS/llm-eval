"""The task loader, which is the harness's only defence against a bad input.

Every case here is a way a task could be wrong such that the run still
completes and the record still looks ordinary. That is the whole class:
`git checkout --detach` onto a SHA that does not resolve leaves the tree
where it was, `git apply` of a re-cut patch changes what the agent was asked
to do, and a duplicate task_id collides in the event log only at the far end
of a matrix, after the tokens are spent.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bakeoff import tasks
from bakeoff.tasks import (
    RefusedManifest,
    TaskBudget,
    TaskError,
    TaskGrading,
    diff_chunks,
    load_task,
    load_task_set,
    load_task_set_with_refusals,
    materialize,
    refusal_warnings,
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
def upstream_two_submodules(tmp_path):
    """`upstream_submodule` with a SECOND gitlink, at `vendor/other`.

    Beside that fixture rather than parameterising it: every existing test in
    the submodule section depends on `upstream_submodule`'s exact halves, and
    a second gitlink changes the derivation's output for all of them.

    Yields a builder taking one keyword, `stanzas: int = 2`. At `stanzas=1`
    the fixture rewrites `.gitmodules` to carry the `vendor/libdep` stanza
    ALONE and commits that, which is the readable-but-no-stanza tree D4 row 2
    is about: `vendor/other` keeps its gitlink and loses its url, and is
    workable only when the manifest declares it unneeded.

    `-c protocol.file.allow=always` on every submodule-touching command, for
    the reason `upstream_submodule`'s docstring gives.
    """
    def _build(stanzas: int = 2):
        libs = {}
        for name in ("libdep", "other"):
            lib = tmp_path / f"two-{name}"
            (lib / name).mkdir(parents=True)
            (lib / name / "__init__.py").write_text(SUB_LIB)
            _sh("git", "init", "-q", cwd=lib)
            _sh("git", "config", "user.email", "t@t.test", cwd=lib)
            _sh("git", "config", "user.name", "t", cwd=lib)
            _sh("git", "add", "-A", cwd=lib)
            _sh("git", "commit", "-q", "-m", f"{name} v1", cwd=lib)
            libs[name] = (lib, _sh("git", "rev-parse", "HEAD", cwd=lib))

        repo = tmp_path / "super-two"
        (repo / "tests").mkdir(parents=True)
        (repo / "calc.py").write_text(BUGGY)
        (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
        _sh("git", "init", "-q", cwd=repo)
        _sh("git", "config", "user.email", "t@t.test", cwd=repo)
        _sh("git", "config", "user.name", "t", cwd=repo)
        _sh("git", "add", "-A", cwd=repo)
        _sh("git", "commit", "-q", "-m", "base", cwd=repo)
        for name, path in (("libdep", "vendor/libdep"),
                           ("other", "vendor/other")):
            _sh("git", "-c", "protocol.file.allow=always", "submodule", "add",
                "-q", str(libs[name][0]), path, cwd=repo)
        _sh("git", "add", "-A", cwd=repo)
        _sh("git", "commit", "-q", "-m", "pin both submodules", cwd=repo)
        if stanzas == 1:
            # The `vendor/libdep` stanza ALONE. `vendor/other` keeps its
            # gitlink and loses its url, which is the one shape the
            # `by_path.get` fallback in `derive_submodules` exists for.
            (repo / ".gitmodules").write_text(
                '[submodule "vendor/libdep"]\n'
                "\tpath = vendor/libdep\n"
                f"\turl = {libs['libdep'][0]}\n"
            )
            _sh("git", "add", ".gitmodules", cwd=repo)
            _sh("git", "commit", "-q", "-m", "drop the other stanza", cwd=repo)
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
        return {
            "path": repo, "base": base, "head": head, "reference": reference,
            "lib": libs["libdep"][0], "pinned": libs["libdep"][1],
            "sub_path": "vendor/libdep",
            "other_lib": libs["other"][0], "other_pinned": libs["other"][1],
            "sub_path_other": "vendor/other",
        }

    return _build


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
        # Raw YAML appended INSIDE the `tests:` block, after `f2p`. Separate
        # from `extra_yaml` because a `tests.` key has to be indented under a
        # mapping this helper already opened; and because a key repeated after
        # the default -- `runner:`, `f2p:` -- overrides it, PyYAML taking the
        # last occurrence, which is what lets a test restate one key without a
        # keyword for every key.
        "tests_extra": "",
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
    if data["tests_extra"]:
        lines.append(data["tests_extra"].rstrip("\n"))
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


_BROKEN_IMAGE_ENV = (
    "image:\n"
    "  env:\n"
    "    SETUPTOOLS_SCM_PRETEND_VERSION: '1.0'\n"
)


def _write_broken_task(root: Path, upstream, name="t-002") -> Path:
    """A sibling that refuses exactly the way the 2026-09-02 measurement did.

    The `image.env` allowlist is the real defect the probe hit; any refusal
    would exercise the collector, but this one keeps the test and the report
    describing one thing. `task_id` is set to the directory name because
    `_manifest`'s default is a fixed `t-001` and these tests turn on the
    selection matching directories by name.
    """
    return _write_task(root, upstream, name=name, task_id=name,
                       extra_yaml=_BROKEN_IMAGE_ENV)


def _git_task_set(root: Path, *, commit: str = "tasks") -> None:
    """Make the task-set directory a git repository with everything in it
    committed. Separate from the `upstream` fixture's repo: this one is the
    SET, and `_manifest_committed` reads its status."""
    _sh("git", "init", "-q", cwd=root)
    _sh("git", "config", "user.email", "t@t.test", cwd=root)
    _sh("git", "config", "user.name", "t", cwd=root)
    _sh("git", "add", "-A", cwd=root)
    _sh("git", "commit", "-q", "-m", commit, cwd=root)


def _write_node_task(root: Path, upstream, *, paths=("tests/",), f2p=None,
                     p2p=None, extra_yaml="") -> Path:
    """A `tests.framework: vitest` manifest over the same fixture repository.

    The repository underneath is still the Python one -- nothing in these
    tests runs a suite, and the reference diff only has to split under
    `paths`. What is under test is the LOADER, which is where a node manifest
    is refused or accepted.

    `extra_yaml` here is the BODY of the `image:` block, not a top-level
    block, because every image key these tests state (`node:`, `python:`)
    lives under one mapping and writing `image:\\n` in each caller would be
    four copies of the same line.
    """
    tests_extra = ["  framework: vitest"]
    if p2p is not None:
        tests_extra.append(f"  p2p: {json.dumps(list(p2p))}")
    return _write_task(
        root, upstream,
        paths=json.dumps(list(paths)),
        runner=json.dumps(["/node_modules/.bin/vitest", "run", "--no-cache"]),
        f2p=json.dumps(list(
            f2p if f2p is not None else ["tests/a.test.js::does a thing"]
        )),
        tests_extra="\n".join(tests_extra),
        extra_yaml=("image:\n" + extra_yaml.rstrip("\n")) if extra_yaml else "",
    )


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
    """`int("600")` and `int(600.0)` both SUCCEED, so a bare `int(...)` accepts
    a quoted or floated value silently, which is how the other two budget keys
    behaved until 2026-09-02 -- and the manifest stops being a faithful
    record. An explicit `null` is refused
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


@pytest.mark.parametrize(
    "yaml_value",
    ['"40"', "40.0", "3.7", "null", "0", "-1", "forty", "1e3", "[40]"],
)
def test_a_max_turns_that_is_not_a_positive_int_is_a_load_error(
    tmp_path, upstream, yaml_value
):
    """Until 2026-09-02 `max_turns` was parsed with a bare `int(...)`, which
    accepted three of these shapes silently: `"40"` -> 40, `40.0` -> 40, and
    `3.7` -> 3 (truncated, not rounded) -- a manifest that stops being a
    faithful record of what an arm was asked to do. `forty` and `null` raised
    a bare `ValueError`/`TypeError` with no manifest path in it. `1e3` is a
    `str` to PyYAML 6.0.3, not a float: YAML 1.1's float resolver requires a
    decimal point in the mantissa and a signed exponent (`1.0e+3`), so the
    shorthand an author reaches for to raise a bound by an order of magnitude
    is one of the cases that must now raise a named `TaskError`."""
    with pytest.raises(TaskError, match="budget.max_turns"):
        load_task(_budget_task(tmp_path, upstream, f"  max_turns: {yaml_value}\n"))


@pytest.mark.parametrize("yaml_value", ["true", "false"])
def test_a_boolean_max_turns_is_refused_rather_than_read_as_one_turn(
    tmp_path, upstream, yaml_value
):
    """`bool` IS an `int`, so `int(True)` is 1 and `int(False)` is 0 -- the
    failure mode differs from every value in the previous test because these
    were ACCEPTED. `max_turns: true` gave every arm of that task one API call
    and a record that reads as a model that stopped after one turn;
    `max_turns: false` gave it zero. Measured 2026-09-02 that the CLI does not
    save this: against claude 2.1.258 (the host binary; the eval image pins
    2.1.220) with `ANTHROPIC_BASE_URL` pointed at an unreachable port,
    `claude -p --output-format stream-json --verbose --max-turns 0 "say hi"`
    emits the `system/init` event and then `api_retry` events -- it starts a
    session and calls the API. `--max-turns -1` and `--max-turns 0.5` behave
    the same. Only a non-numeric argument is refused, by commander, before any
    network: `error: option '--max-turns <turns>' argument 'abc' is invalid.
    must be a number` -- and `--max-turns` is not listed in `claude --help` at
    all, so there is no downstream backstop."""
    with pytest.raises(TaskError, match="budget.max_turns"):
        load_task(_budget_task(tmp_path, upstream, f"  max_turns: {yaml_value}\n"))


@pytest.mark.parametrize(
    "yaml_value", ['"900"', "900.0", "null", "0", "-1", "forty", "[900]"]
)
def test_a_wall_clock_timeout_that_is_not_a_positive_int_is_a_load_error(
    tmp_path, upstream, yaml_value
):
    """`"900"` was accepted silently by the bare `int(...)` this replaced.
    This key is also read by `run_matrix`'s per-cell credential margin
    (`wall_clock_timeout_s + CELL_OVERHEAD_S`, handed to
    `proxy.credential_stop`), so a value nobody wrote used to propagate into
    a refusal-to-start decision about the SSO window."""
    with pytest.raises(TaskError, match="budget.wall_clock_timeout_s"):
        load_task(_budget_task(
            tmp_path, upstream, f"  wall_clock_timeout_s: {yaml_value}\n"))


@pytest.mark.parametrize(
    "body",
    [
        "  wall_clock_timeout_s: true\n",
        "  wall_clock_timeout_s: 0\n  suite_timeout_s: 1800\n",
    ],
)
def test_a_bad_wall_clock_names_its_own_key_not_the_suite_comparison(
    tmp_path, upstream, body
):
    """The item's measured defect, and the ordering pin. Before 2026-09-02,
    with no `suite_timeout_s` declared (the click shape), this manifest
    raised:

        budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s (1)
        by 599s ... Raise wall_clock_timeout_s, or lower suite_timeout_s to
        what the suite actually needs.

    600 is the dataclass default for a key the manifest never mentions, so
    the refusal named a key the author never wrote, did arithmetic over a
    boolean (`int(True)` is 1), and advised lowering the one number that was
    correct. The `not in` assertions below are the load-bearing half: with
    the validation removed the comparison still fires and still raises a
    `TaskError`, so a bare `pytest.raises(TaskError)` would pass on the bug."""
    with pytest.raises(TaskError) as exc:
        load_task(_budget_task(tmp_path, upstream, body))
    message = str(exc.value)
    assert "budget.wall_clock_timeout_s" in message
    assert "must be a positive integer" in message
    assert "suite_timeout_s" not in message
    assert "exceeds" not in message


@pytest.mark.parametrize(
    "name", [f.name for f in dataclasses.fields(TaskBudget)]
)
def test_every_budget_key_refuses_a_boolean(tmp_path, upstream, name):
    """Derived from the dataclass rather than listing three names, so a
    fourth budget key added later and wired up with a bare `int(...)` fails
    here instead of shipping.

    `match=` on the key name alone is NOT enough, and this is measured:
    against a package with only `wall_clock_timeout_s` reverted to the bare
    `int(...)`, `pytest.raises(TaskError, match="budget.wall_clock_timeout_s")`
    PASSES on the bug -- the comparison's own message contains that key name
    (`"... budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s
    (1) by 599s..."`). So the test advertised as the drift pin would have
    gone green for the one key this whole item exists to fix. Asserting
    `"must be a positive integer"` as well flips that case to a failure; a
    reader who trims this back to `match=` alone re-opens the hole silently."""
    with pytest.raises(TaskError) as exc:
        load_task(_budget_task(tmp_path, upstream, f"  {name}: true\n"))
    message = str(exc.value)
    assert f"budget.{name}" in message
    assert "must be a positive integer" in message
    assert "exceeds" not in message


def test_max_turns_has_no_upper_bound(tmp_path, upstream):
    """A ceiling is a judgement about how much is too much, section 5.4
    leaves the number to section 3.5's pilot, `wall_clock_timeout_s` is the
    outer stop whatever `max_turns` says, and the CLI enforces nothing
    (measured in the boolean test above). Pinned so a later cap is a
    deliberate change rather than an unnoticed one."""
    task = load_task(_budget_task(tmp_path, upstream, "  max_turns: 100000\n"))
    assert task.budget.max_turns == 100000


@pytest.mark.parametrize("block", ["budget: []", "budget: 0", 'budget: ""'])
def test_a_budget_section_that_is_not_a_mapping_is_refused_rather_than_defaulted(
    tmp_path, upstream, block
):
    """`budget_raw = data.get("budget") or {}` read every falsy value as an
    unwritten section and applied all three defaults, measured 2026-09-02.
    The precedent is `test_a_grading_section_that_is_not_a_mapping_is_refused_at_load`,
    nine lines below `budget:` in the same function and already parametrized
    over exactly `['grading: ruff', 'grading: []', "grading: ''", 'grading:
    0']` -- this test is that one, for the section that predates it."""
    task_dir = _write_task(tmp_path / "set", upstream, extra_yaml=block)

    with pytest.raises(TaskError, match="budget must be a mapping"):
        load_task(task_dir)


def test_a_null_budget_section_reads_as_absent(tmp_path, upstream):
    """The deliberate asymmetry against `max_turns: null`, which is refused:
    a null SECTION is a commented-out block whose keys all have defaults that
    are exactly the prior behaviour; a null KEY is an author reaching for one
    number and writing none. Pins that the previous test did not tighten this
    by accident."""
    task_dir = _write_task(tmp_path / "set", upstream, extra_yaml="budget: null")
    task = load_task(task_dir)
    defaults = TaskBudget()
    assert task.budget.max_turns == defaults.max_turns
    assert task.budget.wall_clock_timeout_s == defaults.wall_clock_timeout_s
    assert task.budget.suite_timeout_s == defaults.suite_timeout_s


def test_an_absent_budget_block_takes_every_default(tmp_path, upstream):
    """The click manifest and every probe manifest rely on the defaults for
    at least one key, so the no-`budget:` path is the one every task
    actually takes."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    defaults = TaskBudget()
    assert task.budget.max_turns == defaults.max_turns
    assert task.budget.wall_clock_timeout_s == defaults.wall_clock_timeout_s
    assert task.budget.suite_timeout_s == defaults.suite_timeout_s


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


@pytest.mark.parametrize("block", ["image: []", "image: 0", 'image: ""'])
def test_an_image_section_that_is_not_a_mapping_is_refused_rather_than_defaulted(
    tmp_path, upstream, block
):
    """Measured 2026-09-02: each of these loaded as the default python with
    EMPTY `apt`, `pip` and `build`, so one stray bracket throws away
    `build: ["pip install -e ."]` -- imports resolve to site-packages,
    nothing the agent writes takes effect, and every arm fails identically --
    and `apt: ["less"]`, whose absence errors 189 unrelated tests in click's
    pager test. Preflight catches both, so this is hygiene rather than a live
    hole; the point is that `image:` must not be the one section left
    reading a written value as an unwritten one.

    Keep the substring `image_section_that_is_not_a_mapping` in this test's
    name: mutation anchor 3.4 selects on it, and the neighbourhood already
    holds `test_an_image_env_that_is_not_a_mapping_is_refused`, so a
    plausible shortening would both over-collect (`-k
    not_a_mapping_is_refused` takes 5/185) and be selected for the wrong
    reason -- the long form takes 0/185 today.

    Also pins T1.7 as a NARROWING and not a tightening of the null path, in
    the same test rather than a separate one: `image: null` is a
    commented-out block and must keep taking the default base, exactly like
    an absent `image:` block."""
    task_dir = _write_task(tmp_path / "set", upstream, extra_yaml=block)

    with pytest.raises(TaskError, match="image must be a mapping"):
        load_task(task_dir)

    null_task_dir = _write_task(
        tmp_path / "set-null", upstream, extra_yaml="image: null"
    )
    null_task = load_task(null_task_dir)
    assert null_task.image.build == ()
    assert null_task.image.apt == ()
    assert null_task.image.pip == ()


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


@pytest.mark.parametrize("value", ["America/New_York", "UTC", "Etc/GMT+5"])
def test_a_well_shaped_tz_value_loads(tmp_path, upstream, value):
    """The allowlist admits TZ so a suite that pins a zone -- date/time
    libraries do this routinely, e.g. moment/luxon asserting against
    America/New_York -- is a task rather than a rejection. An IANA name, UTC,
    and an Etc/GMT+N form must all load unchanged."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=f'image:\n  env:\n    TZ: "{value}"\n',
    )

    assert load_task(task_dir).image.env == {"TZ": value}


@pytest.mark.parametrize("value", [":/etc/localtime", ":America/New_York"])
def test_a_leading_colon_tz_value_is_refused(tmp_path, upstream, value):
    """A TZ value is consumed by libc's tzset, not by this harness: a value
    starting with `:` makes glibc read the rest as a FILE PATH rather than a
    zone name, and the newline/quote/backslash/`$` blacklist every key gets
    does not catch a bare `:` on its own -- TZ needs a positive allowlist of
    its own shape on top of it."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=f'image:\n  env:\n    TZ: "{value}"\n',
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "IANA zone" in str(exc.value)


@pytest.mark.parametrize(
    "value", ["/etc/localtime", "/usr/share/zoneinfo/Asia/Tokyo", "/repo/tzfile"]
)
def test_a_leading_slash_tz_value_is_refused(tmp_path, upstream, value):
    """The colon guard alone does not close this: glibc's `tzset` reads a
    leading `/` exactly like a leading `:` -- a FILE PATH rather than a zone
    name -- and `/` is a character `_TZ_VALUE`'s class already allows for
    `America/New_York` and `Etc/GMT+5`. Measured 2026-09-02 in the eval image
    (glibc 2.36): `TZ=/usr/share/zoneinfo/Asia/Tokyo` resolves identically to
    `TZ=:/usr/share/zoneinfo/Asia/Tokyo`, and `TZ=/etc/localtime` resolves
    too, with no colon anywhere. Without a leading-`/` refusal a manifest
    could point TZ at a file inside the tree the agent itself edits
    (`/repo/tzfile`), making the zone a property of the run rather than of
    the manifest."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=f'image:\n  env:\n    TZ: "{value}"\n',
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "IANA zone" in str(exc.value)


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


def test_the_node_default_is_itself_an_allowlisted_version():
    """The same relationship one key over, and the same hole: `_node_version`
    returns `_DEFAULT_NODE` before consulting the set, so a default outside it
    reaches a build ungated -- from every node manifest that declares
    nothing."""
    assert tasks._DEFAULT_NODE in tasks._NODE_VERSIONS


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

    It `git rm --cached`s `.gitmodules`, so the refusal it actually reaches is
    the UNREADABLE-BLOB one (measured 2026-09-02: `git config --blob
    <sha>:.gitmodules --list -z` exits 128), not the per-path
    gitlink-with-no-stanza one. The neighbouring
    `test_a_gitlink_whose_only_gitmodules_stanza_names_another_path_is_refused`
    covers that second shape. Both stay refused for a submodule the manifest
    does NOT declare unneeded.
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


def test_a_strip_path_covering_a_submodule_from_above_is_refused(
        tmp_path, upstream_submodule, local_urls):
    """The ANCESTOR direction, which the neighbour above cannot cover.

    `strip_paths: ["vendor"]` with the gitlink at `vendor/libdep` is not AT or
    UNDER the submodule, it CONTAINS it, so the descendant test stays green
    with this half of the refusal deleted. Measured 2026-09-01: accepted, the
    strip's `git rm -r` removes the gitlink, `git submodule update --init`
    then writes nothing, and the next command in `_init_submodules` runs with
    `cwd=<dest>/vendor/libdep` -- a bare `FileNotFoundError` out of
    `subprocess.run`, with no task_id in it, hours into a matrix, wearing the
    costume of a harness crash rather than a manifest the loader could have
    refused in milliseconds.
    """
    up = upstream_submodule
    task = _sub_task(tmp_path, up, extra_yaml='strip_paths: ["vendor"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    with pytest.raises(TaskError, match="above or below") as excinfo:
        tasks.derive_submodules(task, mirror)

    assert "vendor/libdep" in str(excinfo.value)


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


def _fake_run(monkeypatch, predicate, returncode=1, stderr="boom"):
    """Make `tasks`' own `subprocess.run` fail (or no-op) for ONE git call.

    `predicate(argv, cwd)` selects it. Everything else, including this file's
    `_sh` helper, delegates to the real `subprocess.run`.

    Patching `subprocess.run` rather than `tasks._git` is what makes the
    asserted message OBSERVED: `_git`'s `check=True` branch is what formats
    `git <argv> failed (exit N): <stderr>`, and a fake that replaced `_git`
    would never reach it.
    """
    real_run = subprocess.run

    def run(*popenargs, **kwargs):
        argv = popenargs[0]
        if predicate(list(argv), str(kwargs.get("cwd"))):
            return subprocess.CompletedProcess(list(argv), returncode,
                                               "", stderr)
        return real_run(*popenargs, **kwargs)

    monkeypatch.setattr(tasks.subprocess, "run", run)


def test_a_submodule_remote_git_did_not_name_origin_is_still_removed(
        tmp_path, upstream_submodule, local_urls, monkeypatch):
    """Red before Task 2/3 lands. Measured 2026-09-02, git 2.50.1: a clone
    always gets a remote, and the only way it is not called `origin` is an
    operator's `clone.defaultRemoteName`, in which case `remote remove
    origin` exits 2 and the old `check=False` swallowed it -- leaking
    `.git/modules/vendor/libdep/config` and `.git/config`, with
    materialization reporting success.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "clone.defaultRemoteName")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "upstream")
    up = upstream_submodule

    _, run, _ = _materialize_sub(tmp_path, up)

    assert _sh("git", "-C", "vendor/libdep", "remote", cwd=run) == ""
    assert _sh("git", "remote", cwd=run) == ""
    needle = str(tmp_path / "cache").encode()
    leaking = [
        path for path in (run / ".git").rglob("*")
        if path.is_file() and not path.is_symlink()
        and needle in path.read_bytes()
    ]
    assert leaking == []


def test_a_failed_submodule_remote_listing_is_a_task_error(
        tmp_path, upstream_submodule, local_urls, monkeypatch):
    """Pins `check=True` on the LISTING. Its absence would let the D1
    silence back in: at `check=False` a failed listing returns `""`,
    byte-identical to a repository with no remotes, so the loop below it
    would iterate zero times and materialization would simply succeed.
    """
    up = upstream_submodule
    _fake_run(
        monkeypatch,
        lambda argv, cwd: argv == ["git", "remote"]
        and cwd == str(tmp_path / "run" / "vendor" / "libdep"),
    )

    with pytest.raises(TaskError, match=r"git remote failed \(exit 1\): boom"):
        _materialize_sub(tmp_path, up)


def test_a_failed_submodule_remote_removal_is_a_task_error(
        tmp_path, upstream_submodule, local_urls, monkeypatch):
    up = upstream_submodule
    _fake_run(
        monkeypatch,
        lambda argv, cwd: argv[:3] == ["git", "remote", "remove"]
        and cwd == str(tmp_path / "run" / "vendor" / "libdep"),
    )

    with pytest.raises(
        TaskError, match=r"git remote remove \S+ failed \(exit 1\): boom"
    ):
        _materialize_sub(tmp_path, up)


def test_a_failed_submodule_reflog_expire_is_a_task_error(
        tmp_path, upstream_submodule, local_urls, monkeypatch):
    """The predicate is positive and exact, never `cwd != dest`. Measured:
    `reflog expire` runs THREE times under one `materialize` --
    `_build_pruned_mirror` runs it for the superproject's mirror and again
    for the submodule's, both `cwd=<cache>/repos/prune-...tmp`, both
    `check=False` -- so `cwd != dest` would intercept all three and this
    test would be green for a reason it does not state.
    """
    up = upstream_submodule
    _fake_run(
        monkeypatch,
        lambda argv, cwd: argv[:2] == ["git", "reflog"]
        and cwd == str(tmp_path / "run" / "vendor" / "libdep"),
    )

    with pytest.raises(
        TaskError,
        match=r"git reflog expire --expire=now --all failed \(exit 1\): boom",
    ):
        _materialize_sub(tmp_path, up)


def test_a_failed_superproject_reflog_expire_is_a_task_error(
        tmp_path, upstream, monkeypatch):
    """Pins Task 3's tightening, on a task with no submodules."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"
    _fake_run(
        monkeypatch,
        lambda argv, cwd: argv[:2] == ["git", "reflog"] and cwd == str(repo),
    )

    with pytest.raises(
        TaskError,
        match=r"git reflog expire --expire=now --all failed \(exit 1\): boom",
    ):
        materialize(task, repo, tmp_path / "cache")


def test_a_host_mirror_path_surviving_in_the_submodules_reflog_is_refused(
        tmp_path, upstream_submodule, local_urls, monkeypatch):
    """Exit 0, so `_git`'s own `check=True` cannot fire -- the
    POST-CONDITION is the thing under test. The `match=` is what separates
    this failure from the previous test's; both raise `TaskError`.
    """
    up = upstream_submodule
    _fake_run(
        monkeypatch,
        lambda argv, cwd: argv[:2] == ["git", "reflog"]
        and cwd == str(tmp_path / "run" / "vendor" / "libdep"),
        returncode=0,
        stderr="",
    )

    with pytest.raises(
        TaskError, match=r"\.git/modules/vendor/libdep/logs/HEAD"
    ):
        _materialize_sub(tmp_path, up)


def test_a_host_mirror_path_surviving_in_the_superprojects_reflog_is_refused(
        tmp_path, upstream, monkeypatch):
    """Pins D4: the post-condition is in `materialize`, not inside
    `_init_submodules`, and it runs for a submodule-free task.
    """
    task = load_task(_write_task(tmp_path / "set", upstream))
    repo = tmp_path / "run" / "repo"
    _fake_run(
        monkeypatch,
        lambda argv, cwd: argv[:2] == ["git", "reflog"] and cwd == str(repo),
        returncode=0,
        stderr="",
    )

    with pytest.raises(TaskError, match=r"\.git/logs/HEAD"):
        materialize(task, repo, tmp_path / "cache")


def test_the_run_tree_carries_no_host_cache_path_anywhere_under_dot_git(
        tmp_path, upstream_submodule, local_urls):
    """The WHOLE `.git` subtree, not `.git/modules` and not the config files
    alone.

    Measured 2026-09-01/02, git 2.50.1: after `remote remove` and the url
    rewrite the config files are already clean, and the cache path survives in
    `logs/HEAD` and `logs/refs/heads/main` as `clone: from /…/cache/repos/…`
    -- a config-only assertion passes with the leak present, which is exactly
    what the first draft of this test did. Widened further: `.git/logs/*` is
    the SUPERPROJECT's own leak surface, from the identical cause one level up
    in `materialize`'s two guards, and a `.git/modules`-only scan cannot see
    it. The removal clears `logs/refs/remotes/origin/HEAD` and
    `.git/modules/<n>/config`; the expire clears `logs/HEAD` and
    `logs/refs/heads/main`; between them nothing survives.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)
    cache = str(tmp_path / "cache")
    needle = cache.encode()

    leaking = [
        path for path in (run / ".git").rglob("*")
        if path.is_file() and not path.is_symlink()
        and needle in path.read_bytes()
    ]

    assert leaking == []
    assert cache not in (run / ".git" / "config").read_text()
    assert str(up["lib"]) in (run / ".git" / "config").read_text()

    # NOT vacuous, and the two files need DIFFERENT assertions. Measured
    # 2026-09-02, git 2.50.1: `reflog expire` TRUNCATES rather than unlinks,
    # so the submodule's `logs/HEAD` -- whose expire is the last write to it
    # -- is present at 0 bytes. The superproject's is NOT: `materialize`'s
    # expire runs before the setup commit, and that commit appends 160 bytes
    # ("commit: bakeoff: task setup (test oracle)"). So the superproject file
    # is asserted present, NON-EMPTY and needle-free, which is strictly
    # stronger non-vacuity than a size of zero.
    sub_head = run / ".git" / "modules" / "vendor" / "libdep" / "logs" / "HEAD"
    assert sub_head.is_file() and sub_head.stat().st_size == 0

    super_head = run / ".git" / "logs" / "HEAD"
    assert super_head.is_file() and super_head.stat().st_size > 0
    assert cache not in super_head.read_text()


def test_an_alternates_file_under_a_submodule_is_refused(
        tmp_path, upstream_submodule, local_urls):
    """Not covered by the two reflog-refusal tests above: `materialize`'s
    dedicated alternates check is `dest/.git/objects/info/alternates` only
    and runs BEFORE `_init_submodules`, so the submodule's own copy is
    checked by nothing else, and a later narrowing of the walk to skip
    `objects/` would leave every other test in this set green.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)

    alternates = (run / ".git" / "modules" / "vendor" / "libdep"
                  / "objects" / "info" / "alternates")
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(f"{tmp_path / 'cache' / 'repos' / 'x.git'}/objects\n")

    with pytest.raises(TaskError, match=r"objects/info/alternates"):
        tasks._refuse_host_mirror_path(run, tmp_path / "cache", "t-001")


def test_a_directory_under_dot_git_that_cannot_be_listed_is_refused(
        tmp_path, upstream_submodule, local_urls):
    """The anchor for the `os.walk(..., onerror=)` decision. Measured:
    `Path.rglob` returns the mode-000 directory and nothing inside it, with
    no error; `os.walk(..., onerror=)` reports errno 13.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)

    blocked = run / ".git" / "modules" / "vendor" / "libdep" / "logs"
    mode = blocked.stat().st_mode
    os.chmod(blocked, 0o000)
    try:
        with pytest.raises(TaskError, match=r"could not be listed"):
            tasks._refuse_host_mirror_path(run, tmp_path / "cache", "t-001")
    finally:
        os.chmod(blocked, mode)


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

    The EXISTENCE-AND-NON-EMPTY half is what catches it, and that is a
    correction: this half used to sit last and the sha comparison answered
    first. It moved above `remote remove`/`reflog expire` because both of
    those take `dest/sub.path` as their working directory, and
    `subprocess.run` against a cwd that does not exist raises
    `FileNotFoundError` -- a bare OSError naming a path, with no task_id, in
    place of the TaskError this raises.

    The sha comparison did not become redundant, and its own reason is pinned
    by the neighbour below rather than here. Measured 2026-09-01: `git -C
    vendor/libdep rev-parse HEAD` inside an EMPTY gitlink directory SUCCEEDS
    and answers with the SUPERPROJECT's HEAD -- git walks up to the enclosing
    repository -- so `head.returncode != 0` alone never fires.
    """
    up = upstream_submodule
    real_git = tasks._git

    def a_git_whose_update_does_nothing(*args, **kwargs):
        if "submodule" in args and "update" in args:
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(tasks, "_git", a_git_whose_update_does_nothing)

    with pytest.raises(TaskError, match="missing or empty") as excinfo:
        _materialize_sub(tmp_path, up)
    assert "vendor/libdep" in str(excinfo.value)


def test_a_submodule_left_at_the_wrong_commit_is_refused(
        tmp_path, upstream_submodule, local_urls, monkeypatch):
    """What the sha comparison alone can catch, now that the emptiness check
    runs before it.

    A submodule that is POPULATED but not at its gitlink passes the existence
    and non-empty guard and passes `rev-parse HEAD` exit 0; only comparing
    against `sub.sha` says anything is wrong. It is the `+` marker preflight
    reports one layer down, and it is worse than empty in one respect -- the
    suite imports SOMETHING, so it collects and then passes or fails for
    reasons that are not the model's.

    Scripted by moving the submodule's HEAD off the gitlink after the real
    update has populated it -- an empty commit, so the WORKING TREE is
    byte-identical to the healthy case and only HEAD differs. That is what
    makes this the sha comparison's own test: nothing about the directory's
    contents can distinguish it.
    """
    up = upstream_submodule
    real_git = tasks._git

    def a_git_that_moves_the_submodule_head(*args, **kwargs):
        result = real_git(*args, **kwargs)
        if "submodule" in args and "update" in args:
            real_git("-c", "user.email=t@t.test", "-c", "user.name=t",
                     "commit", "-q", "--allow-empty", "-m", "drift",
                     cwd=kwargs["cwd"] / "vendor" / "libdep")
        return result

    monkeypatch.setattr(tasks, "_git", a_git_that_moves_the_submodule_head)

    with pytest.raises(TaskError, match="not at its gitlink") as excinfo:
        _materialize_sub(tmp_path, up)
    assert up["pinned"] in str(excinfo.value)


# --- submodules_unneeded ------------------------------------------------------
#
# The manifest lever that lets a task be cut from a repository whose base_sha
# carries a submodule the suite never reads. The key reaches a fixture manifest
# through `extra_yaml=`, which is the established route for a TOP-LEVEL key
# (`_manifest` has no keyword for it and must not gain one); a test needing two
# top-level keys concatenates them with a newline.


def _ssh_url_fixture(up):
    """`upstream_submodule` with the submodule url rewritten to an ssh one.

    The measured defect verbatim: `tobymao/sqlglot` declares
    `git@github.com:fivetran/sqlglot-integration-tests.git`, which
    `_refuse_submodule_conflicts` refuses before any container starts.

    Returns a dict UPDATE, not just a sha, because the rewrite lands on top of
    the fixture's own fix commit: the new base has to carry the BUGGY halves
    again and a new head has to carry the fixed ones, or the reference diff
    the manifest ships no longer applies at the base it names.
    """
    repo = up["path"]

    def commit(message):
        _sh("git", "add", "-A", cwd=repo)
        _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
            "commit", "-q", "-m", message, cwd=repo)
        return _sh("git", "rev-parse", "HEAD", cwd=repo)

    (repo / ".gitmodules").write_text(
        '[submodule "vendor/libdep"]\n'
        "\tpath = vendor/libdep\n"
        "\turl = git@example.invalid:x/y.git\n"
    )
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
    base = commit("an ssh url, back at the buggy state")
    (repo / "calc.py").write_text(FIXED)
    (repo / "tests" / "test_calc.py").write_text(NEW_TEST)
    head = commit("fix")
    reference = subprocess.run(
        ["git", "diff", base, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    return {**up, "base": base, "head": head, "reference": reference}


def test_a_declared_unneeded_submodule_is_not_refused_for_its_url(
        tmp_path, upstream_submodule):
    """The item, in one test.

    Deliberately WITHOUT `local_urls`: that fixture empties
    `_SUBMODULE_URL_PREFIX`, under which the url refusal cannot fire at all
    and this test would pass with the exemption deleted.
    """
    up = _ssh_url_fixture(upstream_submodule)
    task = _sub_task(tmp_path, up,
                     extra_yaml='submodules_unneeded: ["vendor/libdep"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"],
                                 tmp_path / "cache")

    (sub,) = tasks.derive_submodules(task, mirror)

    assert sub.declared_unneeded is True
    assert sub.url == "git@example.invalid:x/y.git"


def test_an_unneeded_declaration_naming_no_gitlink_is_refused(
        tmp_path, upstream_submodule, local_urls):
    """A typo declares NOTHING: the submodule it was meant to name is still
    populated, or still refused for its url, and nothing downstream would say
    the key did not apply."""
    up = upstream_submodule
    task = _sub_task(tmp_path, up,
                     extra_yaml='submodules_unneeded: ["vendor/typo"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"],
                                 tmp_path / "cache")

    with pytest.raises(TaskError, match="vendor/typo") as excinfo:
        tasks.derive_submodules(task, mirror)

    assert "no gitlink" in str(excinfo.value)


def test_the_typo_refusal_beats_the_url_refusal(tmp_path, upstream_submodule):
    """The typo check's POSITION is the message. It runs immediately after the
    gitlink scan and before anything reads `.gitmodules`, so an author who
    misspells the path of the very submodule they are exempting is told about
    the typo rather than about a url they were trying to opt out of."""
    up = _ssh_url_fixture(upstream_submodule)
    task = _sub_task(tmp_path, up,
                     extra_yaml='submodules_unneeded: ["vendor/libdeps"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"],
                                 tmp_path / "cache")

    with pytest.raises(TaskError) as excinfo:
        tasks.derive_submodules(task, mirror)

    assert "submodules_unneeded" in str(excinfo.value)
    assert "git@example.invalid" not in str(excinfo.value)


def test_the_typo_refusal_beats_the_unreadable_gitmodules_refusal(
        tmp_path, upstream_submodule, local_urls):
    """The other ordering half. Together with the row above this pins the
    typo check to ONE line rather than to a range: either of the two
    `.gitmodules`-derived refusals winning means the check moved."""
    up = upstream_submodule
    _sh("git", "rm", "-q", "--cached", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "drop .gitmodules", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base},
                     extra_yaml='submodules_unneeded: ["vendor/typo"]')
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    with pytest.raises(TaskError) as excinfo:
        tasks.derive_submodules(task, mirror)

    assert "submodules_unneeded" in str(excinfo.value)
    assert "no readable .gitmodules" not in str(excinfo.value)


def test_a_declared_unneeded_gitlink_survives_an_unreadable_gitmodules(
        tmp_path, upstream_submodule, local_urls):
    """Measured 2026-09-02: a tree with gitlinks and no `.gitmodules` BLOB
    makes `git config --blob <sha>:.gitmodules --list -z` exit 128, which is a
    refusal of its own and reached ahead of the per-path url one. A tree whose
    only gitlinks are declared is fully workable with no `.gitmodules` at all,
    because nothing is fetched for them."""
    up = upstream_submodule
    _sh("git", "rm", "-q", "--cached", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "drop .gitmodules", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base},
                     extra_yaml='submodules_unneeded: ["vendor/libdep"]')
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    (sub,) = tasks.derive_submodules(task, mirror)

    assert sub.declared_unneeded is True
    assert sub.url == ""
    assert sub.name == "vendor/libdep"


def test_a_declared_unneeded_gitlink_with_no_stanza_in_a_readable_gitmodules(
        tmp_path, upstream_two_submodules, local_urls):
    """The narrower shape the `by_path.get` fallback was justified for:
    `.gitmodules` is READABLE and carries a stanza for the first gitlink only,
    so the second has a gitlink and no url -- fatal when it is needed, fine
    when it is declared."""
    up = upstream_two_submodules(stanzas=1)
    task = _sub_task(tmp_path, up,
                     extra_yaml='submodules_unneeded: ["vendor/other"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"],
                                 tmp_path / "cache")

    subs = tasks.derive_submodules(task, mirror)

    assert [s.path for s in subs] == ["vendor/libdep", "vendor/other"]
    other = subs[1]
    assert other.declared_unneeded is True
    assert other.url == ""
    assert subs[0].declared_unneeded is False


def test_a_strip_path_covering_a_declared_unneeded_submodule_is_still_refused(
        tmp_path, upstream_submodule, local_urls):
    """KEPT, and outside the guard. A strip is unrelated to population:
    `_strip_paths_from_tree`'s `git rm -r` still removes the gitlink, still
    leaves `.gitmodules` naming a path that no longer exists, and moves
    `start_sha` while doing it."""
    up = upstream_submodule
    task = _sub_task(
        tmp_path, up,
        extra_yaml=('strip_paths: ["vendor/libdep"]\n'
                    'submodules_unneeded: ["vendor/libdep"]'),
    )
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"],
                                 tmp_path / "cache")

    with pytest.raises(TaskError, match="strip_paths"):
        tasks.derive_submodules(task, mirror)


def test_a_reference_diff_touching_a_declared_unneeded_submodule_is_still_refused(
        tmp_path, upstream_submodule, local_urls):
    """KEPT, and the reason is STRONGER here rather than weaker: `git add -A`
    stages nothing for a gitlink path in either state, so a task whose fix
    lives there is ungradable by construction however the manifest declares
    it."""
    up = upstream_submodule
    reference = up["reference"] + (
        "diff --git a/vendor/libdep/tests/test_sub.py"
        " b/vendor/libdep/tests/test_sub.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/vendor/libdep/tests/test_sub.py\n"
        "@@ -0,0 +1 @@\n"
        "+def test_sub():\n"
    )
    task = _sub_task(tmp_path, {**up, "reference": reference},
                     extra_yaml='submodules_unneeded: ["vendor/libdep"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"],
                                 tmp_path / "cache")

    with pytest.raises(TaskError, match="ungradable"):
        tasks.derive_submodules(task, mirror)


@pytest.mark.parametrize("bad", [
    "/abs", "../up", ".", "./", ".git", ".git/modules", "vendor/*",
    ":(exclude)v", "", " v",
])
def test_an_unneeded_declaration_is_shape_validated_at_load(
        tmp_path, upstream, bad):
    """The SHAPE half runs offline, with no repository at all -- these
    manifests carry `base_sha` `"0" * 40` against a repo that does not exist.
    The existence half cannot run here and lives in `derive_submodules`,
    exactly as `strip_paths`' does in `_strip_paths_from_tree`."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=f"submodules_unneeded: [{bad!r}]",
    )

    with pytest.raises(TaskError):
        load_task(task_dir)


def test_a_duplicated_unneeded_declaration_is_refused(tmp_path, upstream):
    """The refusal `strip_paths` does not have. This key is consumed as a SET,
    so a repeat is invisible to every downstream comparison -- it cannot
    change any behaviour, which makes a manifest carrying one a statement the
    key cannot express."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml='submodules_unneeded: ["vendor/libdep", "vendor/libdep"]',
    )

    with pytest.raises(TaskError, match="listed twice"):
        load_task(task_dir)


def test_materialize_leaves_a_declared_unneeded_submodule_empty(
        tmp_path, upstream_submodule, local_urls):
    """The shape the run tree ALREADY has when `_init_submodules` does nothing.

    Measured 2026-09-02, git 2.50.1: `materialize`'s `git clean -xfd` does not
    remove the directory, `git status --porcelain` reports the tree clean, the
    index gitlink is untouched, and `git submodule status` reads `-<sha>`.
    Nothing new is built to produce the state; what changed is the verdict
    rendered over it.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(
        tmp_path, up, extra_yaml='submodules_unneeded: ["vendor/libdep"]')
    sub = run / "vendor" / "libdep"

    assert sub.is_dir()
    assert list(sub.iterdir()) == []
    # NOT `_sh`, which strips -- the leading character is the assertion.
    status = subprocess.run(["git", "submodule", "status"], cwd=run,
                            check=True, capture_output=True, text=True).stdout
    assert status.startswith("-")
    assert _sh("git", "status", "--porcelain", cwd=run) == ""
    assert f"160000 {up['pinned']}" in _sh("git", "ls-files", "-s", cwd=run)
    assert not (run / ".git" / "modules").exists()


def test_no_pruned_mirror_is_built_for_a_declared_unneeded_submodule(
        tmp_path, upstream_submodule, monkeypatch):
    """The `if not needed: return` above the mirror comprehension is what makes
    this a property of the code rather than of an empty comprehension.

    The ssh-url variant, so a mirror attempt would also fail loudly rather
    than quietly succeeding against a path that happens to exist.
    """
    up = _ssh_url_fixture(upstream_submodule)
    calls = []
    real = tasks.ensure_pruned_mirror

    def recording(url, sha, cache_root):
        calls.append((url, sha))
        return real(url, sha, cache_root)

    task = _sub_task(tmp_path, up,
                     extra_yaml='submodules_unneeded: ["vendor/libdep"]')
    monkeypatch.setattr(tasks, "ensure_pruned_mirror", recording)
    materialize(task, tmp_path / "run", tmp_path / "cache")

    assert "git@example.invalid:x/y.git" not in [url for url, _sha in calls]
    # The superproject's own mirror IS built, so an assertion that simply
    # counted zero calls would pass with `_init_submodules` deleted entirely.
    assert calls


def test_declaring_an_unneeded_submodule_does_not_move_start_sha(
        tmp_path, upstream_submodule, local_urls):
    """D8's pin. Nothing this key changes is an input to the setup commit --
    `start_sha` is base_sha + strip_paths + the committed test half +
    gitignore_extra -- and `_init_submodules` runs AFTER `start_sha` is
    computed and compared."""
    up = upstream_submodule
    declared = _sub_task(tmp_path / "a", up,
                         extra_yaml='submodules_unneeded: ["vendor/libdep"]')
    plain = _sub_task(tmp_path / "b", up)

    assert materialize(declared, tmp_path / "ra", tmp_path / "ca") == \
        materialize(plain, tmp_path / "rb", tmp_path / "cb")


def test_a_mixed_task_populates_the_needed_submodule_and_not_the_other(
        tmp_path, upstream_two_submodules, local_urls):
    """The filter is per ENTRY, not per task: a repository can carry two
    submodules of which one is needed (`eemeli/yaml` carries four), which is
    why the key is a list of paths rather than a boolean."""
    up = upstream_two_submodules()
    task = _sub_task(tmp_path, up,
                     extra_yaml='submodules_unneeded: ["vendor/other"]')
    materialize(task, tmp_path / "run", tmp_path / "cache")
    run = tmp_path / "run"

    assert list((run / "vendor" / "other").iterdir()) == []
    assert (run / "vendor" / "libdep" / "libdep" / "__init__.py").exists()
    # NOT `_sh`, which strips: the leading character is the whole assertion.
    # `-` is uninitialised, a space is initialised at the gitlink. The path is
    # field 2; an initialised line carries a `(heads/main)` suffix after it.
    status = subprocess.run(["git", "submodule", "status"], cwd=run,
                            check=True, capture_output=True, text=True).stdout
    markers = {line.split()[1]: line[0]
               for line in status.splitlines() if line}
    assert markers["vendor/other"] == "-"
    assert markers["vendor/libdep"] == " "


# --- tests.framework ----------------------------------------------------------


def test_framework_defaults_to_pytest_so_every_existing_manifest_loads(
    tmp_path, upstream
):
    task_dir = _write_task(tmp_path / "set", upstream)  # no framework: key

    assert load_task(task_dir).tests.framework == "pytest"


def test_the_allowlist_and_the_adapter_registry_cannot_disagree():
    """`for_framework` raises KeyError on an unknown name rather than
    defaulting, and this allowlist is the only thing that keeps that
    unreachable. A default in either place would classify a jest run with
    pytest's exit codes -- exit 1 for a config error, graded as the model's
    failure."""
    from bakeoff.runners import FRAMEWORKS
    from bakeoff.tasks import _FRAMEWORKS

    assert set(_FRAMEWORKS) == set(FRAMEWORKS)


def test_an_unknown_framework_is_refused_with_the_allowlist_in_the_message(
    tmp_path, upstream
):
    task_dir = _write_task(
        tmp_path / "set", upstream, tests_extra="  framework: mocha\n"
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "mocha" in str(excinfo.value)
    assert "pytest" in str(excinfo.value)


def test_a_node_framework_needs_a_node_shaped_runner(tmp_path, upstream):
    """Not preflight's check -- this one costs no daemon. The two are not
    redundant: this refuses a manifest, preflight refuses an IMAGE whose
    runner is on PATH but wrong."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        tests_extra='  framework: vitest\n  runner: ["python", "-m", "pytest"]\n',
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "vitest" in str(excinfo.value)


# --- node id shapes -----------------------------------------------------------


def test_a_node_f2p_id_must_carry_a_file_and_a_full_test_name(
    tmp_path, upstream
):
    """`<file>::<fullName>`. The right half is what the reporter emits --
    ancestorTitles joined by single spaces plus the title -- and what `-t`
    matches, so no translation layer can be wrong about it."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream, f2p=["tests/a.test.js"]
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "::" in str(excinfo.value)


def test_a_node_f2p_id_with_an_empty_half_is_refused(tmp_path, upstream):
    for bad in ("::a name", "tests/a.test.js::"):
        task_dir = _write_node_task(tmp_path / bad.replace("/", "_"), upstream,
                                    f2p=[bad])
        with pytest.raises(TaskError):
            load_task(task_dir)


def test_a_node_f2p_id_outside_tests_paths_is_refused(tmp_path, upstream):
    """The scoped p2p run selects by path prefix, so an id whose file is
    outside the declared scope can never be deselected from it -- and a
    deselection that matches nothing is SILENT on node: measured, `-t` matching
    nothing exits 0 with every test reported skipped."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream, paths=["tests/"],
        f2p=["other/a.test.js::does a thing"],
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "tests/" in str(excinfo.value)


def test_two_declared_ids_sharing_a_full_name_across_files_now_load(
    tmp_path, upstream
):
    """This refusal is GONE, and its removal is the point of round 2 item 1.

    It existed because `-t` matches `fullName` and knew nothing about which
    file a test came from: the positionals and the name pattern were ANDed
    across the whole run, so a quarantine of `a.test.js::works` also
    deselected `b.test.js::works`. The file half is now carried into the argv
    -- one invocation per file, each pattern holding only that file's titles
    -- and measured 2026-09-02 one positional plus one `-t` runs the named
    tests of that file only. So the manifest loads.

    The SAME-file half is a different problem and is still open (`TASKS.md`):
    two tests sharing a full name in one file collapse to the identical node
    id string, which no check comparing `(fullName, path)` pairs can see."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream,
        f2p=["tests/a.test.js::works"], p2p=["tests/b.test.js::works"],
    )

    task = load_task(task_dir)

    assert task.tests.f2p == ("tests/a.test.js::works",)
    assert task.tests.p2p == ("tests/b.test.js::works",)


def test_two_names_in_the_ONE_file_are_not_refused(tmp_path, upstream):
    """The duplicate-name rule is about DIFFERENT files, and it must not fire
    on the ordinary case of one test file declaring several tests.

    The case the rule's own wording invites -- the same full name TWICE in the
    same file -- is not constructible in a manifest and so is not tested here:
    identical left and right halves are one identical string, which the f2p
    duplicate check and the f2p/p2p overlap check already refuse, each with a
    message about the thing that is actually wrong."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream,
        f2p=["tests/a.test.js::works"], p2p=["tests/a.test.js::other"],
    )

    assert load_task(task_dir).tests.framework == "vitest"


def test_a_pytest_manifest_whose_runner_does_not_invoke_pytest_is_refused(
    tmp_path, upstream
):
    """The framework and the runner are CROSS-CHECKED, never derived from each
    other, and the check has teeth on the default framework too -- not only on
    the node ones it was added for.

    `["make", "test"]` is a legal command and an illegal declaration: nothing
    in the argv says pytest, so the adapter reading its exit codes would be
    reading whatever `make` returned. It is the same rule preflight applies at
    gate time, made at LOAD time, where the message can carry the manifest
    path and no image has been built yet."""
    task_dir = _write_task(
        tmp_path / "set", upstream, runner=json.dumps(["make", "test"]),
    )

    with pytest.raises(TaskError, match="tests.runner contains 'pytest'"):
        load_task(task_dir)


def test_a_pytest_manifest_may_share_node_names_across_modules(
    tmp_path, upstream
):
    """pytest selects by the WHOLE node id, path included, so `a.py::test_x`
    and `b.py::test_x` are unambiguous there. Applying the node rule to pytest
    would refuse a manifest that loads today, over a hazard pytest does not
    have."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        tests_extra='  f2p: ["tests/a.py::test_x"]\n  p2p: ["tests/b.py::test_x"]\n',
    )

    assert load_task(task_dir).tests.framework == "pytest"


def test_a_wellformed_node_manifest_loads(tmp_path, upstream):
    task = load_task(_write_node_task(tmp_path / "set", upstream))

    assert task.tests.framework == "vitest"
    assert task.image.node == "22"


def test_a_pytest_id_shape_is_not_newly_constrained(tmp_path, upstream):
    """Backwards compatibility as a rule, not as a hope: adding a shape rule to
    the pytest branch could refuse a manifest that loads today, and there is
    exactly one."""
    task = load_task(_write_task(tmp_path / "set", upstream))

    assert task.tests.framework == "pytest"


# --- the runtime is derived, never declared twice -----------------------------


def test_image_node_on_a_pytest_task_is_a_load_error(tmp_path, upstream):
    """Two keys that can each imply a runtime is two sources for one fact, and
    the failure of a disagreement between them is a task gated in one
    interpreter and run in another."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  node: "22"\n'
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "framework" in str(excinfo.value)


def test_image_python_on_a_node_task_is_a_load_error(tmp_path, upstream):
    task_dir = _write_node_task(
        tmp_path / "set", upstream, extra_yaml='  python: "3.12"\n'
    )

    with pytest.raises(TaskError):
        load_task(task_dir)


def test_task_runtime_names_the_base_a_task_needs(tmp_path, upstream):
    from bakeoff.tasks import task_runtime

    assert task_runtime(load_task(_write_task(tmp_path / "p", upstream))) == (
        "python", "3.12")
    assert task_runtime(load_task(_write_node_task(tmp_path / "n", upstream))) == (
        "node", "22")


def test_a_node_version_nobody_built_is_refused(tmp_path, upstream):
    task_dir = _write_node_task(tmp_path / "set", upstream,
                                extra_yaml='  node: "18"\n')

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "22" in str(excinfo.value)


def test_an_unquoted_node_version_is_refused_rather_than_coerced(
    tmp_path, upstream
):
    """YAML parses a bare 22 as an int. `str(22)` happens to be right; the
    refusal is here because the NEXT version is `22.1` and a bare 22.10 parses
    as the float 22.1, which is the silent-wrong-value shape one key over.

    `"quote it"`, not `"quote"`: every TaskError opens with the manifest PATH,
    which under pytest's tmp_path is derived from this test's own name -- and
    this test's name contains "unquoted". Measured before `image.node` existed
    at all, the bare substring passed against `unknown image key(s) ['node']`,
    so the assertion would have gone on passing whatever the loader said."""
    task_dir = _write_node_task(tmp_path / "set", upstream,
                                extra_yaml="  node: 22\n")

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "quote it" in str(excinfo.value)


def test_the_click_task_still_loads_and_its_start_sha_has_not_moved(tmp_path):
    """None of these keys takes part in the setup commit, so `start_sha`
    cannot move. Asserted anyway, because "cannot move" is the claim rather
    than the evidence."""
    task = load_task(
        Path(__file__).resolve().parent.parent
        / "taskset" / "click-3360-write-usage-empty-args"
    )

    assert task.tests.framework == "pytest"
    assert task.declared_start_sha == (
        "33575cc0b75608fa5cbcb1d3ae3347b81eac437f")
    # `pallets/click` carries no gitlink at this base_sha, and the key it does
    # not declare must load as the empty tuple rather than as anything a
    # downstream `set()` could mistake for a declaration.
    assert task.submodules_unneeded == ()


def test_a_tasks_selection_loads_past_an_uncommitted_broken_sibling(
    tmp_path, upstream
):
    """The item's headline test. Measured 2026-09-02 in a shared drafting
    directory (`~/.cache/bakeoff-probe/taskset/`, not a git repository):
    worker 6's `--tasks tomlkit-514-...` gate was blocked by a different
    worker's in-progress `image.env` typo, and the error named only the
    sibling. A `--tasks` selection must be able to load past an uncommitted
    broken manifest it did not ask for."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_broken_task(root, upstream)

    tasks, refusals = load_task_set_with_refusals(root, only=["t-001"])

    assert [t.task_id for t in tasks] == ["t-001"]
    assert len(refusals) == 1
    assert refusals[0].directory.name == "t-002"
    assert refusals[0].committed is False
    assert "is not an allowed image.env key" in refusals[0].error


def test_a_broken_manifest_that_is_selected_still_refuses(tmp_path, upstream):
    """A selection naming the broken sibling must refuse -- and for the right
    reason. With the selection term dropped from `fatal`, `fatal` is empty,
    the load falls through to the `missing` check, and it still raises a
    TaskError whose message says "no such task(s)" instead -- a bare
    `pytest.raises(TaskError)` would pass over that mutation, which is why
    the message is asserted.

    A second, unselected broken sibling (`t-003`, uncommitted like `t-002`
    in this non-repo set) makes this the one test where a load produces both
    a fatal refusal and a skippable one in the same collection --
    `_refusal_report`'s `skippable` line is otherwise unreached in the suite,
    since every other refusal test's load has exactly one refusal."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_broken_task(root, upstream)
    _write_broken_task(root, upstream, name="t-003")

    with pytest.raises(TaskError) as excinfo:
        load_task_set_with_refusals(root, only=["t-002"])

    message = str(excinfo.value)
    assert "SELECTED by --tasks as 't-002'" in message
    assert "is not an allowed image.env key" in message
    assert "further manifest(s)" in message


def test_no_tasks_selection_refuses_on_any_invalid_manifest(tmp_path, upstream):
    """The full-set path -- no `--tasks` at all -- is the path `grade.py` and
    `judge.py` are on, and it must refuse on any invalid manifest regardless
    of committed-ness. The second assertion pins that their unedited call
    site, the `load_task_set` wrapper, inherits the same refusal."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_broken_task(root, upstream)

    with pytest.raises(TaskError) as excinfo:
        load_task_set_with_refusals(root, only=None)
    message = str(excinfo.value)
    assert "no --tasks selection was given" in message
    assert "task_set_commit" in message
    assert "is not an allowed image.env key" in message

    with pytest.raises(TaskError):
        load_task_set(root)


def test_the_refusal_text_names_the_sibling_and_the_selected_tasks(
    tmp_path, upstream
):
    """"The error text names both." A shared drafting directory holding two
    valid tasks and one broken sibling, exercised on both the warning path
    (broken sibling unselected) and the refusal path (broken sibling
    selected)."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_task(root, upstream, name="t-003", task_id="t-003")
    _write_broken_task(root, upstream)

    tasks, refusals = load_task_set_with_refusals(
        root, only=["t-001", "t-003"]
    )
    lines = refusal_warnings(refusals, root=root, selected={"t-001", "t-003"})
    text = "\n".join(lines)
    assert str(root / "t-002" / "task.yaml") in text
    assert "None of them is a selected task (t-001, t-003)" in text
    assert "A run without --tasks" in text

    with pytest.raises(TaskError) as excinfo:
        load_task_set_with_refusals(root, only=["t-002"])
    message = str(excinfo.value)
    assert str(root / "t-002" / "task.yaml") in message
    assert "SELECTED by --tasks as 't-002'" in message


def test_a_committed_broken_sibling_refuses_even_under_a_selection(
    tmp_path, upstream
):
    """M4: a task set that is committed and carries a broken manifest is not
    work in progress -- the set's own revision does not load whole, and
    `grade.py`, which has no `--tasks`, would grade every record naming that
    task TASK_NOT_FOUND at exit code 0. So a selection cannot skip it, and
    the refusal names the selection -- the item's remedy (b) on the branch
    this change creates."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_broken_task(root, upstream)
    _git_task_set(root)

    with pytest.raises(TaskError) as excinfo:
        load_task_set_with_refusals(root, only=["t-001"])

    message = str(excinfo.value)
    assert "committed, and NOT the task you selected (t-001)" in message
    assert "is not an allowed image.env key" in message


def test_an_untracked_broken_sibling_warns_inside_a_git_task_set(
    tmp_path, upstream
):
    """The pair to the committed test, and the one that catches the pathspec
    trap: `t-001` is committed first, and the broken `t-002` is added
    afterward, uncommitted. Without a resolved pathspec `git status` would
    match nothing against a relative path plus `cwd=root` and report the
    untracked directory as committed."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _git_task_set(root)
    _write_broken_task(root, upstream)

    tasks, refusals = load_task_set_with_refusals(root, only=["t-001"])

    assert [t.task_id for t in tasks] == ["t-001"]
    assert len(refusals) == 1
    assert refusals[0].committed is False

    text = "\n".join(refusal_warnings(refusals, root=root, selected={"t-001"}))
    assert (
        "this directory is in no repository, the manifest is untracked or "
        "ignored there, or it is modified since the last commit"
    ) in text


def test_an_ignored_task_set_inside_a_repo_is_not_committed(tmp_path, upstream):
    """Finding 2's measurement, and the anchor for the `ls-files` term. A
    scratch task set living inside an OUTER repository, under a directory the
    outer repository ignores. `task_set_commit` returns the enclosing repo's
    HEAD, so the empty-commit guard does not fire; `git status --porcelain`
    says nothing about an ignored path and would read as committed; only
    `git ls-files --error-unmatch` reports the truth. With the `ls-files`
    term removed, this test fails with a TaskError tagged `committed`."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".gitignore").write_text("scratch/\n")
    _sh("git", "init", "-q", cwd=outer)
    _sh("git", "config", "user.email", "t@t.test", cwd=outer)
    _sh("git", "config", "user.name", "t", cwd=outer)
    _sh("git", "add", "-A", cwd=outer)
    _sh("git", "commit", "-q", "-m", "outer", cwd=outer)

    root = outer / "scratch" / "taskset"
    root.mkdir(parents=True)
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_broken_task(root, upstream)

    tasks, refusals = load_task_set_with_refusals(root, only=["t-001"])

    assert [t.task_id for t in tasks] == ["t-001"]
    assert len(refusals) == 1
    assert refusals[0].committed is False

    text = "\n".join(refusal_warnings(refusals, root=root, selected={"t-001"}))
    assert (
        "this directory is in no repository, the manifest is untracked or "
        "ignored there, or it is modified since the last commit"
    ) in text


def test_a_modified_broken_manifest_in_a_committed_set_is_not_committed(
    tmp_path, upstream
):
    """The branch `status --porcelain` decides on its own, and the ordinary
    drafting loop: editing an existing tracked task rather than adding a new
    one. Both tasks start valid and committed; `t-002/task.yaml` is then
    rewritten to be broken, without a new commit. Without this test a
    refactor that dropped the `status --porcelain` half entirely would keep
    every other test in this plan green."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_task(root, upstream, name="t-002", task_id="t-002")
    _git_task_set(root)
    (root / "t-002" / "task.yaml").write_text(
        _manifest(task_id="t-002", url=str(upstream["path"]),
                  base_sha=upstream["base"], extra_yaml=_BROKEN_IMAGE_ENV)
    )

    tasks, refusals = load_task_set_with_refusals(root, only=["t-001"])

    assert [t.task_id for t in tasks] == ["t-001"]
    assert len(refusals) == 1
    assert refusals[0].committed is False

    # This is the tracked-but-modified shape: `t-002` IS tracked in the set's
    # revision, just edited since the commit. Finding 1 (round 2 item 4
    # review): the old wording ("it has none, or the directory is not tracked
    # there") is FALSE here on both alternatives, since the set has a
    # revision and the directory is tracked in it. Guard against regressing
    # to that claim, and pin the wording that is true on every shape.
    text = "\n".join(refusal_warnings(refusals, root=root, selected={"t-001"}))
    assert (
        "this directory is in no repository, the manifest is untracked or "
        "ignored there, or it is modified since the last commit"
    ) in text
    assert "it has none, or the directory is not tracked there" not in text


def test_a_manifest_that_is_not_valid_yaml_is_a_refusal_not_a_traceback(
    tmp_path, upstream
):
    """M3: `yaml.safe_load` raises `yaml.YAMLError`, not `TaskError`, so a
    sibling with malformed YAML used to escape both drivers' `except
    TaskError` as a bare traceback. The qualified name is asserted on
    purpose -- `YAMLError` is a base class that is never the concrete type,
    and asserting it would fail against the plan's own formatter."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    broken = root / "t-002"
    broken.mkdir(parents=True)
    (broken / "task.yaml").write_text("task_id: [\n")

    tasks, refusals = load_task_set_with_refusals(root, only=["t-001"])
    assert len(refusals) == 1
    assert str(broken / "task.yaml") in refusals[0].error
    assert "yaml.parser.ParserError" in refusals[0].error

    # `only=None` raises TaskError -- not yaml.YAMLError -- which is what
    # both drivers' `except TaskError` catches.
    with pytest.raises(TaskError):
        load_task_set_with_refusals(root, only=None)


def test_a_selected_id_no_directory_supplies_names_the_refused_manifests(
    tmp_path, upstream
):
    """D8: a refused directory is matched against `only` by directory name,
    because a manifest that did not load has no readable `task_id`. A
    selection naming an id no loaded task carries, with a refusal present,
    must name every refused directory as one that could not be checked for
    that id -- the residual hole D8 accepts, closed loudly rather than
    papered over."""
    root = tmp_path / "set"
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_broken_task(root, upstream)

    with pytest.raises(TaskError) as excinfo:
        load_task_set_with_refusals(root, only=["t-999"])

    message = str(excinfo.value)
    assert "no such task(s)" in message
    assert "t-999" in message
    assert "DIRECTORY NAME only" in message
    assert str(root / "t-002") in message

