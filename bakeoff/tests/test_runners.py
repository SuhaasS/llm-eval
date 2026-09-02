"""The runner adapter seam.

Every red/green judgement this harness makes used to be a branch on pytest's
exit codes. That is not portable -- measured 2026-09-01, vitest 3.2.7 and jest
30.5.0 exit 1 for a failing test, an unresolvable import, a syntax error, a
nonexistent file argument AND a broken config -- so the judgement moves behind
an adapter and the exit code becomes one input among several.

This file pins the adapter contract. `test_preflight.py`, `test_oracle.py` and
`test_grader.py` keep pinning what the CONSUMERS do with it.
"""

import json
from pathlib import Path

import pytest

from bakeoff.runners import (
    FRAMEWORKS,
    KIND_ENVIRONMENT,
    KIND_FAILED,
    KIND_LOAD_ERROR,
    KIND_NOTHING_RAN,
    KIND_PASSED,
    RunnerAdapter,
    for_framework,
)

def test_the_manifest_allowlist_and_this_registry_cannot_disagree():
    """`tasks._FRAMEWORKS` is what a manifest's `tests.framework` is validated
    against, and this registry is what the validated value is then looked up
    in. `for_framework` raises KeyError rather than defaulting, so that
    allowlist is the only thing keeping the raise unreachable -- an entry in
    one and not the other is a KeyError out of the middle of preflight, after
    the container is up, with no manifest path in the message.

    Pinned from both files. `test_tasks.py` asserts the same equality, because
    a reader of either file has to be told that the constant in front of them
    is half of a pair."""
    from bakeoff.tasks import _FRAMEWORKS

    assert set(_FRAMEWORKS) == set(FRAMEWORKS)


def test_the_three_frameworks_resolve_and_nothing_else_does():
    """`for_framework` is reached from `load_task`'s validated value, so an
    unknown name here means the allowlist and the registry disagreed -- and the
    failure of that disagreement is a KeyError out of the middle of preflight
    with no manifest path in it."""
    assert set(FRAMEWORKS) == {"pytest", "vitest", "jest"}
    for name in FRAMEWORKS:
        adapter = for_framework(name)
        assert adapter.name == name
        # The Protocol, not just the name. A registry entry missing a method
        # is a `AttributeError` out of the middle of preflight, after the
        # container is up and the suite has run -- and for the node adapters
        # the missing method would be found only by a task that declared them.
        assert isinstance(adapter, RunnerAdapter), name
    with pytest.raises(KeyError):
        for_framework("mocha")


def test_no_adapter_method_is_a_stub_any_more():
    """`FRAMEWORKS` and `tasks._FRAMEWORKS` had to agree from the first commit,
    so the registry was complete before the node behaviour existed and every
    node method raised `NotImplementedError("broadening 7 Task 6")`. Task 6
    filled them in, and this is that test inverted: a method left raising would
    be found by a task that declared that framework, after the image was built
    and the container was up.

    The Protocol check above pins that the methods EXIST; only calling one pins
    that it does something."""
    for name in FRAMEWORKS:
        adapter = for_framework(name)
        assert adapter.classify(
            exit_code=1, stdout="", stderr="", report=None).kind, name
        assert adapter.select_args(()) == []
        assert adapter.p2p_args(selected=(), scope=("tests/",),
                                deselected=(), ignored=()) == ["tests/"]
        assert adapter.explain(1)
        assert adapter.validate_id_set((), "tests.f2p") is None
        assert adapter.parse_deselected(stdout="", report=None) is None

    for name in ("vitest", "jest"):
        # No report is the config-error signal on these two, and it is the one
        # branch a stub could most plausibly have been left returning.
        assert for_framework(name).classify(
            exit_code=1, stdout="", stderr="", report=None
        ).kind == KIND_ENVIRONMENT, name


# --- the pytest adapter reproduces today's behaviour -------------------------


def test_pytest_p2p_args_deselect_branch_is_byte_identical_to_todays_argv():
    """The whole reason the refactor is safe. `_Runner.pass_to_pass` used to
    build this list inline; if the adapter emits anything else -- a reordered
    segment, an inserted flag -- the gated command stops being the graded one
    and the oracle stops describing the thing being graded."""
    adapter = for_framework("pytest")

    assert adapter.p2p_args(
        selected=(), scope=(), deselected=("tests/a.py::test_one",), ignored=()
    ) == ["--deselect", "tests/a.py::test_one"]


def test_pytest_p2p_args_explicit_branch_is_byte_identical_to_todays_argv():
    adapter = for_framework("pytest")

    assert adapter.p2p_args(
        selected=("tests/b.py::test_two",), scope=(), deselected=(), ignored=()
    ) == ["tests/b.py::test_two"]


def test_pytest_p2p_args_keeps_scope_then_f2p_then_quarantine_then_ignore():
    """The ORDER is the argv. Today `pass_to_pass` emits scope, then one
    --deselect per f2p id, then one per quarantined id, then the --ignores; the
    combined `deselected` tuple is (f2p + quarantine) precisely so that this
    stays true."""
    adapter = for_framework("pytest")

    assert adapter.p2p_args(
        selected=(),
        scope=("tests/",),
        deselected=("tests/a.py::test_one", "tests/c.py::test_flaky"),
        ignored=("tests/broken.py",),
    ) == [
        "tests/",
        "--deselect", "tests/a.py::test_one",
        "--deselect", "tests/c.py::test_flaky",
        "--ignore=tests/broken.py",
    ]


