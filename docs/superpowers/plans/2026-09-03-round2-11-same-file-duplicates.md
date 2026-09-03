# Round 2, item 11 — a declared node id that names two tests in its own file

**Status:** plan, revision 1 (after review 1). Not yet implemented.
**Sequenced AFTER round-2 item 1** (`2026-09-03-round2-1-node-file-selection.md`, FINAL —
read that plan, not the working tree, for anything this one extends). It must land first:
this plan extends `preflight._note` (item 1's D4 accumulator) and states its refusal over a
report shape item 1's `merge_reports` defines.
**Area:** `runners/__init__.py`, `runners/node_adapter.py`, `runners/pytest_adapter.py`,
`preflight.py`, `scripts/mutation_check.py`, three test files, one new fixture pair,
`taskset/HARVESTING.md`, `taskset/click-3360-write-usage-empty-args/task.yaml`,
`docs/BUILDING-A-TASK-SET.md`, `TASKS.md`, `tasks/todo.md`.
**Version constants that move:** `PREFLIGHT_VERSION` only, **read-and-add-one** (see §3.6).
`SCHEMA_VERSION`, `GRADER_VERSION` and `ORACLE_VERSION` do **not** move — no record field
changes shape, and no gated or graded argv changes by one byte (§4, §8).

Every measurement below was taken **2026-09-02** in `bakeoff-eval-agent:base-node-22`
(node v22.23.2, vitest 3.2.7, jest 30.5.0 — its CLI still answers `30.4.2`, which
`images.py:220` and the fixture README both record), against a scratch tree at
**`~/.cache/bakeoff-probe/r2i11`** — under `$HOME`, because a tree under `/private/tmp`
bind-mounts into the image as a **silently empty directory**. The raw reports are at
`~/.cache/bakeoff-probe/r2i11/out/`; the two rewritten fixture candidates §3.5 asks for are
already at `~/.cache/bakeoff-probe/r2i11/fixtures/`. Review 1 reproduced every decisive
measurement in the same image into `~/.cache/bakeoff-probe/r2i11/verify/`.

The scratch tree is two projects (`projv` for vitest, `projj` for jest, the jest one
carrying `jest.config.cjs` with `rootDir: __dirname, testEnvironment: 'node'`), each with
`tests/dup.test.js`:

```js
describe('outer', () => {
  it('adds', () => { expect(1 + 1).toBe(2) })   // line 3 — passes
  it('adds', () => { expect(1 + 1).toBe(3) })   // line 4 — fails
  it('subs', () => { expect(2 - 1).toBe(1) })
})
```

(the vitest copy imports `describe`/`it`/`expect` from `vitest`; the jest copy uses its
injected globals), plus a `tests/other.test.js` holding a third `outer adds` so the
cross-file case stays visible beside the same-file one.

---

## 1. The measured defect

A node id is `<file>::<fullName>` **with no positional index**. Two tests with the same
`fullName` in one file therefore collapse to the *identical* id string: they cannot be told
apart in a manifest, in a `-t` pattern, in `Outcome.failed_ids`, or in a `GradeRecord`.

`TASKS.md`'s item (grep `SAME file are unguarded`) states the loader half. The two guards
that exist compare `(fullName, path)` **pairs** and fire only when the `path` differs —
`node_adapter.validate_id_set` (which item 1 turns into a no-op, because per-file argv
grouping closes the cross-file hazard it named) and preflight's `duplicate_full_names`
check, whose loop is `first = seen.setdefault(name, path); if first != path:`
(`preflight.py:1885-1888`). A same-file pair, where `path` agrees, passes both **by
construction**. After item 1 the cross-file NO-GO is gone entirely and
`duplicate_full_names` is evidence only, so nothing in the gate reads this shape at all.

### The measurements

**M11.1 — the report carries two entries and nothing on the runs this harness makes
separates them.** One `testResults` entry for `tests/dup.test.js` holding **three**
`assertionResults`, of which two have the identical `fullName` `outer adds`, the identical
`title` `adds` and the identical `ancestorTitles` `["outer"]`, at statuses `passed` and
`failed`. On **vitest** the assertion object has no `location` key at all on a default run
(its keys are exactly `ancestorTitles, duration, failureMessages, fullName, meta, status,
title`) — it emits one **only** when a `file:line` positional turns task-location capture on
(measured: `out/v_line.json` carries `"location": {"line": 3, "column": 5}` on every
assertion). On **jest** `location` is present and **`null`** on a default run, and becomes
`{"column": 3, "line": 2}` / `{"column": 3, "line": 3}` with `--testLocationInResults`. The
harness's argv passes neither flag on either framework, so on the runs it actually makes
there is no field that separates the pair.

**M11.2 — `-t` with an exact-anchored pattern runs BOTH, on both frameworks.**
`-t '^(?:outer adds)$'` against that file: vitest exit **1**, jest exit **1**, and both
reports read `numTotalTests 3, numPassedTests 1, numFailedTests 1, numPendingTests 1` with
the two `outer adds` entries at `passed` and `failed` and `outer subs` skipped. This is the
selection half of the defect: the declared f2p id runs two tests, one green and one red.

**M11.3 — the deselection skips both, and the count agrees.**
`-t '^(?!(?:outer adds)$)'`: exit **0** on both, `numPendingTests: 2` for **one** requested
id. So `preflight`/`grader`'s floor invariant (`p2p_deselected < p2p_deselect_requested`
⇒ staleness) cannot fire on it: two tests really were skipped.

**M11.4 — a deselected duplicate is not in `_TERMINAL_STATUSES`.** Its status is `skipped`
on vitest and `pending` on jest. `executed_names` yields only `passed`/`failed`, so the
**scoped p2p run — the run this item's brief proposed reading — cannot see an f2p
duplicate at all**, because that run deselects the f2p ids. The f2p half has to be counted
off the f2p runs. This is why §2 D2's accumulator spans every node run rather than the
scoped one.

**M11.5 — the harness is blind by construction, verified against the captured reports**
(`for_framework(fw).classify(...)` over `same_file_dup.<fw>.json`, both frameworks, and
re-run by review 1): `kind` is `failed`, `failed_ids` is the single-element
`['tests/dup.test.js::outer adds']` for **two** failing-and-passing assertions, `files_run`
is `('tests/dup.test.js',)`, and
`verify_selected(report, ('tests/dup.test.js::outer adds',), adapter)` returns the empty
set — every existing channel reports a clean, unambiguous run.

**M11.6 — a positional index in the id would not help, measured.** vitest accepts a
`file:line` positional and it does separate the pair: `tests/dup.test.js:3` ran the
**passing** one (exit 0, the other two skipped) and `tests/dup.test.js:4` ran the
**failing** one (exit 1). jest reads `tests/dup.test.js:3` as a path regex, collects
**0 tests** and exits 1. Neither framework has a line-scoped *de*selection, which is what
the grade-time quarantine needs. So an id spelling that could *represent* the pair still
could not select one, on one framework, and could deselect one on neither.

### What each of those costs

| gate/grade claim | what a same-file duplicate does to it |
|---|---|
| preflight "the declared f2p tests fail at the start state" | satisfied by **whichever** of the two fails (M11.2). Nothing says which, and a permanently-green twin is indistinguishable from the test the PR fixes. |
| preflight "the declared f2p tests pass after the reference fix" | satisfied by **both** passing. The conjunction of the two claims is not "this test went red → green" any more; it is "at least one of an unnamed pair was red, and both are green now". |
| `Outcome.failed_ids`, `GradeRecord.f2p_failed_node_ids` | one id for two tests (M11.5) — the record cannot say which failed. |
| grader check 6's quarantine | a quarantine of the id deselects both (M11.3) — a healthy sibling silently leaves the regression check, with the deselection count agreeing. |
| `f2p_before_not_run` / `verify_selected` | empty. Both duplicates ran, so nothing is missing (M11.5). |

---

## 2. Design

### D1 — refuse a task whose **declared** id resolves to more than one assertion result in its own file

`preflight` computes, per node run it reads, the `<file>::<fullName>` ids that **more than
one** assertion result in a **single `testResults` entry** reached a terminal verdict for.
The intersection of that map with `tests.f2p ∪ tests.p2p` is a NO-GO; the whole map is
recorded as `evidence["same_file_duplicate_ids"]`.

**Why the boundary is at "declared", with an undeclared duplicate recorded and not
refused.** The argument is about what the gate can *prove* from its own runs:

* For a **declared** duplicate the gate proves the ambiguity outright. Its f2p-before run
  shows two terminal assertions under the one declared id at `passed` and `failed` (M11.2);
  its f2p-after run shows two at `passed`. So the two conjuncts the gate exists to establish
  — "these declared tests were red" and "these declared tests are green" — are demonstrably
  satisfied by *different tests*, and no field downstream can say which: `failed_ids`
  collapses them (M11.5), `verify_selected` reports nothing missing, and `p2p_deselected`
  agrees with a deselection of both (M11.3). The ambiguity is proven, it sits inside a claim
  the record makes **about the model**, and it is unfixable in the id space (M11.6). Refusal
  is the only honest outcome.
* For an **undeclared** duplicate — two identically titled tests in a file under
  `tests.paths` that the manifest never names — the gate proves only that both twins ran and
  both passed. The harm needs the id to be in the quarantine, and the quarantine is
  `oracle._derive`'s, computed at **grade** time from **two** reference runs the gate never
  makes. So the gate cannot show the hazard is present, and its one relevant observation
  (both green in the scoped run) is weak evidence that it is absent. Refusing on it means
  refusing repositories over a condition the refusing component cannot evaluate — in the
  round convened to raise yield, and by re-erecting the corpus-wide exclusion at
  `HARVESTING.md:364` that item 1 is dismantling.
* **The residual is real and is a false-success channel, not merely a narrower check.** If
  an undeclared twin does enter the quarantine, `oracle._derive` deselects by node id
  (`oracle.py:266`) and M11.3 says that removes **both** twins from check 6 — so a
  submission that broke the healthy twin is not caught and can be stamped `resolved: True`.
  It is narrow: it needs the twin to fail or flake at the reference state, and `_derive`
  runs the reference suite **twice** where preflight runs it once, so a *flaky* twin is
  reachable even on a task preflight measured green. It is accepted because it is narrow and
  because, once `same_file_duplicate_ids` is recorded, it is **auditable** — and §3.4(c)'s
  comment and §7's `HARVESTING.md` bullet both say where the two halves of that audit live
  (`same_file_duplicate_ids` in the cached preflight verdict, the quarantine in
  `GradeRecord`) and that **nothing joins them automatically**: the intersection is the
  offline reader's to make. A residual named in a plan and nowhere in the artifacts is
  silence, which is the invariant this repo lists first.

The rule that generalises: **refuse where the gate can prove a verdict about the model would
be ambiguous; record — and say where to audit it — where the hazard is only reachable
through a component the gate cannot run.**

Alternatives rejected:

* **Refuse any same-file duplicate under `tests.paths`.** Over-broad against a hazard the
  gate cannot evaluate (bullet 2 above), and a new corpus-wide exclusion in the round that
  exists to retire one. (It is *not* rejected for being unobservable: D2's accumulator is a
  union over all five runs, and the f2p runs are exactly where the f2p-titled pairs are
  terminal, so the broad claim is measurable. It is rejected because it refuses on a
  condition preflight cannot evaluate.)
* **Give a node id a positional index (`<file>::<fullName>#2`) or a line (`<file>:12`).**
  Measured impossible to act on (M11.6): jest cannot select by line at all (0 tests, exit 1)
  and neither framework can *deselect* by line, which the quarantine needs. It would also be
  a manifest-visible id spelling change for every node task, for a shape the harness still
  could not select.
* **Disambiguate by `location` and keep the id.** The argv would have to change on **both**
  frameworks: jest needs `--testLocationInResults` (without it `location` is `null`), and
  vitest emits a `location` only when a `file:line` positional turns task-location capture
  on, which also changes what runs (M11.1, M11.6). A gated and graded argv altered for every
  node task to serve one refusal, and a source line is not an identity anyway: it is a
  property of the file, which is what a reference diff and a model's patch edit.
* **Revive `validate_id_set` for this.** It cannot see it. A loader has strings; both
  duplicates *are* the same string. Item 1's §7 says this in the same words, and its no-op
  `validate_id_set` is the evidence.
* **Let the grader catch it.** §4 — it cannot be reached there, and the check would be in
  the wrong place: by grade time the tokens are spent.

### D2 — the count is accumulated over EVERY node run, per suite entry, by MAX

Item 1 introduces one accumulator in `preflight` (its D4) applied at each of the five
`runner.classify(...)` sites, for `files_run`. This plan **extends that same helper**
rather than adding a second traversal, and takes the report explicitly:

* **Every run, not the scoped one.** M11.4: the scoped p2p run deselects the f2p ids, and a
  deselected test is `skipped`/`pending` — not terminal, so `executed_names` never yields
  it. A declared **f2p** duplicate is visible only in the f2p-before and f2p-after runs; a
  declared **p2p** duplicate (explicit `tests.p2p`) only in the p2p runs, which is also the
  task shape that makes no scoped run at all. One writer, five sites, exactly as item 1's
  `files_run` accumulator.
* **Per `testResults` entry, not per report.** Since item 1, a report can be the merge of
  several argv groups (`merge_reports` concatenates `testResults`), so one file *could*
  appear as two entries. Counting per entry can then only **under**-report a duplicate,
  never invent one — and over-reporting is the failure that would refuse a healthy task.
  (The overlapping-groups case is itself refused by item 1's `ambiguous_file_filters`; this
  is belt-and-braces, and it is the cheaper direction to be wrong in.)
* **Combined by MAX across runs, never by sum.** The same test executes in the before-run
  and again in the after-run, so a sum reads *every* ordinary test as a duplicate. This is
  the single most likely transcription error in the change, and test 14 pins it: its
  `f2p_twice=True` shape puts the id at count 2 in **both** f2p reports, so a summing
  implementation yields 4 and the test's `== {…: 2}` fails.
* **Terminal statuses only**, the same `_TERMINAL_STATUSES` rule `executed_names` and
  `verify_selected` read. A test that is `it.skip`-ped in the repository can be neither
  selected nor failed, so it cannot make a verdict ambiguous; and a second copy of the
  "what ran" rule is the thing `executed_names`' docstring already forbids.

### D3 — one rule, one place: `_executed` under `executed_names` and `duplicate_ids`

`executed_names` is refactored onto a private generator `_executed(report)` yielding
`(suite ordinal, relpath, fullName)`; `executed_names` becomes a two-line wrapper and the
new `duplicate_ids(report)` counts within an ordinal. The terminal-status test stays in
exactly one place, which is the property `test_verify_selected_and_executed_names_read_ONE_rule`
already pins for the other reader.

**Placement is load-bearing.** `scripts/mutation_check.py`'s entry *"runners: read a report
that was never written as a clean run"* (`mutation_check.py:1829-1841`) anchors the text
`        if report is None:` (eight leading spaces) and replaces the **first** occurrence,
which must be `classify`'s; its comment says the same text also occurs in
`parse_deselected` and `executed_names`, and that both are *defined below `classify` for
that reason*. `_executed` carries that guard now (and `executed_names` loses it), so
**`_executed` must be defined below `classify`** — immediately above `executed_names`, where
the guard lives today. The occurrence count is unchanged (three: `classify`,
`parse_deselected`, `_executed`), because `duplicate_ids` delegates rather than re-guarding;
only the third one's owner changes, `mutation_check.py`'s comment is updated to name it, and
test 11 pins the general property over the whole module rather than over one pair of
methods.

---

## 3. Exact edits

### 3.1 `bakeoff/src/bakeoff/runners/__init__.py`

**(a)** New protocol method beside `executed_names`:

```python
    def duplicate_ids(self, report: dict | None) -> dict[str, int]:
        """`<file>::<fullName>` -> how many tests in that file answer to it.

        Only ids that MORE THAN ONE assertion in a single `testResults` entry
        reached a verdict for; an id that names exactly one test is absent.
        `{}` for a framework whose ids are unique by construction, and `{}`
        for a report that does not exist -- preflight records THAT absence
        with its own flag, because a report-less run measured nothing and an
        empty map here would say it looked and found nothing.
        """
```

**(b)** `RunnerAdapter`'s class docstring (`runners/__init__.py:103`) says *"the eight node
report shapes this package branches on were captured once and can be replayed forever"*.
This item adds a ninth captured shape which is **not** a new `classify` branch. Replacement
clause, verbatim:

