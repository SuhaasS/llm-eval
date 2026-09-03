"""vitest and jest, whose exit codes say nothing. Spec sections 3.3 and 4.2.1.

Measured 2026-09-02 in `node:22-bookworm-slim` (node v22.23.2), vitest 3.2.7
and jest 30.5.0:

    all tests pass ............................. 0
    one test fails ............................. 1
    a file with an unresolvable import ......... 1
    a file with a syntax error ................. 1
    a nonexistent file given as an argument .... 1
    a broken config file ....................... 1
    `-t` matching no test ...................... 0

The gate's whole job -- telling "the bug is present" from "the environment is
broken" -- has no exit code to read here. So this adapter reads a JSON report
instead, written to a file outside `/repo`, and the exit code is recorded
rather than consulted.

The last row is the one with no pytest analogue and the most dangerous shape.
pytest answers a selection that matches nothing with exit 4 and `ERROR: not
found:`; both node frameworks answer it with **0** and a summary that reads
like success (`Tests 3 skipped (3)` / `Tests: 3 skipped, 3 total`). A manifest
naming a renamed f2p test, and an oracle quarantine that swallowed the entire
p2p list, both arrive that way. `classify` calls it KIND_NOTHING_RAN whenever
no assertion reached a terminal status, and `verify_selected` names the
requested ids that did not run.

ONE MODULE FOR BOTH FRAMEWORKS, because one classifier covers both. The
load-error discriminator is a `testResults` entry with `status == "failed"` and
an EMPTY `assertionResults` -- true on both, on an import error and on a syntax
error alike. jest's `numRuntimeErrorTestSuites` says the same thing and vitest
has no such key, so it is deliberately not consulted; a second, jest-only path
would be a second thing that can be wrong about what happened. `testExecError`
is documented by jest and did NOT appear in any measured report, so nothing
here depends on it either.

THE FILE HALF OF A NODE ID IS CARRIED BY THE GROUPING, because it cannot be
carried by the pattern. `-t` matches `fullName` and no flag scopes a name
pattern to a file: measured 2026-09-02, two positionals plus one union `-t`
over one title from each file executed a THIRD test -- the positionals and the
name pattern are ANDed across the whole invocation, never zipped, on jest and
vitest alike. So a selection is one argv PER FILE, each pairing that file's
positional with a pattern over only that file's titles, and a deselection is
1 + K argvs: group 0 is the scope with every file holding a deselection
excluded and no `-t` at all, then one group per such file carrying only its own
deselected titles. Measured, one positional plus one `-t` pairs exactly.

TWO TESTS WITH THE SAME NAME IN ONE FILE ARE ONE ID, and that is unfixable
here rather than merely unguarded. Item 1 gave each file its own argv, which
closes the *cross*-file collision; a same-file pair has nothing left to
separate it. Measured 2026-09-02 (vitest 3.2.7, jest 30.5.0) on a file
holding two `it('adds')` under one `describe('outer')`: the report carries
two `assertionResults` with the identical `fullName`, `title` and
`ancestorTitles`, and on the runs this harness makes neither framework
reports a `location` beside them -- vitest emits the key only when a
`file:line` positional turns task-location capture on, and jest's is `null`
without `--testLocationInResults`. `-t '^(?:outer adds)$'` runs BOTH -- one
passed and one failed in the same run -- and the deselection skips both with
`numPendingTests: 2` for one requested id. `classify` then reports ONE
`failed_id` for the pair and `verify_selected` reports nothing missing, so
every channel reads clean. `duplicate_ids` is what says so instead, and
preflight refuses a task whose DECLARED ids are in it.

TWO FRAMEWORK ASYMMETRIES DECIDE THE ARGV, and both were measured rather than
assumed. First, the per-file positional: jest's is a JavaScript `RegExp` tested
against BOTH the repo-relative path and the absolute one, so a mount-anchored,
escaped, `$`-terminated pattern names exactly one file -- while vitest's is a
substring filter no anchoring reaches (`/repo/tests/doc/a.test.js` still
matched `/repo/pkg/tests/doc/a.test.js`). Preflight refuses the vitest trees
that cannot be separated; `file_filter_matches` is that predicate. Second, the
exclusion channel: jest's `--testPathIgnorePatterns` REPLACES the repository's
own value while vitest's `--exclude` adds to it, so jest excludes through two
negative lookaheads folded into the positional -- one per path spelling -- and
this module emits that flag nowhere.

What a LOADER can still not see is a collision between a declared id and a test
the manifest never mentions; preflight records those as `duplicate_full_names`
against the real report, as evidence rather than as a refusal, since the
per-file grouping is what made the hazard they named unreachable.

The eight report shapes this module branches on are committed under
`tests/fixtures/node_reports/` with the argv each came from; see the README
beside them. They are replayed rather than re-measured because regenerating
one needs a Docker daemon, a network and 73 MB of npm.
"""

from __future__ import annotations

from dataclasses import dataclass