def test_pytest_p2p_args_matches_the_live_runner_on_both_branches():
    """The live argv against the PRE-REFACTOR LITERALS, spelled out here.

    An earlier draft compared `_Runner.pass_to_pass`'s argv against
    `adapter.p2p_args(...)` called with the same arguments -- which, now that
    `pass_to_pass` delegates to exactly that method, is `f(x) == f(x)`. It is
    symmetric, it holds whatever the body emits, and it would stay green
    through an inserted flag or a reordered segment: the two changes the
    property exists to catch.

    So the expectations below are the argv from BEFORE the adapter existed --
    the same literals `test_preflight.py`'s identity tests carry -- and they
    are the gate. A transcription of the code under test is worth nothing; a
    transcription of what that code emitted before the refactor is the whole
    claim this refactor makes.
    """
    from bakeoff.preflight import _Runner

    class _Recorder:
        def __init__(self):
            self.argv = []

        def exec(self, argv):
            self.argv = argv
            return None

    class _Tests:
        def __init__(self, f2p, p2p):
            self.f2p = f2p
            self.p2p = p2p

    cases = [
        # The deselect branch with every keyword supplied: the scope
        # positional, then one `--deselect` per (f2p + extra) id in that
        # order, then one `--ignore=` per path.
        (_Tests(("tests/a.py::test_one",), ()),
         ("tests/c.py::test_flaky",), ("tests/",), ("tests/broken.py",),
         ["tests/", "--deselect", "tests/a.py::test_one",
          "--deselect", "tests/c.py::test_flaky",
          "--ignore=tests/broken.py"]),
        # The deselect branch with nothing supplied -- the argv the GRADER
        # makes, and the one preflight validated.
        (_Tests(("tests/a.py::test_one",), ()), (), (), (),
         ["--deselect", "tests/a.py::test_one"]),
        # The explicit branch: the declared p2p ids and nothing else.
        (_Tests((), ("tests/b.py::test_two",)), (), (), (),
         ["tests/b.py::test_two"]),
        # The explicit branch with extras. `scope` is IGNORED there and the
        # f2p ids are NOT deselected -- selecting node ids never collects them.
        (_Tests(("tests/a.py::test_one",), ("tests/b.py::test_two",)),
         ("tests/c.py::test_flaky",), ("tests/",), ("tests/broken.py",),
         ["tests/b.py::test_two", "--deselect", "tests/c.py::test_flaky",
          "--ignore=tests/broken.py"]),
    ]

    for tests, extra_deselect, scope, ignore, expected in cases:
        recorder = _Recorder()
        runner = _Runner(recorder, ("python", "-m", "pytest", "-q"), 600,
                         for_framework("pytest"))
        runner.pass_to_pass(tests, extra_deselect=extra_deselect, scope=scope,
                            ignore=ignore)

        assert recorder.argv == [
            "timeout", "600", "python", "-m", "pytest", "-q", *expected
        ]


def test_pytest_select_args_are_the_bare_node_ids():
    assert for_framework("pytest").select_args(("a::b", "c::d")) == ["a::b", "c::d"]


def test_pytest_select_args_match_the_live_runner():
    """The live `_Runner.select` argv against the PRE-REFACTOR LITERAL.

    Comparing it against `adapter.select_args(node_ids)` was `f(x) == f(x)`
    once `select` came to call exactly that -- symmetric, and green through
    any flag the method might insert. The f2p selection is the run the whole
    red-before verdict is read off, so this is the argv least able to afford a
    drift, and the literal is what pins it.

    The second id carries `::` inside its brackets on purpose: a parametrized
    id must reach pytest verbatim, and any splitting or quoting on the way
    through would show up here.
    """
    from bakeoff.preflight import _Runner

    class _Recorder:
        def __init__(self):
            self.argv = []

        def exec(self, argv):
            self.argv = argv
            return None

    node_ids = ("tests/a.py::test_one", "tests/a.py::Klass::test_two[x::y]")

    recorder = _Recorder()
    _Runner(recorder, ("python", "-m", "pytest", "-q"), 600,
            for_framework("pytest")).select(node_ids)

    assert recorder.argv == [
        "timeout", "600", "python", "-m", "pytest", "-q",
        "tests/a.py::test_one", "tests/a.py::Klass::test_two[x::y]",
    ]


def test_pytest_writes_no_report_and_asks_for_no_reporter_flags():
    """pytest's evidence is its exit code and its `-q` summary lines. Asking it
    for a JSON report would add a second thing that can be wrong about what
    happened, inside the check that exists to be right about it -- the reason
    `failed_node_ids` parses the summary rather than a JUnit file."""
    adapter = for_framework("pytest")

    assert adapter.report_path() is None
    assert adapter.report_args("/tmp/x.json") == []


@pytest.mark.parametrize(
    "exit_code, output, kind",
    [
        (0, "3 passed in 0.01s", KIND_PASSED),
        (1, "FAILED tests/a.py::test_one\n1 failed in 0.01s", KIND_FAILED),
        (4, "ERROR tests/new.py", KIND_LOAD_ERROR),
        (2, "ERROR tests/new.py", KIND_LOAD_ERROR),
        (5, "no tests ran in 0.00s", KIND_NOTHING_RAN),
        (4, "ERROR: not found: tests/a.py::gone", KIND_ENVIRONMENT),
        (3, "INTERNALERROR", KIND_ENVIRONMENT),
    ],
)
def test_pytest_classify_maps_each_exit_code_to_the_kind_it_means(
    exit_code, output, kind
):
    """The Phase 0c failure in one table: 1, 2, 3, 4 and 5 are all non-zero and
    only ONE of them means a test failed. `ERROR: not found:` carries a colon
    `_FAILED_LINE` does not match, so it parses to an empty set and stays an
    environment problem -- a manifest naming a renamed test must keep stopping
    the matrix."""
    outcome = for_framework("pytest").classify(
        exit_code=exit_code, stdout=output, stderr="", report=None
    )

    assert outcome.kind == kind
    assert outcome.exit_code == exit_code


