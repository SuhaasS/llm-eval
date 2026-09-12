"""What "the tests failed" means, per test runner. Spec sections 3.3 and 4.2.1.

Every red/green judgement this harness makes was, until this package existed, a
branch on pytest's exit codes -- and that is the right design for pytest and
only for pytest. The whole reason `preflight.py` can tell "the bug is present"
from "the environment is broken" is that pytest answers them with 1 and with
2/4/5, which is the distinction Phase 0c's `returncode != 0` did not make and
that invalidated every capability figure taken under it.

Measured 2026-09-01 in `node:22-bookworm-slim` against vitest 3.2.7 and jest
30.5.0: BOTH frameworks exit 1 for a failing test, for a test file with an
unresolvable import, for a test file with a syntax error, for a nonexistent
file passed as an argument, and for a broken config file. There is no exit code
to branch on. Worse, a `-t` name pattern that matches NOTHING exits **0** with
every test reported skipped -- so a manifest naming a renamed test reads as a
green gate rather than as the usage error pytest would raise.

So the judgement moves here. An adapter takes what a suite invocation produced
-- exit code, stdout, stderr, and (for the frameworks that write one) a parsed
JSON report -- and returns an `Outcome` in terms no framework owns. The
consumers keep their structure, their messages and their own comparisons
against the manifest; what they stop doing is knowing which numbers mean what.

The `Outcome` deliberately does NOT compare anything against the task. Preflight
needs EQUALITY between the errored files and the declared f2p modules; the
grader needs CONTAINMENT (see the offline-grader spec, check 5). Folding both
behind a flag would make each call site unreadable about which claim it is
making, which is the reason `collection_error_modules` never made that
comparison either.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: The suite ran and everything selected passed.
KIND_PASSED = "passed"
#: The suite ran and something failed an assertion. The ONLY kind that is a
#: statement about the code under test.
KIND_FAILED = "failed"
#: A test FILE could not be loaded -- an import error, a syntax error, a
#: collection error. `errored_files` names them. Whether that is an accepted
#: task shape (broadening 2) or a broken image is the CALLER's comparison.
KIND_LOAD_ERROR = "load_error"
#: The invocation completed and executed no test. pytest's exit 5; on node,
#: zero assertions with a terminal status -- which is also what a `-t` pattern
#: matching nothing and a quarantine that swallowed the whole p2p list produce,
#: both of which exit 0 (measured).
KIND_NOTHING_RAN = "nothing_ran"
#: The suite did not run, or did not say what it did. Never a statement about
#: the model: this is the Phase 0c failure, and a bare `!= 0` reports it as one.
KIND_ENVIRONMENT = "environment"


@dataclass(frozen=True)
class Outcome:
    """One suite invocation, in terms no framework owns.

    Fields are three-valued where a missing answer and a negative answer are
    different facts, because two absences that render identically are the same
    defect one layer down.
    """

    kind: str
    exit_code: int
    #: Node ids that failed an assertion, in the framework's own id shape.
    failed_ids: frozenset[str] = frozenset()
    #: Files that could not be LOADED. `None` means the question was not asked
    #: -- this exit code is not a collection failure, or no report existed. An
    #: EMPTY frozenset means it WAS asked and nothing parsed, which is a
    #: refusal rather than an absence: preflight's message says "not parsed"
    #: for the first and "empty" for the second, and it can only do that while
    #: these stay apart.
    errored_files: frozenset[str] | None = None
    #: Rootdir-relative paths the runner LOADED or executed -- every
    #: `testResults` entry, including a file that failed to load and therefore
    #: ran nothing. "Executed" alone would be the wrong word and the wrong set:
    #: what D7c's scope check asks is whether the run touched a file outside
    #: `tests.paths`, and a file the filter pulled in and then failed to parse
    #: is exactly such a file. `None` when the framework does not report them
    #: (pytest). Node fills it because its positional arguments are SUBSTRING
    #: FILTERS over the absolute path rather than paths -- measured, `vitest
    #: run tests/` matched `/repo/jtests/...`.
    files_run: tuple[str, ...] | None = None
    #: Requested ids that produced no terminal status. Node only; pytest
    #: answers this with exit 4 and an `ERROR: not found:` line. Empty for
    #: pytest, and empty is honest there -- the exit code already carried it.
    not_run: frozenset[str] = frozenset()
    #: A human phrase for `kind`/`exit_code`, for a problem message. Never
    #: parsed.
    explain: str = ""


@dataclass(frozen=True)
class PropertyScan:
    """How to recognise a property-based suite, and a pinned seed in it.

    Consumed by preflight's node determinism check (round-2 item 12). A match
    on `import_pattern` proves a scanned file imports a property-based
    framework; a match on `pin_pattern` proves a scanned file calls
    `configureGlobal` with a `seed`. Neither proves the pin EXECUTES, runs
    before every property, or is not overridden by a `describe`-local
    `fc.assert` -- unlike pytest's `image.env: {CI: "1"}` lever, which
    preflight reads back OUT of the container, a `configureGlobal` match is
    only a grep over source. It is a signal the repository has thought about
    the problem, and no more than that.
    """

    import_pattern: str
    pin_pattern: str
    frameworks: tuple[str, ...]


@runtime_checkable
class RunnerAdapter(Protocol):
    """The seam. One implementation per `tests.framework`.

    Everything here is a pure function of its arguments except that nothing
    here touches a container: the adapter builds argv and reads output, and the
    caller runs the command. That is what keeps `classify` testable against a
    captured report with no Docker daemon -- which matters, because the nine
    captured node report shapes were measured once and can be replayed
    forever, while re-measuring them needs a network and 73 MB of npm. (Eight
    of the nine are `classify` branches; the ninth is the two-tests-one-name
    shape `duplicate_ids` reads, which `classify` sees as an ordinary
    failure.)
    """

    #: The manifest's `tests.framework` value.
    name: str
    #: A substring that must appear somewhere in `tests.runner`. Preflight
    #: refuses a manifest whose declared framework and declared argv disagree
    #: -- each catches the other's typo, which is why neither is derived from
    #: the other.
    runner_marker: str
    #: Flags the runner needs so the suite writes nothing into the tree.
    #: Section 5.6 stages everything, so anything a test run drops lands in
    #: every submission diff and diff size measures the runner rather than the
    #: agent.
    no_cache_args: tuple[str, ...]

    def select_argvs(self, node_ids: tuple[str, ...]) -> list[list[str]]:
        """Argvs that run exactly these ids and nothing else.

        One per FILE, not one per call: measured 2026-09-02, two positionals
        plus one union `-t` executed a test declared for neither pairing --
        the positionals and the name pattern are ANDed across the whole
        invocation, never zipped. Empty ids give an EMPTY LIST, not an argv
        with no filter; `_Runner.run` raises on it rather than falling
        through to running the whole suite as the selection.
        """

    def p2p_argvs(self, *, selected: tuple[str, ...], scope: tuple[str, ...],
                  deselected: tuple[str, ...],
                  ignored: tuple[str, ...]) -> list[list[str]]:
        """The WHOLE p2p argv sequence, in one call, because it cannot be composed.

        vitest and jest express both selection and deselection through a
        single `-t <regex>`, and emitting two is NOT "last one wins" -- the two
        frameworks disagree and neither answer is usable. Measured 2026-09-02:
        vitest REJECTS it (`Expected a single value for option "-t,
        --testNamePattern <pattern>", received ["a", "b"]`, exit 1, and no
        report file, so it classifies as KIND_ENVIRONMENT), while jest
        COMMA-JOINS them into `adds,subs` -- a regex matching neither test --
        and exits **0** having run nothing. One method that owns the whole
        argv cannot be composed wrongly.

        What `-t` cannot do is name a file, so the FILE half is carried by
        the grouping instead: one argv per file, each pairing that file's
        positional with a pattern over only that file's titles. The deselect
        branch is 1 + K commands -- group 0 is the declared scope with every
        file holding a deselection excluded and no `-t` at all, then one
        group per such file carrying only its own deselected titles. That is
        what keeps a quarantine of `a::works` off `b::works`.

        The EXCLUSION channel differs per framework and it is not cosmetic:
        jest's `--testPathIgnorePatterns` REPLACES the repository's own value
        while vitest's `--exclude` adds to it, so jest excludes through
        negative lookaheads folded into the positional and emits that flag
        nowhere. See the round-2 item 1 plan's D3b.

        `selected` is the manifest's explicit `tests.p2p`; when it is empty the
        run is the deselect branch and `scope` is the declared `tests.paths`.
        `deselected` is (f2p + quarantine) on the deselect branch and the
        quarantine alone on the explicit branch. `ignored` has exactly one
        caller -- preflight's p2p run at the START state on a task whose f2p
        module does not import there.
        """

    def merge_reports(self, reports: list[dict | None]) -> dict | None:
        """One report per argv group, folded into the one a check reads.

        `None` for an EMPTY list and for any list holding a `None`: nobody
        ran and nobody counted, versus a group that produced no evidence at
        all. Never a partial merge -- that would report a green suite for a
        run half of which said nothing.
        """

    def file_filter_matches(self, declared_path: str,
                            candidate_path: str) -> bool:
        """Would this framework's filter for `declared_path` also pull in
        `candidate_path`?

        Both arguments are rootdir-relative. The channel for preflight's
        `ambiguous_file_filters` refusal: on vitest the positional is a
        SUBSTRING filter that no anchoring reaches, so a per-file group
        carries its name pattern into a second file. Every other framework
        answers plain equality and the refusal can never fire.
        """

    def report_args(self, report_path: str) -> list[str]:
        """Argv that makes the runner write a machine-readable report there."""

    def report_path(self) -> str | None:
        """Where that report goes, or `None` for a framework that writes none.

        FIXED, never per-invocation: it is an argv element, and the gated argv
        must be byte-identical to the graded one. Staleness is handled by
        deleting the file before each run rather than by varying the name --
        measured, a config error writes NO file, so a leftover report from the
        previous invocation would stand in as this run's evidence.
        """

    def classify(self, *, exit_code: int, stdout: str, stderr: str,
                 report: dict | None) -> Outcome:
        """What that invocation did. Never a comparison against the manifest."""

    def parse_deselected(self, *, stdout: str,
                         report: dict | None) -> int | None:
        """How many items the runner did not run. `None` = nobody counted."""

    def executed_names(self, report: dict | None
                       ) -> Iterable[tuple[str, str]]:
        """Every test that reached a verdict, as `(relpath, name)` pairs.

        The channel for preflight's duplicate-`fullName` assertion, which is
        the half of D2's rule a LOADER cannot make: the collision that breaks
        a quarantine is between a declared id and a test the manifest never
        mentions, and only the real report of the scoped run holds both.

        EMPTY for pytest, and that is a claim rather than a gap -- a pytest
        node id carries the file, so `--deselect a.py::test_x` cannot reach
        `b.py::test_x` and the hazard does not exist there. preflight writes
        the evidence key either way, as `[]`, which is "measured, nothing
        found" and not "not measured".
        """

    def duplicate_ids(self, report: dict | None) -> dict[str, int]:
        """`<file>::<fullName>` -> how many tests in that file answer to it.

        Only ids that MORE THAN ONE assertion in a single `testResults` entry
        reached a verdict for; an id that names exactly one test is absent.
        `{}` for a framework whose ids are unique by construction, and `{}`
        for a report that does not exist -- preflight records THAT absence
        with its own flag, because a report-less run measured nothing and an
        empty map here would say it looked and found nothing.
        """

    def module_of(self, node_id: str) -> str:
        """The FILE half of a node id."""

    def validate_node_id(self, node_id: str, paths: tuple[str, ...],
                         where: str) -> None:
        """Raise `TaskError` if this id cannot be selected by this framework."""

    def validate_id_set(self, node_ids: tuple[str, ...], where: str) -> None:
        """Raise `TaskError` if the declared ids are unselectable TOGETHER.

        Separate from `validate_node_id` because the defect is a property of
        the SET: no id in it is wrong on its own. Empty on every adapter
        today; kept as a seam because a set-level refusal is not expressible
        anywhere else in the loader.
        """

    def hypothesis_interpreter(self, runner: tuple[str, ...]) -> str | None:
        """The interpreter to probe for hypothesis, or `None` for no probe.

        A Python-ecosystem determinism check (broadening 3). `None` from a
        non-Python adapter makes preflight skip it and leave both evidence keys
        `None` -- a recorded absence, never a claim that the suite is
        deterministic.
        """

    def property_scan(self) -> PropertyScan | None:
        """How to recognise a property-based suite in this ecosystem.

        Consumed by preflight over the p2p-BEFORE run's `Outcome.files_run`
        -- what the certified sweep actually loaded -- rather than over
        `tests.paths`, which with an empty `tests.p2p` is a small subset of it.

        `None` from an adapter whose determinism question is answered
        elsewhere -- pytest's is `hypothesis_interpreter` above, whose remedy
        (`image.env: {CI: "1"}`) is a lever this one has no analogue for. It
        would also have no scope: `files_run` is `None` for pytest.
        Measured 2026-09-02 against fast-check 3.23.2 and 2.25.0: the package
        reads no environment variable at all and `readSeed` falls back to
        `Date.now() ^ (Math.random() * 0x100000000)`, so there is nothing to
        declare and the node check is a refusal rather than a lever.
        """

    def explain(self, code: int) -> str:
        """A phrase naming what this exit code means to this framework."""


def for_framework(name: str) -> RunnerAdapter:
    """The adapter for a validated `tests.framework`.

    Raises `KeyError` on an unknown name rather than defaulting, and the
    manifest loader's closed allowlist is what keeps that unreachable: a
    default here would silently classify a jest run with pytest's exit codes.
    """
    return _REGISTRY[name]


def _build_registry() -> dict[str, RunnerAdapter]:
    # Imported inside the function so `bakeoff.runners` can be imported by the
    # adapters themselves for `Outcome` and the KIND_* names without a cycle.
    from bakeoff.runners.node_adapter import JEST, VITEST
    from bakeoff.runners.pytest_adapter import ADAPTER as PYTEST

    return {PYTEST.name: PYTEST, VITEST.name: VITEST, JEST.name: JEST}


#: In manifest-documentation order: the default first.
FRAMEWORKS: tuple[str, ...] = ("pytest", "vitest", "jest")

#: Built last, so a NameError here is a missing adapter rather than a module
#: that half-imported. `tasks._FRAMEWORKS` must equal `FRAMEWORKS`, and the
#: equality is pinned from BOTH files -- `test_runners.py`'s
#: `test_the_manifest_allowlist_and_this_registry_cannot_disagree` and
#: `test_tasks.py`'s counterpart -- because a reader of either constant has to
#: be told it is half of a pair. `for_framework` raises KeyError rather than
#: defaulting, so that allowlist is the only thing keeping the raise
#: unreachable: an entry in one and not the other is a KeyError out of the
#: middle of preflight, after the container is up, with no manifest path in the
#: message.
_REGISTRY = _build_registry()