from bakeoff.runners import (
    KIND_ENVIRONMENT,
    KIND_FAILED,
    KIND_LOAD_ERROR,
    KIND_NOTHING_RAN,
    KIND_PASSED,
    Outcome,
    PropertyScan,
)

#: Where `RunContainer` binds the run tree, and therefore the prefix the
#: reporters' absolute `name` fields carry. Spelled here rather than imported
#: from `container.REPO_MOUNT` for the reason `tasks._REPO_MOUNT` gives:
#: container.py imports `docker` at module level, and this module is reached
#: from the loader, which deliberately runs without a daemon.
#: `test_the_node_repo_mount_constant_does_not_drift` is where that is caught.
_REPO_MOUNT = "/repo"

#: The report path, FIXED. It is an argv element (`--outputFile=...`), and the
#: gated argv must be byte-identical to the graded one -- a uuid or a counter
#: in it breaks that property for every node task, silently, in the one place
#: this codebase has a test for. Staleness is handled by deleting the file
#: before each invocation instead: measured, a config error writes NO file, so
#: a leftover report from the previous run would stand in as this run's
#: evidence -- a passing report for a run that never happened. `/tmp` is inside
#: the container and never bind-mounted, so it cannot reach a submission diff
#: (section 5.6 stages everything).
REPORT_PATH = "/tmp/bakeoff-run-report.json"

#: The FOURTEEN characters a JavaScript `RegExp` treats as syntax:
#: `. * + ? ^ $ { } ( ) | [ ] \`. Everything else is a literal and must NOT be
#: escaped. (The plan and its brief both call this list "twelve" while
#: enumerating fourteen; the enumeration is what is right, and the count is
#: corrected here rather than copied, because a reader checking the set against
#: a stated size is the reader this comment exists for.)
_JS_SYNTAX = frozenset(".*+?^${}()|[]\\")

#: Assertion statuses that mean the test actually reached a verdict. Anything
#: else -- vitest's `skipped`, jest's `pending`, jest's `todo` -- is a test
#: that did not run, which is exactly what a `-t` matching nothing produces on
#: every test in the file, at exit 0.
_TERMINAL_STATUSES = ("passed", "failed")

_EXIT_MEANING = {
    0: "exit code 0, which does NOT mean the suite passed -- a `-t` pattern "
       "matching no test exits 0 with every test reported skipped (measured)",
    124: "the command hit the suite timeout (budget.suite_timeout_s)",
}


def _js_escape(text: str) -> str:
    """Escape for a JavaScript RegExp, which is not Python's escape set.

    `re.escape` escapes characters JS reads as identity escapes, and an
    identity escape of a non-syntax character is a SyntaxError under the `u`
    flag. A space is the one that would bite first: Python escaped it through
    3.6, and a `\\ ` reaching node is a pattern that means something else.

    Measured 2026-09-02: `-t '^(?:handles a\\+b \\(x\\) \\[y\\])$'` selects
    exactly the test named `handles a+b (x) [y]` and skips `... [y] EXTRA`.
    Unescaped, the `+` and the groups change what the pattern means -- and an
    over-broad deselection pattern removes tests from the regression check
    silently, since a skipped test and a test that was never selected are the
    same shape in the report.
    """
    return "".join("\\" + ch if ch in _JS_SYNTAX else ch for ch in text)


def verify_selected(report: dict | None, requested: tuple[str, ...],
                    adapter: "_NodeFlavour") -> frozenset[str]:
    """The requested ids for which the report holds no terminal verdict.

    Separate from `classify` because only the caller knows what it asked for:
    in the report, a test that was skipped and a test that was never selected
    are the same shape. This is what fills `Outcome.not_run`, and it is the
    node answer to pytest's exit 4 -- measured, a `-t` pattern naming a test
    that no longer exists exits **0** with every test reported skipped, so a
    manifest naming a renamed f2p test would otherwise read as a green gate and
    then as a solved run on every arm.

    `passed` OR `failed`. An f2p id is EXPECTED to fail at the start state, so
    counting only passes would report every f2p test as not-run and turn
    preflight's own red half into an environment problem.

    A missing report answers "all of them", not "none of them": `report=None`
    is the config-error signal, and "nothing was requested that did not run" is
    the one reading a run which produced no evidence cannot support.

    That branch is for DIRECT callers -- this function is public and its rule
    has to hold for whatever asks it. `preflight._Runner.classify`, the only
    caller in the harness, cannot reach it: it guards on `last_report is not
    None` before asking, because a report-less run is already an ENVIRONMENT
    outcome there and replacing `not_run` on top of that would state the same
    absence twice, once as a kind and once as a list of ids. Preflight's own
    `f2p_before_not_run` says which absence it is instead -- `None` for a run
    that wrote no report, `[]` for one that did and named every id.
    """
    if report is None:
        return frozenset(requested)
    # Through `executed_names` rather than beside it: "which tests reached a
    # verdict" is one rule with two readers (this one, and preflight's
    # duplicate-`fullName` assertion), and a second copy of it is a second
    # thing that can be wrong about what ran -- inside the two checks that
    # exist to be right about it.
    seen = {f"{path}::{name}" for path, name in adapter.executed_names(report)}
    return frozenset(node_id for node_id in requested if node_id not in seen)