def test_pytest_classify_reads_stderr_beside_stdout():
    """`collection_error_modules` is called on `stdout + stderr` today, because
    `RunContainer.exec` demuxes and a summary section can land on either. A
    classifier reading only stdout would report an empty errored set -- an
    ENVIRONMENT refusal -- for the broadening-2 task shape it exists to
    accept."""
    outcome = for_framework("pytest").classify(
        exit_code=4, stdout="", stderr="ERROR tests/new.py", report=None
    )

    assert outcome.kind == KIND_LOAD_ERROR
    assert outcome.errored_files == frozenset({"tests/new.py"})


def test_pytest_classify_distinguishes_parsed_empty_from_never_parsed():
    """Two different absences that render identically are the same defect one
    layer down. preflight's message says "empty" for one and "not parsed (exit
    N is not a collection failure)" for the other, and it can only do that
    while these stay apart."""
    adapter = for_framework("pytest")

    parsed_empty = adapter.classify(
        exit_code=4, stdout="ERROR: not found: x", stderr="", report=None
    )
    never_parsed = adapter.classify(
        exit_code=3, stdout="INTERNALERROR", stderr="", report=None
    )

    assert parsed_empty.errored_files == frozenset()
    assert never_parsed.errored_files is None


def test_pytest_classify_reports_the_failed_node_ids():
    outcome = for_framework("pytest").classify(
        exit_code=1,
        stdout="FAILED tests/a.py::test_one\nERROR tests/b.py::test_two\n",
        stderr="",
        report=None,
    )

    assert outcome.failed_ids == {"tests/a.py::test_one", "tests/b.py::test_two"}


def test_pytest_classify_explains_every_kind_it_returns():
    """`explain` feeds a preflight problem message, and an empty one there
    turns "usage error -- a selected node id does not exist" into a bare exit
    number the author has to go look up."""
    adapter = for_framework("pytest")

    for exit_code in (0, 1, 2, 3, 4, 5, 124, 137):
        outcome = adapter.classify(
            exit_code=exit_code, stdout="", stderr="", report=None)
        assert outcome.explain


def test_pytest_module_of_splits_once_from_the_left():
    """A parametrized id can carry `::` inside its brackets and a class-scoped
    id carries two, so rsplit or an unbounded split names something that is not
    a module."""
    adapter = for_framework("pytest")

    assert adapter.module_of("tests/a.py::test_one[x::y]") == "tests/a.py"
    assert adapter.module_of("tests/a.py::Klass::test_one") == "tests/a.py"


def test_pytest_f2p_modules_goes_through_the_one_splitter():
    """One splitter, not two. `f2p_modules` is the preflight-side name and
    `module_of` is the adapter-side one; a second copy of "split from the left,
    maxsplit 1" is a second thing that can be wrong about which file a node id
    names, inside the equality preflight's confinement check rests on."""
    from bakeoff.preflight import f2p_modules

    ids = ("tests/a.py::test_one[x::y]", "tests/a.py::Klass::test_two",
           "tests/b.py::test_three")
    adapter = for_framework("pytest")

    assert f2p_modules(ids) == frozenset(adapter.module_of(i) for i in ids)
    assert f2p_modules(ids) == frozenset({"tests/a.py", "tests/b.py"})


def test_pytest_validate_node_id_adds_no_rule_pytest_did_not_have():
    """There is exactly one manifest and backwards compatibility is a
    constraint of this broadening, so a shape rule added now could refuse a
    task that loads today."""
    adapter = for_framework("pytest")

    assert adapter.validate_node_id("-weird::not even an id", (), "f2p") is None


def test_pytest_parse_deselected_is_the_summary_line_parser():
    adapter = for_framework("pytest")

    assert adapter.parse_deselected(
        stdout="1 passed, 3 deselected in 0.00s", report=None
    ) == 3
    assert adapter.parse_deselected(
        stdout="4 passed in 0.00s", report=None
    ) == 0
    assert adapter.parse_deselected(stdout="", report=None) is None


def test_pytest_hypothesis_interpreter_comes_off_the_runner():
    """A runner of ["/opt/venv/bin/python", "-m", "pytest"] resolves imports
    against that venv, so probing whichever python is first on PATH would
    answer a question about a different environment."""
    adapter = for_framework("pytest")

    assert adapter.hypothesis_interpreter(
        ("/opt/venv/bin/python", "-m", "pytest")) == "/opt/venv/bin/python"
    assert adapter.hypothesis_interpreter(("pytest", "-q")) == "python"


def test_pytest_no_cache_args_names_the_flag_the_manifest_should_carry():
    """Advisory for pytest and asserted for node -- see the plan's D7d. The
    value is here so one place says what each framework needs."""
    assert for_framework("pytest").no_cache_args == ("-p", "no:cacheprovider")


