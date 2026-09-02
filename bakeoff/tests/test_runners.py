"""The runner adapter seam.

Every red/green judgement this harness makes used to be a branch on pytest's
exit codes. That is not portable -- measured 2026-09-01, vitest 3.2.7 and jest
30.5.0 exit 1 for a failing test, an unresolvable import, a syntax error, a
nonexistent file argument AND a broken config -- so the judgement moves behind
an adapter and the exit code becomes one input among several.

This file pins the adapter contract. `test_preflight.py`, `test_oracle.py` and
`test_grader.py` keep pinning what the CONSUMERS do with it.
"""

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

# TODO (broadening 7 Task 4): pin `tasks._FRAMEWORKS == FRAMEWORKS` here once
# the manifest key and its closed allowlist exist. Until then the registry has
# nothing to disagree with, and `for_framework` raises KeyError rather than
# defaulting precisely so that disagreement can never be silent.


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


def test_the_node_adapters_are_stubs_until_task_6():
    """`FRAMEWORKS` and `tasks._FRAMEWORKS` must agree from the first commit,
    so the registry is complete now and the behaviour arrives later. A
    registry that GROWS later is a registry the loader's allowlist can
    disagree with, and that disagreement surfaces as a KeyError out of the
    middle of preflight."""
    for name in ("vitest", "jest"):
        adapter = for_framework(name)
        with pytest.raises(NotImplementedError, match="broadening 7 Task 6"):
            adapter.classify(exit_code=1, stdout="", stderr="", report=None)


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
    """Identity against the REAL `_Runner.pass_to_pass`, not against a literal
    transcribed from it. A transcription is a second copy of the thing under
    test, and it agrees with the adapter for exactly as long as both are wrong
    in the same way."""
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

    adapter = for_framework("pytest")

    cases = [
        (_Tests(("tests/a.py::test_one",), ()),
         ("tests/c.py::test_flaky",), ("tests/",), ("tests/broken.py",)),
        (_Tests(("tests/a.py::test_one",), ()), (), (), ()),
        (_Tests((), ("tests/b.py::test_two",)), (), (), ()),
        (_Tests(("tests/a.py::test_one",), ("tests/b.py::test_two",)),
         ("tests/c.py::test_flaky",), ("tests/",), ("tests/broken.py",)),
    ]

    for tests, extra_deselect, scope, ignore in cases:
        recorder = _Recorder()
        runner = _Runner(recorder, ("python", "-m", "pytest", "-q"), 600)
        runner.pass_to_pass(tests, extra_deselect=extra_deselect, scope=scope,
                            ignore=ignore)
        live = recorder.argv[len(["timeout", "600", "python", "-m", "pytest",
                                 "-q"]):]

        if tests.p2p:
            through_adapter = adapter.p2p_args(
                selected=tuple(tests.p2p), scope=(),
                deselected=tuple(extra_deselect), ignored=tuple(ignore))
        else:
            through_adapter = adapter.p2p_args(
                selected=(), scope=tuple(scope),
                deselected=tuple(tests.f2p) + tuple(extra_deselect),
                ignored=tuple(ignore))

        assert through_adapter == live


def test_pytest_select_args_are_the_bare_node_ids():
    assert for_framework("pytest").select_args(("a::b", "c::d")) == ["a::b", "c::d"]


def test_pytest_select_args_match_the_live_runner():
    """Identity against the REAL `_Runner.select`, for the reason the p2p
    identity test gives: a literal transcribed from the code under test is a
    second copy of it, and it agrees for exactly as long as both are wrong in
    the same way. The f2p selection is the run the whole red-before verdict
    is read off, so this is the argv least able to afford a drift."""
    from bakeoff.preflight import _Runner

    class _Recorder:
        def __init__(self):
            self.argv = []

        def exec(self, argv):
            self.argv = argv
            return None

    adapter = for_framework("pytest")
    node_ids = ("tests/a.py::test_one", "tests/a.py::Klass::test_two[x::y]")

    recorder = _Recorder()
    _Runner(recorder, ("python", "-m", "pytest", "-q"), 600,
            adapter).select(node_ids)
    live = recorder.argv[len(["timeout", "600", "python", "-m", "pytest",
                             "-q"]):]

    assert adapter.select_args(node_ids) == live


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
