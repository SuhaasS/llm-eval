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
from bakeoff.runners.pytest_adapter import failed_node_ids

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
        assert adapter.select_argvs(()) == []
        assert adapter.p2p_argvs(selected=(), scope=("tests/",),
                                 deselected=(), ignored=()) == [["tests/"]]
        assert adapter.merge_reports([]) is None
        assert adapter.file_filter_matches("a", "a") is True
        assert adapter.explain(1)
        assert adapter.validate_id_set((), "tests.f2p") is None
        assert adapter.parse_deselected(stdout="", report=None) is None
        assert adapter.duplicate_ids({"testResults": []}) == {}, name

    for name in ("vitest", "jest"):
        # No report is the config-error signal on these two, and it is the one
        # branch a stub could most plausibly have been left returning.
        assert for_framework(name).classify(
            exit_code=1, stdout="", stderr="", report=None
        ).kind == KIND_ENVIRONMENT, name


# --- the pytest adapter reproduces today's behaviour -------------------------


def test_pytest_p2p_argvs_deselect_branch_is_byte_identical_to_todays_argv():
    """The whole reason the refactor is safe. `_Runner.pass_to_pass` used to
    build this list inline; if the adapter emits anything else -- a reordered
    segment, an inserted flag -- the gated command stops being the graded one
    and the oracle stops describing the thing being graded.

    The identity property is now stated over a SEQUENCE of argvs, because a
    node check is one command per file. pytest emits exactly one group and
    its element is the literal below, so the property is unchanged in
    content: a second group here would be a second suite run for nothing.
    """
    adapter = for_framework("pytest")

    assert adapter.p2p_argvs(
        selected=(), scope=(), deselected=("tests/a.py::test_one",), ignored=()
    ) == [["--deselect", "tests/a.py::test_one"]]


def test_pytest_p2p_argvs_explicit_branch_is_byte_identical_to_todays_argv():
    """One group, and its element is the pre-grouping literal."""
    adapter = for_framework("pytest")

    assert adapter.p2p_argvs(
        selected=("tests/b.py::test_two",), scope=(), deselected=(), ignored=()
    ) == [["tests/b.py::test_two"]]


def test_pytest_p2p_argvs_keep_scope_then_f2p_then_quarantine_then_ignore():
    """The ORDER is the argv. Today `pass_to_pass` emits scope, then one
    --deselect per f2p id, then one per quarantined id, then the --ignores; the
    combined `deselected` tuple is (f2p + quarantine) precisely so that this
    stays true. One group, as every pytest check is."""
    adapter = for_framework("pytest")

    assert adapter.p2p_argvs(
        selected=(),
        scope=("tests/",),
        deselected=("tests/a.py::test_one", "tests/c.py::test_flaky"),
        ignored=("tests/broken.py",),
    ) == [[
        "tests/",
        "--deselect", "tests/a.py::test_one",
        "--deselect", "tests/c.py::test_flaky",
        "--ignore=tests/broken.py",
    ]]


def test_pytest_select_argvs_and_p2p_argvs_are_exactly_one_group_each():
    """One group on both branches, and never two.

    The node adapters emit one argv per FILE; pytest's node id carries its
    own file, so a second group would be a second suite invocation buying
    nothing -- and the gated-equals-graded byte identity is stated over this
    sequence now.
    """
    adapter = for_framework("pytest")

    assert adapter.select_argvs(("a.py::test_x", "b.py::test_y")) == [
        ["a.py::test_x", "b.py::test_y"]]
    assert len(adapter.p2p_argvs(
        selected=(), scope=("tests/",),
        deselected=("tests/a.py::test_one",), ignored=())) == 1
    assert len(adapter.p2p_argvs(
        selected=("tests/b.py::test_two",), scope=(),
        deselected=("tests/b.py::test_flaky",), ignored=())) == 1


def test_pytest_merge_reports_is_None_and_that_is_a_claim():
    """pytest writes no machine-readable report at all -- `report_path()` is
    `None` -- so there is nothing to fold and never will be. `None` here is
    the claim "this framework reports nothing", not the gap "a group produced
    no evidence" that the node adapters' `None` names."""
    adapter = for_framework("pytest")

    assert adapter.report_path() is None
    assert adapter.merge_reports([]) is None
    assert adapter.merge_reports([{"testResults": []}]) is None


def test_pytest_p2p_argvs_matches_the_live_runner_on_both_branches():
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


def test_pytest_select_argvs_are_the_bare_node_ids():
    assert for_framework("pytest").select_argvs(
        ("a::b", "c::d")) == [["a::b", "c::d"]]


def test_pytest_select_argvs_match_the_live_runner():
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