def test_pytest_runner_marker_is_what_a_manifest_argv_must_carry():
    """Framework and runner argv are cross-checked rather than derived from
    each other, so each catches the other's typo. The marker is that check's
    left-hand side."""
    assert for_framework("pytest").runner_marker == "pytest"


def test_the_moved_names_are_still_importable_from_preflight_and_grader():
    """The refactor must be invisible to every existing caller and to every
    mutation anchor. A name that moved out from under `from bakeoff.preflight
    import ...` is a silent break in a module nothing in this task touches."""
    from bakeoff import grader, preflight

    assert preflight.EXIT_ALL_PASSED == 0
    assert preflight.EXIT_TESTS_FAILED == 1
    assert preflight.EXIT_NOTHING_COLLECTED == 5
    assert preflight.EXIT_COLLECTION_FAILURES == (4, 2)
    assert preflight.failed_node_ids("FAILED a::b") == {"a::b"}
    assert preflight.collection_error_modules("ERROR a.py") == frozenset({"a.py"})
    assert preflight.f2p_modules(("a.py::b",)) == frozenset({"a.py"})
    assert preflight._runner_python(("python", "-m", "pytest")) == "python"
    assert grader.parse_deselected("1 passed, 2 deselected in 0.0s") == 2


def test_the_moved_names_are_the_same_objects_the_adapter_holds():
    """Re-exported, not re-defined. Two definitions of `_FAILED_LINE` agree
    until one of them is edited, and the edit that matters is the one nobody
    makes twice."""
    from bakeoff import grader, preflight
    from bakeoff.runners import pytest_adapter

    for name in ("EXIT_ALL_PASSED", "EXIT_TESTS_FAILED",
                 "EXIT_COLLECTION_INTERRUPTED", "EXIT_USAGE_ERROR",
                 "EXIT_NOTHING_COLLECTED", "EXIT_COLLECTION_FAILURES",
                 "_EXIT_MEANING", "_FAILED_LINE", "_PYTHON_BASENAME",
                 "_runner_python", "collection_error_modules", "f2p_modules",
                 "failed_node_ids"):
        assert getattr(preflight, name) is getattr(pytest_adapter, name), name

    for name in ("_SUMMARY_LINE", "_DESELECTED", "parse_deselected"):
        assert getattr(grader, name) is getattr(pytest_adapter, name), name


def test_no_production_call_site_leaves_the_runner_on_its_pytest_default():
    """The enforcement `_Runner`'s default gave up, put back as a source rule.

    The plan asked for NO default on `_Runner(..., adapter)`, so that a call
    site which forgot it would not compile. It has one anyway, because
    `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` is the
    argv-identity gate on this refactor, constructs a `_Runner` with three
    positional arguments, and may not be edited -- a gate rewritten to
    accommodate the change it gates has stopped gating anything.

    What the default costs is exactly this: a production call site can now be
    left on pytest silently. Under jest, exit 1 is what a config error, an
    unresolvable import and a failing assertion all return alike, so such a
    site stamps a model failure on an environment defect -- permanently, in an
    append-only store. Parsed rather than grepped because `_Runner(env, argv,
    t)` and `_Runner(env, argv, t,\n adapter)` are the same string prefix.

    Scoped to `src/bakeoff/` on purpose: the tests' own three-argument
    constructions are the ones the default exists for.
    """
    import ast

    import bakeoff

    src = Path(bakeoff.__file__).parent
    sites = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name != "_Runner":
                continue
            supplied = len(node.args) >= 4 or any(
                kw.arg == "adapter" for kw in node.keywords)
            sites.append((f"{path.relative_to(src)}:{node.lineno}", supplied))

    # Not vacuous: preflight's gate, the oracle's derivation and the grader's
    # two checks are four, and a refactor that inlined one of them away would
    # otherwise make this test pass by finding nothing.
    assert len(sites) >= 4, sites
    assert [where for where, ok in sites if not ok] == []


# --- the node adapters --------------------------------------------------------

_REPORTS = Path(__file__).resolve().parent / "fixtures" / "node_reports"