> which matters, because the nine captured node report shapes were measured once and can be
> replayed forever, while re-measuring them needs a network and 73 MB of npm. (Eight of the
> nine are `classify` branches; the ninth is the two-tests-one-name shape `duplicate_ids`
> reads, which `classify` sees as an ordinary failure.)

`Outcome` is unchanged. No other protocol method moves.

### 3.2 `bakeoff/src/bakeoff/runners/pytest_adapter.py`

```python
    def duplicate_ids(self, report):
        """`{}`, a CLAIM and not a gap.

        A pytest node id names exactly one test by construction: two
        functions with the same name in one module shadow each other, a class
        scope is part of the id, and pytest appends an index when two
        parametrized cases would otherwise collide -- so `--deselect
        a.py::test_x` reaches one test and one test only. The node
        frameworks' ids do not: `-t` matches `fullName`, and two
        `it('works')` in one file collapse to the identical `<file>::works`
        (measured 2026-09-02, vitest 3.2.7 and jest 30.5.0 -- an exact
        anchored `-t` ran both, one passing and one failing in the same run).
        """
        return {}
```

### 3.3 `bakeoff/src/bakeoff/runners/node_adapter.py`

Module docstring: a new paragraph after the `THE FILE HALF …` one item 1 rewrites —

> TWO TESTS WITH THE SAME NAME IN ONE FILE ARE ONE ID, and that is unfixable
> here rather than merely unguarded. Item 1 gave each file its own argv, which
> closes the *cross*-file collision; a same-file pair has nothing left to
> separate it. Measured 2026-09-02 (vitest 3.2.7, jest 30.5.0) on a file
> holding two `it('adds')` under one `describe('outer')`: the report carries
> two `assertionResults` with the identical `fullName`, `title` and
> `ancestorTitles`, and on the runs this harness makes neither framework
> reports a `location` beside them -- vitest emits the key only when a
> `file:line` positional turns task-location capture on, and jest's is `null`
> without `--testLocationInResults`. `-t '^(?:outer adds)$'` runs BOTH -- one
> passed and one failed in the same run -- and the deselection skips both with
> `numPendingTests: 2` for one requested id. `classify` then reports ONE
> `failed_id` for the pair and `verify_selected` reports nothing missing, so
> every channel reads clean. `duplicate_ids` is what says so instead, and
> preflight refuses a task whose DECLARED ids are in it.

Then, defined **below `classify`** and immediately above `executed_names` (D3):

```python
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
```

`        if count > 1:` (eight spaces) is a mutation anchor (§3.7); the implementer confirms
with `grep -n` that the text is unique in the file before adding the entry.

### 3.4 `bakeoff/src/bakeoff/preflight.py`

**(a) Seed, in the pre-container block beside `evidence["duplicate_full_names"] = None`
(`preflight.py:889`):**

```python
    #: `None` is "no run this gate made produced a report a duplicate could be
    #: counted in" -- the runner-gate early return below starts no container at
    #: all, and a node run that wrote NO REPORT (measured, what a broken config
    #: gives on both frameworks) counted nothing. `{}` is the WEAKER claim it
    #: looks like: "at least one node run was read, and no id in the runs that
    #: were read names two tests". A run that wrote no report contributes
    #: nothing to it and is named by that run's own exit and kind evidence --
    #: this key does not count absences, and does not pretend to.
    evidence["same_file_duplicate_ids"] = None
```

**(b) Extend item 1's D4 accumulator** — the block declared beside `problems`/`evidence`.
`_note` gains the report as a second parameter, because the ids are counted off the report
and `Outcome` carries no assertion list:

```python
    #: Every `<file>::<fullName>` that MORE THAN ONE assertion in a single
    #: `testResults` entry reached a verdict for, with the largest count any
    #: run reported. Accumulated by MAX and never by SUM: the same test
    #: executes in the before-run and again in the after-run, so a sum reads
    #: every ordinary test as a duplicate.
    #:
    #: Over EVERY node run rather than the scoped one, and that is measured:
    #: the scoped p2p run DESELECTS the f2p ids, and a deselected test is
    #: `skipped` on vitest and `pending` on jest -- neither is terminal, so it
    #: never reaches `executed_names`. A declared f2p duplicate is visible only
    #: in the f2p runs, and a declared p2p one only in the p2p runs, which is
    #: also the task shape (explicit `tests.p2p`) that makes no scoped run.
    same_file_dupes: dict[str, int] = {}
    dupes_measured = False

    def _note(outcome, report):
        nonlocal files_measured, dupes_measured
        if outcome.files_run is not None:
            files_measured = True
            seen_files.update(outcome.files_run)
        # `report_path() is None` is pytest, whose `duplicate_ids` is `{}` as a
        # CLAIM its node ids back -- not an absence. A node run that wrote no
        # report is the absence, and it leaves this flag alone: the flag says
        # "at least one run was counted", never "every run was".
        if adapter.report_path() is None or report is not None:
            dupes_measured = True
            for node_id, count in adapter.duplicate_ids(report).items():
                same_file_dupes[node_id] = max(
                    same_file_dupes.get(node_id, 0), count)
        return outcome
```

Call sites: item 1's §3.4 substitutes `_note(...)` at the five `runner.classify(` sites, of
which **two bind a name** (`red_outcome`, `scoped_outcome`) and **three read `.kind`
inline** — item 1 forbids restructuring those three into locals, so they become
`_note(runner.classify(green), runner.last_report).kind == KIND_PASSED`. The five sites are
`preflight.py:1587`, `1659`, `1791`, `1804`, `1876` (pre-item-1 line numbers, verified
against the file as the complete set of `runner.classify(` call sites; item 1's own §3.4
quotes `1646, 1778, 1791` for the inline three, which is stale). Argument order is safe:
Python evaluates left to right, `_Runner.classify` (`preflight.py:653`) reads
`self.last_report` and never writes it, and only `_Runner.run` assigns it.

**(c) The check**, at the function's own indentation, **immediately after item 1's
`ambiguous_file_filters` block** and before the final `return PreflightResult(...)`
(`preflight.py:2016`):

```python
    evidence["same_file_duplicate_ids"] = (
        dict(sorted(same_file_dupes.items())) if dupes_measured else None)
    # DECLARED ids only, and the boundary is about what THIS component can
    # prove. For a declared id the gate proves the ambiguity from its own runs:
    # the f2p-before run shows two terminal assertions under the one id at
    # `passed` and `failed` (measured), the f2p-after run shows two at
    # `passed`, so red-before and green-after are satisfied by different tests
    # and nothing downstream can say which -- `failed_ids` collapses them and
    # `verify_selected` reports nothing missing. For an UNDECLARED duplicate
    # the gate proves only that both twins ran and both passed; the harm needs
    # the id to reach the quarantine, which `oracle._derive` computes at GRADE
    # time from two reference runs this gate never makes.
    #
    # The accepted residual, stated because a residual nobody records is
    # silence: a flaky undeclared twin CAN be quarantined, `_derive`
    # deselects by node id, and that removes BOTH twins from check 6 -- so a
    # submission that broke the healthy one can still be `resolved: True`. The
    # two halves of the audit are the map above, in this task's cached
    # preflight verdict, and `GradeRecord`'s quarantine; NOTHING joins them,
    # and the intersection is the offline reader's to make.
    declared = frozenset(tests.f2p) | frozenset(tests.p2p)
    declared_dupes = {node_id: count
                      for node_id, count in sorted(same_file_dupes.items())
                      if node_id in declared}
    if declared_dupes:
        problems.append(
            "these declared test ids each name MORE THAN ONE test in their "
            "own file: "
            + "; ".join(f"{node_id!r} names {count} tests"
                        for node_id, count in declared_dupes.items())
            + f". A {adapter.name} id is `<file>::<fullName>` with no "
            "positional index, so two identically titled tests in one file "
            "collapse to the SAME id and there is no way to declare, select "
            "or deselect one of them. Measured 2026-09-02 (vitest 3.2.7, jest "
            "30.5.0) on a file holding two tests titled `outer adds`: `-t "
            "'^(?:outer adds)$'` ran BOTH on both frameworks -- one passed and "
            "one failed in the same run -- so the red-before check is "
            "satisfied by whichever of them fails and the green-after check by "
            "both passing, with nothing saying they are the same test; and a "
            "quarantine of the id deselects both, with p2p_deselected "
            "AGREEING, because two tests really were skipped. Declare a "
            "different test id, or cut the task from a PR whose tests are "
            "uniquely titled; see taskset/HARVESTING.md."
        )