def test_failed_node_ids_reads_a_subfailed_line():
    """Fix 3. pytest 9's core-integrated subtests print `SUBFAILED(label) <id>
    - <msg>` for a failing `unittest.subTest`, never `FAILED <id>`, for a node
    whose ONLY failures are subtest failures -- measured 2026-09-02 against
    the pinned eval image's pytest 9.1.1. A node's failure must map to its own
    id regardless of how many subtests failed under it, so the node id is the
    fact and the label is noise."""
    output = (
        "SUBFAILED(i=0) tests/x.py::T::test_sub - AssertionError: 0 != 1\n"
        "1 failed, 2 subtests passed in 0.01s\n"
    )

    assert failed_node_ids(output) == {"tests/x.py::T::test_sub"}


def test_failed_node_ids_collapses_two_subfailed_lines_for_one_id():
    """Two failing subtests under the same node print two SUBFAILED lines --
    measured -- and both must resolve to the SAME node id, not two."""
    output = (
        "SUBFAILED(i=0) tests/x.py::T::test_sub - AssertionError: 0 != 1\n"
        "SUBFAILED(i=2) tests/x.py::T::test_sub - AssertionError: 2 != 1\n"
        "2 failed, 1 subtests passed in 0.01s\n"
    )

    assert failed_node_ids(output) == {"tests/x.py::T::test_sub"}


def test_failed_node_ids_reads_the_positional_subtest_label_shape():
    """Measured 2026-09-02: a keyword `subTest(i=i)` labels its SUBFAILED line
    `(i=0)`; a positional `subTest(i)` labels it `[0]` instead -- pytest picks
    the bracket by the subtest's OWN call shape, not by anything the adapter
    controls, so both must be accepted."""
    output = "SUBFAILED[0] tests/x.py::T::test_sub_no_kwargs - AssertionError: 0 != 1\n"

    assert failed_node_ids(output) == {"tests/x.py::T::test_sub_no_kwargs"}


def test_failed_node_ids_reads_a_subfailed_beside_a_failed_for_different_ids():
    """A plain node failure and a subtest-only node failure in the same run
    are two different facts and both must survive."""
    output = (
        "FAILED tests/a.py::test_plain - AssertionError: x\n"
        "SUBFAILED(i=1) tests/x.py::T::test_sub - AssertionError: 1 != 0\n"
    )

    assert failed_node_ids(output) == {
        "tests/a.py::test_plain",
        "tests/x.py::T::test_sub",
    }


def test_failed_node_ids_does_not_read_a_subfailed_as_an_arbitrary_prefix():
    """There is no `SUBERROR`: measured 2026-09-02, a bare exception raised
    inside a `subTest` block (not just a failing assertion) is STILL labelled
    `SUBFAILED` by pytest 9.1.1, so no second alternative belongs in the
    regex. And a hypothetical `SUBPASSED` line -- pytest prints none under
    `-q`, but the regex must not accept one by accident -- must not be read as
    a failure: only the literal `SUBFAILED` alternative is in `_FAILED_LINE`,
    so a node whose subtests all pass (no FAILURES entry, no summary line
    naming it at all) stays absent from the set."""
    output = "SUBPASSED(i=0) tests/x.py::T::test_ok - ok\n"

    assert failed_node_ids(output) == set()


def test_failed_node_ids_still_excludes_the_colon_not_found_case():
    """The pre-existing `ERROR: not found:` refusal must survive the widened
    alternation untouched -- a manifest typo still stops the matrix rather
    than reading as a passed check."""
    output = "ERROR: not found: /repo/tests/a.py::test_gone\n\nno tests ran in 0.00s\n"

    assert failed_node_ids(output) == set()


