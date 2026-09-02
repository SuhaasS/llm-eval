"""pytest, behind the runner-adapter seam. Spec sections 3.3 and 4.2.1.

Nothing in this module is new. Every constant, regex and parser below was
`preflight.py`'s or `grader.py`'s until broadening 7 needed a SECOND runner,
and each is moved verbatim -- docstrings included, because they carry the
measurements (`Measured 2026-09-01 against pytest 9.1.1 and 8.3.5`, the
six-row collection table, the `-q` summary-line samples) and re-deriving those
needs a container and a corpus.

They are re-exported from `bakeoff.preflight` and `bakeoff.grader` under their
exact bare names, so no caller, no test and no `scripts/mutation_check.py`
anchor changes. That re-export is not stylistic: a namespaced re-spelling would
rot six mutation anchors silently, and `mutation_check` reports a missing
anchor as STALE ANCHOR only on a run somebody makes.

What IS new is the `PytestAdapter` wrapper at the bottom, which gives each of
those decisions a signature the node adapter can also satisfy.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from bakeoff.runners import (
    KIND_ENVIRONMENT,
    KIND_FAILED,
    KIND_LOAD_ERROR,
    KIND_NOTHING_RAN,
    KIND_PASSED,
    Outcome,
)

# pytest's exit codes, which are the whole reason this module can tell "the
# bug is present" from "the environment is broken". Both are non-zero, and an
# `assert returncode != 0` passes on the second -- which is precisely the
# Phase 0c failure. See https://docs.pytest.org/en/stable/reference/exit-codes
EXIT_ALL_PASSED = 0
EXIT_TESTS_FAILED = 1
#: Collection was interrupted. Reached by a run that collects a DIRECTORY or a
#: MODULE PATH -- not by the f2p selection, which is positional node ids. See
#: EXIT_USAGE_ERROR.
EXIT_COLLECTION_INTERRUPTED = 2
#: Two causes, and telling them apart is the whole of broadening 2. Measured
#: 2026-09-01 against pytest 9.1.1 and 8.3.5: selecting `mod.py::test` when
#: `mod.py` raises on import exits 4 with an `ERROR mod.py` summary line, and
#: selecting a node id that does not exist in a module that imports fine ALSO
#: exits 4 -- with `ERROR: not found:` (colon), which `_FAILED_LINE` does not
#: match, so the reported set is empty. The first is a task shape to accept;
#: the second is a manifest typo that must keep stopping the matrix.
EXIT_USAGE_ERROR = 4
#: The scoped run's failure mode, and the whole of its detection. No "collected
#: 0 items" summary matching: the pinned runners carry `-q`, which suppresses
#: that line (measured), so a guard on the string could not fire under the
#: configuration actually used -- the dead-guard shape a mutation cannot catch.
#: `-q` does NOT suppress "Interrupted: N error during collection" (also
#: measured); nothing parses that line and nothing should.
EXIT_NOTHING_COLLECTED = 5
#: The two codes a collection error can arrive as. 4 first, because that is the
#: one preflight's own f2p run produces.
EXIT_COLLECTION_FAILURES = (EXIT_USAGE_ERROR, EXIT_COLLECTION_INTERRUPTED)
_EXIT_MEANING = {
    2: "collection was interrupted (an import error in a test module, most "
       "often a dependency the image does not ship)",
    3: "pytest hit an internal error",
    4: "usage error -- a selected node id does not exist, OR the module it "
       "names could not be imported",
    5: "no tests were collected",
    124: "the command hit the suite timeout (budget.suite_timeout_s)",
}

#: `_EXIT_MEANING` plus the codes the `timeout` wrapper and the shell
#: contribute on top of pytest's own process -- 127 in particular is "the
#: interpreter this probe resolved is not on PATH", the exact "the agent
#: cannot run the command it will naturally type" failure both `preflight.py`
#: and `oracle.py` need to name a cause for. Lives here, not in either of
#: those two, because `oracle.py` already imports from `preflight.py`
#: (`_EXIT_MEANING`, `_Runner`, `_existing_prefixes`) -- defining this dict in
#: either one and importing it back into the other would be a cycle. Both
#: modules already import from `bakeoff.runners.pytest_adapter` directly, so
#: this is the one place both can reach without one.
_PROCESS_EXIT_MEANING = {
    **_EXIT_MEANING,
    125: "the `timeout` wrapper itself failed",
    126: "the runner was found but could not be executed",
    127: "the runner is not on PATH in this image",
    137: "the command was killed (SIGKILL -- usually the container OOM)",
}

#: `SUBFAILED` joins the alternation for the same reason `ERROR` is already
#: in it: pytest 9's core-integrated subtests (subtests were folded into core
#: in 9.0) print one `SUBFAILED<label> <node id> - <msg>` line per FAILING
#: subtest instead of a `FAILED <node id>` line for the node, so a node whose
#: only failures are subtest failures used to vanish from `failed_node_ids`
#: entirely -- reported set empty, exit code 1, and preflight's own
#: contradiction message ("declared f2p tests did not fail at the start
#: state") on a run that plainly failed. Measured 2026-09-02 against the
#: pinned eval image's pytest 9.1.1: the label is `(i=0)` for a keyword
#: `subTest(i=i)` and `[0]` for a positional `subTest(i)`, with NO space
#: between `SUBFAILED` and the label and no colon before the node id (unlike
#: `ERROR: not found:`, which the alternation must keep excluding) --
#: `[^)\n]*` / `[^\]\n]*` bound the label to one line so a stray unmatched
#: bracket in a subtest's own `-` message can never swallow the node id past
#: it. The accepted cost is the opposite direction: a label whose OWN value
#: contains a balanced `)` or `]` (`SUBFAILED(msg='a)b') tests/x.py::T::t -
#: ...`) stops the class one character early, so neither alternative matches
#: and the line's node id is DROPPED rather than mis-captured -- degrading to
#: the pre-fix (v7) behaviour for that one line. Judged rarer than the id
#: being swallowed, and unaddressed.
#:
#: There is no `SUBERROR`: the same measurement raised a bare `ValueError`
#: (not an assertion) inside a `subTest` block and pytest still labelled the
#: line `SUBFAILED`, so no second alternative was added for a shape that does
#: not occur. A node all of whose subtests pass prints no FAILURES entry and
#: no summary line at all (measured) -- absent from `failed_node_ids`, as a
#: passing node must be.
_FAILED_LINE = re.compile(
    r"^(?:FAILED|ERROR|SUBFAILED(?:\([^)\n]*\)|\[[^\]\n]*\])?)\s+(\S+)",
    re.MULTILINE,
)

#: The `-q` summary line: the FINAL non-empty line of pytest's stdout, ending
#: in a duration. Measured against the eval image's pytest 9.1.1 on
#: 2026-08-17 (the image pins `PYTEST_VERSION=9.1.1`; an earlier draft of this
#: module said 8.3.5, which is not what is installed):
#:
#:     4 passed in 0.00s
#:     1 passed, 3 deselected in 0.00s
#:     4 deselected in 0.00s                      (and exit 5)
#:     no tests ran in 0.00s                      (and exit 5)
#:     4 passed, 1 deselected, 1 warning in 0.00s
#:     1 failed, 4 passed in 0.01s
#:
#: The optional trailing `(H:MM:SS)` is what pytest appends past 60 seconds
#: (`_pytest.terminal.format_session_duration`), and without it every p2p run
#: over a minute would read as "no summary line" -- which is `None`, which is
#: "not measured", on the majority of real suites.
#:
#: A wrong discriminator inverts the 0-vs-None distinction in one direction or
#: the other, which is why it is pinned here rather than inferred: too loose
#: and a stray tail line reads as a summary with no `deselected` token, so a
#: total quarantine loss renders as a measured zero; too tight and a measured
#: zero renders as "nobody counted".
_SUMMARY_LINE = re.compile(r"\bin \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?$")
_DESELECTED = re.compile(r"(\d+) deselected")


#: Basenames that mean "this argv element is a Python interpreter".
#: `python`, `python3`, and `pythonX.Y` -- the three shapes a `tests.runner`
#: actually carries. Matched on the BASENAME so an absolute
#: `/usr/local/bin/python3.12` counts, and anchored so `pythonish-wrapper`
#: does not.
_PYTHON_BASENAME = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")


def _runner_python(runner: tuple[str, ...]) -> str:
    """The interpreter this task's suite runs under, or "python".

    The availability probe has to ask the interpreter the RUNNER uses, not
    whichever `python` is first on PATH: a task whose runner is
    `["/opt/venv/bin/python", "-m", "pytest", ...]` resolves imports against
    that venv's site-packages, and probing the system interpreter would answer
    a question about a different environment. `click-3360`'s runner is
    `["python", "-m", "pytest", ...]`, so the common case is unchanged.

    Falls back to "python" when `runner[0]` is not an interpreter at all --
    `["pytest", "-q", ...]` is a legal runner, and the console script gives no
    interpreter path to reuse.

    Two callers, two different costs for the same guess. The availability
    probe's fallback is allowed to be a guess: it decides whether to ASK for
    `CI`, and preflight's runner check has already established that `pytest`
    is in the argv, so a wrong guess there costs one skipped question. The
    bare-runner probe (`preflight.py`, broadening 7) is the SECOND caller,
    and its fallback consequence is a NO-GO, not a question -- on a
    `["pytest", "-q", ...]` runner the fallback's `python -m pytest` can
    resolve a different interpreter than the gated argv would have run under,
    and a usage error from THAT mismatch is filed against the repository's
    own configuration exactly as a real one would be.
    """
    if runner and _PYTHON_BASENAME.match(PurePosixPath(runner[0]).name):
        return runner[0]
    return "python"


def failed_node_ids(output: str) -> set[str]:
    """Node ids pytest reported as FAILED or ERROR, from `-q` output.

    Parsed from the summary lines rather than from a JUnit report. The report
    would need `classname` mapped back to a node id, and that mapping is
    ambiguous -- a dotted segment is a package or a class and the XML does
    not say which -- so the parser becomes a second thing that can be wrong
    about what happened, sitting inside the check that exists to be right
    about it.
    """
    return {match.group(1) for match in _FAILED_LINE.finditer(output)}


def collection_error_modules(output: str) -> frozenset[str] | None:
    """The test modules pytest could not COLLECT -- or `None` for anything else.

    Section 3.3's loop ends in "runs tests, sees failures, self-corrects", and
    a PR that ADDS a symbol hands the agent an `ImportError` instead of an
    assertion. That is a real task shape, and it is one preflight refused
    outright until broadening 2, because both of its exit codes (4 for a
    selection, 2 for a directory sweep) are also what a broken image gives.

    The discriminator is `::`, and it is the whole parser. Measured 2026-09-01
    against pytest 9.1.1 and 8.3.5, under the pinned `-q -p no:cacheprovider`:

    * a module that raised on import          -> `ERROR tests/new.py`
    * a test that failed                      -> `FAILED tests/new.py::test_x`
    * a test whose fixture raised             -> `ERROR tests/new.py::test_x`
    * a node id that does not exist           -> `ERROR: not found: ...`
    * a path that does not exist              -> `ERROR: file or directory ...`
    * a broken `tests/conftest.py`            -> no `short test summary info`
      section at all -- exit 4 with the reported set EMPTY, so the run is
      UNCONFINED (measured 2026-09-01, pytest 9.1.1 and 8.3.5)

    The last three carry no `ERROR <path>` line matching `_FAILED_LINE` --
    the first two are prefixed with a colon `_FAILED_LINE` does not match, the
    third prints no summary section for the parser to find at all -- so they
    arrive here as an empty set, which is refused: a manifest naming a renamed
    test, or a task whose conftest cannot even import, must keep stopping the
    matrix rather than being read as "the module could not be collected".

    `None` rather than an empty frozenset for the refusal: "no collection
    errors" and "collection errors mixed with test results" are different
    facts, and a caller comparing an empty set against the declared modules
    would silently accept the second on a task that declares no f2p ids.

    The comparison against `f2p_modules` is deliberately NOT made here. The two
    callers need different ones -- preflight equality, the grader containment
    (see the offline-grader spec, check 5) -- and folding both behind a flag
    would make each call site unreadable about which claim it is making.
    """
    reported = failed_node_ids(output)
    if not reported or any("::" in item for item in reported):
        return None
    return frozenset(reported)


def f2p_modules(f2p: tuple[str, ...]) -> frozenset[str]:
    """The module half of each declared f2p node id.

    Split once, from the LEFT: a parametrized id can carry `::` inside its
    brackets (`tests/a.py::test_one[x::y]`) and a class-scoped id carries two,
    so `rsplit` or an unbounded `split` would name something that is not a
    module and the equality in `preflight` would never hold.

    That split lives in `PytestAdapter.module_of` and is called from here
    rather than repeated: broadening 7 gave every framework a "file half of a
    node id" method, and a second copy of the rule is a second thing that can
    be wrong about which file an id names, sitting inside the equality
    preflight's confinement check rests on.
    """
    return frozenset(ADAPTER.module_of(node_id) for node_id in f2p)


def parse_deselected(stdout: str) -> int | None:
    """How many items pytest said it deselected. `None` means no summary line.

    TWO ABSENCES, KEPT APART. A summary line with no `deselected` token is
    `0`, an OBSERVATION -- pytest prints no token at zero (measured), and on
    the explicit-p2p branch a wholly stale quarantine produces exactly that,
    which is the total loss most worth seeing. `None` is reserved for "no
    summary line was found", which is the grader not having counted.

    Read off stdout alone. `RunContainer.exec` demuxes, pytest writes its
    summary to stdout, and a stderr tail concatenated on the end would
    displace the final-line discriminator.
    """
    for line in reversed(stdout.split("\n")):
        line = line.strip()
        if not line:
            continue
        if not _SUMMARY_LINE.search(line):
            return None
        found = _DESELECTED.search(line)
        return int(found.group(1)) if found else 0
    return None


class PytestAdapter:
    """pytest, whose exit codes are the reason this seam is shaped as it is.

    Every method here is today's inline logic with a signature around it. The
    argv methods reproduce `_Runner.pass_to_pass`'s emission ORDER exactly,
    which is what makes `test_grading_p2p_with_no_extras_is_the_argv_preflight
    _validated` -- untouched -- the gate on this refactor.
    """

    name = "pytest"
    runner_marker = "pytest"
    #: Advisory, not asserted. Without it pytest writes `.pytest_cache/` into
    #: the tree, and preflight's existing dirty-tree check fires loudly on it
    #: -- so nothing more is needed here, and adding an assertion could refuse
    #: a manifest that loads today. The node adapters ARE asserted, because
    #: vitest's artifact is `node_modules/`, which every JavaScript repo's
    #: .gitignore already hides from that check.
    no_cache_args = ("-p", "no:cacheprovider")

    def select_argvs(self, node_ids):
        # EXACTLY ONE group, and its element is byte-identical to the argv
        # this adapter emitted before the node grouping landed. The node
        # adapters need one invocation per file because `-t` matches by name
        # alone; a pytest node id carries its own file, so a second group
        # here would be a second suite run for nothing.
        return [list(node_ids)] if node_ids else []

    def p2p_argvs(self, *, selected, scope, deselected, ignored):
        out = list(selected) if selected else list(scope)
        for node_id in deselected:
            out += ["--deselect", node_id]
        out += [f"--ignore={path}" for path in ignored]
        return [out]

    def merge_reports(self, reports):
        # `None`, and it is a CLAIM rather than a gap: pytest writes no
        # machine-readable report at all (`report_path()` is `None`), so
        # there is nothing to fold and never will be. `_Runner` reads this
        # only when `report_path()` is set, so this branch answers direct
        # callers and the protocol's own test.
        return None

    def file_filter_matches(self, declared_path, candidate_path):
        # pytest's positional IS a path, so two distinct paths never collide
        # and this can never fire. It exists so the ambiguity check reads one
        # rule for every framework.
        return declared_path == candidate_path

    def report_args(self, report_path):
        # pytest's evidence is its exit code and its `-q` summary lines. A
        # JUnit report would need `classname` mapped back to a node id, and
        # that mapping is ambiguous -- a dotted segment is a package or a class
        # and the XML does not say which -- so the parser becomes a second
        # thing that can be wrong about what happened, inside the check that
        # exists to be right about it.
        return []

    def report_path(self):
        return None

    def classify(self, *, exit_code, stdout, stderr, report):
        output = stdout + stderr
        if exit_code == EXIT_ALL_PASSED:
            return Outcome(kind=KIND_PASSED, exit_code=exit_code,
                           explain="all selected tests passed")
        if exit_code == EXIT_TESTS_FAILED:
            return Outcome(kind=KIND_FAILED, exit_code=exit_code,
                           failed_ids=frozenset(failed_node_ids(output)),
                           explain=self.explain(exit_code))
        if exit_code == EXIT_NOTHING_COLLECTED:
            return Outcome(kind=KIND_NOTHING_RAN, exit_code=exit_code,
                           explain=self.explain(exit_code))
        if exit_code in EXIT_COLLECTION_FAILURES:
            modules = collection_error_modules(output)
            # `None` from the parser means "no bare-module ERROR line" -- a
            # renamed node id (`ERROR: not found:`, a colon `_FAILED_LINE`
            # does not match) or a conftest that could not import (no summary
            # section at all). Both must keep stopping the matrix, so they
            # become an ENVIRONMENT outcome carrying an EMPTY errored_files:
            # the parse ran and reported nothing, which is not the same fact
            # as never having run.
            if modules:
                return Outcome(kind=KIND_LOAD_ERROR, exit_code=exit_code,
                               errored_files=modules,
                               explain=self.explain(exit_code))
            return Outcome(kind=KIND_ENVIRONMENT, exit_code=exit_code,
                           errored_files=frozenset(),
                           explain=self.explain(exit_code))
        return Outcome(kind=KIND_ENVIRONMENT, exit_code=exit_code,
                       errored_files=None, explain=self.explain(exit_code))

    def parse_deselected(self, *, stdout, report):
        return parse_deselected(stdout)

    def executed_names(self, report):
        # Nothing, and it is a claim rather than an omission. A pytest node id
        # carries the FILE, so `--deselect a.py::test_x` cannot reach
        # `b.py::test_x` and the duplicate-`fullName` hazard the node adapters
        # have does not exist here. preflight writes its evidence key from
        # this anyway -- as `[]`, "measured, nothing found", which is a
        # different fact from the `None` an explicit-p2p task leaves.
        return ()

    def module_of(self, node_id):
        return node_id.split("::", 1)[0]

    def validate_node_id(self, node_id, paths, where):
        # Deliberately empty. pytest ids are validated today only for
        # non-emptiness, duplication and f2p/p2p overlap, and adding a shape
        # rule now could refuse a manifest that loads -- there is exactly one,
        # and backwards compatibility is a constraint of this broadening.
        return None

    def validate_id_set(self, node_ids, where):
        # Also deliberately empty, and for a second reason on top of the one
        # above: pytest selects by the WHOLE node id, path included, so
        # `a.py::test_x` and `b.py::test_x` are unambiguous to `--deselect`
        # and to a positional alike. The node adapters' duplicate-`fullName`
        # refusal here would refuse a manifest that loads today, over a hazard
        # pytest does not have.
        return None

    def hypothesis_interpreter(self, runner):
        return _runner_python(tuple(runner))

    def explain(self, code):
        return _EXIT_MEANING.get(code, f"exit code {code}")


ADAPTER = PytestAdapter()
