"""vitest and jest -- STUB. The behaviour lands in broadening 7 Task 6.

The registry is complete from this commit and empty of behaviour, which is
deliberate. `FRAMEWORKS` and `tasks._FRAMEWORKS` are cross-checked, and
`for_framework` raises `KeyError` on an unknown name rather than defaulting --
so a registry that GROWS later is a registry the loader's allowlist can
disagree with for one commit, and the failure of that disagreement is a
`KeyError` out of the middle of preflight with no manifest path in it.

Every method raises `NotImplementedError("broadening 7 Task 6")` EXCEPT
`validate_node_id` and `validate_id_set`, which Task 4 implemented because
`load_task` calls them: a stub there would let a malformed manifest through to
a preflight that had already built an image. Everything else stays loud, which
is the point -- a stub returning a plausible `Outcome` would classify a jest
run as passed.

Those two are also the reason this module imports `bakeoff.tasks` INSIDE the
methods rather than at module scope: `tasks` imports `for_framework` from
`bakeoff.runners`, and `bakeoff.runners` builds its registry out of this
module.

One module for both frameworks, parameterised by `_NodeFlavour`, because the
measured report shapes (plan M2) need one classifier: the load-error
discriminator is `status == "failed"` with empty `assertionResults`, which
does not need jest's `numRuntimeErrorTestSuites` and therefore needs no
jest-specific branch.
"""

from __future__ import annotations

_UNBUILT = "broadening 7 Task 6"


class _NodeFlavour:
    """One node framework's identity. The behaviour arrives in Task 6."""

    def __init__(self, name: str, runner_marker: str,
                 no_cache_args: tuple[str, ...]):
        self.name = name
        self.runner_marker = runner_marker
        self.no_cache_args = no_cache_args

    def select_args(self, node_ids):
        raise NotImplementedError(_UNBUILT)

    def p2p_args(self, *, selected, scope, deselected, ignored):
        raise NotImplementedError(_UNBUILT)

    def report_args(self, report_path):
        raise NotImplementedError(_UNBUILT)

    def report_path(self):
        raise NotImplementedError(_UNBUILT)

    def classify(self, *, exit_code, stdout, stderr, report):
        raise NotImplementedError(_UNBUILT)

    def parse_deselected(self, *, stdout, report):
        raise NotImplementedError(_UNBUILT)

    def module_of(self, node_id):
        raise NotImplementedError(_UNBUILT)

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
        raise NotImplementedError(_UNBUILT)

    def explain(self, code):
        raise NotImplementedError(_UNBUILT)


#: The identities only. Task 6 owns every value that has to be right about
#: what the runner DOES; these three are what the registry needs to exist.
#:
#: `no_cache_args` is provisional and comes from the plan's M6, not from a
#: measurement made here: `vitest run` writes `<cwd>/node_modules/.vite` into
#: the bind-mounted tree and `--no-cache` stops it, while jest's default
#: `cacheDirectory` is already `/tmp/jest_0` and jest wrote nothing into the
#: tree in any measured run -- so the empty tuple is a claim, not an omission.
#: Nothing reads these yet; the assertion that a manifest carries them is
#: D7d's, and it lands with Task 6.
VITEST = _NodeFlavour("vitest", "vitest", ("--no-cache",))
JEST = _NodeFlavour("jest", "jest", ())