def test_pytest_classify_is_failed_when_only_subtests_failed():
    """The end-to-end shape: exit 1 with SUBFAILED-only output must classify
    as KIND_FAILED and carry the node id in `failed_ids`, exactly as a plain
    FAILED line would."""
    outcome = for_framework("pytest").classify(
        exit_code=1,
        stdout=(
            "SUBFAILED(i=0) tests/x.py::T::test_sub - AssertionError: 0 != 1\n"
            "1 failed, 2 subtests passed in 0.01s\n"
        ),
        stderr="",
        report=None,
    )

    assert outcome.kind == KIND_FAILED
    assert outcome.failed_ids == {"tests/x.py::T::test_sub"}


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
    """One of the nine measured report shapes, replayed from disk.

    Captured once in `node:22-bookworm-slim` against vitest 3.2.7 and jest
    30.5.0; see the README beside them for the argv each came from and for the
    exit code each run produced. They are replayed rather than re-measured
    because regenerating one needs a Docker daemon, a network and 73 MB of npm
    -- and because the whole point of these tests is that the exit code is not
    what the classification rests on. The ninth (`same_file_dup`) in
    `bakeoff-eval-agent:base-node-22`, whose runners are the same pinned
    versions -- see the README beside them.
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


def test_node_select_argvs_pair_each_file_with_only_its_own_names():
    """The measurement this whole design turns on.

    2026-09-02, in the pinned node base, jest 30.5.0 and vitest 3.2.7 alike:
    two file positionals plus ONE union `-t` over one title from each file
    executed THREE tests -- the third being the second file's copy of the
    first file's title. `-t` matches `fullName` and the positionals are ANDed
    across the whole invocation, never zipped, so the pairing has to be the
    invocation. One argv per file is that pairing.
    """
    for framework in ("vitest", "jest"):
        adapter = for_framework(framework)

        groups = adapter.select_argvs(
            ("tests/a.test.js::works", "tests/b.test.js::other"))

        assert len(groups) == 2, framework
        assert "other" not in groups[0][-1], framework
        assert "works" not in groups[1][-1], framework


def test_node_select_argvs_group_each_file_once_and_in_first_seen_order():
    """One group per FILE, not one per id: a repeated positional is a second
    pattern that has to agree with the first, and the argv is what the
    gated-equals-graded byte identity is stated over."""
    assert for_framework("vitest").select_argvs(
        ("tests/a.test.js::x", "tests/b.test.js::y", "tests/a.test.js::z")
    ) == [
        ["tests/a.test.js", "-t", "^(?:x|z)$"],
        ["tests/b.test.js", "-t", "^(?:y)$"],
    ]


def test_node_select_argvs_on_nothing_are_no_groups_not_a_match_all():
    """`[]` used to be ONE argv with no filter -- which `_Runner.run` handed
    to the bare runner, running the WHOLE SUITE as the selection and
    classifying it as one. It is now NO GROUPS, and `run` raises on it."""
    for framework in ("vitest", "jest"):
        assert for_framework(framework).select_argvs(()) == [], framework


def test_jest_file_filter_is_mount_anchored_and_vitest_is_a_bare_substring():
    r"""Measured 2026-09-02, and the two frameworks needed different strings.

    jest's positional is a JS `RegExp` tested against BOTH the repo-relative
    path and the absolute one: bare `tests/doc/stringify.test.js` and
    `$`-only `tests/doc/stringify\.test\.js$` each ALSO matched
    `/repo/pkg/tests/doc/stringify.test.js`, while
    `^/repo/tests/doc/stringify\.test\.js$` matched exactly one file.

    vitest's is a substring filter no anchoring reaches: both the relative and
    the absolute spelling pulled the tail-colliding file in, so the bare
    relative path is the honest spelling and preflight refuses the trees it
    cannot separate.
    """
    jest = for_framework("jest").select_argvs(
        ("tests/doc/stringify.test.js::x",))
    vitest = for_framework("vitest").select_argvs(
        ("tests/doc/stringify.test.js::x",))

    assert jest[0][0] == "^/repo/tests/doc/stringify\\.test\\.js$"
    assert vitest[0][0] == "tests/doc/stringify.test.js"


def test_a_tail_colliding_path_is_matched_by_vitests_filter_and_not_by_jests():
    """`file_filter_matches` is the predicate preflight refuses on, and it is
    the adapter's because only the adapter knows what its positional is.

    The vitest branch OVER-APPROXIMATES on purpose: `a in b` fires for
    `tests/a.test.js` against `src/tests/a.test.js`, which is a genuine vitest
    collision, and does not fire for `tests/foo.test.js` against
    `tests/xfoo.test.js`, which is not one. jest's branch is equality and can
    never fire; pytest's is equality for a second reason -- its positional IS
    a path.
    """
    assert for_framework("vitest").file_filter_matches(
        "tests/doc/a.test.js", "pkg/tests/doc/a.test.js") is True
    assert for_framework("jest").file_filter_matches(
        "tests/doc/a.test.js", "pkg/tests/doc/a.test.js") is False
    assert for_framework("pytest").file_filter_matches(
        "tests/doc/a.test.js", "pkg/tests/doc/a.test.js") is False


def test_the_jest_file_filter_escapes_a_regex_character_in_a_directory_component():
    r"""Measured 2026-09-02, and the earlier example for this was wrong.

    A `foo.test.js` / `foo_test.js` pair is NOT the hazard: jest's default
    `testMatch` never collects `foo_test.js` at all, and the positional is
    applied AFTER `testMatch`, so the unescaped `.` never sees it. The real
    collision is between two files that both match `testMatch`, and it lives
    in the DIRECTORY components:

        jest '^/repo/tests/v1.2/a.test.js$'    -> ran tests/v1.2/ AND tests/v1X2/
        jest '^/repo/tests/v1\.2/a\.test\.js$' -> ran tests/v1.2/ only
    """
    import re

    from bakeoff.runners.node_adapter import _REPO_MOUNT

    pattern = for_framework("jest").select_argvs(
        ("tests/v1.2/a.test.js::x",))[0][0]
    unescaped = "^" + _REPO_MOUNT + "/tests/v1.2/a.test.js$"

    assert re.search(pattern, "/repo/tests/v1.2/a.test.js")
    assert not re.search(pattern, "/repo/tests/v1X2/a.test.js")
    assert re.search(unescaped, "/repo/tests/v1X2/a.test.js")


def test_node_p2p_argvs_deselect_branch_puts_each_deselected_file_in_its_own_group():
    """1 + K commands. Group 0 is the scope with every file holding a
    deselection EXCLUDED and no `-t` at all; then one group per such file,
    carrying only that file's own deselected titles.

    That is what keeps a quarantine of `a::works` off `b::works`. Under the
    single-invocation argv it removed both, silently, with `p2p_deselected`
    agreeing because two tests really were skipped."""
    from bakeoff.runners.node_adapter import _js_escape

    for framework in ("vitest", "jest"):
        spelling = _js_escape if framework == "jest" else (lambda path: path)
        groups = for_framework(framework).p2p_argvs(
            selected=(), scope=("tests/",),
            deselected=("tests/a.test.js::works", "tests/b.test.js::works"),
            ignored=())

        assert len(groups) == 3, framework
        # Group 0 names BOTH deselected files -- as exclusions, a lookahead on
        # jest and an `--exclude=` on vitest, never as positionals that
        # collect. Under the pre-grouping argv it named neither and ran both
        # under one global negative pattern.
        assert "-t" not in groups[0], framework
        assert spelling("tests/a.test.js") in " ".join(groups[0]), framework
        assert spelling("tests/b.test.js") in " ".join(groups[0]), framework
        assert groups[0][0].startswith(
            "^(?!" if framework == "jest" else "tests/"), framework
        # Groups 1 and 2 pair one file with only its own title.
        assert groups[1][-1] == "^(?!(?:works)$)", framework
        assert groups[2][-1] == "^(?!(?:works)$)", framework
        assert "b.test.js" not in groups[1][0], framework
        assert "a.test.js" not in groups[2][0], framework


def test_node_p2p_argvs_deselect_group_zero_carries_no_dash_t():
    """No `-t` at all, and not `-t ''`: an empty pattern matches every name,
    which is what this group wants and is also exactly what a builder that
    failed to fill the pattern in would emit."""
    for framework in ("vitest", "jest"):
        group_zero = for_framework(framework).p2p_argvs(
            selected=(), scope=("tests/",),
            deselected=("tests/a.test.js::works",), ignored=())[0]

        assert "-t" not in group_zero, framework
        assert "" not in group_zero, framework


def test_node_p2p_argvs_explicit_branch_pairs_selection_and_deselection_per_file():
    """One group per selected file, and a quarantine reaches only the group
    whose file it names."""
    groups = for_framework("jest").p2p_argvs(
        selected=("tests/a.test.js::x", "tests/b.test.js::y"), scope=(),
        deselected=("tests/a.test.js::q",), ignored=())

    assert len(groups) == 2
    assert groups[0][-1] == "^(?!(?:q)$)(?:x)$"
    assert groups[1][-1] == "^(?:y)$"


def test_a_quarantined_id_naming_an_uncollected_file_emits_no_pattern_for_it():
    """On the explicit branch a deselection whose file is not in `selected` is
    DROPPED rather than emitted: no group collects that file, so a pattern for
    it would be a no-op that reads like a deselection."""
    groups = for_framework("vitest").p2p_argvs(
        selected=("tests/a.test.js::x",), scope=(),
        deselected=("tests/z.test.js::q",), ignored=())

    assert len(groups) == 1
    assert not any("q" in arg for arg in groups[0])
    assert not any("z.test.js" in arg for arg in groups[0])


def test_the_explicit_branch_still_drops_an_ignored_file_when_another_will_run():
    """The guard added below for the all-ignored case must not resurrect the
    ordinary drop: with a second, non-ignored file selected, the ignored
    file's group is skipped exactly as it was before."""
    groups = for_framework("vitest").p2p_argvs(
        selected=("tests/f.test.js::red", "tests/g.test.js::blue"), scope=(),
        deselected=(), ignored=("tests/f.test.js",))

    assert len(groups) == 1
    assert not any("f.test.js" in arg for arg in groups[0])
    assert any("g.test.js" in arg for arg in groups[0])