def _report(shape: str, framework: str) -> dict:
    """One of the eight measured report shapes, replayed from disk.

    Captured once in `node:22-bookworm-slim` against vitest 3.2.7 and jest
    30.5.0; see the README beside them for the argv each came from and for the
    exit code each run produced. They are replayed rather than re-measured
    because regenerating one needs a Docker daemon, a network and 73 MB of npm
    -- and because the whole point of these tests is that the exit code is not
    what the classification rests on.
    """
    return json.loads((_REPORTS / f"{shape}.{framework}.json").read_text())


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_report_that_was_never_written_is_an_environment_problem(framework):
    """Measured 2026-09-02: a broken config exits 1 and writes NO report file,
    on both frameworks. So does a runner that could not start. Reading silence
    as anything but "the command did not say what it did" is the Phase 0c
    failure, and here it would stamp a config error on the model."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="Validation Error", report=None
    )

    assert outcome.kind == KIND_ENVIRONMENT
    assert outcome.errored_files is None


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_all_passing_is_passed(framework):
    outcome = for_framework(framework).classify(
        exit_code=0, stdout="", stderr="", report=_report("pass", framework)
    )

    assert outcome.kind == KIND_PASSED
    assert outcome.failed_ids == frozenset()
    assert outcome.files_run == ("tests/pass.test.js",)


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_failing_assertion_is_failed_with_the_id_the_manifest_uses(framework):
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report("fail", framework)
    )

    assert outcome.kind == KIND_FAILED
    assert outcome.failed_ids == {"tests/fail.test.js::will fail"}


@pytest.mark.parametrize("framework", ["vitest", "jest"])
@pytest.mark.parametrize("shape", ["import_error", "syntax_error"])
def test_a_file_that_could_not_load_is_a_load_error(framework, shape):
    """The portable discriminator is a testResults entry with status 'failed'
    and an EMPTY assertionResults -- true on both frameworks and on both
    shapes. jest's numRuntimeErrorTestSuites says the same thing and vitest has
    no such key, so it is deliberately not consulted: one classifier, not two."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report(shape, framework)
    )

    assert outcome.kind == KIND_LOAD_ERROR
    assert outcome.errored_files == {
        "import_error": frozenset({"tests/broken.test.js"}),
        "syntax_error": frozenset({"tests/syntax.test.js"}),
    }[shape]


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_load_error_beats_a_passing_file_in_the_same_run(framework):
    """Measured: one good file plus one unloadable file reports THREE PASSING
    tests and zero failing ones. Classified in the other order that grades as
    `passed`, and a task whose f2p file stopped importing is scored as solved."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report("mixed", framework)
    )

    assert outcome.kind == KIND_LOAD_ERROR
    assert "tests/broken.test.js" in outcome.errored_files


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_an_unloadable_file_is_still_a_file_the_run_touched(framework):
    """`files_run` is every suite LOADED or executed, the unloadable one
    included. D7c's scope check asks whether the run left `tests.paths`, and a
    file a substring filter pulled in and then failed to parse is exactly such
    a file -- dropping it would make the over-match invisible in the one place
    that looks for it."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report("mixed", framework)
    )

    assert outcome.files_run == ("tests/broken.test.js", "tests/pass.test.js")


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_no_files_matched_is_nothing_ran_whatever_success_says(framework):
    """jest reports `success: true` while exiting 1 on 'no test files matched'
    (measured). Reading `success` would call a run that executed nothing a
    pass, which on the p2p check is a regression suite that ran zero tests."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report("no_files", framework)
    )

    assert outcome.kind == KIND_NOTHING_RAN


def test_the_no_files_fixtures_really_do_disagree_about_success():
    """Not decoration. The rule above is only load-bearing because jest and
    vitest answer the SAME run with opposite `success` values, and a fixture
    regenerated against a future jest that agreed with vitest would make the
    test above pass for a reason that no longer holds."""
    assert _report("no_files", "jest")["success"] is True
    assert _report("no_files", "vitest")["success"] is False


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_name_pattern_that_matched_nothing_is_nothing_ran_despite_exit_zero(
    framework
):
    """The single most dangerous measured behaviour in this broadening. `-t`
    with a pattern matching no test exits **0** on both frameworks, with every
    test reported skipped and a summary that reads like success. pytest answers
    the same input with exit 4 and `ERROR: not found:`.

    Two things land here: a manifest naming a renamed f2p test, and an oracle
    quarantine that swallowed the entire p2p list -- which `derive_quarantine`'s
    guard used to catch through pytest's exit 5."""
    outcome = for_framework(framework).classify(
        exit_code=0, stdout="", stderr="", report=_report("t_nomatch", framework)
    )

    assert outcome.kind == KIND_NOTHING_RAN
    assert outcome.exit_code == 0


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_verify_selected_names_the_requested_ids_that_did_not_run(framework):
    """Separate from `classify`, because only the caller knows what it asked
    for. A skipped test and a test that was never selected are the same shape
    in the report."""
    from bakeoff.runners.node_adapter import verify_selected

    report = _report("t_nomatch", framework)
    adapter = for_framework(framework)

    assert verify_selected(
        report, ("tests/pass.test.js::outer adds",), adapter
    ) == {"tests/pass.test.js::outer adds"}
    assert verify_selected(
        _report("t_match", framework),
        ("tests/pass.test.js::outer adds",), adapter
    ) == frozenset()


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_verify_selected_reads_a_missing_report_as_nothing_having_run(framework):
    """`report=None` is the config-error signal, and `classify` calls it
    KIND_ENVIRONMENT. This helper must not answer the same input with an empty
    set -- "nothing was requested that did not run" is the one reading a run
    that produced no evidence cannot support."""
    from bakeoff.runners.node_adapter import verify_selected

    assert verify_selected(
        None, ("tests/pass.test.js::outer adds",), for_framework(framework)
    ) == {"tests/pass.test.js::outer adds"}


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_failing_assertion_counts_as_having_run(framework):
    """`verify_selected` asks whether a test reached a TERMINAL status, not
    whether it passed. An f2p id is expected to FAIL at the start state, so a
    helper that only counted passes would report every f2p test as not-run and
    turn preflight's own red half into an environment problem."""
    from bakeoff.runners.node_adapter import verify_selected

    assert verify_selected(
        _report("fail", framework), ("tests/fail.test.js::will fail",),
        for_framework(framework)
    ) == frozenset()


# --- node argv ----------------------------------------------------------------


def test_node_select_args_are_the_files_plus_one_anchored_name_alternation():
    adapter = for_framework("vitest")

    assert adapter.select_args(
        ("tests/a.test.js::outer adds", "tests/a.test.js::top level")
    ) == ["tests/a.test.js", "-t", "^(?:outer adds|top level)$"]