```

`    if declared_dupes:` (four spaces) is the second mutation anchor (§3.7); the implementer
confirms uniqueness with `grep -n` before adding the entry.

No `problem_codes` entry is added. The only consumer of `problem_codes` is
`scripts/grade.py:251`'s `preflight_refusal`, which maps `SCOPE_COLLECTS_NOTHING` onto
`NotGradedReason.SCOPE_COLLECTED_NOTHING` and everything else onto
`NotGradedReason.PREFLIGHT_FAILED` — `run_matrix.py` never reads the field. So a new code
with no new `NotGradedReason` member is invisible to every offline reader, and a new member
is a `GradeRecord` vocabulary change this item disclaims (§8). `PREFLIGHT_FAILED` already
says the true thing: the manifest is wrong and its author must edit it.

### 3.5 The fixture — `bakeoff/tests/fixtures/node_reports/`

Two new files, `same_file_dup.vitest.json` and `same_file_dup.jest.json`, captured
2026-09-02 and already rewritten at `~/.cache/bakeoff-probe/r2i11/fixtures/` — copy those,
or regenerate with the recipe below. Both hold exactly one `testResults` entry named
`/repo/tests/dup.test.js` with three `assertionResults`: `outer adds` **passed**,
`outer adds` **failed**, `outer subs` passed. Suite `status` is `failed` with a **non-empty**
`assertionResults`, so `classify` reads it as `KIND_FAILED` and not as a load error. The only
absolute paths in either file are `/repo/tests/dup.test.js`, `/node_modules/...` stack
frames and one `node:internal/process/task_queues` — the same shape the committed sixteen
carry. (The vitest file's `numTotalTestSuites: 2` beside a single `testResults` entry is
genuine vitest output, reproduced under review; it is not a corrupted rewrite.)

**Capture recipe for `same_file_dup`** — this is *not* the README's recipe for the other
eight, and §3.5(b) below records the difference in the README itself:

```bash
W="$HOME/.cache/bakeoff-probe/r2i11"          # under $HOME: /private/tmp mounts EMPTY
# projv/tests/dup.test.js and projj/tests/dup.test.js as in this plan's header
docker run --rm -v "$W:/repo" -w /repo/projv bakeoff-eval-agent:base-node-22 sh -lc \
  'rm -f /repo/out/v_full.json; /node_modules/.bin/vitest run --globals --no-cache \
     --reporter=json --outputFile=/repo/out/v_full.json tests/dup.test.js'      # exit 1
docker run --rm -v "$W:/repo" -w /repo/projj bakeoff-eval-agent:base-node-22 sh -lc \
  'rm -f /repo/out/j_full.json; /node_modules/.bin/jest -c jest.config.cjs --json \
     --outputFile=/repo/out/j_full.json tests/dup.test.js'                      # exit 1
# rewrite: text.replace("/repo/projv", "/repo").replace("/repo/projj", "/repo")
#          -- and NO node_modules step; the base image already resolves the
#          runners at /node_modules, which is where the stack frames point.
# then:    json.dump(json.loads(text), out, indent=2, sort_keys=True) + "\n"
```

**(a)** `tests/fixtures/node_reports/README.md` gains a row in the shape table —

| `same_file_dup` | `tests/dup.test.js` (a file with two tests titled `outer adds`) | 1 | 1 | yes |

**(b)** and its heading, its opening sentence and its provenance paragraph are corrected,
because the ninth pair is neither a `classify` branch nor captured the same way. Verbatim:

> `# The nine captured node report shapes`
>
> `node_adapter.classify` branches on eight of them and on nothing else; the ninth
> (`same_file_dup`) is not a new branch — it is what two identically titled tests in one
> file produce, which `classify` reads as an ordinary failure and which `duplicate_ids` is
> what sees. They are replayed from disk rather than re-measured: regenerating one needs a
> Docker daemon, a network and a 73 MB `npm install`, and a fixture whose provenance is not
> written down is a fixture nobody dares regenerate.

and, after the existing `node:22-bookworm-slim` provenance paragraph:

> The ninth pair (`same_file_dup`) was captured **2026-09-02** in
> `bakeoff-eval-agent:base-node-22` — node v22.23.2 with the same pinned `vitest 3.2.7` /
> `jest 30.5.0` the base installs at `/node_modules` — with the tree mounted at `/repo`
> rather than `/work`. Its rewrite is therefore `"/repo/projv" → "/repo"` and
> `"/repo/projj" → "/repo"` with **no** `node_modules` step, because that image already
> resolves the runners at `/node_modules`. Everything else is the recipe above: `rm -f`
> before each invocation, record whether the file came back, then pretty-print with
> `indent=2, sort_keys=True`. Its source is two `it('adds')` under one `describe('outer')`,
> one passing and one failing, plus an `it('subs')`.

**(c)** and a fourth entry under *"argv facts measured beside these"*:

> 4. **A `-t` cannot address one of two identically titled tests in one file.** Measured
>    2026-09-02: `-t '^(?:outer adds)$'` against `dup.test.js` ran **both**, one passing and
>    one failing, on vitest and jest alike; the negated form skipped **both**, at
>    `numPendingTests: 2` for one requested id. Nothing in either default report separates
>    the pair: vitest emits a `location` only when a `file:line` positional turns
>    task-location capture on, and jest's is `null` without `--testLocationInResults`.
>    vitest's `file:line` positional *does* select one (`dup.test.js:3` ran the passer,
>    `dup.test.js:4` the failer) while jest reads `dup.test.js:3` as a path regex and
>    collects **0 tests** at exit 1 — and neither framework has a line-scoped
>    *de*selection, which is what the quarantine needs. Hence a refusal rather than an id
>    spelling; see `preflight`'s `same_file_duplicate_ids`.

**(d)** `tests/test_runners.py:587`, `_report`'s docstring: *"One of the eight measured
report shapes"* → *"One of the nine measured report shapes"*, and its capture sentence gains
*"…; the ninth (`same_file_dup`) in `bakeoff-eval-agent:base-node-22`, whose runners are the
same pinned versions — see the README beside them"*.

### 3.6 `PREFLIGHT_VERSION` — read-and-add-one

**Do not transcribe a number from this plan.** Two round-2 items in flight move this
constant (item 3, already applied in the working tree, took it to `"14"`; item 1 takes it to
`"15"`). The implementer reads the current value in `preflight.py` and adds one — expected
`"15"` → `"16"` if item 1 is the only thing in between. The `#: N -> N+1:` block says:

> `N -> N+1` adds a refusal a node manifest can trip and a pytest one cannot: a declared
> f2p or p2p id that names MORE THAN ONE test in its own file. A node id is
> `<file>::<fullName>` with no positional index, so two identically titled tests in one
> file are the same id — measured 2026-09-02, an exact anchored `-t` runs both (one passed
> and one failed in the same run) and the deselection skips both, so the red-before check
> was satisfied by whichever one fails, the green-after check by both passing, and no
> count downstream disagreed. A cached PASS under `N` on a **node** task is stale: it was
> taken by a gate that could not see the shape. A cached NO-GO is unaffected — nothing this
> version adds turns a NO-GO into a GO — and no **pytest** verdict moves at all, since
> `pytest_adapter.duplicate_ids` is `{}` as a claim its node ids back. The new evidence key
> `same_file_duplicate_ids` is `{}` on every verdict this version writes for a task at least
> one of whose node runs reported, and `None` where nothing counted.

### 3.7 `bakeoff/scripts/mutation_check.py`

Two entries added, in the file's existing comment style.

| name | file | anchor | mutant | selector |
|---|---|---|---|---|
| `runners: count two same-named tests in one file as one` | `src/bakeoff/runners/node_adapter.py` | `        if count > 1:` | `        if False:` | `tests/test_runners.py -k more_than_once_in_one_file` |
| `preflight: accept a declared id that names two tests` | `src/bakeoff/preflight.py` | `    if declared_dupes:` | `    if False:` | `tests/test_preflight.py -k names_more_than_one_test` |

Comments to write beside them, respectively: the M11.2/M11.3 measurement (an exact anchored
`-t` runs both; the deselection skips both at `numPendingTests: 2`), and the consequence —
red-before satisfied by whichever one fails, green-after by both passing, `F2P_FAILED`
stamped on a pair the record cannot name.