def test_the_explicit_branch_does_not_drop_every_group_when_every_selected_file_is_ignored():
    """Mirrors the deselect branch's own guard
    (`test_group_zero_survives_when_it_is_the_only_group_there_could_be` in
    `test_preflight.py`). `ignored` is preflight's p2p-BEFORE flag, naming
    files whose f2p module failed to import at the start state -- and on an
    explicit `tests.p2p` task every declared id can live in that one module,
    so dropping every one of them the way the partial case above drops a
    single file would return `groups == []` here. An empty SEQUENCE is what
    `_Runner.run` refuses, and `run_matrix.py` has no `except` around
    `preflight(...)` -- so an unconditional drop turns a per-task NO-GO into a
    `ValueError` that kills every remaining task's gate. The group is emitted,
    and the run is left loud (KIND_NOTHING_RAN), instead."""
    for framework in ("vitest", "jest"):
        groups = for_framework(framework).p2p_argvs(
            selected=("tests/f.test.js::red",), scope=(),
            deselected=(), ignored=("tests/f.test.js",))

        assert len(groups) == 1, framework
        assert "-t" in groups[0], framework


def test_no_node_argv_group_ever_carries_two_dash_t():
    """The ONE-`-t` rule is per ARGV and was never about one invocation.
    Measured 2026-09-02:

        $ vitest run -t 'a' -t 'b' tests/pass.test.js
        Error: Expected a single value for option
        "-t, --testNamePattern <pattern>", received ["a", "b"]
        -> exit 1, and NO report file

        $ jest -t 'adds' -t 'subs'
        Ran all test suites with tests matching "adds,subs".
        -> exit 0, zero tests run

    vitest's refusal is loud and classifies as KIND_ENVIRONMENT; jest's
    comma-join is the silent hole, reached by an argv nobody meant to write.
    """
    for framework in ("vitest", "jest"):
        adapter = for_framework(framework)
        sequences = [
            adapter.select_argvs(("a.test.js::x", "b.test.js::y")),
            adapter.p2p_argvs(selected=(), scope=("tests/",),
                              deselected=("tests/a.test.js::x",
                                          "tests/b.test.js::y"),
                              ignored=()),
            adapter.p2p_argvs(selected=("a.test.js::x",), scope=(),
                              deselected=("a.test.js::q",), ignored=()),
        ]
        for groups in sequences:
            for argv in groups:
                assert argv.count("-t") <= 1, (framework, argv)