def test_node_select_args_name_each_file_once_and_in_order():
    """The file half is a positional filter, and repeating it is not harmless:
    on jest a positional is a REGEX over the absolute path, so a duplicate is a
    second pattern that has to agree with the first, and on both frameworks the
    argv is what the byte-identity property is stated over."""
    assert for_framework("jest").select_args(
        ("tests/b.test.js::two", "tests/a.test.js::one", "tests/b.test.js::three")
    ) == ["tests/b.test.js", "tests/a.test.js", "-t", "^(?:two|one|three)$"]


def test_node_select_args_on_nothing_is_an_empty_argv_not_a_match_all():
    """`^(?:)$` matches the empty name and nothing else -- which is M1's silent
    hole with the pattern the adapter itself wrote. An empty selection has no
    argv, and the caller's own branch is what decides whether that is legal."""
    assert for_framework("vitest").select_args(()) == []


def test_node_p2p_args_compose_selection_and_deselection_into_ONE_pattern():
    """Emitting two `-t` flags is not "last one wins" -- the frameworks
    disagree and neither answer is usable. Measured 2026-09-02:

        $ vitest run -t 'a' -t 'b' tests/pass.test.js
        Error: Expected a single value for option
        "-t, --testNamePattern <pattern>", received ["a", "b"]
        -> exit 1, and NO report file

        $ jest -t 'adds' -t 'subs'
        Ran all test suites with tests matching "adds,subs".
        -> exit 0, zero tests run

    vitest's refusal is loud and classifies as KIND_ENVIRONMENT; jest's
    comma-join is M1's silent hole reached by an argv nobody meant to write.
    Hence one method owning the whole argv, and hence this count."""
    adapter = for_framework("vitest")

    argv = adapter.p2p_args(
        selected=(), scope=("tests/",),
        deselected=("tests/a.test.js::outer adds",), ignored=())

    assert argv.count("-t") == 1
    assert argv == ["tests/", "-t", "^(?!(?:outer adds)$)"]


def test_node_p2p_args_on_the_explicit_branch_anchor_both_halves():
    adapter = for_framework("jest")

    assert adapter.p2p_args(
        selected=("tests/b.test.js::keeps working",), scope=(),
        deselected=("tests/b.test.js::flaky",), ignored=()
    ) == ["tests/b.test.js", "-t", "^(?!(?:flaky)$)(?:keeps working)$"]


def test_node_p2p_args_with_no_pattern_at_all_emit_no_dash_t():
    """A bare scoped run with nothing to deselect. `-t ''` is not the same
    argv: an empty pattern is a regex matching every name, which is what this
    run wants and is also indistinguishable from a pattern the builder failed
    to fill in."""
    assert for_framework("vitest").p2p_args(
        selected=(), scope=("tests/", "src/"), deselected=(), ignored=()
    ) == ["tests/", "src/"]


def test_a_test_name_with_regex_metacharacters_is_escaped_the_JS_way():
    """Measured 2026-09-02: `-t '^(?:handles a\\+b \\(x\\) \\[y\\])$'` selects
    exactly the test named `handles a+b (x) [y]` and skips
    `handles a+b (x) [y] EXTRA`. Unescaped, the `+` and the groups change what
    the pattern means.

    `re.escape` is NOT usable here and the difference is not cosmetic: Python
    escapes characters JavaScript treats as IDENTITY ESCAPES, and an identity
    escape of a non-syntax character is a SyntaxError in a Unicode-mode
    RegExp. The pattern is compiled by node, not by Python, so the escape set
    has to be JavaScript's -- exactly the fourteen characters `. * + ? ^ $ { }
    ( ) | [ ] \\` -- and nothing else. A space in particular must NOT be escaped;
    Python's `re.escape` escaped it through 3.6 and a `\\ ` reaching node is a
    pattern that means something different.
    """
    argv = for_framework("vitest").select_args(("tests/m.test.js::a+b (x) y",))

    assert argv[-1] == r"^(?:a\+b \(x\) y)$"


def test_the_escape_set_is_javascripts_and_not_pythons():
    from bakeoff.runners.node_adapter import _js_escape

    assert _js_escape("a+b") == r"a\+b"
    assert _js_escape("a b") == "a b"          # a space is NOT escaped
    assert _js_escape("a-b") == "a-b"          # nor a hyphen, outside a class
    assert _js_escape("[x]") == r"\[x\]"


def test_the_escape_set_is_exactly_javascripts_syntax_characters():
    """Pinned as a set rather than by example, because both directions are
    defects and neither shows up in a passing run: an unescaped `(` silently
    changes which tests a quarantine removes, and an escaped `-` or `/` or `#`
    is a SyntaxError node raises instead of running the suite."""
    from bakeoff.runners.node_adapter import _js_escape

    escaped = {ch for ch in map(chr, range(32, 127)) if _js_escape(ch) != ch}

    assert escaped == set(".*+?^${}()|[]\\")
    assert all(_js_escape(ch) == "\\" + ch for ch in escaped)


def test_node_report_args_write_to_a_fixed_path_outside_repo():
    """FIXED because it is an argv element and the gated argv must equal the
    graded argv; outside /repo because section 5.6 stages everything and a
    report inside the tree lands in every submission diff. Staleness is handled
    by deleting the file before each run, which `_Runner.run` does as a
    separate exec."""
    for framework, flag in (("vitest", "--reporter=json"), ("jest", "--json")):
        adapter = for_framework(framework)
        path = adapter.report_path()

        assert path.startswith("/tmp/")
        assert adapter.report_args(path) == [flag, f"--outputFile={path}"]


