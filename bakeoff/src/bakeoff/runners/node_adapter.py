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

THE FILE HALF OF A NODE ID IS THROWN AWAY when a name pattern is built, and two
guards elsewhere are what make that safe. `-t` matches `fullName` and no flag
scopes a name pattern to a file, so two tests sharing a name across files are
indistinguishable to a selection or a deselection: a quarantine of one silently
removes the other, with the deselection count agreeing because two tests really
were skipped. `validate_id_set` refuses a manifest whose declared ids collide,
and preflight asserts against the real report that no two EXECUTED tests under
`tests.paths` share a name -- which is the half a loader cannot see, and the
half where the collision is with a test the manifest never mentions.

The eight report shapes this module branches on are committed under
`tests/fixtures/node_reports/` with the argv each came from; see the README
beside them. They are replayed rather than re-measured because regenerating
one needs a Docker daemon, a network and 73 MB of npm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bakeoff.runners import (
    KIND_ENVIRONMENT,
    KIND_FAILED,
    KIND_LOAD_ERROR,
    KIND_NOTHING_RAN,
    KIND_PASSED,
    Outcome,
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


@dataclass(frozen=True)
class _NodeFlavour:
    """One node framework. The classifier is shared; only the argv differs.

    A dataclass rather than two subclasses because everything that differs
    between vitest and jest is a string: the report flag, the ignore flag, and
    whether that flag needs jest's built-in `/node_modules/` rule re-emitted
    beside it. A second class would be a second place for the classification to
    drift, on frameworks whose report shapes were measured to agree.
    """

    name: str
    #: A substring that must appear in `tests.runner`. Preflight refuses a
    #: manifest whose declared framework and declared argv disagree.
    runner_marker: str
    #: Flags this framework needs so the suite writes nothing into the tree.
    no_cache_args: tuple[str, ...]
    #: The flag that turns the machine-readable report on.
    _report_flag: str
    #: The per-file exclusion flag. One caller: preflight's p2p run at the
    #: START state, which the grader never makes -- so the two spellings
    #: cannot diverge between a gated and a graded argv.
    _ignore_flag: str
    #: Emitted before the ignored paths, and only when there are any.
    _ignore_prefix: tuple[str, ...] = field(default=())

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

    def _files(self, node_ids: tuple[str, ...]) -> list[str]:
        """The distinct file halves, in first-seen order.

        Deduplicated because a repeated positional is not harmless: jest's
        positional is a REGEX over the absolute path and vitest's is a
        substring filter, so a duplicate is a second pattern that has to agree
        with the first -- and because the argv is what the gated/graded
        byte-identity property is stated over.
        """
        seen, out = set(), []
        for node_id in node_ids:
            path = node_id.partition("::")[0]
            if path not in seen:
                seen.add(path)
                out.append(path)
        return out

    def _names(self, node_ids: tuple[str, ...]) -> list[str]:
        # The FILE half is thrown away here, and two guards elsewhere are what
        # make that safe: `-t` matches `fullName` and no flag scopes a name
        # pattern to a file, so two tests sharing a name across files are
        # indistinguishable to a selection or a deselection. The loader refuses
        # a manifest whose declared ids collide (`validate_id_set`), and
        # preflight asserts against the real report that no two EXECUTED tests
        # under `tests.paths` share a name -- which is the half a loader cannot
        # see. See the plan's D2.
        return [_js_escape(node_id.partition("::")[2]) for node_id in node_ids]

    def select_args(self, node_ids):
        if not node_ids:
            # NOT `^(?:)$`, which matches the empty name and nothing else --
            # M1's silent hole, reached with a pattern this adapter wrote
            # itself. An empty selection has no argv; the caller's own branch
            # decides whether that is legal.
            return []
        return [*self._files(node_ids), "-t",
                "^(?:" + "|".join(self._names(node_ids)) + ")$"]

    def p2p_args(self, *, selected, scope, deselected, ignored):
        # ONE `-t`, always, and never two. Measured 2026-09-02: vitest
        # REJECTS a second occurrence (`Expected a single value for option
        # "-t, --testNamePattern <pattern>", received ["a", "b"]`, exit 1, and
        # no report file), while jest COMMA-JOINS them into `adds,subs` -- a
        # regex matching neither test -- and exits 0 having run nothing. So
        # emitting a selection pattern and a deselection pattern separately is
        # a hard failure on one framework and a silent empty run on the other.
        #
        # The ORDER below is a correctness rule too, not a style one. jest's
        # `--testPathIgnorePatterns` is a greedy yargs array: measured, with
        # the positional LAST it swallows `tests/` into the ignore list and
        # runs NOTHING at exit 1 with an empty `testResults`, which is the p2p
        # check reporting a regression suite that executed zero tests. The
        # scope therefore precedes the flags. The trailing `-t` is safe on the
        # same greedy option because it starts with `-`, which ends the array.
        head = self._files(selected) if selected else list(scope)
        if ignored:
            head += list(self._ignore_prefix)
            head += [f"{self._ignore_flag}={path}" for path in ignored]
        pattern = ""
        if deselected:
            pattern += "(?!(?:" + "|".join(self._names(deselected)) + ")$)"
        if selected:
            pattern += "(?:" + "|".join(self._names(selected)) + ")$"
        if not pattern:
            # `-t ''` is not the same argv. An empty pattern matches every
            # name, which is what this run wants -- and is also exactly what a
            # builder that failed to fill the pattern in would emit.
            return head
        return [*head, "-t", "^" + pattern]

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

    def executed_names(self, report):
        """Every assertion that reached a verdict, as `(relpath, fullName)`.

        Defined AFTER `classify` on purpose: `scripts/mutation_check.py`
        anchors that classifier's `if report is None:` by its exact text and
        replaces the FIRST occurrence, so a second one earlier in the file
        would silently move the mutation to a different guard.

        `passed` OR `failed`, the same rule `verify_selected` reads through
        this method: a test that was skipped and a test that was never
        selected are the same shape in the report, and preflight's
        duplicate-name assertion is a claim about what actually RAN under
        `tests.paths`.
        """
        if report is None:
            return
        for suite in report.get("testResults") or []:
            path = self._relpath(suite.get("name") or "")
            for item in suite.get("assertionResults") or []:
                if item.get("status") in _TERMINAL_STATUSES:
                    yield path, item.get("fullName", "")

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
        """No two declared ids may share a full name across different files.

        `-t` matches `fullName` and knows nothing about which file a test came
        from: the file positionals and the name pattern are ANDed across the
        whole run, never paired, and no flag scopes a name to a file. So a
        quarantine of `a.test.js::works` ALSO deselects `b.test.js::works` --
        silently, with `p2p_deselected` agreeing, because two tests really were
        skipped -- and a selection of `a.test.js::works` plus
        `b.test.js::other` also runs `b.test.js::works`.

        This is the half an author can create. The half that matters more is a
        collision between a declared id and a test the manifest never mentions,
        which no loader can see; preflight asserts that against the real report
        of the scoped run.

        Per-file invocations would pair them exactly and are rejected: they
        multiply every gated and graded suite run by the number of distinct
        files, change what the framework collects, and turn the gated argv into
        a LIST of argvs, which the byte-identity property is not stated over.
        """
        from bakeoff.tasks import TaskError

        by_name: dict[str, str] = {}
        for node_id in node_ids:
            path, _, title = node_id.partition("::")
            first = by_name.setdefault(title, path)
            if first != path:
                raise TaskError(
                    f"{where}: {title!r} is the full name of a test in both "
                    f"{first!r} and {path!r}. {self.name} selects and "
                    "deselects by name alone -- there is no flag that scopes a "
                    "name pattern to a file -- so a quarantine of one would "
                    "silently remove the other from the regression check, and "
                    "the deselection count would agree. Rename one of them in "
                    "the task repo, or narrow tests.paths so only one is in "
                    "scope; see taskset/HARVESTING.md"
                )

    def hypothesis_interpreter(self, runner):
        # `None`, so preflight skips the whole block and leaves both evidence
        # keys `None` -- a recorded ABSENCE, never a claim that the suite is
        # deterministic. The JavaScript property-based libraries (fast-check,
        # jest-fuzz) have their own seed mechanisms and no such check exists
        # yet; that is named follow-up work in TASKS.md, not an assumption
        # made here.
        return None

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

#: jest's `--testPathIgnorePatterns` REPLACES its built-in `/node_modules/`
#: ignore rather than adding to it, so emitted alone it makes jest collect test
#: files out of `node_modules` -- which, with the runners installed at
#: `/node_modules` (D4), means jest's own vendored fixtures. The default is
#: re-emitted alongside, and only when `ignored` is non-empty: emitting it on
#: every run would put a flag in both the gated and the graded argv that
#: changes what jest collects for EVERY task, for the benefit of the one
#: preflight run that uses `ignored`.
JEST = _NodeFlavour(
    name="jest",
    runner_marker="jest",
    no_cache_args=(),
    _report_flag="--json",
    _ignore_flag="--testPathIgnorePatterns",
    _ignore_prefix=("--testPathIgnorePatterns=/node_modules/",),
)