def test_an_ignored_file_gets_no_group_of_its_own_and_is_excluded_from_the_scope_group():
    """preflight's p2p-BEFORE shape: the f2p module does not import at the
    start state, so it must not be collected at all. A file in `ignored` is
    excluded from group 0 and gets no group of its own, even when it also
    holds a deselection -- a group for it would collect exactly the module the
    ignore exists to keep out."""
    from bakeoff.runners.node_adapter import _js_escape

    for framework in ("vitest", "jest"):
        spelling = _js_escape if framework == "jest" else (lambda path: path)
        groups = for_framework(framework).p2p_argvs(
            selected=(), scope=("tests/",),
            deselected=("tests/f.test.js::red",), ignored=("tests/f.test.js",))

        assert len(groups) == 1, framework
        assert "-t" not in groups[0], framework
        assert spelling("tests/f.test.js") in " ".join(groups[0]), framework


def test_jest_excludes_through_the_positional_with_both_path_spellings():
    """jest emits NO `--testPathIgnorePatterns`, and that is a measurement.

    The flag REPLACES the repository's own `testPathIgnorePatterns` rather
    than adding to it, and re-emitting `/node_modules/` beside it restores
    jest's BUILT-IN default rather than anything the repository declared.
    `eemeli/yaml` -- the corpus's only node task -- reports
    `["tests/_utils", "tests/json-test-suite/"]` and no `/node_modules/`
    through its own `--showConfig`, and `tests/_utils` matches its
    `testMatch`, so the flag would pull its helper modules into the
    regression check at gate time and at grade time alike.

    Two lookaheads per file, because jest tests a positional against BOTH
    spellings and collects if either matches. Measured 2026-09-02: a lookahead
    naming only the absolute spelling did not exclude, and one naming only the
    relative spelling did not either.
    """
    groups = for_framework("jest").p2p_argvs(
        selected=(), scope=("tests/",),
        deselected=("tests/a.test.js::gone",), ignored=())

    assert groups[0] == [
        "^(?!tests/a\\.test\\.js$)(?!/repo/tests/a\\.test\\.js$).*tests/"]
    assert not any("--testPathIgnorePatterns" in arg
                   for group in groups for arg in group)


def test_vitest_excludes_through_the_bare_exclude_flag_not_a_glob():
    """Measured 2026-09-02: on vitest the SAME string is a substring filter as
    a positional and a GLOB as `--exclude`.

        --exclude=tests/doc/a.test.js       -> pkg/tests/doc/a.test.js STILL RAN
        --exclude='**/tests/doc/a.test.js'  -> pkg/tests/doc/a.test.js EXCLUDED

    The bare spelling is what this design wants: group 0 must lose exactly the
    deselected file, never a second file whose path ends the same way."""
    groups = for_framework("vitest").p2p_argvs(
        selected=(), scope=("tests/",),
        deselected=("tests/a.test.js::gone",), ignored=())

    assert groups[0] == ["tests/", "--exclude=tests/a.test.js"]