def test_both_node_frameworks_agree_on_the_report_path():
    """One path, so `_Runner`'s `rm -f` before the run and `cat` after it are
    the same two execs whatever the manifest declared. Two would be a second
    place for a stale report to survive."""
    assert (for_framework("vitest").report_path()
            == for_framework("jest").report_path()
            == "/tmp/bakeoff-run-report.json")


def test_node_ignore_flags_are_per_framework_and_jest_keeps_its_default():
    """`ignored` has exactly ONE caller -- preflight's p2p run at the START
    state, which the grader never makes -- so the two spellings cannot diverge
    between a gated and a graded argv.

    Measured 2026-09-02: vitest `--exclude=<path>` and jest
    `--testPathIgnorePatterns=<path>` both drop the named file. jest's flag
    REPLACES its built-in `/node_modules/` ignore rather than adding to it, so
    emitted alone it makes jest collect test files out of node_modules --
    which, with the runners installed at /node_modules, means jest's own
    vendored fixtures. The default is therefore re-emitted alongside."""
    assert for_framework("vitest").p2p_args(
        selected=(), scope=("tests/",), deselected=(), ignored=("tests/x.js",)
    ) == ["tests/", "--exclude=tests/x.js"]

    assert for_framework("jest").p2p_args(
        selected=(), scope=("tests/",), deselected=(), ignored=("tests/x.js",)
    ) == ["tests/",
          "--testPathIgnorePatterns=/node_modules/",
          "--testPathIgnorePatterns=tests/x.js"]


def test_jest_does_not_emit_its_default_ignore_when_there_is_nothing_to_ignore():
    """Emitting it on every run would put a flag in both the gated and the
    graded argv that changes what jest collects for EVERY task, for the benefit
    of the one preflight run that uses `ignored`."""
    assert for_framework("jest").p2p_args(
        selected=(), scope=("tests/",), deselected=(), ignored=()
    ) == ["tests/"]


def test_the_scope_precedes_the_ignore_flags_and_the_pattern_follows_them():
    """Measured 2026-09-02, and it is a correctness rule, not a style one.
    `--testPathIgnorePatterns` is a greedy yargs array: with the positional
    LAST, `jest --testPathIgnorePatterns=fail.test.js tests/` swallows `tests/`
    into the ignore list and runs NOTHING -- exit 1, empty `testResults` -- so
    the p2p check would report a regression suite that executed zero tests.
    With the positional first it drops the named file and runs the rest.

    The trailing `-t` is safe on the same greedy option because it starts with
    `-`, which ends the array."""
    argv = for_framework("jest").p2p_args(
        selected=(), scope=("tests/",),
        deselected=("tests/a.test.js::gone",), ignored=("tests/x.js",))

    assert argv == ["tests/",
                    "--testPathIgnorePatterns=/node_modules/",
                    "--testPathIgnorePatterns=tests/x.js",
                    "-t", "^(?!(?:gone)$)"]


def test_node_no_cache_args_are_measured_per_framework():
    """vitest creates <cwd>/node_modules/.vite -- inside the bind-mounted tree
    -- and `--no-cache` prevents it entirely (measured). jest's DEFAULT
    cacheDirectory is /tmp/jest_0, already outside the tree, and jest wrote
    nothing into the tree in any measured run."""
    assert for_framework("vitest").no_cache_args == ("--no-cache",)
    assert for_framework("jest").no_cache_args == ()


def test_node_parse_deselected_counts_pending_tests():
    report = _report("t_nomatch", "vitest")

    assert for_framework("vitest").parse_deselected(stdout="", report=report) == 3
    assert for_framework("vitest").parse_deselected(stdout="", report=None) is None


def test_node_module_of_is_the_file_half_of_an_id_whose_name_carries_colons():
    """Split once from the LEFT. A JavaScript test title may itself contain
    `::` -- nothing forbids it -- and an rsplit would name a file that does not
    exist, which reaches preflight as a confinement equality that can never
    hold."""
    assert for_framework("jest").module_of(
        "tests/a.test.js::parses a::b as one token") == "tests/a.test.js"


def test_the_node_adapters_ask_for_no_hypothesis_probe():
    """A recorded absence, never a claim that the suite is deterministic. The
    hypothesis block is a Python-ecosystem check; fast-check and jest-fuzz have
    their own seed mechanisms and no such check exists yet (TASKS.md)."""
    for framework in ("vitest", "jest"):
        assert for_framework(framework).hypothesis_interpreter(
            ("/node_modules/.bin/vitest", "run")) is None


def test_node_explain_never_claims_an_exit_code_means_anything():
    """The one sentence this adapter exists to be able to say. Every measured
    failure shape returns 1 and a `-t` matching nothing returns 0, so a phrase
    that named a cause would be inventing one."""
    phrase = for_framework("vitest").explain(1)

    assert "1" in phrase
    assert for_framework("jest").explain(0) != phrase


def test_a_report_entry_with_no_name_does_not_lose_the_other_entries():
    """`_relpath` must never raise: a malformed entry is still evidence about
    the entries beside it, and a classifier that died here would report a
    passing suite as KIND_ENVIRONMENT through `_Runner`'s own error path."""
    outcome = for_framework("vitest").classify(
        exit_code=0, stdout="", stderr="", report={"testResults": [
            {"assertionResults": [{"status": "passed", "fullName": "ok"}]},
            {"name": "/repo/tests/a.test.js",
             "assertionResults": [{"status": "passed", "fullName": "fine"}]},
        ]})

    assert outcome.kind == KIND_PASSED
    assert "tests/a.test.js" in outcome.files_run