#: The npm specifiers that mean "this file drives a property-based suite".
#: Matched as a QUOTED MODULE SPECIFIER rather than anchored to an import
#: statement the way pytest's `^\s*(from|import)\s+hypothesis\b` is: JavaScript
#: has four spellings that all reach the same package -- `import fc from`,
#: `import * as fc from`, `import { fc } from`, `const fc = require(...)` and
#: `await import(...)` -- and `require` is an expression that can appear
#: anywhere on a line, so an anchored pattern would miss the two commonest
#: forms. The quoted specifier is the one shape all of them share.
#:
#: Measured 2026-09-02 against ripgrep 13.0.0 in bakeoff-eval-agent:base-node-22
#: over twelve fixtures: all seven import spellings match, a file importing
#: only `yaml` does not. `@fast-check/<pkg>` covers the official vitest and
#: jest integrations, which re-export fast-check and share its seed.
_PROPERTY_IMPORT_PATTERN = (
    r"""['"](@fast-check/[A-Za-z0-9._-]+|fast-check|jest-fuzz|jsverify)['"]"""
)

#: A SUITE-WIDE seed pin. `[^{}]*?` rather than `.*?`: a negated class matches
#: newlines, so this spans a multi-line `configureGlobal({\n seed: 1234,\n})`
#: under `rg -U` while the brace bound keeps it inside the one options object --
#: a `.*?` would happily pair a `configureGlobal(` here with a `seed:` two
#: hundred lines away.
#:
#: BRACES, NOT PARENTHESES, and it was measured both ways. `[^)]*?` misses a
#: genuine pin whose options carry a nested CALL --
#: `configureGlobal({ randomType: prand.xorshift128plus(), seed: 42 })`, a
#: supported fast-check shape -- and refusing a task whose suite is already
#: deterministic is the cost. `[^{}]` matches that one and still refuses the
#: case the bound exists for: an unseeded `configureGlobal({ numRuns: 500 })`
#: in the same file as a per-assert `fc.assert(..., { seed: 7 })`, because the
#: text between them contains a `{`. Measured 2026-09-02 over sixteen fixtures:
#: 16/16 for `[^{}]`, 15/16 for `[^)]`.
#:
#: A per-call seed is deliberately not a pin: seeding one assertion is not a
#: claim about the file. The residual miss is a pin whose options object
#: contains a nested OBJECT literal, which the brace bound cannot cross -- a
#: false refusal, which is the loud direction.
_PROPERTY_PIN_PATTERN = r"configureGlobal\s*\(\s*\{[^{}]*?\bseed\s*:"

#: Shared by both flavours because they disagree about nothing here: vitest and
#: jest resolve the same npm packages by the same specifier strings, and a
#: per-flavour copy would be two places for one fact to drift.
_NODE_PROPERTY_SCAN = PropertyScan(
    import_pattern=_PROPERTY_IMPORT_PATTERN,
    pin_pattern=_PROPERTY_PIN_PATTERN,
    frameworks=("fast-check", "@fast-check/*", "jest-fuzz", "jsverify"),
)


