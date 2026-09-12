# Round 2, item 1 — a node selection that carries the file half

**Status:** plan, revision 1 (after review 1). Not yet implemented.
**Area:** `runners/__init__.py`, `runners/node_adapter.py`, `runners/pytest_adapter.py`, `preflight.py`, `grader.py`, `oracle.py`, `scripts/mutation_check.py`, `taskset/HARVESTING.md`, and three test files.
**Version constants that move:** `PREFLIGHT_VERSION` 13 → 14, `GRADER_VERSION` 8 → 9, `ORACLE_VERSION` 4 → 5. `SCHEMA_VERSION` does **not** move (no `RunRecord` or `GradeRecord` field changes shape).

Every measurement in this plan was taken in `bakeoff-eval-agent:base-node-22`
(node v22.23.2, vitest 3.2.7, jest 30.5.0 — its CLI answers `30.4.2` from the stale
inlined string `images.py:220` documents), against scratch trees under **`$HOME`**:
`~/.cache/bakeoff-probe/r2i1` and `~/.cache/bakeoff-probe/r2i1x` (the same tree plus
`jest.config.js` and `vitest.config.js` declaring non-default ignore lists,
`node_modules/somedep/vendor.test.js`, `tests/skipme/ignored.test.js`,
`pkg/tests/doc/stringify.test.js`, and `tests/v1.2/a.test.js` beside
`tests/v1X2/a.test.js`). A scratch tree under `/private/tmp` bind-mounts into the node
image as a **silently empty directory** — the same hazard `CLAUDE.md` records for
`/var/folders` and `--basetemp`. Any re-measurement must live under `$HOME`.

---

## 1. The measured defect

A node id is `<file>::<fullName>` and is unambiguous. The selection built from it is not.

`_NodeFlavour.select_args` emits *every* declared file as a positional and *one* `-t`
alternation over *every* declared name, ANDed across the whole invocation and never
paired. `_NodeFlavour.p2p_args`'s deselect branch is worse: the head is the declared
`tests.paths` scope and the negative `-t` is a name alternation over the whole scope,
so a deselection of one file's test removes every identically-named test in every
other file under scope.

Because of that, two guards refuse task shapes the id spelling already disambiguates:

* `node_adapter.validate_id_set` — a `TaskError` at **load** time when two *declared*
  ids share a `fullName` across different files.
* `preflight.py`'s `duplicate_full_names` check — a NO-GO when any two *executed*
  tests under `tests.paths` share a `fullName` across different files. This is the
  binding one, because the flake quarantine is derived at grade time by
  `oracle._derive` and can name **any** executed p2p node id, so every executed test
  is a potential deselection target.

**Measured 2026-09-02**, `~/.cache/bakeoff-probe/reports/w7b-yaml-474.md`, step 4
("interaction probe (`-wide`)"), against `eemeli/yaml` at `base_sha 92821f2b8164`,
task `yaml-474-single-newline-empty-value`, jest 30.5.0: widening `tests.paths` from
`tests/doc/stringify.ts` to `tests/` gates **NO-GO** with four collisions, verbatim:

```
two or more tests under tests.paths share a full name: 'circular references parent
at root' in tests/doc/stringify.ts and tests/doc/createNode.ts; 'circular references
ancestor at root' in tests/doc/stringify.ts and tests/doc/createNode.ts; 'circular
references sibling sequences' in tests/doc/stringify.ts and tests/doc/createNode.ts;
'circular references further relatives' in tests/doc/stringify.ts and
tests/doc/createNode.ts.
```

All four are under `describe('circular references', …)` and all four are tests the
manifest never mentions. The refusal fires **after all five of preflight's suite
runs**, on the scoped p2p-after run's report (`preflight.py:1866-1920` reads
`runner.last_report` of that run, the last invocation preflight makes) —
`w7b-yaml-474.md:166` infers "before any suite runs" from the absence of `PASS`/`FAIL`
lines in a `--json` log, which is an inference about a reporter's stdout and not about
ordering. The cost is a real yield cost on a real task: `tests.paths` was forced down
to one file, and `HARVESTING.md`'s screened-corpus row for `eemeli/yaml` records that.
`HARVESTING.md:364` turns the same rule into a corpus-wide exclusion: *"A repository
whose suite genuinely carries duplicate titles within one scope is excluded."*

### The measurements that decided the design

**M1 — the cross-product is real, on both frameworks.** Two positionals plus one
union `-t` over `circular references parent at root` (declared for `stringify`) and
`nodes only in createNode` (declared for `createNode`) executed **three** tests, not
two: `createNode.test.js :: circular references parent at root` ran as well, jest
exit 0 and vitest exit 0 alike. This is the selection half of the defect, and it
reproduced under review.

**M2 — one positional plus one `-t` pairs exactly on jest, and on vitest only for a
tree the D4 refusal admits.** `jest '^/repo/tests/doc/stringify\.test\.js$' -t
'^(?!(?:circular references parent at root)$)'` ran the other two tests **of that
file only**. `vitest run --globals --no-cache tests/doc/stringify.test.js -t
'^(?!(?:circular references parent at root)$)'` ran **three**: the two from
`tests/doc/stringify.test.js` *and* `pkg/tests/doc/stringify.test.js :: vendored
circular references parent at root`. The earlier reading of this as "the same" was
taken before `pkg/tests/doc/stringify.test.js` existed in the tree; the positive
polarity looked exact for the same reason and by coincidence (the vendored title is
`vendored circular references parent at root`, which the positive pattern does not
match). So: **per-file grouping is the mechanism on jest unconditionally, and on
vitest only where no executed file's path contains another's** — which is what D4
refuses, and why D1's disjointness is by refusal rather than by construction.

**M3 — the per-file positional is a different string on each framework, jest can be
made exact, and vitest cannot.** jest's positional is a JS `RegExp` tested against
**both** the repo-relative path and the absolute one (the mechanism is derived from
M9 below and is what makes the rest of this table coherent). Bare
`tests/doc/stringify.test.js` and `$`-only `tests/doc/stringify\.test\.js$` both also
matched `/repo/pkg/tests/doc/stringify.test.js`; `^/repo/tests/doc/stringify\.test\.js$`
matched **exactly one file**. vitest's positional is a substring filter that no
anchoring reaches: `tests/doc/stringify.test.js` and `/repo/tests/doc/stringify.test.js`
both pulled `pkg/tests/doc/stringify.test.js` in.

**M3b — the escaping matters, and the earlier example for it was wrong.**
`tests/doc/foo.test.js` as an unescaped regex does **not** reach
`tests/doc/foo_test.js`: jest's default `testMatch` never collects `foo_test.js`
(`0 matches`), and the positional is applied *after* `testMatch`. The real collision
is between two files that both match `testMatch`, and it lives in the directory
components. Measured on `~/.cache/bakeoff-probe/r2i1x`:

```
jest '^/repo/tests/v1.2/a.test.js$'      → ran tests/v1.2/a.test.js AND tests/v1X2/a.test.js
jest '^/repo/tests/v1\.2/a\.test\.js$'   → ran tests/v1.2/a.test.js only
```

**M4 — the ONE-`-t` rule, re-confirmed.** `jest tests/doc -t a -t b`: exit 0, report
written, **nothing ran** (comma-joined into `a,b`). `vitest run … tests/doc -t a -t b`:
exit 1, **no report**, `Expected a single value for option "-t, --testNamePattern
<pattern>"`.

**M5 — jest's ignore flag REPLACES the repository's configuration; vitest's is
ADDITIVE.** This is the finding that decides D3b. Measured on
`~/.cache/bakeoff-probe/r2i1x`, whose `jest.config.js` is
`{ testPathIgnorePatterns: ["/node_modules/", "/tests/skipme/"] }` and whose
`vitest.config.js` excludes `**/tests/skipme/**`:

```
jest tests/                                            → tests/skipme/ignored.test.js NOT run
jest tests/ --testPathIgnorePatterns=/node_modules/ \
            --testPathIgnorePatterns='^/repo/tests/doc/stringify\.test\.js$'
                                                       → tests/skipme/ignored.test.js RAN
vitest run --no-cache tests/                           → tests/skipme/ NOT run
vitest run --no-cache tests/ --exclude=tests/doc/stringify.test.js
                                                       → tests/skipme/ still NOT run
```

Re-emitting `/node_modules/` restores jest's **built-in default**, never the
repository's own entries.

**M5b — and it is not hypothetical on the corpus.** `eemeli/yaml`'s
`config/jest.config.js`, read through `--showConfig` inside the real pinned image
`bakeoff-task-yaml-474-single-newline-empty-value:v1`:

```
testPathIgnorePatterns: ["tests/_utils","tests/json-test-suite/"]
testMatch:              ["**/tests/**/*.{js,ts}"]
roots:                  ["/repo"]
```

No `/node_modules/` at all, and `tests/_utils` matches that `testMatch`. So emitting
`--testPathIgnorePatterns` on this task's p2p run pulls its helper modules into the
regression check — at gate time **and** at grade time, where the result is
`P2P_REGRESSION`, `resolved: False`, on every arm. It also confirms `roots: ["/repo"]`,
which makes D3's mount anchor the right anchor for this task.