def test_a_relative_report_name_is_left_alone():
    """`_relpath` strips the container's /repo mount and nothing else. A name
    that is already relative is passed through rather than guessed at -- the
    alternative is a path this code invented appearing in `failed_ids`, which
    the grader publishes as the id the model failed."""
    from bakeoff.runners.node_adapter import VITEST

    assert VITEST._relpath("/repo/tests/a.test.js") == "tests/a.test.js"
    assert VITEST._relpath("tests/a.test.js") == "tests/a.test.js"
    assert VITEST._relpath("/elsewhere/a.test.js") == "/elsewhere/a.test.js"
    assert VITEST._relpath("") == ""


# --- node id validation, which runs at LOAD time -------------------------------


@pytest.mark.parametrize("framework", ["vitest", "jest"])
@pytest.mark.parametrize("node_id", [
    "tests/../../etc/passwd.test.js::does a thing",
    "tests/../src/a.test.js::does a thing",
    "/repo/tests/a.test.js::does a thing",
])
def test_a_traversing_or_absolute_file_half_is_refused_at_load(framework, node_id):
    """`_under` is `is_relative_to`, which is PURELY LEXICAL: it does not
    normalise `..`, so `tests/../../etc/x.test.js` is "under" `tests/` and
    loads clean. The runner then resolves it against the real filesystem and
    selects nothing -- which on both frameworks exits 0 with every test
    skipped, so the id is a silent no-op and no downstream check says so.

    An absolute path is refused for the same reason one component over: the
    positional is matched against the absolute path inside the container, and a
    host-shaped or mount-shaped prefix in a manifest names a file the scope
    check cannot reason about."""
    from bakeoff.tasks import TaskError

    with pytest.raises(TaskError, match="tests.f2p"):
        for_framework(framework).validate_node_id(
            node_id, ("tests/",), "tests.f2p")


def test_the_node_repo_mount_constant_does_not_drift():
    """Three copies of `/repo` now exist -- `container.REPO_MOUNT`,
    `tasks._REPO_MOUNT` and this one -- because container.py imports `docker`
    at module level and both the loader and this adapter run without a daemon.
    Drift is silent: a stale prefix here means `_relpath` strips nothing, and
    every `failed_ids` entry the grader publishes carries a `/repo/` prefix no
    manifest uses, so the confinement comparison can never hold."""
    from bakeoff.runners.node_adapter import _REPO_MOUNT
    from bakeoff.tasks import _REPO_MOUNT as _TASKS_REPO_MOUNT

    assert _REPO_MOUNT == _TASKS_REPO_MOUNT


# --- executed_names, the channel preflight's duplicate-name rule reads --------


@pytest.mark.parametrize("framework", ("vitest", "jest"))
def test_executed_names_reports_only_the_tests_that_reached_a_verdict(framework):
    """`passed` OR `failed`, made rootdir-relative.

    Not "every assertion in the report": vitest reports a deselected test as
    `skipped` and jest reports it as `pending`, and preflight's
    duplicate-`fullName` assertion is a claim about what actually RAN under
    `tests.paths`. Counting a skipped test would refuse a task over a
    collision between two names that never appear in one run.
    """
    adapter = for_framework(framework)
    report = {"testResults": [
        {"name": "/repo/tests/a.test.js", "status": "failed",
         "assertionResults": [
             {"status": "passed", "fullName": "works"},
             {"status": "failed", "fullName": "does not"},
             {"status": "skipped", "fullName": "deselected"},
             {"status": "pending", "fullName": "also deselected"},
         ]},
        {"name": "/repo/tests/b.test.js", "status": "passed",
         "assertionResults": [{"status": "passed", "fullName": "works"}]},
    ]}

    assert list(adapter.executed_names(report)) == [
        ("tests/a.test.js", "works"),
        ("tests/a.test.js", "does not"),
        ("tests/b.test.js", "works"),
    ]


@pytest.mark.parametrize("framework", ("vitest", "jest"))
def test_executed_names_of_a_report_that_does_not_exist_is_empty(framework):
    """A run that wrote no report ran nothing this can name. It is NOT the
    caller's cue that nothing collided: preflight only reaches the duplicate
    rule off a scoped run it classified, and `report=None` classifies as an
    environment problem before that."""
    assert list(for_framework(framework).executed_names(None)) == []


def test_pytest_executed_names_is_empty_and_that_is_a_claim():
    """Not an omission. A pytest node id carries the FILE, so `--deselect
    a.py::test_x` cannot reach `b.py::test_x` and the duplicate-`fullName`
    hazard does not exist. preflight still writes its evidence key from this
    -- as `[]`, "measured, nothing found", which is a different fact from the
    `None` an explicit-p2p task leaves."""
    assert list(for_framework("pytest").executed_names({"testResults": []})) == []


def test_verify_selected_and_executed_names_read_ONE_rule():
    """`verify_selected` is defined over `executed_names` rather than beside
    it. "Which tests reached a verdict" has two readers -- the not-run check
    and the duplicate-name check -- and a second copy of the rule is a second
    thing that can be wrong about what ran, inside the two checks that exist
    to be right about it."""
    import inspect

    from bakeoff.runners import node_adapter

    assert "executed_names" in inspect.getsource(node_adapter.verify_selected)