def test_the_jest_scope_segment_stays_unanchored_so_the_scope_check_can_still_fire():
    """`scope_files_outside` is a claim about what the DECLARED prefix matches
    -- measured, `vitest run tests/` matched `/repo/jtests/fail.test.cjs` and
    jest's `tests/` matched `/repo/pkg/tests/doc/...`. Anchoring the scope
    segment at the mount also works as an exclusion (measured) and would make
    that check unable to fire: a check that looks like a measurement and
    cannot be one. So the raw prefix stays, behind a `.*`."""
    group_zero = for_framework("jest").p2p_argvs(
        selected=(), scope=("tests/",),
        deselected=("tests/a.test.js::gone",), ignored=())[0]

    assert ".*tests/" in group_zero[0]
    assert "/repo/tests/" not in group_zero[0].split(".*")[-1]


def test_group_zero_is_byte_identical_to_todays_argv_when_nothing_is_excluded():
    """The scoped p2p run of a task with no quarantine and no ignore is the
    argv the grader has always made. Nothing about the grouping may move it."""
    for framework in ("vitest", "jest"):
        assert for_framework(framework).p2p_argvs(
            selected=(), scope=("tests/", "src/"), deselected=(), ignored=()
        ) == [["tests/", "src/"]], framework


def test_the_scope_precedes_the_exclusions_and_the_pattern_follows_them():
    """Measured 2026-09-02, and it is a correctness rule, not a style one.
    jest's `--testPathIgnorePatterns` was a greedy yargs array: with the
    positional LAST it swallowed `tests/` into the ignore list and ran NOTHING
    -- exit 1, empty `testResults` -- so the p2p check reported a regression
    suite that executed zero tests. jest emits no such flag any more, but
    vitest's `--exclude` sits in the same place, so the scope still precedes
    the flags. A trailing `-t` is safe on a greedy option because it starts
    with `-`, which ends the array."""
    group_zero = for_framework("vitest").p2p_argvs(
        selected=(), scope=("tests/",),
        deselected=("tests/a.test.js::gone",), ignored=("tests/x.js",))[0]

    assert group_zero == ["tests/",
                          "--exclude=tests/x.js",
                          "--exclude=tests/a.test.js"]


def test_a_scope_prefix_that_is_itself_the_deselected_file_gets_no_group_zero():
    """`tests.paths` may name a FILE, not a directory prefix.

    `yaml-474-single-newline-empty-value` declares
    `["tests/doc/stringify.ts"]`, which is also its only f2p file, so group 0
    would exclude the whole of its own scope and collect nothing -- measured
    2026-09-02, both frameworks answer that with exit 1 and a report of ZERO
    tests, which the gate reads as a regression suite that is not green on a
    task that is fine. The check is then exactly one command, and it is the
    one the pre-grouping argv made."""
    for framework in ("vitest", "jest"):
        groups = for_framework(framework).p2p_argvs(
            selected=(), scope=("tests/doc/stringify.ts",),
            deselected=("tests/doc/stringify.ts::maps x",), ignored=())

        assert len(groups) == 1, framework
        assert groups[0][-1] == "^(?!(?:maps x)$)", framework


def test_group_zero_survives_when_it_is_the_only_group_there_could_be():
    """The skip above is conditional on something ELSE running. With no
    deselected file there is no other group, and an empty SEQUENCE is what
    `_Runner.run` refuses -- so group 0 is kept instead of a crash out of the
    middle of the gate. It does not stay empty, though: with the whole
    declared scope excluded, `_scope_positionals` falls back to sweeping the
    whole repository minus the excluded file -- the same argv the `scope=()`
    callers already get -- so what is emitted is a WIDENED regression check,
    not a loud empty one."""
    groups = for_framework("jest").p2p_argvs(
        selected=(), scope=("tests/a.test.js",), deselected=(),
        ignored=("tests/a.test.js",))

    assert len(groups) == 1
    assert "-t" not in groups[0]
    assert groups[0] == [
        "^(?!tests/a\\.test\\.js$)(?!/repo/tests/a\\.test\\.js$).*"]


def test_node_merge_reports_concatenate_test_results_and_sum_pending():
    """`Outcome.files_run` and the deselection count are read off the merged
    report, so a check that ran three commands has to answer as one run."""
    adapter = for_framework("vitest")

    merged = adapter.merge_reports([
        {"testResults": [{"name": "/repo/a.test.js"}], "numPendingTests": 1},
        {"testResults": [{"name": "/repo/b.test.js"}], "numPendingTests": 2},
    ])

    assert merged == {
        "testResults": [{"name": "/repo/a.test.js"},
                        {"name": "/repo/b.test.js"}],
        "numPendingTests": 3,
    }