**M6 — a positional matching nothing is loud.** `jest tests/nosuchdir` and
`vitest run … tests/nosuchdir`: exit **1** on both, report **written**, zero tests.

**M7 — a scope whose only file is excluded is loud in the same way.** `jest
tests/other --testPathIgnorePatterns=… '^/repo/tests/other/misc\.test\.js$'` and the
vitest `--exclude` equivalent: exit **1**, report written, zero tests.

**M8 — every report's `testResults[].name` is absolute** (`/repo/…`) on both
frameworks, which is `_relpath`'s premise and what lets `executed_names` /
`verify_selected` check the file *and* the name.

**M9 — a jest negative lookahead in the positional works, and only in one spelling.**
This is D3b's mechanism; every variant was measured:

| argv | result |
|---|---|
| `^(?!/repo/tests/doc/stringify\.test\.js$)/repo/tests/` | excludes it — but also drops `pkg/tests/doc/…`; the scope became exact |
| `^(?!/repo/…$)(?!/repo/tests/doc/foo\.test\.js$)/repo/tests/` | both excluded, same scope narrowing |
| `^(?!/repo/…$).*tests/` | **does NOT exclude** — it still ran |
| `^(?!tests/doc/stringify\.test\.js$).*tests/` | **does NOT exclude** |
| `^(?!tests/doc/stringify\.test\.js$)(?!/repo/tests/doc/stringify\.test\.js$).*tests/` | **excludes it, and `pkg/tests/doc/…` still ran** |
| `^(?!tests/doc/…$)(?!/repo/tests/doc/…$)` (no scope segment) | **excludes it**, everything else ran |
| `^(?!tests/doc/…$)(?!/repo/tests/doc/…$).*` (no scope segment) | same |
| `.*tests/` vs bare `tests/` (controls) | identical file sets |

The pattern that emerges — and the reason the single-spelling forms fail — is that
jest tests a positional against **both** the repo-relative and the absolute path and
collects the file if *either* matches. A lookahead naming only one spelling is
defeated by the other. Two lookaheads per file, and a `.*` before the raw scope
prefix so the scope segment keeps its unanchored substring behaviour, is the only
form that excludes the file **and** preserves the scope over-match
`scope_files_outside` exists to catch.

**M10 — vitest's two `--exclude` spellings are not equivalent, and the bare one is
what this design wants.** On `~/.cache/bakeoff-probe/r2i1x`:

```
vitest run --no-cache tests/ --exclude=tests/doc/stringify.test.js
    → pkg/tests/doc/stringify.test.js STILL RAN     (correct: only the dfile is gone)
vitest run --no-cache tests/ --exclude='**/tests/doc/stringify.test.js'
    → pkg/tests/doc/stringify.test.js EXCLUDED      (wrong: drops a second file)
```

The load-bearing fact is that on vitest **the same string is a substring filter as a
positional and a glob as `--exclude`**. That asymmetry is why group 0 excludes exactly
the dfile while group i's positional pulls in the dfile *and* its colliders, which is
the whole of D4.

---

## 2. Design

### D1 — the adapter returns a SEQUENCE of argvs, and each one pairs one file with its own names

`select_args` → `select_argvs`, `p2p_args` → `p2p_argvs`, both returning
`list[list[str]]`. `_Runner` runs the sequence in order and merges the reports.

* **pytest returns a one-element list** and its single element is byte-identical to
  today's. `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated`
  (`tests/test_preflight.py:664`) compares `recorder.commands` against a **literal**
  list-of-one-command, so it fails both if pytest's argv changes and if pytest ever
  emits a second group — the argv-identity gate stays **green without being edited**,
  which is the property the gate exists to have.
* **Node's selection groups by file** in first-seen order. `select_argvs(("a::x",
  "b::y", "a::z"))` is two argvs: `[filter(a), "-t", "^(?:x|z)$"]` and
  `[filter(b), "-t", "^(?:y)$"]`. M1's cross-product is gone.
* **Node's p2p deselect branch splits into 1 + K argvs**, K = distinct files holding a
  deselected id and not already in `ignored`: group 0 is the scope with those files
  excluded and **no `-t`**; group i is one dfile with a negative `-t` naming only that
  file's deselected titles.
* **Node's p2p explicit branch** groups `selected` by file and, per group, filters
  `deselected` down to that group's file. A quarantined id naming a file that is not
  in `selected` is dropped rather than emitted — that file is collected by no group,
  and a pattern for it would be a no-op that reads like a deselection.

**The groups are disjoint by refusal, not by construction.** Group 0 excludes every
dfile and each dfile has exactly one group, but on vitest a group-i positional also
pulls in any file whose path contains the dfile's (M2, M3). D4 is what makes the
disjointness true, and it is stated that way rather than as a property of the argv.

### D2 — the ONE-`-t` rule is preserved, unchanged, and it was never about one invocation

M4's hazard is a *second `-t` inside one argv*. Every argv this design emits carries
**at most one**, so the rule is untouched. The helper that builds it stays a single,
guarded place:

```python
    def _argv(self, head, pattern):
        # `-t ''` is not the same argv. An empty pattern matches every name,
        # which is what group 0 of the deselect branch wants -- and is also
        # exactly what a builder that failed to fill the pattern in would emit.
        if not pattern:
            return list(head)
        return [*head, "-t", "^" + pattern]
```

`        if not pattern:` (eight leading spaces) stays the mutation anchor for
`"runners: emit selection and deselection as two -t flags"`, unchanged and reachable
(group 0 takes it on every deselect-branch run).

### D3 — the per-file positional is per flavour, because M3 says it has to be

New field on `_NodeFlavour`: `_file_filter_is_regex: bool` (`True` for jest, `False`
for vitest), and:

```python
    def _file_filter(self, path: str) -> str:
        if self._file_filter_is_regex:
            return "^" + _js_escape(_REPO_MOUNT + "/" + path) + "$"
        return path
```

* jest: `^/repo/tests/doc/stringify\.test\.js$` — measured exact (M3), including
  against a tail-colliding path, and the escape is load-bearing for a directory
  component holding a regex metacharacter (M3b).
* vitest: the bare repo-relative path — measured, the absolute form buys nothing.

`_REPO_MOUNT` is already spelled in this module and already has a drift test
(`test_the_node_repo_mount_constant_does_not_drift`); M5b confirms `roots: ["/repo"]`
on the corpus's node task. If a task's jest config reports files under some other
absolute prefix, the anchored positional matches nothing — exit 1, a report of zero
tests (M6), `KIND_NOTHING_RAN`, a preflight NO-GO. Loud, not silent.

### D3b — the exclusion channel is per flavour too, and on jest it is not a flag at all

M5 and M5b: `--testPathIgnorePatterns` **replaces** the repository's own
`testPathIgnorePatterns`, and the corpus's only node task declares two entries and no
`/node_modules/`, so the flag would pull that task's `tests/_utils` helpers into the
regression check on every gated and graded p2p run. Re-emitting `/node_modules/`
restores jest's built-in default and nothing the repository declared. So:

* **vitest** excludes through `--exclude=<repo-relative path>` — additive (M5), and
  the bare spelling rather than the `**/` one (M10).
* **jest** excludes through **negative lookaheads folded into the positional**, two
  per file (relative and absolute spellings), with the raw scope prefix kept
  unanchored behind a `.*` so `scope_files_outside` keeps the semantics it was
  measured against (M9). `JEST` loses `_ignore_flag` and `_ignore_prefix` entirely,
  and with them the "jest collects out of the tree's own `node_modules`" hazard the
  re-emission existed to patch.

This also closes a **pre-existing** defect: `ignored` (preflight's p2p-before
f2p-module ignore) goes through the same channel, so today's single use of
`--testPathIgnorePatterns` on a jest task — which already clobbers that task's config
— stops happening.

Alternatives rejected for D3b:

* **Read the effective `testPathIgnorePatterns` out of `jest --showConfig` and
  re-emit it.** The graded tree is the tree the *model* edited, so the argv would
  become a function of a file the model can rewrite — the model choosing the command
  it is graded by. It also breaks gated-equals-graded byte identity in a
  model-controlled way, and puts configuration into an argv.
* **Refuse a jest task whose config declares any `testPathIgnorePatterns` beyond
  `/node_modules/`.** Measured to refuse `eemeli/yaml` — the verification vehicle and
  the corpus's only node task (M5b). Disqualified by its own first test case.
* **Anchor the scope at the mount** (`^(?!…$)/repo/tests/`, which M9 shows also
  works). It makes `scope_files_outside` unable to fire on jest — a check that looks
  like a measurement and cannot be one — and it silently narrows the graded regression
  check relative to today's argv.

### D4 — the un-anchorable vitest positional is refused, per task, over EVERY node report preflight reads

M2/M3 leave one residual, vitest-only: if one executed file's repo-relative path is
contained in another's, vitest's per-file positional selects both, and group i's
negative `-t` then deselects the colliding file's same-named tests.

The refusal is computed over the **union of `files_run` across every node run
preflight makes** — f2p-before, p2p-before, f2p-after, p2p-after and the scoped
after-run — not over the scoped run alone. `select_argvs` is per-file too, so the f2p
check carries the same positional (M2: the vitest f2p-shaped negative run executed
`pkg/tests/doc/stringify.test.js`), and an explicit-`tests.p2p` task makes no scoped
run at all and would otherwise be covered by nothing. `last_report` is overwritten by
the next `run`, so the union is accumulated at each classification site.

One key, one writer, three absences — `None` for pytest (whose `files_run` is always
`None`), `None` when no node run reported a file list at all, `[]` for
measured-and-clean. The predicate is the adapter's:

```python
    def file_filter_matches(self, declared_path: str, candidate_path: str) -> bool:
        if self._file_filter_is_regex:
            return candidate_path == declared_path
        return declared_path in candidate_path