**One existing entry's comment is edited, not its anchor:** *"runners: read a report that
was never written as a clean run"* says the anchor text *"also occurs in `parse_deselected`
and in `executed_names`; `run` replaces the FIRST occurrence, which is the classifier's, and
both of those are defined below it for that reason."* `executed_names` becomes `_executed`
in that sentence (D3). The anchor, the mutant and the selector are unchanged.

**Counts are a DELTA: +2 over whatever HEAD carries.** The tree today has **164** entries
(AST count over `MUTATIONS`); item 1 is `-2 +3`, so if item 1 is the only thing in between,
expect **167**. Read the actual baseline from HEAD before running and treat a mismatch as a
question about what else landed.

### 3.8 The grader and the oracle: no edit, and §4 says why

`grader.py`, `oracle.py`, `grade_schema.py`, `schema.py` and every version constant in them
are untouched. No argv changes, so the gated-equals-graded byte-identity property is
unaffected by this item.

---

## 4. Does the grader need this check? — **No**, for three reasons

1. **The ids are already validated.** Grading resolves the same manifest the gate refused
   or passed, under a recorded `graded_under_preflight_version`. A declared id that names two
   tests never reaches a grade, because the task never collects.
2. **The model cannot introduce one under `tests.paths`.** `_check_test_restore`
   (`grader.py:899`) applies the submission with `--index` — which is what makes an
   agent-*added* file tracked and therefore removable — then does
   `git rm -r -f --ignore-unmatch -- <tests.paths>` and `git checkout <start_sha> -- <prefix>`.
   Every test file under `tests.paths` in the graded tree is therefore the start state's,
   which is the tree preflight measured; `_check_f2p`'s own docstring states it (*"the f2p
   names in this tree are the MANIFEST's and not whatever the model wrote"*). And
   `validate_node_id` (`node_adapter.py:412`) refuses a declared id whose file is outside
   `tests.paths`, so no declared id escapes that restore.
3. **Where a duplicate could still appear, it cannot turn a failure into a pass.** A model
   file *outside* `tests.paths` that a vitest substring positional over-collects is item 1's
   §7 residual, and a same-file duplicate there is not one of the manifest's ids: `classify`
   is `KIND_FAILED` if **any** assertion failed, so an added twin can only make check 5 or 6
   stricter, never greener — and it cannot mask `not_run` either, since `verify_selected`
   keys on `f"{path}::{name}"` and an out-of-file twin has a different `path`. The one shape
   that would be a false success — deleting the failing test and adding a passing one under
   its title — is reachable without duplicates at all, is what check 2's restore exists to
   prevent, and is that check's territory.

So `GRADER_VERSION` and `ORACLE_VERSION` do not move, and no grade already written becomes
suspect because of this item. **This is not the same as "no residual"**: D1's third bullet
names the one that survives (a flaky *undeclared* twin, quarantined, taking its healthy
sibling out of check 6), which is accepted, recorded and auditable rather than closed.

---

## 5. Tests, by name, with what each asserts

### `bakeoff/tests/test_runners.py`

Parametrized over `["vitest", "jest"]` where marked (×2), reading the fixture through the
existing `_report(shape, framework)` helper.

1. `test_the_same_file_dup_fixture_holds_two_assertions_under_one_name` (×2) — fixture
   integrity, in the style of `test_the_no_files_fixtures_really_do_disagree_about_success`:
   exactly one `testResults` entry, three `assertionResults`, two of them with `fullName ==
   "outer adds"` at statuses `{"passed", "failed"}`. Docstring: M11.1, including that on a
   default run vitest emits no `location` key and jest's is `null`, so nothing in the report
   this harness reads separates the pair.
2. `test_two_tests_with_the_same_name_in_one_file_are_ONE_failed_id` (×2) — `classify` over
   the fixture: `kind == KIND_FAILED`, `failed_ids == {"tests/dup.test.js::outer adds"}` (one
   id for two assertions), `files_run == ("tests/dup.test.js",)`. Docstring: M11.5, and that
   this is what the record would carry.
3. `test_verify_selected_reports_nothing_missing_for_a_duplicated_id` (×2) —
   `verify_selected(fixture, ("tests/dup.test.js::outer adds",), adapter)` is empty: the
   existing channels read clean, which is why a new one is needed.
4. `test_duplicate_ids_counts_a_name_that_appears_more_than_once_in_one_file` (×2) —
   `duplicate_ids(fixture) == {"tests/dup.test.js::outer adds": 2}`. **Mutation selector
   `more_than_once_in_one_file`.**
5. `test_duplicate_ids_of_a_clean_report_is_empty_and_that_is_a_measurement` (×2) —
   `duplicate_ids(_report("pass", fw)) == {}`, whose three `fullName`s are distinct.
6. `test_duplicate_ids_of_a_report_that_does_not_exist_is_empty` (×2) —
   `duplicate_ids(None) == {}`. Docstring: this is deliberately not `None`; the absence is
   preflight's to record, with the same guard `duplicate_full_names` carries, because a
   report-less run measured nothing.
7. `test_duplicate_ids_counts_only_tests_that_reached_a_verdict` (×2) — a hand-built report
   with two `outer adds` of which one is `skipped` (vitest's spelling) or `pending` (jest's):
   `{}`. Docstring: M11.4, and that this is why the scoped p2p run — which deselects the f2p
   ids — cannot see an f2p duplicate, hence preflight's accumulator over every run.
8. `test_duplicate_ids_does_not_count_one_test_seen_in_two_suite_entries` (×2) — a report
   whose two `testResults` entries each name `/repo/tests/a.test.js` with one
   `works` assertion each (the shape `merge_reports` produces): `{}`. Docstring: counting per
   entry can only under-report, never invent, and item 1's `ambiguous_file_filters` is what
   makes the overlapping-groups case unreachable in the first place.
9. `test_pytest_duplicate_ids_is_empty_and_that_is_a_claim` — `{}` for `{"testResults": []}`
   and for `None`. Docstring carries pytest's node-id uniqueness argument verbatim from §3.2.
10. `test_executed_names_and_duplicate_ids_read_ONE_rule` — `inspect.getsource` of both
    methods contains `_executed`, mirroring the existing
    `test_verify_selected_and_executed_names_read_ONE_rule`.
11. `test_the_first_eight_space_report_guard_in_this_module_is_the_classifiers` — the
    property `mutation_check` actually depends on, stated over the **whole module** rather
    than over one pair of methods:

    ```python
        lines = inspect.getsource(node_adapter).splitlines()
        guards = [index + 1 for index, line in enumerate(lines)
                  if line == "        if report is None:"]
        body, start = inspect.getsourcelines(node_adapter._NodeFlavour.classify)
        assert guards, "the anchor mutation_check.py replaces no longer exists"
        assert start <= guards[0] < start + len(body), (
            f"the first eight-space `if report is None:` is at line {guards[0]}, "
            f"outside classify (lines {start}-{start + len(body) - 1})")
    ```

    Docstring: `mutation_check` replaces the FIRST occurrence and it must be `classify`'s —
    indentation is what excludes `verify_selected`'s four-space guard, position is what picks
    `classify` out of the method bodies, and nothing in the language enforces it. A **new**
    method added above `classify` with that guard defeats the anchor exactly as a moved
    `_executed` would, and `mutation_check` would report CAUGHT for the wrong reason.
12. `test_no_adapter_method_is_a_stub_any_more` — existing, extended: for all three adapters
    `duplicate_ids({"testResults": []}) == {}`.
13. `test_executed_names_reports_only_the_tests_that_reached_a_verdict` (existing, ×2) —
    unchanged; it now runs through the refactored `_executed` and is the regression guard for
    that refactor.

### `bakeoff/tests/test_preflight.py`

`_node_container` (`test_preflight.py:3122`) gains **three** keywords. Written out, because
two of the tests below cannot be built without them:

```python
def _node_container(*, f2p_ran=True, f2p_twice=False, p2p_twice=False,
                    missing=(), scope_names=None, scope_files=None, p2p=(),
                    runner=("/node_modules/.bin/vitest", "run",
                            "--no-cache")):
```