def test_node_merge_reports_of_a_group_that_wrote_no_report_is_None():
    """A partial merge would report a green suite for a run half of which
    produced no evidence. `classify` reads the `None` as KIND_ENVIRONMENT,
    which is what a group that did not say what it did actually is."""
    adapter = for_framework("jest")

    merged = adapter.merge_reports([{"testResults": []}, None])

    assert merged is None
    assert adapter.classify(exit_code=1, stdout="", stderr="",
                            report=merged).kind == KIND_ENVIRONMENT


def test_node_merge_reports_of_nothing_is_None_not_an_empty_run():
    """`{"testResults": []}` would be a CLAIM that a run happened and executed
    nothing, which `classify` reads as KIND_NOTHING_RAN -- a statement about
    the selection. An empty list of groups is "nobody ran and nobody counted",
    which is the environment absence."""
    adapter = for_framework("vitest")

    assert adapter.merge_reports([]) is None
    assert adapter.classify(exit_code=1, stdout="", stderr="",
                            report={"testResults": []}
                            ).kind == KIND_NOTHING_RAN


def test_node_merge_reports_omit_the_pending_count_when_a_group_did_not_report_one():
    """`parse_deselected` then answers `None` -- "nobody counted" -- rather
    than a sum with a hole in it, which the staleness floor would read as a
    real number."""
    adapter = for_framework("jest")

    merged = adapter.merge_reports([
        {"testResults": [], "numPendingTests": 2},
        {"testResults": []},
    ])

    assert "numPendingTests" not in merged
    assert adapter.parse_deselected(stdout="", report=merged) is None


def test_node_merge_reports_of_one_report_return_it_unchanged():
    """Identity, not a rebuilt copy: a one-group check must leave the report
    exactly as the framework wrote it, keys this merge drops included."""
    adapter = for_framework("vitest")
    report = {"testResults": [], "success": True, "numPendingTests": 0}

    assert adapter.merge_reports([report]) is report


def test_node_validate_id_set_no_longer_refuses_a_cross_file_duplicate():
    """The refusal is gone because the hazard is. Measured 2026-09-02: one
    positional plus one `-t` runs the named tests of that file only, so
    `a.test.js::works` and `b.test.js::works` are as unambiguous to the
    adapter as their id spelling already was.

    The SAME-file case is untouched and is a different problem: two tests
    sharing a full name in one file collapse to the identical node id string,
    which no set-level check comparing `(fullName, path)` pairs can see."""
    for framework in ("vitest", "jest"):
        assert for_framework(framework).validate_id_set(
            ("a.test.js::works", "b.test.js::works"), "tests.f2p") is None


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
    argv = for_framework("vitest").select_argvs(
        ("tests/m.test.js::a+b (x) y",))[0]

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
    caller's cue that nothing collided, and the ORDER does not rescue the
    caller either -- which is what this docstring used to claim. preflight
    runs the duplicate rule IMMEDIATELY after the scoped invocation and
    reaches the KIND_PASSED comparison forty lines later, so an empty answer
    here reached the evidence as `[]` -- "measured, nothing found" -- for a
    run that measured nothing at all. preflight guards its own write on
    `last_report` for that reason; this method stays total."""
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


# --- duplicate_ids: two tests in one file answering to the same id ------------


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_the_same_file_dup_fixture_holds_two_assertions_under_one_name(framework):
    """M11.1: one testResults entry holding three assertionResults, two of
    them with the identical fullName, title and ancestorTitles at statuses
    passed and failed. On the runs this harness makes, nothing in the report
    separates the pair: vitest emits no `location` key at all on a default
    run (it appears only when a file:line positional turns task-location
    capture on), and jest's `location` is present and `null` without
    `--testLocationInResults`."""
    report = _report("same_file_dup", framework)

    assert len(report["testResults"]) == 1
    suite = report["testResults"][0]
    assertions = suite["assertionResults"]
    assert len(assertions) == 3
    dups = [a for a in assertions if a["fullName"] == "outer adds"]
    assert len(dups) == 2
    assert {a["status"] for a in dups} == {"passed", "failed"}
    assert all(a["ancestorTitles"] == ["outer"] for a in dups)
    assert all(a["title"] == "adds" for a in dups)
    if framework == "vitest":
        assert all("location" not in a for a in dups)
    else:
        assert all(a["location"] is None for a in dups)


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_two_tests_with_the_same_name_in_one_file_are_ONE_failed_id(framework):
    """M11.5: `classify` collapses the pair to one failed_id for two
    assertions, which is what the record would carry -- one id, and no field
    saying which of the two tests actually failed."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="",
        report=_report("same_file_dup", framework))

    assert outcome.kind == KIND_FAILED
    assert outcome.failed_ids == {"tests/dup.test.js::outer adds"}
    assert outcome.files_run == ("tests/dup.test.js",)


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_verify_selected_reports_nothing_missing_for_a_duplicated_id(framework):
    """The existing channels read this run clean -- which is why a new one
    (`duplicate_ids`) is needed."""
    from bakeoff.runners.node_adapter import verify_selected

    report = _report("same_file_dup", framework)
    adapter = for_framework(framework)

    assert verify_selected(
        report, ("tests/dup.test.js::outer adds",), adapter) == frozenset()


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_duplicate_ids_counts_a_name_that_appears_more_than_once_in_one_file(
        framework):
    """Mutation selector `more_than_once_in_one_file`."""
    adapter = for_framework(framework)

    assert adapter.duplicate_ids(_report("same_file_dup", framework)) == {
        "tests/dup.test.js::outer adds": 2}


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_duplicate_ids_of_a_clean_report_is_empty_and_that_is_a_measurement(
        framework):
    """The `pass` fixture's three fullNames are distinct."""
    adapter = for_framework(framework)

    assert adapter.duplicate_ids(_report("pass", framework)) == {}


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_duplicate_ids_of_a_report_that_does_not_exist_is_empty(framework):
    """Deliberately not `None`: the absence is preflight's to record, with
    the same guard `duplicate_full_names` carries, because a report-less run
    measured nothing."""
    assert for_framework(framework).duplicate_ids(None) == {}


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_duplicate_ids_counts_only_tests_that_reached_a_verdict(framework):
    """M11.4: the scoped p2p run DESELECTS the f2p ids, and a deselected
    duplicate is `skipped` on vitest and `pending` on jest -- neither
    terminal, so it cannot make a verdict ambiguous. This is why the scoped
    p2p run cannot see an f2p duplicate at all, and preflight's accumulator
    has to span every node run rather than the scoped one."""
    status = "skipped" if framework == "vitest" else "pending"
    report = {"testResults": [
        {"name": "/repo/tests/dup.test.js", "status": "passed",
         "assertionResults": [
             {"status": "passed", "fullName": "outer adds"},
             {"status": status, "fullName": "outer adds"},
         ]},
    ]}

    assert for_framework(framework).duplicate_ids(report) == {}


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_duplicate_ids_does_not_count_one_test_seen_in_two_suite_entries(
        framework):
    """Counting per `testResults` entry can only UNDER-report a duplicate,
    never invent one -- and item 1's `ambiguous_file_filters` is what makes
    the overlapping-groups shape (`merge_reports` concatenating two entries
    for the same file) unreachable in the first place."""
    report = {"testResults": [
        {"name": "/repo/tests/a.test.js", "status": "passed",
         "assertionResults": [{"status": "passed", "fullName": "works"}]},
        {"name": "/repo/tests/a.test.js", "status": "passed",
         "assertionResults": [{"status": "passed", "fullName": "works"}]},
    ]}

    assert for_framework(framework).duplicate_ids(report) == {}