@dataclass(frozen=True)
class _NodeFlavour:
    """One node framework. The classifier is shared; only the argv differs.

    A dataclass rather than two subclasses because everything that differs
    between vitest and jest is a string or a flag: the report flag, the
    exclusion flag, and whether the file positional is a regex. A second class
    would be a second place for the classification to drift, on frameworks
    whose report shapes were measured to agree.
    """

    name: str
    #: A substring that must appear in `tests.runner`. Preflight refuses a
    #: manifest whose declared framework and declared argv disagree.
    runner_marker: str
    #: Flags this framework needs so the suite writes nothing into the tree.
    no_cache_args: tuple[str, ...]
    #: The flag that turns the machine-readable report on.
    _report_flag: str
    #: The per-file exclusion flag, VITEST ONLY, and empty on jest -- whose own
    #: `--testPathIgnorePatterns` REPLACES the repository's configuration rather
    #: than adding to it (measured; see `_exclude_args`). Defaulted because jest
    #: no longer carries one; `_exclude_args` guards on the flavour and never on
    #: the emptiness of this string, so a flavour that forgot to set it emits
    #: nothing rather than `=<path>`.
    _ignore_flag: str = ""
    #: Whether the file positional is a JS RegExp (jest) or a substring filter
    #: (vitest). AFTER every field without a default, or the dataclass raises at
    #: import.
    _file_filter_is_regex: bool = False

    # -- argv --------------------------------------------------------------

    def _relpath(self, name: str) -> str:
        """A report `name` made rootdir-relative. Never raises.

        A `testResults` entry with no `name` is still evidence about the
        entries beside it, so a malformed one may not take the classification
        down with it: `classify` is called from inside `_Runner`, whose error
        path would report a passing suite as an environment problem.

        Strips the container's bind-mount prefix and NOTHING else. A name that
        is already relative, or that names a path outside the mount, is passed
        through unchanged rather than guessed at -- the alternative is a path
        this code invented appearing in `failed_ids`, which the grader
        publishes as the id the model failed.
        """
        prefix = _REPO_MOUNT + "/"
        return name[len(prefix):] if name.startswith(prefix) else name

    def _file_filter(self, path: str) -> str:
        # jest: a JS RegExp tested against BOTH the repo-relative and the
        # absolute path, so only a mount-anchored, escaped, `$`-terminated
        # pattern names one file. Measured 2026-09-02: `tests/doc/a.test.js$`
        # also matched `/repo/pkg/tests/doc/a.test.js`, and an unescaped
        # `^/repo/tests/v1.2/a.test.js$` also matched `tests/v1X2/a.test.js`.
        # vitest: a substring filter no anchoring reaches -- the absolute form
        # matched the tail-colliding file too -- so the bare relative path is
        # the honest spelling and preflight refuses the trees it cannot
        # separate.
        if self._file_filter_is_regex:
            return "^" + _js_escape(_REPO_MOUNT + "/" + path) + "$"
        return path

    def file_filter_matches(self, declared_path: str, candidate_path: str) -> bool:
        if self._file_filter_is_regex:
            return candidate_path == declared_path
        return declared_path in candidate_path

    def _group_by_file(self, node_ids) -> list[tuple[str, list[str]]]:
        """`(file, titles)` in first-seen order, split once from the LEFT.

        A JavaScript title may itself contain `::`, which is why this is
        `partition` and not `rsplit` -- the same rule `module_of` states.
        """
        groups: dict[str, list[str]] = {}
        for node_id in node_ids:
            path, _, title = node_id.partition("::")
            groups.setdefault(path, []).append(title)
        return list(groups.items())

    def _titles_in(self, node_ids, path: str) -> list[str]:
        """The ESCAPED titles of `node_ids` that live in `path`, in order."""
        return [_js_escape(title)
                for other, titles in self._group_by_file(node_ids)
                if other == path
                for title in titles]

    def _exclude_args(self, paths) -> list[str]:
        # vitest ONLY. Measured 2026-09-02: vitest's `--exclude` is additive to
        # the config's `test.exclude`, while jest's `--testPathIgnorePatterns`
        # REPLACES the config's -- and `eemeli/yaml`, the corpus's only node
        # task, declares `["tests/_utils", "tests/json-test-suite/"]` and no
        # `/node_modules/`, so the flag would pull its helper modules (which
        # match its own `testMatch`) into the regression check at gate time and
        # at grade time alike. The bare relative path, never `**/<path>`:
        # measured, the glob form also drops a second file whose path ends the
        # same way, which is the defect this module exists to close.
        if self._file_filter_is_regex or not paths:
            return []
        return [f"{self._ignore_flag}={path}" for path in paths]

    def _scope_positionals(self, scope, excluded) -> list[str]:
        """Group 0's positionals: the declared prefixes, minus these files.

        `scope` arrives already stripped of any prefix that IS an excluded
        file, because such a prefix collects nothing at all (see `p2p_argvs`).
        Byte-identical to `list(scope)` whenever nothing is excluded, and on
        vitest always -- there the exclusion is `_exclude_args`' job.

        On jest the exclusion is a negative lookahead folded in here, TWO per
        file, because jest tests a positional against the repo-relative path
        AND the absolute one and collects the file if either matches: measured
        2026-09-02, a lookahead naming only the absolute spelling did not
        exclude, and one naming only the relative spelling did not either.

        The scope segment stays the RAW declared prefix behind a `.*`, never
        anchored at the mount, because `scope_files_outside` is a claim about
        what that exact string matches -- measured, `vitest run tests/` matched
        `/repo/jtests/fail.test.cjs` and jest's `tests/` matched
        `/repo/pkg/tests/doc/...`. Measured both ways here too:
        `^(?!<rel>$)(?!<abs>$).*tests/` excludes the named file and still runs
        `pkg/tests/doc/...`, while `^(?!<abs>$)/repo/tests/` excludes both and
        would make that check unable to fire.
        """
        if not excluded or not self._file_filter_is_regex:
            return list(scope)
        guard = "^" + "".join(
            f"(?!{_js_escape(path)}$)"
            f"(?!{_js_escape(_REPO_MOUNT + '/' + path)}$)"
            for path in excluded
        )
        return [guard + ".*" + prefix for prefix in scope] or [guard + ".*"]

    def _argv(self, head, pattern) -> list[str]:
        # `-t ''` is not the same argv. An empty pattern matches every name,
        # which is what group 0 of the deselect branch wants -- and is also
        # exactly what a builder that failed to fill the pattern in would emit.
        if not pattern:
            return list(head)
        return [*head, "-t", "^" + pattern]

    def select_argvs(self, node_ids):
        # ONE ARGV PER FILE. Measured 2026-09-02: two positionals plus one
        # union `-t` executed a third test, declared for neither pairing --
        # `-t` matches `fullName` and the positionals are ANDed across the
        # whole invocation, never zipped. An empty selection is NO GROUPS, not
        # an argv with no filter: `_Runner.run` raises on it, because falling
        # through would run the whole suite as the selection.
        return [self._argv([self._file_filter(path)],
                           "(?:" + "|".join(_js_escape(t) for t in titles) + ")$")
                for path, titles in self._group_by_file(node_ids)]

    def p2p_argvs(self, *, selected, scope, deselected, ignored):
        # The ORDER inside group 0 is a correctness rule, not a style one.
        # jest's `--testPathIgnorePatterns` was a greedy yargs array: measured,
        # with the positional LAST it swallowed `tests/` into the ignore list
        # and ran NOTHING at exit 1 with an empty `testResults`. jest emits no
        # such flag any more, but vitest's `--exclude` sits in the same place
        # and the scope therefore still precedes the flags. The trailing `-t`
        # is safe on a greedy option because it starts with `-`, which ends
        # the array.
        if selected:
            pairs = list(self._group_by_file(selected))
            # A file in `ignored` gets no group at all -- that flag's one
            # caller is preflight's p2p run at the START state, whose whole
            # point is that the f2p module must not be collected. But only
            # while some other group will run: if EVERY selected file is
            # ignored (the f2p module's own failure to load takes every
            # explicitly-declared p2p id in it down too), dropping them all
            # would return an empty SEQUENCE, which `_Runner.run` refuses --
            # turning what used to be a per-task NO-GO into a `ValueError`
            # that `run_matrix.py` has no `except` around, killing every
            # remaining task's gate. Emit the groups anyway and let the run
            # be loud (KIND_LOAD_ERROR -- a file in `ignored` is one that
            # failed to import, so re-collecting it yields a `testResults`
            # entry with an empty `assertionResults`) instead of the driver
            # going quiet.
            keep_any = any(path not in ignored for path, _ in pairs)
            groups = []
            for path, titles in pairs:
                if path in ignored and keep_any:
                    continue
                pattern = ""
                drop = self._titles_in(deselected, path)
                if drop:
                    pattern += "(?!(?:" + "|".join(drop) + ")$)"
                pattern += "(?:" + "|".join(_js_escape(t) for t in titles) + ")$"
                groups.append(self._argv([self._file_filter(path)], pattern))
            return groups
        dfiles = [path for path, _ in self._group_by_file(deselected)
                  if path not in ignored]
        excluded = [*ignored, *dfiles]
        # A `tests.paths` entry may be a FILE rather than a directory prefix
        # -- `yaml-474-single-newline-empty-value` declares
        # `["tests/doc/stringify.ts"]`, which is also its only f2p file. Group
        # 0 then excludes the whole of its own scope and collects nothing:
        # measured 2026-09-02, both frameworks answer that with exit 1 and a
        # report of ZERO tests, so a check that emitted it would report the
        # regression suite as not green on a task that is fine. Those prefixes
        # are dropped, and group 0 with them when nothing is left for it to
        # run under the narrowed scope -- but only while some other group
        # will run, because an empty SEQUENCE is what `_Runner.run` refuses.
        # When the whole declared scope collapses this way, group 0 does not
        # stay empty: `_scope_positionals`' `or [guard + ".*"]` fallback
        # sweeps the whole repository minus the excluded files -- the same
        # argv the scope=() callers already get -- so this is kept-and-
        # widened, not the loud-and-empty invocation it might read as.
        remaining = [prefix for prefix in scope if prefix not in excluded]
        # Group 0: everything under scope EXCEPT the files holding a
        # deselection, and no `-t` at all. Then one group per such file,
        # carrying only that file's own deselected titles -- which is what
        # keeps a quarantine of `a::works` off `b::works`.
        groups = []
        if remaining or not scope or not dfiles:
            head = [*self._scope_positionals(remaining, excluded),
                    *self._exclude_args(excluded)]
            groups.append(self._argv(head, ""))
        for path in dfiles:
            drop = self._titles_in(deselected, path)
            groups.append(self._argv([self._file_filter(path)],
                                     "(?!(?:" + "|".join(drop) + ")$)"))
        return groups

    def merge_reports(self, reports):
        """One report per argv group, folded into one. See the plan's D5.

        `None` for an EMPTY list and for any list holding a `None`: nobody ran
        and nobody counted, versus a group that produced no evidence at all. A
        `{"testResults": []}` would be a CLAIM that a run happened and executed
        nothing, which `classify` reads as KIND_NOTHING_RAN rather than as the
        environment problem a missing report is.

        Only `testResults` and `numPendingTests` survive. `success` most of all
        is dropped: `classify` already refuses to consult it (jest reports
        `success: true` while exiting 1 on "no test files matched", and vitest
        reports `false` for the same run), and a scalar carried over from one
        group is a value no invocation produced.
        """
        if not reports or any(report is None for report in reports):
            return None
        if len(reports) == 1:
            return reports[0]
        merged = {"testResults": [entry for report in reports
                                  for entry in (report.get("testResults") or [])]}
        pending = [report.get("numPendingTests") for report in reports]
        if all(isinstance(count, int) for count in pending):
            merged["numPendingTests"] = sum(pending)
        return merged

    def report_args(self, report_path):
        return [self._report_flag, f"--outputFile={report_path}"]

    def report_path(self):
        return REPORT_PATH

    # -- reading what happened ---------------------------------------------

    def classify(self, *, exit_code, stdout, stderr, report):
        # `stdout` and `stderr` are accepted and NOT read, on purpose. They are
        # in the protocol because pytest's judgement is built out of them, and
        # here there is nothing in either to parse: measured, vitest writes its
        # human summary to stdout while jest writes its own to stderr, so any
        # regex over them would be a second, per-framework classifier sitting
        # beside the one that reads the report. The exit code is recorded and
        # not consulted for the same reason.
        if report is None:
            return Outcome(
                kind=KIND_ENVIRONMENT, exit_code=exit_code, errored_files=None,
                explain=("the runner wrote no JSON report, so it did not say "
                         "what it did -- measured, a broken config exits 1 and "
                         "writes no file on both frameworks, and so does a "
                         "runner that could not start"),
            )
        suites = report.get("testResults") or []
        errored, failed, ran, files = set(), set(), 0, []
        for suite in suites:
            path = self._relpath(suite.get("name") or "")
            files.append(path)
            assertions = suite.get("assertionResults") or []
            # A suite that FAILED and reported no assertion is a file that
            # could not be LOADED. The only portable discriminator, and it is
            # the same on an import error and on a syntax error.
            if suite.get("status") == "failed" and not assertions:
                errored.add(path)
                continue
            for item in assertions:
                status = item.get("status")
                if status == "failed":
                    failed.add(f"{path}::{item.get('fullName', '')}")
                    ran += 1
                elif status == "passed":
                    ran += 1
        # Every suite the run LOADED or executed, the unloadable one included:
        # D7c's scope check asks whether the run left `tests.paths`, and a file
        # a substring filter pulled in and then failed to parse is exactly such
        # a file.
        files_run = tuple(sorted(files))
        if errored:
            # BEFORE the failure branch. Measured: one good file plus one
            # unloadable file reports three PASSING tests and zero failing
            # ones, so the other order grades a task whose f2p file stopped
            # importing as solved.
            return Outcome(kind=KIND_LOAD_ERROR, exit_code=exit_code,
                           failed_ids=frozenset(failed),
                           errored_files=frozenset(errored),
                           files_run=files_run,
                           explain="a test file did not load")
        if failed:
            return Outcome(kind=KIND_FAILED, exit_code=exit_code,
                           failed_ids=frozenset(failed),
                           errored_files=frozenset(), files_run=files_run,
                           explain="tests ran and failed")
        if ran == 0:
            # `success` is NOT consulted: jest reports `success: true` while
            # exiting 1 on "no test files matched" (measured -- and vitest
            # reports `false` for the same run, which is why neither value can
            # be the classifier). And this is where a `-t` matching nothing
            # lands, at exit 0.
            return Outcome(kind=KIND_NOTHING_RAN, exit_code=exit_code,
                           errored_files=frozenset(), files_run=files_run,
                           explain=("no test reached a terminal status -- no "
                                    "file matched, or every selected test was "
                                    "skipped"))
        return Outcome(kind=KIND_PASSED, exit_code=exit_code,
                       errored_files=frozenset(), files_run=files_run,
                       explain="all selected tests passed")

    def parse_deselected(self, *, stdout, report):
        """`numPendingTests`, or `None` when no report was read.

        The UNITS are not pytest's, and the record has to say which framework
        produced the number (D11): pytest counts deselections it was asked to
        make, this counts tests that did not run for ANY reason, `it.skip`
        included. That is the caveat `p2p_deselected`'s docstring already
        carries for pytest one framework over -- the floor claim stays honest
        and its power to fire is weaker still.

        Read off the report, never off stdout: measured, vitest writes its
        human summary to stdout and jest writes its own to STDERR, so there is
        no cross-framework summary line to parse.
        """
        if report is None:
            return None
        return report.get("numPendingTests")

    def _executed(self, report):
        """`(suite ordinal, relpath, fullName)` for every assertion that
        reached a verdict.

        ONE rule with three readers -- `executed_names` (which
        `verify_selected` and preflight's cross-file duplicate evidence go
        through) and `duplicate_ids` (preflight's same-file refusal). A second
        copy of the terminal-status test is a second thing that can be wrong
        about what ran, inside the checks that exist to be right about it.

        `passed` OR `failed`, never `skipped`/`pending`/`todo`: a deselected
        test and a test that was never selected are the same shape in the
        report (measured -- a `-t` matching nothing exits 0 with every test
        skipped), and both of this generator's readers are claims about what
        actually RAN.

        THE SUITE ORDINAL is carried because `duplicate_ids` counts within one
        `testResults` entry and not across the report. Since item 1 a report
        can be the merge of several argv groups, so one file could appear as
        two entries; counting per entry can only UNDER-report a duplicate,
        never invent one, and inventing one refuses a healthy task.

        DEFINED BELOW `classify`, and that is not stylistic:
        `scripts/mutation_check.py` anchors `classify`'s own `if report is
        None:` by its exact eight-space text and replaces the FIRST
        occurrence. This method now carries that guard (`executed_names` used
        to), so an eight-space `if report is None:` added ANYWHERE above
        `classify` -- this one moved, or a new method -- silently moves the
        mutation to a different guard, and `mutation_check` reports CAUGHT for
        the wrong reason. Pinned over the whole module by
        `test_the_first_eight_space_report_guard_in_this_module_is_the_classifiers`.
        """
        if report is None:
            return
        for ordinal, suite in enumerate(report.get("testResults") or []):
            path = self._relpath(suite.get("name") or "")
            for item in suite.get("assertionResults") or []:
                if item.get("status") in _TERMINAL_STATUSES:
                    yield ordinal, path, item.get("fullName", "")

    def executed_names(self, report):
        """Every assertion that reached a verdict, as `(relpath, fullName)`.

        `passed` OR `failed`, the same rule `verify_selected` reads through
        this method: a test that was skipped and a test that was never
        selected are the same shape in the report, and preflight's
        duplicate-name assertion is a claim about what actually RAN under
        `tests.paths`.

        A two-line wrapper over `_executed`, which owns that rule and the
        `report is None` guard for all three readers -- this one,
        `verify_selected` through it, and `duplicate_ids`. The mutation-anchor
        argument that used to live in this docstring moved there with the
        guard.
        """
        for _, path, name in self._executed(report):
            yield path, name

    def duplicate_ids(self, report):
        """`<file>::<fullName>` -> the tests in that file answering to it,
        for the ids where that is more than one.

        The half NEITHER a loader NOR a cross-file rule can see: both
        duplicates are the identical id string, so `validate_id_set` compares
        it with itself and `duplicate_full_names`' `first != path` is False by
        construction. Measured 2026-09-02 (vitest 3.2.7, jest 30.5.0): an
        exact anchored `-t` runs both -- one passed and one failed in the same
        run -- and the deselection skips both with `numPendingTests: 2` for
        one requested id, so no count downstream disagrees with anything.

        `{}` for a report that does not exist, which is NOT the same fact as
        "a report was read and holds no collision": preflight records that
        absence with the flag that guards this call, exactly as it does for
        `duplicate_full_names` and `f2p_before_not_run`.
        """
        counts: dict[tuple[int, str], int] = {}
        for ordinal, path, name in self._executed(report):
            key = (ordinal, f"{path}::{name}")
            counts[key] = counts.get(key, 0) + 1
        out: dict[str, int] = {}
        for (_, node_id), count in counts.items():
            if count > 1:
                out[node_id] = max(out.get(node_id, 0), count)
        return dict(sorted(out.items()))

    def module_of(self, node_id):
        # Split once, from the LEFT. A JavaScript test title may itself
        # contain `::` -- nothing forbids it -- and an rsplit would name a file
        # that does not exist, which reaches preflight as a confinement
        # equality that can never hold.
        return node_id.split("::", 1)[0]

    # -- load-time validation ----------------------------------------------

    def validate_node_id(self, node_id, paths, where):
        """`<file>::<full test name>`, and the file must be inside the scope.

        `::` rather than a custom separator so `f2p_modules`' "split once from
        the left" stays correct verbatim across both runtimes, and so the
        `::`-presence discriminator that tells a test id from a file id keeps
        working. A JS title may itself contain `::`; splitting from the LEFT
        with maxsplit 1 is unambiguous anyway, which is the same argument the
        pytest parser makes for parametrized ids.

        The scope rule exists because a deselection that matches nothing is
        SILENT here: measured 2026-09-01, `-t` with a pattern matching no test
        exits 0 on both vitest and jest with every test reported skipped. An id
        whose file is outside `tests.paths` can never be deselected from the
        scoped p2p run, and nothing downstream would say so.

        `..` and an absolute path are refused BEFORE that scope check rather
        than left to it, because `_under` is `is_relative_to` and therefore
        purely LEXICAL: it does not normalise, so `tests/../../etc/x.test.js`
        is "under" `tests/` and would load clean. The runner then resolves it
        against the real filesystem and selects nothing -- the same silent
        no-op the scope rule exists to prevent, reached through the rule
        itself.

        Implemented ahead of the rest of this module -- Task 4 needs it at
        LOAD time, and a stub here would let a manifest through and be found
        only by a preflight that had already built an image.
        """
        from bakeoff.tasks import TaskError, _under

        if "::" not in node_id:
            raise TaskError(
                f"{where}: {node_id!r} is not a {self.name} node id. The shape "
                "is `<file>::<full test name>`, where the name is the "
                "reporter's `fullName` -- the describe titles and the test "
                "title joined by single spaces"
            )
        path, _, title = node_id.partition("::")
        if not path or not title:
            raise TaskError(
                f"{where}: {node_id!r} has an empty half; both the file and "
                "the full test name are required"
            )
        if path.startswith("-"):
            raise TaskError(
                f"{where}: {node_id!r}'s file half starts with '-', which the "
                "runner would parse as a flag rather than as a file filter"
            )
        if path.startswith("/") or ".." in path.split("/"):
            raise TaskError(
                f"{where}: {node_id!r}'s file half must be a plain "
                "repo-relative path -- no leading '/', no '..' component. The "
                "scope check below is lexical (`is_relative_to`) and does not "
                "normalise, so 'tests/../elsewhere' would pass it while naming "
                "a file the scoped p2p run never collects -- and a selection "
                "that matches nothing exits 0 with every test skipped "
                "(measured), so nothing downstream would say so"
            )
        if not _under(path, tuple(paths)):
            raise TaskError(
                f"{where}: {node_id!r}'s file is outside tests.paths "
                f"({list(paths)}). The scoped p2p run selects by those "
                "prefixes, so this id could never be deselected from it -- and "
                "a deselection that matches nothing exits 0 with every test "
                "skipped (measured), so nothing downstream would say so"
            )

    def validate_id_set(self, node_ids, where):
        """Nothing, and the emptiness is the finding rather than a gap.

        This is where two declared ids sharing a `fullName` across different
        files were refused, because `-t` matches by name alone. They are not
        refused any more: a selection is one argv per FILE and each pattern
        holds only that file's titles, so `a.test.js::works` and
        `b.test.js::works` are as unambiguous to this adapter as their id
        spelling already was. Measured 2026-09-02: one positional plus one
        `-t` runs the named tests of that file only.

        The same-file case is a different problem and is NOT this method's:
        two tests sharing a full name in ONE file collapse to the identical
        node id string, so a set-level check comparing `(fullName, path)`
        pairs cannot see them at all. That is a manifest-representation gap,
        tracked in `TASKS.md`.
        """
        return None

    def hypothesis_interpreter(self, runner):
        # `None`, so preflight skips the whole block and leaves both evidence
        # keys `None` -- a recorded ABSENCE, never a claim that the suite is
        # deterministic. The JavaScript determinism question is answered by
        # `property_scan` below instead, and it is a REFUSAL rather than a
        # probe-plus-remedy: measured 2026-09-02, fast-check reads no
        # environment variable, so there is no node analogue of CI=1 to
        # declare.
        return None

    def property_scan(self):
        return _NODE_PROPERTY_SCAN

    def explain(self, code):
        # Deliberately says what the number does NOT tell anyone. Every
        # measured failure shape returns 1 and a `-t` matching nothing returns
        # 0, so a phrase naming a cause would be inventing one.
        return _EXIT_MEANING.get(
            code,
            f"exit code {code}, which {self.name} returns for a failing test, "
            "an unresolvable import, a syntax error, a nonexistent file "
            "argument and a broken config alike -- the JSON report is the only "
            "thing that says which",
        )


