"""vitest and jest -- STUB. The behaviour lands in broadening 7 Task 6.

The registry is complete from this commit and empty of behaviour, which is
deliberate. `FRAMEWORKS` and `tasks._FRAMEWORKS` are cross-checked, and
`for_framework` raises `KeyError` on an unknown name rather than defaulting --
so a registry that GROWS later is a registry the loader's allowlist can
disagree with for one commit, and the failure of that disagreement is a
`KeyError` out of the middle of preflight with no manifest path in it.

Every method raises `NotImplementedError("broadening 7 Task 6")`. That is the
loud shape: a stub returning a plausible `Outcome` would classify a jest run
as passed. Nothing can reach these yet in any case -- `tests.framework` does
not exist as a manifest key until Task 2.

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
        raise NotImplementedError(_UNBUILT)

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