def test_pytest_duplicate_ids_is_empty_and_that_is_a_claim():
    """A pytest node id names exactly one test by construction: two
    functions with the same name in one module shadow each other, a class
    scope is part of the id, and pytest appends an index for a colliding
    parametrized case -- so `--deselect a.py::test_x` reaches one test and
    one test only. See `pytest_adapter.duplicate_ids`' own docstring."""
    adapter = for_framework("pytest")

    assert adapter.duplicate_ids({"testResults": []}) == {}
    assert adapter.duplicate_ids(None) == {}


def test_executed_names_and_duplicate_ids_read_ONE_rule():
    """Mirrors `test_verify_selected_and_executed_names_read_ONE_rule`:
    both `executed_names` and `duplicate_ids` delegate to `_executed` rather
    than each re-implementing the terminal-status test."""
    import inspect

    from bakeoff.runners import node_adapter

    assert "_executed" in inspect.getsource(
        node_adapter._NodeFlavour.executed_names)
    assert "_executed" in inspect.getsource(
        node_adapter._NodeFlavour.duplicate_ids)


def test_the_first_eight_space_report_guard_in_this_module_is_the_classifiers():
    """`mutation_check.py` replaces the FIRST eight-space `if report is
    None:` in this module, and it must be `classify`'s: indentation is what
    excludes `verify_selected`'s four-space guard, position is what picks
    `classify` out among the method bodies, and nothing in the language
    enforces either. A NEW method added above `classify` carrying that guard
    defeats the anchor exactly as a moved `_executed` would, and
    `mutation_check` would report CAUGHT for the wrong reason."""
    import inspect

    from bakeoff.runners import node_adapter

    lines = inspect.getsource(node_adapter).splitlines()
    guards = [index + 1 for index, line in enumerate(lines)
              if line == "        if report is None:"]
    body, start = inspect.getsourcelines(node_adapter._NodeFlavour.classify)
    assert guards, "the anchor mutation_check.py replaces no longer exists"
    assert start <= guards[0] < start + len(body), (
        f"the first eight-space `if report is None:` is at line {guards[0]}, "
        f"outside classify (lines {start}-{start + len(body) - 1})")