#: Measured values, not guesses. `no_cache_args`: `vitest run` writes
#: `<cwd>/node_modules/.vite` into the bind-mounted tree and `--no-cache` stops
#: it, while jest's default `cacheDirectory` is already `/tmp/jest_0` and jest
#: wrote nothing into the tree in any measured run -- so the empty tuple is a
#: claim, not an omission. It matters more than pytest's equivalent because
#: every JavaScript repository's `.gitignore` carries `node_modules/`, which
#: makes preflight's dirty-tree check BLIND to vitest's artifact (D7d).
VITEST = _NodeFlavour(
    name="vitest",
    runner_marker="vitest",
    no_cache_args=("--no-cache",),
    _report_flag="--reporter=json",
    _ignore_flag="--exclude",
)

#: jest carries NO exclusion flag, and that is a measurement rather than an
#: omission. `--testPathIgnorePatterns` REPLACES the repository's own value
#: instead of adding to it (measured 2026-09-02), and re-emitting
#: `/node_modules/` beside it restores jest's BUILT-IN default rather than
#: anything the repository declared -- `eemeli/yaml`, the corpus's only node
#: task, reports `testPathIgnorePatterns: ["tests/_utils",
#: "tests/json-test-suite/"]` and no `/node_modules/` at all through its own
#: `--showConfig`, and `tests/_utils` matches its `testMatch`. So the flag
#: would pull that task's helper modules into the regression check on every
#: gated and graded p2p run. jest excludes through negative lookaheads folded
#: into the positional instead (`_scope_positionals`), which touches no
#: configuration. `_file_filter_is_regex` is jest's other half: its positional
#: is a JS RegExp over the file path, so it can be anchored exactly.
JEST = _NodeFlavour(
    name="jest",
    runner_marker="jest",
    no_cache_args=(),
    _report_flag="--json",
    _file_filter_is_regex=True,
)