```

Both arguments are rootdir-relative, which is what `_relpath` produces. The vitest
branch **over-approximates**: `a in b` fires for `tests/a.test.js` versus
`src/tests/a.test.js`, which is a genuine vitest collision, and does not fire for
`tests/foo.test.js` versus `tests/xfoo.test.js`, which is not one. That is the right
shape for a refusal. jest's branch can never fire, which the tests pin.

### D5 — the reports are merged, and one missing report poisons the whole merge

New protocol method `merge_reports(reports: list[dict | None]) -> dict | None`.

* pytest returns `None` unconditionally — a claim, not a gap.
* node, **empty list**: `None`. Nobody ran, so nobody counted. A `{"testResults": []}`
  would be a *claim* that a run happened and executed nothing, which `classify` reads
  as `KIND_NOTHING_RAN`. Unreachable through `_Runner` (D6 rule 1 raises first), but
  the protocol is public and its own test calls it.
* node, **any element `None`**: `None`. A partial merge would report a green suite for
  a run half of which produced no evidence.
* node, all present: `{"testResults": <concatenated in group order>}` plus
  `"numPendingTests": <sum>` **only when every group reported an int**; omitted
  otherwise, so `parse_deselected` answers `None` rather than a sum with a hole in it.
* Every other key is dropped on purpose, `success` most of all — `classify` already
  refuses to consult it, and a scalar carried over from one group is a value no
  invocation produced.
* A single-element list returns **that dict unchanged**, not a rebuilt copy.

`Outcome.files_run` therefore becomes the union across groups, sorted.

**What the deselection count now means.** Under today's single invocation a
deselected title matching in three files makes three `numPendingTests`; under the
per-file grouping only the paired file's tests are skipped. The floor invariant
`p2p_deselected < p2p_deselect_requested` ⇒ staleness still holds and gets *tighter*,
because the count stops being inflated by cross-file over-deselection.

### D6 — `_Runner` runs the sequence, and the merged exit code is ordered, not positional

`_Runner.run(argv_groups: list[list[str]])`:

1. Raise `ValueError` on an empty list, and `TypeError` if any element is a `str`.
   `select_argvs(())` is `[]`, and a `_Runner` asked to run nothing must not fall
   through to the bare runner — which is what today's `select_args(()) == []` does,
   silently running the **whole suite** as the selection. The `str` guard exists
   because the new signature would otherwise splat a flat argv character by character
   into a command made of letters, and run it.
2. Per group: `rm -f <report_path>` (unchanged — a config error writes no file, so a
   leftover would stand in as this group's evidence), then
   `["timeout", str(self.timeout_s), *self.runner, *extra]`, appended to
   `self.last_argvs`, exec'd, and its report read.
3. `self.last_report = adapter.merge_reports(reports)` when `report_path()` is set,
   else `None`.
4. Return the group's own result when there is one group; otherwise a `_MergedResult`
   dataclass (`exit_code`, `stdout`, `stderr`, `duration_ms`) with stdout/stderr
   joined in group order by `"\n"`, `duration_ms` the sum of
   `getattr(r, "duration_ms", 0)` (defaulted because `test_preflight.py`'s `_Recorder`
   stub returns an object without it), and `exit_code` chosen by an **ordered** rule:

   ```python
   #: Exit codes that OUTRANK an ordinary test failure when one check is several
   #: commands. `grader._check_f2p` (line 1204), `_check_p2p` (1346) and
   #: `_check_command` (1135) branch on 124 (`grader._TIMEOUT_EXIT`) and on
   #: 125/126/127/137 (`grader._INFRA_EXITS`) BEFORE they read a failure -- and
   #: group 0 of a node deselect-branch run exits 1 whenever its post-exclusion
   #: scope is empty (measured 2026-09-02, both frameworks, with a report of zero
   #: tests). A "first non-zero wins" merge would therefore report 1 for a check
   #: whose SECOND command was killed at the bound, and `timed_out` -- a distinct
   #: recorded fact -- becomes unrepresentable for every multi-group node check.
   #: Spelled here rather than imported: preflight importing grader.py would be a
   #: dependency in the wrong direction, and the two consumers are named above so
   #: a reader can check the pair.
   _OUTRANKING_EXITS: tuple[int, ...] = (124, 137, 127, 126, 125)
   ```

   Rule: the first member of `_OUTRANKING_EXITS` that any group produced; else the
   first non-zero in group order; else 0.

`last_argv` becomes a read-only property returning `self.last_argvs[0]` (or `[]`), so
`last_timeout_s` is unchanged. Every group carries the same `timeout` prefix. The two
problem messages that print `' '.join(runner.last_argv)` (`preflight.py:1672`,
`preflight.py:1988`) print **every** group, one per line.

**The suite timeout is per invocation, not per check.** A node deselect-branch p2p run
is bounded by `budget.suite_timeout_s` × (1 + K) of wall clock.
`GradeRecord.suite_timeout_s` still says what bound each command carried, which is the
true statement; dividing the budget by K would make the gated bound a function of the
quarantine, so two tasks declaring the same number would get different ones.
**K is not recorded anywhere** — §7 names it and §6 files it in `TASKS.md`.

### D7 — what the refusals become

| guard | today | after |
|---|---|---|
| `node_adapter.validate_id_set` | raises on two declared ids sharing a `fullName` across files | **no-op**, docstring citing M2 and naming the same-file item. Kept as a protocol method (`tasks.py:1248` calls it for every framework). |
| preflight `duplicate_full_names` **problem** | NO-GO | **removed**. |
| preflight `duplicate_full_names` **evidence** | list of `"'name' in fileA and fileB"`, `None` when no scoped run was made or it wrote no report | **unchanged, key for key** — including its cross-file-only meaning. Its *content* grows (§3.4's version note, point 4). The comment gains a sentence saying the key is cross-file only and naming the item that owns the same-file half. |
| — | — | **new** `ambiguous_file_filters` (D4): a list of `"'<a>' also selects <b>"`, `None` for pytest and when no node run reported a file list. Non-empty is a NO-GO. |

### Alternatives rejected (selection design)

* **Keep one invocation and narrow the refusal to names that could actually be
  deselected.** `oracle._derive` computes the quarantine at grade time from the two
  reference runs' failed ids, so every executed p2p node id is a potential target.
* **A flag that scopes a name pattern to a file.** Neither framework has one; M2 shows
  the positional + `-t` AND *is* that flag once the invocation holds one file.
* **Two invocations always** (scope-minus-dfiles, plus one run over all dfiles with the
  union negative pattern). Leaves the same defect one size down: two deselected ids in
  two files, one of whose titles also exists in the other, still over-deselect.
* **One invocation per file for the whole scope.** Multiplies every gated and graded
  p2p run by the number of test files in `tests.paths`.
* **`--exclude` / `--testPathIgnorePatterns` instead of a positional for group i.**
  Path-level only; it can remove a file, not a *test* from a file.
* **Anchoring vitest's positional.** Measured impossible (M3). Refused per task (D4).
* **Renaming titles in the task repo**, as `HARVESTING.md` advises today. That is a
  patch to the repository under test, inside the diff the model is graded on.

---

## 3. Exact edits

### 3.1 `bakeoff/src/bakeoff/runners/__init__.py`

* `select_args(node_ids) -> list[str]` → `select_argvs(node_ids) -> list[list[str]]`.
  Docstring: *"Argvs that run exactly these ids and nothing else. One per FILE, not
  one per call: measured 2026-09-02, two positionals plus one union `-t` executed a
  test declared for neither pairing — the positionals and the name pattern are ANDed
  across the whole invocation, never zipped. Empty ids give an EMPTY LIST, not an argv
  with no filter; `_Runner.run` raises on it rather than falling through to running
  the whole suite as the selection."*
* `p2p_args(...) -> list[str]` → `p2p_argvs(...) -> list[list[str]]`. Keep the
  existing docstring's ONE-`-t` measurement and the `selected`/`scope`/`deselected`/
  `ignored` contract; replace the *"It also matches by NAME only"* paragraph with the
  per-file grouping rule, the 1 + K shape, and a pointer to D3b for why the exclusion
  channel differs per framework.
* New `merge_reports(reports) -> dict | None` (D5) and
  `file_filter_matches(declared_path, candidate_path) -> bool` (D4), with the
  docstring content named there.
* `validate_id_set` docstring: drop the *"it is where the node adapters refuse two ids
  sharing a `fullName` across files"* sentence.
* `Outcome`: unchanged.

### 3.2 `bakeoff/src/bakeoff/runners/pytest_adapter.py`

* `select_argvs(node_ids)` → `[list(node_ids)] if node_ids else []`;
  `p2p_argvs(...)` → `[<today's p2p_args body>]`.
* `merge_reports(reports)` → `None`, claim-not-gap docstring.
* `file_filter_matches(a, b)` → `a == b`, docstring: *"pytest's positional IS a path,
  so two distinct paths never collide and this can never fire. It exists so the
  ambiguity check reads one rule for every framework."*

### 3.3 `bakeoff/src/bakeoff/runners/node_adapter.py`

Module docstring: replace the whole `THE FILE HALF OF A NODE ID IS THROWN AWAY`
paragraph with M1/M2/M3/M9's story, the 1 + K shape, and the D3/D3b asymmetries.

`_NodeFlavour`'s field block, **given literally** because it is the one place the
rest of §3.3 leaves a decision. `_ignore_flag` has no default today, so `JEST`
dropping it means the field must gain one, and `_file_filter_is_regex` must sit after
every field that has none or the dataclass raises at import. `_ignore_prefix` is
deleted outright — only `JEST` ever set it, and jest now emits no ignore flag at all:

```python
    name: str
    runner_marker: str
    no_cache_args: tuple[str, ...]
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
```

`VITEST` keeps `_ignore_flag="--exclude"` and takes the `False` default; `JEST` sets
`_file_filter_is_regex=True`, passes no `_ignore_flag`, and its constant's docstring
paragraph about re-emitting `/node_modules/` is replaced by M5/M5b and the reason no
flag is used. The old `_files` and `_names` helpers are replaced by `_group_by_file`
and `_titles_in`.

Literal code — transcribe, do not invent. The mutation anchors in §3.8 name lines from
this block:

```python
    def _file_filter(self, path: str) -> str:
        # jest: a JS RegExp tested against BOTH the repo-relative and the
        # absolute path, so only a mount-anchored, escaped, `$`-terminated
        # pattern names one file. Measured 2026-09-02: `tests/doc/a.test.js$`
        # also matched `/repo/pkg/tests/doc/a.test.js`, and an unescaped
        # `^/repo/tests/v1.2/a.test.js$` also matched `tests/v1X2/a.test.js`.
        # vitest: a substring filter no anchoring reaches -- the absolute form
        # matched the tail-colliding file too -- so the bare relative path is
        # the honest spelling and D4 refuses the trees it cannot separate.
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
        if selected:
            groups = []
            for path, titles in self._group_by_file(selected):
                # A file in `ignored` gets no group at all -- that flag's one
                # caller is preflight's p2p run at the START state, whose whole
                # point is that the f2p module must not be collected.
                if path in ignored:
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
        head = [*self._scope_positionals(scope, excluded),
                *self._exclude_args(excluded)]
        # Group 0: everything under scope EXCEPT the files holding a
        # deselection, and no `-t` at all. Then one group per such file,
        # carrying only that file's own deselected titles -- which is what
        # keeps a quarantine of `a::works` off `b::works`.
        groups = [self._argv(head, "")]
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
```

`validate_id_set`: body becomes `return None`, docstring rewritten to say the
cross-file rule is gone (per-file grouping, M2) and that the same-file case is
*unrepresentable* as two ids, which is `TASKS.md`'s open item and not this method's.

### 3.4 `bakeoff/src/bakeoff/preflight.py`

* `_Runner.__init__`: `self.last_argv: list[str] = []` → `self.last_argvs:
  list[list[str]] = []`, plus the `last_argv` property (D6). Keep the existing
  "initialised HERE, at construction" reasoning in the comment.
* `_MergedResult` and `_OUTRANKING_EXITS` — module level, next to `_Runner`.
* `_Runner.run` per D6; `select` and `pass_to_pass` pass `adapter.select_argvs(...)` /
  `adapter.p2p_argvs(...)`.
* Seed `evidence["ambiguous_file_filters"] = None` in the block that seeds
  `scope_files_run` / `scope_files_outside`, with the same "None is not measured on
  this path" comment.
* **The D4 accumulator**, declared beside `problems`/`evidence` at the top of
  `preflight`, and applied at every `runner.classify(...)` site (f2p-before,
  p2p-before, f2p-after, p2p-after, scoped-after):

  ```python
    #: Every rootdir-relative file ANY node run in this gate loaded or executed.
    #: Accumulated at each classification site rather than read at the end,
    #: because `_Runner.last_report` is overwritten by the next `run` -- and
    #: over EVERY run rather than the scoped one alone, because `select_argvs`
    #: is per-file too (the f2p check carries the same positional) and an
    #: explicit-`tests.p2p` task makes no scoped run at all.
    seen_files: set[str] = set()
    files_measured = False

    def _note(outcome):
        nonlocal files_measured
        if outcome.files_run is not None:
            files_measured = True
            seen_files.update(outcome.files_run)
        return outcome
  ```

  Each site becomes `outcome = _note(runner.classify(result))` **where one is
  bound today** — and three of the five are not. `preflight.py:1646`, `1778` and
  `1791` read `runner.classify(x).kind …` inline, so the substitution there is
  `_note(runner.classify(green)).kind == KIND_PASSED`, never a restructuring into a
  local. `_note` returns its argument precisely so that works, and leaving those
  three lines' shape alone is what keeps the "classified IMMEDIATELY after its own
  invocation" comments that sit on them true.
* In the scoped-p2p block (`preflight.py:1866-1920`): delete the `if duplicates:` /
  `problems.append(...)` block; keep the `duplicates` computation and the
  `evidence["duplicate_full_names"]` assignment verbatim; add a comment saying it is
  now evidence (per-file grouping made the hazard it named unreachable), that it is
  **cross-file only**, and that `TASKS.md`'s same-file item owns the other half.
* **The D4 check**, at the function's own indentation immediately before the final
  `return PreflightResult(...)` — after both branches have rejoined, needing no
  container:

  ```python
    ambiguous = (
        [f"{a!r} also selects {b}"
         for a in sorted(seen_files) for b in sorted(seen_files)
         if a != b and adapter.file_filter_matches(a, b)]
        if files_measured else None
    )
    evidence["ambiguous_file_filters"] = ambiguous
    if ambiguous:
        problems.append(
            "one declared test file's selection filter also selects another "
            "executed file: " + ", ".join(ambiguous)
            + ". Measured 2026-09-02: vitest's positional is a SUBSTRING filter "
            "that no anchoring reaches -- `/repo/tests/doc/a.test.js` still "
            "matched `/repo/pkg/tests/doc/a.test.js` -- so the per-file grouping "
            "this harness selects and deselects with would carry a name pattern "
            "into a file it does not name, and a quarantine would silently "
            "remove a test from the regression check. jest anchors at the mount "
            "and cannot hit this. Rename or move one of the files in the task "
            "repo, or narrow tests.paths; see taskset/HARVESTING.md."
        )
  ```

  The implementer confirms with `grep -n '    if ambiguous:'` that the line is unique
  in the file before adding the mutation entry that anchors on it.
* Both `' '.join(runner.last_argv)` sites → every group, one per line.
* `PREFLIGHT_VERSION` `"13"` → `"14"`, with a `#: 13 -> 14:` block naming **four**
  things:
  1. node selection and deselection became per-file, so a v13 NO-GO for a cross-file
     duplicate `fullName` is stale;
  2. the jest per-file positional gained its mount anchor and its escape, and jest
     stopped emitting `--testPathIgnorePatterns` at all, so a v13 verdict was taken
     with a filter that could match a second file and with the repository's own ignore
     list replaced;
  3. `ambiguous_file_filters` is a new refusal, so a v13 PASS on a **vitest** task with
     contained test paths is stale in the other direction;
  4. **`duplicate_full_names`' CONTENT grows for an unchanged task.** Under v13 the
     scoped run deselected every f2p title globally, so an identically-titled test in
     another file never reached a terminal status and never entered `executed_names`;
     under v14 group 0 runs those files unfiltered and the key lists collisions the v13
     verdict for the identical task did not. A reader diffing two cached verdicts
     across the bump — which verification step 7 asks for — must not read that growth
     as a regression. The key's shape and meaning are unchanged; what changed is what
     the gate could see.

  **No pytest verdict changes** under 13 → 14: `pytest_adapter` emits one group whose
  argv is the v13 argv, and `file_filter_matches` cannot fire there.

### 3.5 `bakeoff/tests/test_preflight.py` — the scripted node fixture

The largest transcription risk in this change: the fixture keys its scripted reports on
an **invocation ordinal**, and 1 + K groups per check breaks that keying silently —
group 1 of the p2p-**before** run would increment `p2p_runs` to 2 and be served the
**p2p_after** report, producing green tests over the wrong evidence.

* `_ScriptedContainer._node_timeout` (`tests/test_preflight.py:1108-1147`):
  * the f2p discriminator becomes `select = adapter.select_argvs(tuple(self.tests.f2p))`
    and matches when `rest[:len(g)] == g` for **any** `g in select` — the adapter
    still owns the argv rule, and a list-of-lists can no longer prefix-match `rest`
    the way the old flat list did;
  * the p2p and scoped discriminators stop counting invocations and start recognising
    a **check boundary**: a run whose argv carries no `-t` (group 0) opens a new check
    and increments `p2p_runs`/`scoped_runs`; a run that carries a `-t` and a single
    file positional while a check is open is a *continuation* and is served the same
    key;
  * the docstring's "the container that answers its **five** suite runs" becomes "its
    five suite *checks*, each of which is now one or more commands", and states that
    every group of a check is served the same report — the merge is what turns them
    back into one.
* `_node_container` (`tests/test_preflight.py:3129-3130`) hardcodes
  `_FakeTests(..., framework="vitest")`. Add a `framework="vitest"` parameter to the
  helper and thread it into `_FakeTests` (which already has the field, defaulted to
  `"pytest"`), so tests 33/34 can build the jest variant.
* `test_last_timeout_s_is_none_before_any_invocation` (`tests/test_preflight.py:1644`)
  calls `runner.run([])`. Under D6 that raises. Edit to `runner.run([[]])`, and add to
  its docstring: *"the argument is now a SEQUENCE of argvs, and one empty argv is
  still one invocation — `[]` means no groups at all, which `run` refuses."*
* `test_the_runner_routes_every_argv_and_verdict_through_its_adapter`
  (`tests/test_preflight.py:2853`): its fake `_Adapter` defines `select_args` /
  `p2p_args` returning flat lists. Rename to `select_argvs` / `p2p_argvs` returning
  list-of-lists and add `merge_reports` / `file_filter_matches`, or it becomes a
  protocol-violating stub the moment `_Runner` calls the new names.

### 3.6 `bakeoff/tests/test_integration_node_task.py`

`tests/test_integration_node_task.py:393` calls
`runner.run(["--config", "/tmp/does-not-exist.mjs"])`. Under the new signature this
iterates a list of **strings** and splats each character into an argv — it does not
raise, it runs a command made of letters. Edit to
`runner.run([["--config", "/tmp/does-not-exist.mjs"]])`; D6's `TypeError` guard is the
backstop for the next one.

### 3.7 `bakeoff/src/bakeoff/grader.py` and `oracle.py`

No behavioural edit — both go through `_Runner.select` / `_Runner.pass_to_pass`.
Verify `_capture` reads only `exit_code`, `stdout`, `stderr` (it does today).

`GRADER_VERSION` `"8"` → `"9"`: the **graded argv** changed for a node task in three
ways — the deselect branch is now 1 + K commands whose deselections no longer cross
files; the jest per-file positional is mount-anchored and escaped; and jest no longer
emits `--testPathIgnorePatterns`, which under 8 replaced the repository's own ignore
list on the one run that used it. A grade produced under 8 for a node task was made
against a regression check from which identically-titled tests in other files had been
silently removed, so `p2p_failed_node_ids` and `resolved` can both differ. No pytest
grade changes.

`ORACLE_VERSION` `"4"` → `"5"`: `_derive`'s two reference runs are now the per-file
argv sequence, so a quarantine cached under 4 for a node task was derived from runs in
which a deselection crossed files — it can name an id that never needed quarantining
and miss one that did. No pytest quarantine changes; a cached verdict must be
re-derived rather than re-read.

### 3.8 `bakeoff/scripts/mutation_check.py`

Remove two entries whose anchors go stale (the check fails hard on a stale anchor):

* `"preflight: let one quarantine silently deselect two tests"` — anchor
  `"                    if duplicates:"`; the guard is gone.
* `"tasks: accept two declared ids that share a full name"` — anchor
  `"            if first != path:"`; `validate_id_set`'s body is gone.

Add three, each with a comment in the file's style naming the measurement it protects:

| name | file | anchor | mutant | selector |
|---|---|---|---|---|
| `runners: spell the jest file filter so it can match a second file` | `src/bakeoff/runners/node_adapter.py` | `            return "^" + _js_escape(_REPO_MOUNT + "/" + path) + "$"` | `            return path` | `tests/test_runners.py -k mount_anchored` |
| `runners: let a deselected file also run unfiltered in the scope group` | `src/bakeoff/runners/node_adapter.py` | `        excluded = [*ignored, *dfiles]` | `        excluded = [*ignored]` | `tests/test_runners.py -k own_group` |
| `preflight: accept a file filter that selects a second executed file` | `src/bakeoff/preflight.py` | `    if ambiguous:` | `    if False:` | `tests/test_preflight.py -k selects_a_second_executed_file` |

Both node anchors appear **verbatim** in §3.3's literal code at the indentation shown.
The preflight anchor appears verbatim in §3.4's literal block at four spaces; the
implementer confirms uniqueness with `grep -n` before adding the entry.

`"runners: emit selection and deselection as two -t flags"` is **kept unchanged** —
`        if not pattern:` survives verbatim in `_argv`.

The per-file *selection* pairing (M1's cross-product) is pinned by unit tests and
deliberately not by a mutation anchor: no one-line revert restores the cross-product
without also destroying the file half, so an anchor would report CAUGHT for the wrong
reason — the same argument `executed_names`' docstring makes about the
`if report is None:` anchor's indentation. The jest dual-lookahead spelling is pinned
the same way, by test 13.

---

## 4. Tests, by name, with what each asserts

### `bakeoff/tests/test_runners.py`

1. `test_node_select_argvs_pair_each_file_with_only_its_own_names` — two ids in two
   files give two argvs; neither pattern contains the other file's title. Docstring
   carries M1.
2. `test_node_select_argvs_group_each_file_once_and_in_first_seen_order` — ids
   `a::x, b::y, a::z` give exactly two argvs, `a` first, `x|z` in one pattern.
3. `test_node_select_argvs_on_nothing_are_no_groups_not_a_match_all` —
   `select_argvs(()) == []` on both flavours; docstring names what `[]` used to mean.
4. `test_jest_file_filter_is_mount_anchored_and_vitest_is_a_bare_substring` — literals
   `"^/repo/tests/doc/stringify\\.test\\.js$"` and `"tests/doc/stringify.test.js"`.
   Mutation selector `mount_anchored`.
5. `test_a_tail_colliding_path_is_matched_by_vitests_filter_and_not_by_jests` —
   `file_filter_matches("tests/doc/a.test.js", "pkg/tests/doc/a.test.js")` is `True`
   for `VITEST`, `False` for `JEST`, `False` for pytest.
6. `test_the_jest_file_filter_escapes_a_regex_character_in_a_directory_component` —
   `JEST._file_filter("tests/v1.2/a.test.js")` under `re.search` matches
   `/repo/tests/v1.2/a.test.js` and **not** `/repo/tests/v1X2/a.test.js`, while the
   unescaped string matches both. Docstring records M3b, including that the positional
   is applied **after** `testMatch`, so a non-test-file collider is not the hazard and
   the `foo_test.js` example was not one.
7. `test_node_p2p_argvs_deselect_branch_puts_each_deselected_file_in_its_own_group` —
   `scope=("tests/",)`, `deselected=("tests/a.test.js::works","tests/b.test.js::works")`
   gives three argvs; group 0 excludes both files and carries no `-t`; groups 1 and 2
   name one file each with only that file's title. Mutation selector `own_group`.
8. `test_node_p2p_argvs_deselect_group_zero_carries_no_dash_t` — no `-t` at all, not
   `-t ''`.
9. `test_node_p2p_argvs_explicit_branch_pairs_selection_and_deselection_per_file` —
   `selected=("a::x","b::y")`, `deselected=("a::q",)`: two argvs, only `a`'s carries
   `(?!(?:q)$)`.
10. `test_a_quarantined_id_naming_an_uncollected_file_emits_no_pattern_for_it` —
    explicit branch, `deselected=("z::q",)` with `z` not in `selected`: no argv
    mentions `q`, no extra group.
11. `test_no_node_argv_group_ever_carries_two_dash_t` — over both branches and both
    flavours, `argv.count("-t") <= 1`. Docstring carries M4.
12. `test_an_ignored_file_gets_no_group_of_its_own_and_is_excluded_from_the_scope_group`
    — `ignored=("tests/f.test.js",)` with the same file in `deselected`: it appears
    only as an exclusion. This is preflight's p2p-**before** shape.
13. `test_jest_excludes_through_the_positional_with_both_path_spellings` — group 0's
    jest positional for `scope=("tests/",)` and one dfile is exactly
    `"^(?!tests/a\\.test\\.js$)(?!/repo/tests/a\\.test\\.js$).*tests/"`, and the argv
    contains **no** `--testPathIgnorePatterns`. Docstring carries M5, M5b and M9: the
    flag replaces the repository's config, `eemeli/yaml` declares two entries and no
    `/node_modules/`, and a single-spelling lookahead was measured not to exclude.
14. `test_vitest_excludes_through_the_bare_exclude_flag_not_a_glob` — group 0's vitest
    argv carries `--exclude=tests/a.test.js` and the scope positional is the raw
    `"tests/"`. Docstring carries M10.
15. `test_the_jest_scope_segment_stays_unanchored_so_the_scope_check_can_still_fire` —
    the group-0 positional contains `".*tests/"` and not `"/repo/tests/"`. Docstring
    carries M9's last rows and names `scope_files_outside`.
16. `test_group_zero_is_byte_identical_to_todays_argv_when_nothing_is_excluded` —
    `p2p_argvs(selected=(), scope=("tests/",), deselected=(), ignored=())` is
    `[["tests/"]]` on both flavours.
17. `test_the_scope_precedes_the_exclusions_and_the_pattern_follows_them` — the
    existing order test, updated for group 0. Still pins jest's greedy-yargs order for
    whatever flags group 0 carries.
18. `test_node_merge_reports_concatenate_test_results_and_sum_pending`.
19. `test_node_merge_reports_of_a_group_that_wrote_no_report_is_None` — `classify` of
    that is `KIND_ENVIRONMENT`.
20. `test_node_merge_reports_of_nothing_is_None_not_an_empty_run` — `merge_reports([])`
    is `None`; docstring says `{"testResults": []}` would be a *claim* that a run
    happened and executed nothing, which `classify` reads as `KIND_NOTHING_RAN`.
21. `test_node_merge_reports_omit_the_pending_count_when_a_group_did_not_report_one` —
    `parse_deselected` then answers `None`.
22. `test_node_merge_reports_of_one_report_return_it_unchanged` — identity.
23. `test_pytest_merge_reports_is_None_and_that_is_a_claim`.
24. `test_pytest_select_argvs_and_p2p_argvs_are_exactly_one_group_each` — both
    branches, byte-identical to the strings the existing
    `test_pytest_p2p_args_*_is_byte_identical_to_todays_argv` tests assert, wrapped in
    a one-element list. (Those three are renamed `…_argvs_…`, their literals wrapped,
    and their docstrings gain a sentence saying the sequence shape is what the identity
    property is now stated over.)
25. `test_node_validate_id_set_no_longer_refuses_a_cross_file_duplicate` — `None` for
    both flavours on `("a.test.js::works", "b.test.js::works")`; docstring cites M2.
26. `test_no_adapter_method_is_a_stub_any_more` — existing, updated: `select_argvs(())
    == []`, `p2p_argvs(selected=(), scope=("tests/",), deselected=(), ignored=()) ==
    [["tests/"]]`, `merge_reports([]) is None`, `file_filter_matches("a", "a") is True`.

### `bakeoff/tests/test_tasks.py`

27. `test_two_declared_ids_sharing_a_full_name_across_files_are_refused` (line 3037) is
    **inverted and renamed** to `…_now_load`: the same manifest loads with no
    `TaskError`; docstring says the file half is now carried into the argv (M2) and
    points at `TASKS.md`'s same-file item.
28. `test_two_names_in_the_ONE_file_are_not_refused` — unchanged.

### `bakeoff/tests/test_preflight.py`

29. `test_a_cross_file_duplicate_full_name_is_recorded_and_no_longer_a_problem` — the
    scoped run's report carries two files sharing a `fullName`;
    `evidence["duplicate_full_names"]` still lists them in today's exact string shape
    and `result.ok` is `True`. (The existing NO-GO assertion at line 3233 moves here;
    if that test asserts only the evidence, keep its name and drop the problem
    assertion.)
30. `test_duplicate_full_names_is_recorded_when_there_are_none` (line 3237) — unchanged.
31. The two `duplicate_full_names is None` tests (lines 3269, 3483) — unchanged.
32. `test_a_file_filter_that_selects_a_second_executed_file_is_refused` — a **vitest**
    task whose runs report `tests/doc/a.test.js` and `tests/vendor/tests/doc/a.test.js`
    (both under `tests/`, so `scope_files_outside` stays `[]` and this isolates the new
    check): the problem fires and `ambiguous_file_filters` names the pair. Mutation
    selector `selects_a_second_executed_file`.
33. `test_ambiguous_file_filters_cannot_fire_on_jest` — the same report under a jest
    manifest (via `_node_container(framework="jest")`) gives `[]` and `result.ok`.
34. `test_ambiguous_file_filters_is_measured_off_the_f2p_run_too` — a task with an
    explicit `tests.p2p` (so no scoped run is made at all) whose **f2p** run reports the
    colliding pair: the problem still fires. This is the gap a scoped-only version had.
35. `test_ambiguous_file_filters_is_None_when_no_node_run_reported_files` — a pytest
    task, and a node task whose runs wrote no report; both `None`.
36. `test_every_argv_group_carries_the_suite_timeout_prefix` — a node deselect-branch
    preflight: every recorded command starts `["timeout", "<n>"]`, and the p2p checks
    record more than one command each.
37. `test_a_problem_message_names_every_argv_group_that_ran` — the p2p-not-green
    message contains both group 0's and group 1's command text.
38. `test_running_no_argv_groups_raises_rather_than_running_the_bare_runner` —
    `_Runner(...).run([])` raises `ValueError`; docstring names what falling through
    would have done.
39. `test_running_a_flat_argv_raises_rather_than_splatting_it` —
    `_Runner(...).run(["--config", "x"])` raises `TypeError`. Docstring: the old
    signature's shape reaching the new one runs a command made of letters, silently.
40. `test_the_merged_exit_code_prefers_a_timeout_over_an_ordinary_failure` — two
    groups, the first exiting 1 (M7's empty-scope shape) and the second 124: the merged
    `exit_code` is **124**. Docstring names `grader._TIMEOUT_EXIT` and `timed_out`.
41. `test_the_merged_exit_code_prefers_an_infra_code_over_an_ordinary_failure` — 1 then
    127 gives 127.
42. `test_the_merged_exit_code_is_the_first_non_zero_when_none_outrank` — 0 then 1 then
    2 gives 1.
43. `test_a_single_group_returns_the_containers_own_result_object` — identity, so no
    existing single-command path changes shape.
44. `test_last_timeout_s_reads_the_first_group` — `last_argv` is `last_argvs[0]`.
45. The `assert len(bounded) == 7` count (line ~1594) — **verify** it is still 7. It is
    a pytest task, one group per check. If it is not, the cause is a pytest path that
    grew a group, which is a defect and not a number to update.

### `bakeoff/tests/test_integration_node_task.py` (marked `integration`, `task_image`)

46. `test_a_cross_file_duplicate_full_name_gates_and_selects_only_its_own_file` —
    copies the `fixtures/node_task/` template as the existing test does, writes an
    **extra** `tests/calc_dup.test.js` whose test title equals the p2p test's
    (`keeps subtracting elsewhere`) and which passes on both sides, then gates.
    Asserts: `result.ok`; `duplicate_full_names` names the collision;
    `ambiguous_file_filters == []`; and the duplicate file's test appears in the p2p
    run's executed set — the thing the old global deselection silently removed.
47. `test_integration_node_task.py:295`'s `duplicate_full_names == []` — unchanged. The
    shared fixture is **not** modified, so `fixtures/node_task/README.md` needs no edit.

---

## 5. Verification

Run from `bakeoff/`, with `.venv/bin/python`.

**Every count below is a DELTA over whatever HEAD carries when this lands, not an
absolute.** Round 2's items commit sequentially into one tree, so the baselines quoted
here (`1539 passed, 62 deselected`; 160 mutation entries) are the round-start numbers
and will already have moved. Item 3 lands before this one and adds **+15 tests** and
**+4 mutation anchors**, so if item 3 is the only thing in between, the numbers to
expect are `1554 + <this item's new tests>` and `164 - 2 + 3 = 165`. Re-read the
actual baseline from HEAD before running, and treat a mismatch as a question about
what else landed rather than as a failure of this change.

1. **Unit.** `.venv/bin/python -m pytest tests/ -v`. Baseline `1539 passed, 62
   deselected`; expect that plus the new tests, zero failures.
2. **The logger gate.** `.venv/bin/python scripts/verify_logger.py` → `GATE PASSED`.
3. **Integration.** `.venv/bin/python -m pytest -v -m integration
   --basetemp="$HOME/.cache/bakeoff-pytest"`.
4. **Mutation, solo.** `.venv/bin/python scripts/mutation_check.py`. Baseline 160/160
   (AST count over `MUTATIONS`); expect **161/161**.
5. **The verification vehicle.** `~/.cache/bakeoff-probe/taskset/yaml-474-single-newline-empty-value`
   exists and is the gated manifest.

   ```bash
   mkdir -p ~/.cache/bakeoff-probe/ts-yaml-474-wide/yaml-474-single-newline-empty-value-wide
   cp ~/.cache/bakeoff-probe/taskset/yaml-474-single-newline-empty-value/task.yaml \
      ~/.cache/bakeoff-probe/taskset/yaml-474-single-newline-empty-value/reference.diff \
      ~/.cache/bakeoff-probe/ts-yaml-474-wide/yaml-474-single-newline-empty-value-wide/
   # edit that task.yaml: task_id -> yaml-474-single-newline-empty-value-wide
   #                      tests.paths -> ["tests/doc/"]     (then again with ["tests/"])
   .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
     --task-set ~/.cache/bakeoff-probe/ts-yaml-474-wide \
     --tasks yaml-474-single-newline-empty-value-wide
   ```

   **`tests/doc/` is a NARROWER scope than the one measured.** `w7b-yaml-474.md:153`
   records the `-wide` probe as `tests.paths: ["tests/"]`; `tests/doc/` is chosen here
   to isolate the four duplicate-name collisions from the `tests/properties.ts`
   fast-check sweep the corpus row at `HARVESTING.md:743` records, and the claim "and
   nothing else was wrong" is **not** inherited — it was made about `["tests/"]`.
   Run **both**: `["tests/doc/"]` must PASS, and `["tests/"]` is the one whose
   2026-09-02 NO-GO message is inherited verbatim, so a PASS there is the stronger
   result and a failure there needs its cause named before it is accepted.

   Expected for `["tests/doc/"]`: exit 0, `preflight PASS`. In
   `~/.cache/bakeoff/preflight/yaml-474-single-newline-empty-value-wide.json`:
   `"duplicate_full_names"` holds the four entries (recorded, not refused),
   `"ambiguous_file_filters": []`, `"scope_files_run"` names both
   `tests/doc/stringify.ts` and `tests/doc/createNode.ts`, `"ok": true`.
6. **No regression on the narrow gated node task.** Re-gate
   `yaml-474-single-newline-empty-value` from `~/.cache/bakeoff-probe/taskset` with
   `--force-preflight`; expect PASS in ≈60 s, as measured 2026-09-02.
7. **No pytest verdict moves.** Re-gate `sqlglot-6927-dremio-trycast` from
   `~/.cache/bakeoff-probe/ts-sqlglot-6927-dremio-trycast` with `--force-preflight` and
   diff its evidence against the stored block for everything except
   `preflight_version` and `ambiguous_file_filters`. On a **node** task the same diff
   must tolerate `duplicate_full_names` growing (§3.4's version note, point 4).
8. **The grader still grades, under the new version.** The event log and the isolated
   task-set copy both exist:
   `~/.cache/bakeoff-probe/eventlog-yaml-474-single-newline-empty-value` (two runs,
   `grades/grades.jsonl` with two lines) and
   `~/.cache/bakeoff-probe/ts-yaml-474-single-newline-empty-value` (`task.yaml` +
   `reference.diff`).

   ```bash
   .venv/bin/python scripts/grade.py \
     --event-log ~/.cache/bakeoff-probe/eventlog-yaml-474-single-newline-empty-value \
     --taskset ~/.cache/bakeoff-probe/ts-yaml-474-single-newline-empty-value
   ```

   The stored lines are `grader_version: "6"`, `oracle_version: "3"`,
   `graded_under_preflight_version: "11"` — **not** 8/4/13 — so the new lines sit
   beside **v6** ones. Expect two new lines at `grader_version: "9"`, reference
   `resolved: true`, empty-patch `grade_failure: empty_patch`, `framework: jest` on
   both, matching the 2026-09-02 self-grade's verdicts. If the log is ever lost,
   rebuild it with `~/.cache/bakeoff-probe/self_grade.py <taskset> <task_id>
   <eventlog>` (`w7b-yaml-474.md:130-136`) before grading.
9. **A direct re-measure in the base image of the argv shapes this plan introduces**,
   under `$HOME` (never `/private/tmp` — it bind-mounts empty), against a tree whose
   `jest.config.js` declares a non-default `testPathIgnorePatterns`:
   * group 0 with a scope and one exclusion, jest:
     `jest '^(?!tests/a\.test\.js$)(?!/repo/tests/a\.test\.js$).*tests/'` — the named
     file gone, everything else run, **and the tree's own ignore list still honoured**;
   * group 0 with an **empty** scope (preflight's p2p-before/after), jest: the same
     pattern with `.*` in place of `.*tests/`;
   * the vitest equivalents with `--exclude=tests/a.test.js`.

---

## 6. Docs

* **`bakeoff/taskset/HARVESTING.md:364`** — rewrite the bullet. Cross-file duplicate
  `fullName`s are no longer a screening exclusion. Two rules replace it:
  * *"No two test **files** under `tests.paths` may have one repo-relative path
    contained in another's — on **vitest** only."* jest's per-file positional is
    anchored at the mount and escaped; vitest's is a substring no anchoring reaches
    (M3). Preflight refuses it as `ambiguous_file_filters`.
  * *"Two tests with the same full name in the **same** file cannot be named as two ids
    at all"* — a node id is `<file>::<fullName>` with no positional index. Open,
    tracked in `TASKS.md`; such a repository is still excluded.
  Replace the `node -e` snippet with one that reports **same-file** collisions and
  contained file paths, and drop the cross-file DUP report.
* **New `HARVESTING.md` bullet, jest only:** *"A jest task's own
  `testPathIgnorePatterns` is honoured."* Under `GRADER_VERSION` 9 the harness emits no
  `--testPathIgnorePatterns` at all, so the repository's config decides — a change from
  8, where the one run that used the flag replaced it.
* **`HARVESTING.md:547`** — drop the duplicate-`fullName` half of the "narrowing
  `tests.paths` to dodge a submodule or a duplicate-`fullName` problem" sentence.
* **`HARVESTING.md:743`**, the `eemeli/yaml` row — the "cross-file duplicate
  `fullName`s … force `tests.paths` down to one file" clause becomes a record of what
  *used* to force it, dated.
* **`docs/BUILDING-A-TASK-SET.md`** — grep for `fullName` / `duplicate` and update any
  repetition; `HARVESTING.md` remains the specification.
* **`TASKS.md`** — tick item 1 and fold in the outcome. Amend the same-file item's
  opening clause (*"a different gap from the cross-file one broadening 7 closed"*),
  since that refusal is gone. **File a new P2 item:** *"A node check is several
  commands and the record does not say how many."* `GradeRecord.suite_timeout_s` says
  600 for a p2p check bounded by 600 s × (1 + K) of wall clock, and K is a function of
  the f2p set and the quarantine. `SCHEMA_VERSION` may not move for it in this item.
* **`tasks/todo.md`** — a review section: what was measured, what the refusal narrowed
  to, the jest ignore-flag finding, and the K-invocation cost.
* **No manifest key is added**, so the worked-example `task.yaml` comments do not move.
* **No `CLAUDE.md` edit** (the round's global constraint forbids it on this branch).
  Sentences to add later:
  * *"A node selection is one invocation per file. `-t` matches `fullName` and the
    positionals are ANDed across the whole invocation, never zipped — measured
    2026-09-02, two positionals plus a union `-t` executed a test declared for neither
    file. The ONE-`-t`-per-argv rule is unchanged: vitest rejects a second `-t` with no
    report, jest comma-joins it into a pattern matching nothing."*
  * *"jest's file positional is a `RegExp` tested against BOTH the repo-relative and
    the absolute path, and vitest's is a substring no anchoring reaches. So jest
    excludes a file with two negative lookaheads folded into the positional and vitest
    with `--exclude`, and preflight refuses a vitest task whose executed files include
    one path contained in another."*
  * *"`--testPathIgnorePatterns` REPLACES a jest config's own value, and re-emitting
    `/node_modules/` restores jest's built-in default rather than the repository's —
    measured on `eemeli/yaml`, which declares `tests/_utils` and no `/node_modules/`.
    The harness therefore emits that flag nowhere."*

---

## 7. What this does NOT do

* **Same-file duplicate `fullName`s** (round-2 item 11). `duplicate_full_names` keeps
  its exact shape and its cross-file meaning; item 11 adds
  `same_file_duplicate_ids` beside it. The reason the split is here: a same-file
  pair collapses to the *identical* node id string, so it is a **manifest
  representation** problem while this item is a **selection argv** problem — and
  `validate_id_set` becoming a no-op is the evidence, since a loader comparing
  `(fullName, path)` pairs cannot see a pair whose `path` agrees.
* **`_check_p2p`'s missing `not_run` branch** (item 8).
* **A `fast-check` determinism probe** (item 12).
* **Preflight's observed suite duration** (item 7) — `_MergedResult` sums `duration_ms`
  because it must, not because anything records it.
* **Recording K.** Nothing in `RunRecord` or `GradeRecord` says how many commands a
  check was, so `suite_timeout_s: 600` cannot be read as the check's wall-clock bound.
  Filed in `TASKS.md`; `SCHEMA_VERSION` does not move here.
* **The scope prefix's own semantics.** The raw declared prefix is preserved behind a
  `.*` on jest and unchanged on vitest, so `scope_files_outside` keeps the behaviour it
  was measured against and its mutation anchor is untouched.
* **Closing vitest's substring positional.** Measured impossible; refused instead.
* **Refusing a suffix collider a SUBMISSION introduces.** `ambiguous_file_filters` is
  computed against the **preflight** tree; a model that adds a test file whose path
  contains a declared one's is not caught by it. The graded p2p run would then partly
  deselect the new file — a narrower version of the defect this item closes, reachable
  only by a submission that adds such a file.
* **Splitting `budget.suite_timeout_s` across groups.**
* **Any pytest behaviour.** One group, today's argv, today's verdicts.
* **Any `RunRecord` / `GradeRecord` field.**

---

## 8. Open questions — closed by review 1

* **Q1 (rename `duplicate_full_names`)** — **No.** The key's content and meaning are
  unchanged; a rename churns four `test_preflight.py` assertions, one integration
  assertion and every cached node verdict's stored evidence for zero information, and a
  version bump makes a rename *survivable*, not free. Adopted, plus the ruling's
  addition: the evidence comment gains a sentence saying the key is cross-file **only**
  and naming the item that owns the same-file half (§3.4).
* **Q2 (`suite_timeout_s × (1 + K)`)** — **Accepted**, with both conditions adopted: the
  ordered exit-code merge is mandatory rather than optional (D6 rule 4, tests 40–42),
  and K's absence from the record is named in §7 and filed in `TASKS.md` (§6).
* **Q3 (measure the ambiguity off the f2p run too)** — **Yes, as one key with one
  writer.** D4 now accumulates `files_run` across every node report preflight reads;
  test 34 pins the explicit-`tests.p2p` case the scoped-only version left uncovered.

---

## Review 1 → changes

All 16 findings addressed; **15 adopted in full, 1 disputed in part** (finding 13's
first bullet, with evidence; its other two bullets adopted). Every disputed or
re-measured claim below was re-run in `bakeoff-eval-agent:base-node-22` under `$HOME`.

| # | change |
|---|---|
| 1 | **Adopted, and it changed the design.** Reproduced the reviewer's measurement (M5), then found the fact that settles the choice among their three options: `eemeli/yaml`'s own `--showConfig` reports `testPathIgnorePatterns: ["tests/_utils","tests/json-test-suite/"]` with **no** `/node_modules/`, and `tests/_utils` matches its `testMatch` (M5b). That disqualifies option (c) — it would refuse the verification vehicle — and option (a) — the argv would become a function of a file the model can edit, i.e. the model choosing the command it is graded by. Took option (b), measured into a specific spelling: M9 shows a lookahead works **only** with two spellings per file (relative and absolute, because jest tests the positional against both) and with the scope kept unanchored behind `.*`, since the anchored form also defeats `scope_files_outside`. New §2 D3b, literal code in §3.3 (`_exclude_args`, `_scope_positionals`), tests 13/14/15, a new `HARVESTING.md` bullet, the `GRADER_VERSION` note, and a `CLAUDE.md` sentence. `JEST` loses `_ignore_flag`/`_ignore_prefix`, and the pre-existing p2p-before clobber goes with them. |
| 2 | **Adopted.** New §3.5 spells the `_ScriptedContainer._node_timeout` rework: `select_argvs` prefix-matched against **any** group, and p2p/scoped keyed on a check boundary (a `-t`-less group-0 argv opens a check; a `-t`-carrying single-file argv continues it) instead of an invocation ordinal. The docstring's "five suite runs" becomes "five suite checks, each one or more commands", and states that every group of a check is served the same report. |
| 3 | **Adopted.** §3.5 names the `framework=` parameter on `_node_container`, threaded into `_FakeTests` (which already has the field). Tests 33/34 use it. |
| 4 | **Adopted.** §3.5 names the edit to `test_last_timeout_s_is_none_before_any_invocation` (`run([])` → `run([[]])`) with the docstring sentence distinguishing "one empty argv" from "no groups". The old claim that all three `last_timeout_s` tests stay unedited is gone; test 38 is now its honest companion. |
| 5 | **Adopted.** §3.6 names `test_integration_node_task.py:393` and §3.5 names `test_preflight.py:2853`'s `_Adapter` fake. D6 rule 1 gains the `TypeError` guard for a flat argv, pinned by test 39. |
| 6 | **Adopted.** §3.3 is now literal code, including `        excluded = [*ignored, *dfiles]` at eight spaces. The preflight anchor moved to `    if ambiguous:` at four spaces (the check now sits before the final `return`, since D4 spans both branches), and §3.8 tells the implementer to `grep -n` for uniqueness before adding the entry. |
| 7 | **Adopted.** D6 rule 4 is an ordered merge with `_OUTRANKING_EXITS = (124, 137, 127, 126, 125)`, a docstring naming `grader._TIMEOUT_EXIT` (lines 1204/1346/1135) and `grader._INFRA_EXITS` as the consumers it exists for, and tests 40–42. |
| 8 | **Adopted, ruling taken.** D4 accumulates `files_run` across every node run preflight makes via one `_note` helper and one writer; §3.4 spells it. Test 34 covers the explicit-`tests.p2p` case. Both smaller points are in: the preflight-tree limitation is a §7 bullet, and the over-approximation's two directions are stated in D4. |
| 9 | **Adopted.** The `foo.test.js`/`foo_test.js` claim is removed — re-measured, jest's `testMatch` never collects `foo_test.js`, so the positional never sees it. Replaced with M3b, a fresh measurement whose collider *is* a jest test file: unescaped `^/repo/tests/v1.2/a.test.js$` runs `tests/v1.2/a.test.js` **and** `tests/v1X2/a.test.js`; escaped runs one. Test 6 renamed, and its docstring says the discriminator is applied after `testMatch`. |
| 10 | **Adopted.** Re-measured with the collider file present: the vitest negative-`-t` run executes `pkg/tests/doc/stringify.test.js` too. M2 is restated with that result and its cause (the earlier reading predated the collider in the tree; the positive polarity was exact only by title coincidence). D1's *"disjoint by construction"* is now *"disjoint by refusal"*, naming D4 as the precondition. |
| 11 | **Adopted.** M5/M10 corrected: the two `--exclude` spellings differ, the bare one is right, and the reason is stated where it belongs — on vitest the same string is a **substring filter** as a positional and a **glob** as `--exclude`, which is why group 0 excludes exactly the dfile while group i pulls in colliders. In D3b, in `_exclude_args`' docstring, and in test 14. |
| 12 | **Adopted.** §1 now says the refusal fires **after all five suite runs**, on the scoped run's report, and names the `w7b-yaml-474.md:166` inference it came from. |
| 13 | **Partly disputed.** Bullet 1 is **wrong**, checked directly: `~/.cache/bakeoff-probe/eventlog-yaml-474-single-newline-empty-value` exists (`index.jsonl`, two `runs/*.json`, `grades/grades.jsonl` with two lines, `grades/artifacts/…/v6/`), and **seven** `ts-*` directories exist, including `ts-yaml-474-single-newline-empty-value` with `task.yaml` and `reference.diff`. Step 8 needs no rebuild prerequisite. What the finding did surface is a real error, now fixed: the stored lines are `grader_version: "6"`, `oracle_version: "3"`, `graded_under_preflight_version: "11"` — so the new v9 lines sit beside **v6** ones, not v8. The rebuild command is kept as a fallback. Bullets 2 and 3 **adopted**: step 5 says plainly that `tests/doc/` is narrower than the measured `["tests/"]`, drops "and nothing else", and runs **both** scopes with `["tests/"]` carrying the inherited NO-GO. |
| 14 | **Adopted.** Moot for jest in the new design (no flag at all), and spelled literally for vitest: `_exclude_args` is guarded on `self._file_filter_is_regex or not paths`, and group 0's head is `[*_scope_positionals(scope, excluded), *_exclude_args(excluded)]` with `excluded = [*ignored, *dfiles]`. Test 13 asserts the jest argv carries **no** `--testPathIgnorePatterns`. |
| 15 | **Adopted.** D5 and `merge_reports`' literal body give `merge_reports([])` → `None`, with the reason: `{"testResults": []}` is a *claim* that a run happened and executed nothing, which `classify` reads as `KIND_NOTHING_RAN`. Test 20 pins it; test 26 no longer relies on the unspecified case. |
| 16 | **Adopted.** The `PREFLIGHT_VERSION` 13 → 14 note gains a fourth point: `duplicate_full_names`' **content** grows for an unchanged task, because group 0 now runs the f2p files' siblings unfiltered where v13's global deselection skipped them. Verification step 7 says to tolerate it, and §7 states that the key keeps its shape and meaning while its content grows. |

### Review 2

**APPROVE, 0 open findings** (16 of 16 resolved: 15 addressed, 1 withdrawn as the
reviewer's own error), with the design change finding 1 forced independently
re-measured in the pinned image — the two-spelling lookahead, the `.*`-guarded scope
segment, the `scope=()` degenerate form, the tail collider's survival into
`scope_files_outside`, the escape's necessity, and the preservation of both
frameworks' ignore configuration all reproduced. Two non-blocking implementer notes,
both folded in above rather than left in the review:

| note | change |
|---|---|
| **Dataclass field defaults and ordering.** `_ignore_flag: str` has no default today, so `JEST` dropping it needs the field to gain one; and `_file_filter_is_regex: bool = False` must sit after every field without a default or the dataclass raises at import. §3.3 gave literal code for every method and prose for the fields, which left this as the one decision an implementer had to make. | §3.3's field block is now **literal**, in declaration order, with both defaults spelled and `_ignore_prefix` deleted outright. The comment on `_ignore_flag` says `_exclude_args` guards on the flavour and never on the string's emptiness, so a flavour that forgot to set it emits nothing rather than `=<path>`. |
| **Three of the five `_note` sites bind no name.** `preflight.py:1646`, `1778` and `1791` read `runner.classify(x).kind …` inline, so "each site becomes `outcome = _note(runner.classify(result))`" is not a mechanical substitution there. | §3.4 now says the substitution at those three is `_note(runner.classify(green)).kind == KIND_PASSED`, that `_note` returns its argument precisely so that works, and that the three lines must not be restructured into locals — the "classified IMMEDIATELY after its own invocation" comments sitting on them depend on their shape. |

One further change, not from a finding: §5 now opens by saying **every count in it is a
delta over whatever HEAD carries when this lands**. Round 2 commits sequentially into
one tree and item 3 lands first (+15 tests, +4 anchors), so the round-start baselines
quoted in steps 1 and 4 will already have moved, and a mismatch is a question about
what else landed rather than a failure of this change.