* `f2p_twice` / `p2p_twice` — the declared id names TWO tests in its own file, M11.2's
  measured shape, as a second assertion **inside the same `testResults` entry** (which is
  what one file is). The two f2p report literals become:

  ```python
        "f2p_before": _node_report(
            [("tests/a.test.js",
              [("failed" if f2p_ran else "skipped", "does a thing")]
              + ([("passed", "does a thing")] if f2p_twice else []))],
            status="failed" if f2p_ran else "passed",
        ),
        "f2p_after": _node_report(
            [("tests/a.test.js",
              [("passed", "does a thing")] * (2 if f2p_twice else 1))]),
  ```

  and, in the `p2p_report` construction (`test_preflight.py:3155-3159`), the per-id
  assertion list becomes
  `[("passed", node_id.partition("::")[2])] * (2 if p2p_twice else 1)`. One
  `testResults` entry per declared id, exactly as today — the shape is **not** regrouped by
  file, because `files_run` would then change for existing tests.
* `missing` — report keys to blank out: `for key in missing: reports[key] = None`. That is
  the measured broken-config shape (exit 1, no file), and `_ScriptedContainer` already
  answers it the way the real container does (`cat` exit 1, `test_preflight.py:968-980`,
  `1139-1140`), so `_Runner.last_report` is `None` for that run.

`scope_names` needs no change: it already accepts the same path twice and `by_file` folds
those into one entry with two assertions, which is the undeclared shape.

14. `test_a_declared_f2p_id_that_names_more_than_one_test_is_a_problem` —
    `_node_container(f2p_twice=True)`: `not result.ok`; a problem contains
    `"names 2 tests"`; `result.evidence["same_file_duplicate_ids"] ==
    {"tests/a.test.js::does a thing": 2}`. **Mutation selector `names_more_than_one_test`.**
    Docstring: red-before is satisfied by the failing twin and green-after by both passing,
    so the gate's two conjuncts stop meaning "this test went red → green" — and the `== 2`
    is also what fails a summing accumulator, since this shape puts the id at 2 in both f2p
    reports.
15. `test_a_declared_p2p_id_that_names_more_than_one_test_is_a_problem` —
    `_node_container(p2p=("tests/b.test.js::keeps working",), p2p_twice=True)`: refused, and
    the evidence names it at count 2. This is also the task shape that makes **no scoped run
    at all**, so it pins that the check does not depend on one.
16. `test_an_undeclared_same_file_duplicate_is_recorded_and_not_refused` —
    `scope_names=[("tests/b.test.js", "works"), ("tests/b.test.js", "works")]`:
    `result.ok` is `True`, `same_file_duplicate_ids == {"tests/b.test.js::works": 2}`, and no
    problem mentions it. Docstring carries D1's boundary in one sentence — the gate can
    prove the ambiguity of a declared id and cannot prove the hazard of an undeclared one,
    because the quarantine is `oracle._derive`'s — and names the accepted residual and where
    its two halves are audited.
17. `test_the_declared_duplicate_refusal_reads_the_f2p_run_not_the_scoped_one` —
    `_node_container(f2p_twice=True)` with a **clean** scoped report (the default): still
    refused, and `same_file_duplicate_ids` names the f2p id. Docstring carries M11.4 — the
    scoped run deselects the f2p ids and a deselected test is `skipped`/`pending`, so a
    check written over the scoped report alone would see nothing.
18. `test_same_file_duplicate_ids_is_recorded_when_there_are_none` — `_node_container()`:
    `== {}`, the measurement, beside `duplicate_full_names == []`.
19. `test_same_file_duplicate_ids_is_None_when_no_node_run_wrote_a_report` —
    `_node_container(missing=("f2p_before", "f2p_after", "p2p_before", "p2p_after",
    "scoped"))`: the key is `None`, not `{}`. The verdict is a NO-GO for other reasons and
    control still reaches the end of `preflight` (there are exactly two
    `return PreflightResult(` sites, `preflight.py:963` and `2016`, and only the runner-gate
    one returns early), so this asserts the accumulator's absence and not the seed's.
20. `test_a_run_that_wrote_no_report_contributes_nothing_and_does_not_erase_what_did` —
    `_node_container(f2p_twice=True, missing=("scoped",))`: the evidence is still
    `{"tests/a.test.js::does a thing": 2}` and the refusal still fires. Docstring states the
    flag's exact semantics: `{}` is *"at least one node run was read and no id in the runs
    that were read names two tests"*, never *"every run was counted"*; a run that wrote no
    report contributes nothing and is named by its own exit and kind evidence.
21. `test_a_pytest_task_records_an_empty_same_file_duplicate_map` — `_pytest_container()`:
    `== {}`, a claim pytest's ids back, never `None`.
22. `test_same_file_duplicate_ids_is_None_on_the_runner_gate_early_return` — the
    `tests.runner`/`tests.framework` mismatch return (`preflight.py:963`), which starts no
    container: `None`, straight from the seed.
23. `test_a_healthy_node_task_passes_the_whole_gate` (existing) — unchanged, and it is the
    test both new mutations must not be able to pass through: an over-counting
    `duplicate_ids` refuses it.

### `bakeoff/tests/test_integration_node_task.py` (`integration`, `task_image`)

24. `test_the_node_fixture_gates_green` (existing) — one line added beside the
    `duplicate_full_names == []` assertion: `assert result.evidence["same_file_duplicate_ids"]
    == {}`. The `{}` side of the distinction, measured end to end in a real image.
25. `test_a_same_file_duplicate_of_the_f2p_title_is_refused` — a **second** module-scoped
    task/image/tree trio (`dup_task_dir`, `dup_node_image`, `dup_node_tree`), built from the
    same `fixtures/node_task/` template as `task_dir` but whose upstream
    `tests/calc.test.js` is:

    ```js
    import { it, expect } from 'vitest';
    it('keeps subtracting elsewhere', () => { expect(3 - 1).toBe(2); });
    it('adds two numbers', () => { expect(1 + 1).toBe(2); });
    ```

    The appended decoy shares the f2p `fullName` (`F2P_ID` is
    `tests/calc.test.js::adds two numbers`) and always passes. `_REFERENCE` still applies:
    its hunk is `@@ -1,2 +1,4 @@`, whose pre-image is lines 1-2 and whose last context line is
    `it('keeps subtracting elsewhere' …)`, so a third line beyond the hunk changes nothing.
    The start state therefore holds two tests titled `adds two numbers` — the reference test
    half's, which fails, and the decoy, which passes: M11.2's shape in a real image.

    **`_manifest` gains a parameter**, since it hard-codes `task_id: node-smoke`
    (`test_integration_node_task.py:104-134`):
    `def _manifest(upstream: Path, base: str, task_id: str = "node-smoke") -> str:` with
    `f"task_id: {task_id}\n"` in the body and a docstring sentence saying why — a second task
    over the same template needs its own id so `build_task_image` caches it separately and
    neither fixture's image is ever served for the other. `dup_task_dir` passes
    `task_id="node-smoke-dup"`.

    Asserts `not result.ok`, that a problem contains `"names 2 tests"`, and that
    `result.evidence["same_file_duplicate_ids"] ==
    {"tests/calc.test.js::adds two numbers": 2}`.

---

## 6. Verification

From `bakeoff/`, with `.venv/bin/python`. **Every count here is a DELTA over whatever HEAD
carries when this lands, never an absolute** — round 2 commits sequentially into one tree.

1. **Unit.** `.venv/bin/python -m pytest tests/ -v` — HEAD's baseline plus this item's new
   tests, zero failures. Read the baseline from HEAD first; a mismatch is a question about
   what else landed.
2. **The logger gate.** `.venv/bin/python scripts/verify_logger.py` → `GATE PASSED`.
3. **Integration.** `.venv/bin/python -m pytest -v -m integration
   --basetemp="$HOME/.cache/bakeoff-pytest"` — including the new `task_image` test.
4. **Mutation, solo.** `.venv/bin/python scripts/mutation_check.py` — **+2 over HEAD**. The
   tree today carries 164 entries and item 1 is `-2 +3`, so with item 1 as the only thing in
   between expect **167**. A MISSED on either new entry means the selector's test is
   asserting something that was already true.
5. **No false refusal on the corpus's only node task.** Re-gate with `--force-preflight`:

   ```bash
   .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
     --task-set ~/.cache/bakeoff-probe/taskset \
     --tasks yaml-474-single-newline-empty-value
   ```

   Expect `preflight PASS` in ≈60 s, and in
   `~/.cache/bakeoff/preflight/yaml-474-single-newline-empty-value.json`:
   `"same_file_duplicate_ids": {}` — the measurement, not `None`. **A NO-GO here is a
   finding, not a bug:** it would mean `eemeli/yaml`'s declared f2p id genuinely names two
   tests in `tests/doc/stringify.ts`, in which case record the id and the count in
   `HARVESTING.md`'s screened-corpus row and stop rather than weakening the check.
6. **No pytest verdict moves.** Re-gate `sqlglot-6927-dremio-trycast` from
   `~/.cache/bakeoff-probe/ts-sqlglot-6927-dremio-trycast` with `--force-preflight`; diff its
   evidence against the stored block. Only `preflight_version` and the new
   `same_file_duplicate_ids` (`{}`) may differ.
7. **The base-image re-measure**, under `$HOME`, if any measured claim is disputed — the
   §3.5 recipe plus:

   ```bash
   #   -t '^(?:outer adds)$'          -> both run, exit 1, pass 1 / fail 1 / pending 1
   #   -t '^(?!(?:outer adds)$)'      -> both skipped, exit 0, numPendingTests 2
   #   vitest 'tests/dup.test.js:3'   -> the passer only, exit 0, location present
   #   vitest 'tests/dup.test.js:4'   -> the failer only, exit 1
   #   jest   'tests/dup.test.js:3'   -> 0 tests, exit 1
   ```

---

## 7. Docs

* **`taskset/HARVESTING.md`, the node bullet item 1 rewrites** (`:364` today). Item 1 already
  replaces its `node -e` snippet with one reporting **same-file** collisions and contained
  file paths; this item supplies the prose that snippet's output is read against, as a
  bullet of its own:

  > * **A declared id's `fullName` must be unique inside its own file.** A node id is
  >   `<file>::<fullName>` with no positional index, so two identically titled tests in one
  >   file are the *same id* — measured 2026-09-02 (vitest 3.2.7, jest 30.5.0), an exact
  >   anchored `-t` runs both (one passed and one failed in the same run) and the negated
  >   form skips both at `numPendingTests: 2` for one requested id. Preflight refuses a task
  >   whose declared f2p or p2p ids are in that shape (`same_file_duplicate_ids`); there is
  >   no remedy but declaring a different test, since the id cannot be narrowed and neither
  >   framework can deselect by anything but the name. A same-file duplicate the manifest
  >   never names is **recorded and allowed**: every check still runs both, and the harm
  >   needs the id to reach the grade-time quarantine, which the gate cannot compute. That
  >   residual is real — a quarantined twin takes its healthy sibling out of the regression
  >   check, so a submission that broke the sibling can still grade `resolved: true` — and
  >   it is **auditable, but only by hand**: the ids are in the task's cached preflight
  >   verdict (`same_file_duplicate_ids`) and the quarantine is in `GradeRecord`, and
  >   nothing joins them.

* **`taskset/click-3360-write-usage-empty-args/task.yaml`**, the commented vitest example
  (`:80-85`), gains three lines after the `f2p:` example:

  ```yaml
  #   # The fullName must be unique inside its own file: a node id carries no
  #   # positional index, so two identically titled tests in one file are one
  #   # id -- `-t` runs both and deselects both -- and preflight refuses a
  #   # manifest that declares it.
  ```

* **`docs/BUILDING-A-TASK-SET.md`**, the troubleshooting table at `:791` gains a row:

  | preflight says a declared id *names more than one test in its own file* | two tests in that file share a `fullName`. The id carries no positional index, so it names both — `-t` runs both and deselects both. Declare a different test, or cut the task from a PR whose tests are uniquely titled |

  and its `:560` pointer at HARVESTING's node rules is re-read for the duplicate-`fullName`
  sentence, which item 1 already touches.

* **`TASKS.md`** — tick item 11 and fold in the outcome: the declared half is refused, the
  undeclared half is recorded in `same_file_duplicate_ids` and deliberately not refused, with
  the reason and the named residual. **And file one new P2 bullet**, because §8 disclaims the
  work that would close that residual and a residual named only in a plan is silence:

  > * **Nothing joins `same_file_duplicate_ids` to a `GradeRecord` quarantine, and the
  >   residual round-2 item 11 accepted lives in exactly that gap.** Preflight records every
  >   `<file>::<fullName>` that more than one test in one file answers to, declared or not;
  >   `oracle._derive` quarantines by node **id**, so a quarantined *undeclared* twin
  >   deselects **both** twins from check 6 — measured 2026-09-02 (vitest 3.2.7, jest
  >   30.5.0), an exact anchored `-t` runs both and the negated form skips both at
  >   `numPendingTests: 2` for one requested id — and a submission that broke the healthy
  >   twin can still grade `resolved: true`. A flaky twin reaches this even on a task
  >   preflight measured green, because `_derive` runs the reference suite twice where
  >   preflight runs it once. Both halves are stored — the map in the task's cached preflight
  >   verdict, the quarantine in `GradeRecord` — and **nothing intersects them**, so the audit
  >   exists only if a reader thinks to make it by hand. The close is an offline view, or a
  >   line in `grade.py`'s end-of-batch summary, that flags any graded run whose quarantine
  >   names an id in that task's `same_file_duplicate_ids`. No `SCHEMA_VERSION` move: both
  >   fields already exist.

* **`tasks/todo.md`** — one review section: what was measured (M11.1–M11.6), the boundary and
  why it is where it is, the accepted residual and where its two halves are audited, the
  `_executed` refactor and the mutation-anchor placement hazard it carries, and the grader
  answer from §4.

* **Item 1's plan file**, `2026-09-03-round2-1-node-file-selection.md` §7, forward-names this
  key as `same_file_full_name_collisions`. One-word correction to `same_file_duplicate_ids`
  so the two documents name the same key. That string was checked to appear **only** in the
  two plan files — re-run `grep -rn same_file_full_name_collisions` at implementation time in
  case item 1 landed it into a doc in the meantime.

* **No `CLAUDE.md` edit** (the round forbids it on this branch). Sentences to apply later:
  * *"A node id is `<file>::<fullName>` with no positional index, so two identically titled
    tests in one file are the SAME id — measured 2026-09-02, an exact anchored `-t` runs both
    (one passed and one failed in the same run) and the negated form skips both at
    `numPendingTests: 2` for one requested id, so `failed_ids` reports one id for two tests
    and no count downstream disagrees. Preflight refuses a task whose DECLARED ids are in
    that shape and records the rest in `same_file_duplicate_ids`: the gate can prove a
    declared id's verdict is ambiguous, and cannot prove an undeclared twin's hazard, which
    needs the grade-time quarantine it never computes."*
  * *"A duplicate is counted off every node run the gate makes, not the scoped one: the
    scoped p2p run deselects the f2p ids, and a deselected test is `skipped` on vitest and
    `pending` on jest — neither terminal, so it never reaches `executed_names`."*

---

## 8. What this does NOT do

* **It does not make a same-file duplicate representable.** Measured impossible to act on
  (M11.6): vitest's `file:line` positional can select one, jest reads it as a path regex and
  collects nothing, and neither framework can deselect by line. A task with this shape in its
  declared ids is refused, not supported.
* **It does not refuse an undeclared same-file duplicate**, which is a deliberate boundary
  (D1) with a named, accepted residual: a flaky twin can be quarantined and take its healthy
  sibling out of check 6. Recorded in the same key, audited by hand against `GradeRecord`.
* **It does not join the two halves of that audit.** No code intersects
  `same_file_duplicate_ids` with a `GradeRecord` quarantine; that is an offline view's job
  and is not built here.
* **It does not revive `validate_id_set`.** A loader has strings; both duplicates are the
  identical string. Item 1's no-op stands.
* **It does not refuse a manifest that declares the same id twice.** `-t` alternations are
  idempotent (`x|x`), the counts here are over assertion results and not declarations, and
  the shape is a different (harmless) defect.
* **It does not count absences.** `same_file_duplicate_ids` is `{}` when at least one node
  run was read and clean, `None` when none was; a gate that read three runs of five and one
  that read all five render identically, and each report-less run is named by its own exit
  and kind evidence rather than by a second key here.
* **It does not touch the grader, the oracle, or any argv.** §4. `GRADER_VERSION`,
  `ORACLE_VERSION` and `SCHEMA_VERSION` all stay; gated-equals-graded is unaffected.
* **It does not catch a duplicate a SUBMISSION introduces.** Under `tests.paths` the restore
  makes that unreachable (§4.2); outside it, the reachable shape is item 1's
  `ambiguous_file_filters` residual and it can only make a check stricter (§4.3).
* **It does not bound the evidence map.** A pathological suite with many duplicated titles
  produces a large dict, exactly as `duplicate_full_names` does today. Truncating it would be
  a lie about what was measured.
* **It does not give the refusal a `problem_code`** (§3.4c).
* **It does not add a `fast-check` determinism probe** (item 12) or a `_check_p2p`
  `not_run` branch (item 8).

---

## 9. Open questions — closed by review 1

* **Q1 — key name.** **`same_file_duplicate_ids`**, and item 1's §7 forward-reference is
  corrected (§7). The value is a map keyed by node id with a count; the sibling
  `duplicate_full_names` is a list of prose strings about names and should not drag this
  key's name after it.
* **Q2 — refuse declared, record undeclared.** **Upheld**, with the reasoning rewritten to
  the "what the gate can prove" form and the false-success residual named (D1, §3.4c, §7).
* **Q3 — one key, one writer.** **Correct as planned.** The refusal is a predicate over the
  measurement, not a second measurement, and a second key doubles the absence-semantics
  surface. "What was refused" is not lost: the problem message enumerates the declared ids
  and their counts, and `PreflightResult.problems` is stored beside the evidence.
* **Q4 — no `problem_code`.** **Right conclusion, reason replaced** (§3.4c): the only
  consumer is `grade.py::preflight_refusal`, so a code with no new `NotGradedReason` member
  is invisible and a new member is a `GradeRecord` vocabulary change this item disclaims.
* **Q5 — keep the second integration image.** **Kept**, with finding 9's specification
  (§5.25). Neither the unit fixtures (report shape) nor the scripted tests (harness logic)
  prove that a real `vitest run` under the real f2p argv, in a real task image, on a real
  materialized start state, reaches this refusal — and that is the path on which four
  "model failures" have already turned out to be environment defects. The cost is bounded:
  `integration` + `task_image` is excluded from the §6.6 gate by construction.

---

## Review 1 → changes

All 11 findings addressed; **11 adopted in full, 0 disputed.** The three rulings that changed
plan text (Q2's reasoning, Q4's justification, Q1's correction direction) are folded in above.

| # | change |
|---|---|
| 1 | **Adopted.** §3.7 and §6.4 are now stated as **+2 over whatever HEAD carries**, with "164 today, 167 if item 1 is the only thing in between" as an illustration rather than an assertion; §6.1 quotes no absolute test count and says to read the baseline from HEAD. The stale "161" came from quoting item 1's step 4 rather than its §5 delta preamble. |
| 2 | **Adopted.** The fixture-count edit list now covers all three texts: `runners/__init__.py:103` (§3.1b, with the replacement clause written out and the "eight of the nine are `classify` branches" clarification the finding asked for), `tests/fixtures/node_reports/README.md`'s heading and opening sentence (§3.5b), and `tests/test_runners.py:587`'s `_report` docstring (§3.5d). |
| 3 | **Adopted.** §3.5 stops calling the capture "the README's recipe" and now titles it *"Capture recipe for `same_file_dup`"*, and §3.5(b) adds the README paragraph that records this pair's own environment verbatim — base image instead of `node:22-bookworm-slim` + `npm install`, `/repo` mount instead of `/work`, the two-way rewrite, and that the `node_modules` step is unnecessary because the base already resolves the runners at `/node_modules`. The ninth fixture stays regenerable from the document beside it. |
| 4 | **Adopted.** The over-broad *"vitest emits no `location` at all"* is scoped everywhere it appears — M11.1, D1's rejected alternative, the module docstring (§3.3), README argv-fact 4 (§3.5c) and test 1's docstring: *no `location` on the runs this harness makes*, emitted only when a `file:line` positional turns task-location capture on (`out/v_line.json`, `location: {"line": 3, "column": 5}`). The alternative's rejection now rests on the real reason — **the argv would have to change on both frameworks** — plus "a source line is not an identity". |
| 5 | **Adopted.** §3.4(c) and Q4 no longer claim `run_matrix` reads `problem_codes`. The reason is now the true one: the only consumer is `scripts/grade.py:251`'s `preflight_refusal`, which maps codes onto `NotGradedReason`, so a code with no new enum member is invisible and a new member is a `GradeRecord` vocabulary change this item disclaims; `PREFLIGHT_FAILED` already says the right thing. |
| 6 | **Adopted.** §5's helper paragraph is replaced by a literal `_node_container` signature with **three** new keywords and the report literals written out: `f2p_twice` / `p2p_twice` (a second assertion inside the same `testResults` entry, with the exact expression for each of the three report constructions, and a note that the p2p report is deliberately not regrouped by file because `files_run` would change for existing tests) and `missing=()` (blank a run's report — the measured broken-config shape, answered by `_ScriptedContainer`'s `cat` exit 1). Test 15 is now `p2p=(…), p2p_twice=True`, and test 19 is `missing=(all five)` rather than an unspecified construction. |
| 7 | **Adopted, first option, plus the test the finding asked for.** The seed comment (§3.4a) and `_note`'s comment (§3.4b) now state the flag's exact semantics — `{}` is *"at least one node run was read and no id in the runs that were read names two tests"*, never *"every run was counted"* — the `PREFLIGHT_VERSION` note says the same, §8 carries an explicit "it does not count absences" bullet, and **new test 20** (`test_a_run_that_wrote_no_report_contributes_nothing_and_does_not_erase_what_did`, `f2p_twice=True, missing=("scoped",)`) pins the one-read-one-absent case. |
| 8 | **Adopted in full, both halves.** (a) D1's second bullet no longer says an undeclared duplicate "makes no check ambiguous"; a third bullet names the **false-success channel** plainly — `_derive` quarantines by node id, the deselection removes both twins from check 6, and a broken healthy twin can still grade `resolved: True` — including the flaky-twin reachability (`_derive` runs the reference suite twice where preflight runs it once). §4 gains a closing sentence so "the grader needs no change" is not read as "no residual". (b) The unsound rejection reason for the broad refusal is **deleted** (D2's accumulator is a union over all five runs, so the broad claim *is* measurable) and replaced by the "cannot compute the quarantine / re-erects the exclusion item 1 is retiring" one, with a parenthesis saying why the deleted reason was wrong. The audit's two halves — `same_file_duplicate_ids` in the cached verdict, the quarantine in `GradeRecord`, nothing joining them — are now stated in §3.4(c)'s comment, §7's `HARVESTING.md` bullet, test 16's docstring and a §8 bullet. |
| 9 | **Adopted.** §5.25 names the `_manifest` signature change (`task_id: str = "node-smoke"` parameter, `f"task_id: {task_id}\n"` in the body, plus the docstring sentence saying why) and the literal value the dup fixture passes, `"node-smoke-dup"`. |
| 10 | **Adopted.** Test 11 is renamed `test_the_first_eight_space_report_guard_in_this_module_is_the_classifiers` and asserts the **general** property with literal code: every eight-space `if report is None:` line in the module source, first one must fall inside `classify`'s `getsourcelines` span. `_executed`'s docstring is reworded to match ("added ANYWHERE above `classify` — this one moved, or a new method"). |
| 11 | **Adopted.** `executed_names`' replacement docstring is written out in full in §3.3 — the `passed` OR `failed` paragraph kept verbatim from today's, plus the two sentences saying the rule and the guard now live in `_executed` and that the mutation-anchor argument moved there with the guard. Nothing is left for an implementer to compose. |

Also folded in from the review's "what verified clean" section, as corrections rather than
findings: the five `runner.classify(` call sites this plan names are the complete set and
item 1's `1646, 1778, 1791` are stale (§3.4b now says so), and item 1's three inline
`.kind` sites must keep their shape (§3.4b spells the substitution).

### Review 2

**APPROVED**, one LOW finding, adopted.

| # | change |
|---|---|
| N1 | **Adopted.** §7's `TASKS.md` bullet said *"Nothing new is filed"* while §8 disclaimed the join between `same_file_duplicate_ids` and a `GradeRecord` quarantine — so the accepted false-success residual would have been named in this plan and in no backlog. §7 now files it as a **P2 bullet, written out verbatim**: what the gap is, why a flaky undeclared twin reaches it even on a green gate, that both halves are stored and nothing intersects them, and what closing it looks like (an offline view, or a `grade.py` summary line flagging a graded run whose quarantine names an id in that task's map). `SCHEMA_VERSION` still does not move — both fields already exist — so nothing in §8's disclaimers changes except the sentence that contradicted them. |
