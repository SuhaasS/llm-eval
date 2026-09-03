# Round-2 item 8 — `_check_p2p` gains the `not_run` branch, and the structural half is refused with a measurement

Repo: `/Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden`, branch `broaden-taskset`.

**Baseline: HEAD `8ab0b08`** (round-2 item 3 landed; `GRADER_VERSION` is `"9"` there).
Revision 2, after review 1 — see "Review 1 → changes" at the end.

**Line numbers in this plan are advisory; the function and symbol names are the
contract.** Items 1, 2, 4, 5, 7 and 6 all land ahead of this one
(`LEDGER.md`: `3 → 1 → 2 → 4 → 5 → 7 → 6 → then 8`), and several of them edit
`grader.py` and `preflight.py`. Locate every anchor below by `grep -n` on the quoted
text, not by seeking to a number.

---

## 1. The measured defect

### 1.1 What is missing

`grader._check_f2p` reads `outcome.not_run` and routes a non-empty set to
`state.environment(...)`, **before** the `KIND_PASSED` branch (`grader.py:1188` at
`8ab0b08`, the block opening `    if outcome.not_run:`):

```python
    if outcome.not_run:
        state.environment(
            "f2p",
            "these declared f2p ids did not run: "
            + ", ".join(sorted(outcome.not_run)),
            result,
        )
```

`_check_p2p` (`def _check_p2p` at `grader.py:1257`, running to `_check_secret_scan` at
`:1380`) has no such branch. It classifies, sets `p2p_deselected`, and goes straight to
`KIND_PASSED` / `KIND_FAILED` / `_TIMEOUT_EXIT` / `KIND_NOTHING_RAN` / environment
fallthrough.

### 1.2 The shape that reaches a wrong verdict

`Outcome.not_run` is *"requested ids that produced no terminal status"*
(`runners/__init__.py:90-92`), filled by `preflight._Runner.classify` through
`node_adapter.verify_selected` (the `if self._selected and self.last_report is not None:`
guard, `preflight.py:676`). Measured 2026-09-01 and recorded in `verify_selected`'s
docstring: **a `-t` pattern naming a test that no longer exists exits `0` on both vitest
and jest with every test reported skipped.**

So on a node task with an explicit `tests.p2p`, if *some but not all* of the declared p2p
ids stopped matching:

* the ids that still match run and pass,
* `assertionResults` therefore carries at least one terminal status, so the node
  classifier returns `KIND_PASSED` rather than `KIND_NOTHING_RAN`,
* `_check_p2p` reaches `state.passed("p2p", result)` and **returns**,
* the ladder runs to the end and the record can carry **`resolved: True`**.

That is the defect in one line: **a submission graded `resolved: True` while part of its
regression check silently never ran.** Not caught by the exit code (0), not by
`p2p_deselected` (nothing was deselected — the ids were selected and matched nothing), not
by `p2p_failed_node_ids` (`()`).

The total-loss case is already routed and mislabelled rather than wrong: if *every*
declared p2p id stops matching, the node classifier sees zero assertions with a terminal
status and returns `KIND_NOTHING_RAN`, which `_check_p2p` refuses as
`SCOPE_COLLECTED_NOTHING` — `resolved: None` under a bucket that names the wrong cause.
D4 says what happens to it.

### 1.3 `TASKS.md`'s "structurally absent" half is stale, and by how much

The item-8 bullet says:

> `_check_p2p` calls `pass_to_pass`, which never touches [`_selected`].

**That stopped being true at commit `59f4488`** (`feat: refuse the three node task shapes
that read as a green gate`); `git log -S"self._selected = tuple(tests.p2p)" --
bakeoff/src/bakeoff/preflight.py` returns exactly that commit.
`preflight._Runner.pass_to_pass` at `8ab0b08` (`if tests.p2p:` at `preflight.py:771`):

```python
        if tests.p2p:
            result = self.run(self.adapter.p2p_args(
                selected=tuple(tests.p2p), scope=(),
                deselected=tuple(extra_deselect), ignored=tuple(ignore)))
            self._selected = tuple(tests.p2p)
            return result
        result = self.run(self.adapter.p2p_args(...))
        self._selected = ()
        return result
```

So the data is present on the **explicit-`tests.p2p` branch** and absent on the deselect
branch — and its absence there is deliberate and correct, not a gap: the deselect branch
declares no id list, so a leftover `_selected` would make a correct scoped run report the
f2p ids as "did not run", which is precisely what a correct p2p run does to them (the
reset's own comment says so).

The bullet must be rewritten, not merely ticked. §7.

### 1.4 The same line is also wrong about the quarantine — the blocking half

`_selected` is the **whole** declared `tests.p2p`, quarantined ids included, while the
quarantine is simultaneously *deselected out of the same argv*. Measured at `8ab0b08`:

* `grader._check_p2p` passes it: `runner.pass_to_pass(task.tests,
  extra_deselect=quarantined, scope=scope)` (`grader.py:1313`) — and that is the **only**
  call site in the tree that passes `extra_deselect` at all (`oracle.py:221`/`:223`,
  `preflight.py:1655`/`:1802`/`:1870` all leave it `()`).
* `node_adapter.p2p_args` turns it into a negative lookahead inside the single `-t`
  (`node_adapter.py:268-269`):
  `pattern += "(?!(?:" + "|".join(self._names(deselected)) + ")$)"`. The quarantined tests
  are therefore **skipped**.
* `executed_names` yields only `_TERMINAL_STATUSES = ("passed", "failed")`
  (`node_adapter.py:97`, `:400-401`), so a skipped test is invisible to it.
* `verify_selected` returns `frozenset(node_id for node_id in requested if node_id not in
  seen)` — the quarantined ids fall straight through into `Outcome.not_run`.
* The id shapes match exactly: `node_adapter.classify` builds `failed_ids` as
  `f"{path}::{fullName}"`, `derive_quarantine` returns those verbatim, and on the explicit
  branch the quarantine is a **subset of `tests.p2p` by construction**, because
  `derive_quarantine` derives it from two `pass_to_pass` runs over that same list.
* `derive_quarantine` refuses only a *total* cover (`oracle.py:239`). One flaky id out of
  four is accepted.

So a `not_run` branch added naively would stamp `ENVIRONMENT_ERROR` / `resolved: None` on
**every cell of every arm** of the first node task that needs an explicit `tests.p2p` and
has one flake — permanently, in an append-only file. The field is inert today; this item
is what activates it. D1a is the fix.

### 1.5 The structural half, measured

The design brief proposed, for the deselect branch (`tests.p2p: []`), comparing the
executed-id set against *"the set executed at the oracle's baseline minus the quarantine
minus ids the submission legitimately removed"*. Every term is measurably unavailable, and
the failure mode it aims at is measurably unreachable:

**(a) pytest reports no executed ids at all, on purpose.**
`pytest_adapter.report_path()` → `None` (`:322-323`); `report_args()` → `[]` with a
recorded reason (a JUnit report needs `classname` mapped back to a node id, and *"a dotted
segment is a package or a class and the XML does not say which"*); `executed_names()` →
`()` with a recorded claim (`:359`); `Outcome.files_run` is `None` for pytest.
`_Runner.classify`'s guard is `if self._selected and self.last_report is not None:`, which
pytest can never satisfy. So `not_run` is structurally empty on every pytest run.

**(b) The oracle stores no baseline.** `Oracle` carries `fingerprint`, `quarantined`,
`oracle_version` only (`oracle.py:133-149`). `derive_quarantine` returns `first ^ second` —
failed ids only; on a healthy suite both sets are empty and no id is ever enumerated.
Adding a baseline is a new `Oracle` field plus an `ORACLE_VERSION` bump, which re-derives
**every** cached quarantine at two full suite runs per task.

**(c) The failure mode the check aims at is already prevented, inside the graded scope.**
`_check_test_restore` runs, for the declared `tests.paths`:

```
git rm -r -f --quiet --ignore-unmatch -- <paths> [<gitlink excludes>]
git checkout <start_sha> -- <prefix> [<gitlink excludes>]
```

and `_check_p2p`'s deselect branch scopes the run to
`_existing_prefixes(env, tuple(task.tests.paths))`. **The p2p scope is a subset of the tree
check 2 has just restored to the start state.** A submission that deletes or renames a
tracked p2p test under `tests.paths` has that deletion undone before check 6 runs.
(Untracked files the agent *added* survive — deliberate, and recorded in the restore's own
comment — but adding cannot shrink the executed set.)

**(d) The same holds on the branch that can actually fire.**
`node_adapter.validate_node_id` refuses, at manifest-load time, any f2p **or** p2p id whose
file half is not `_under(tests.paths)`; `tasks.py:1246-1248` applies it to `(*f2p, *p2p)`.
So on node × explicit-`tests.p2p` — the one combination where `not_run` can be non-empty —
every declared p2p id lives under a prefix check 2 restores.
(The pytest implementation of `validate_node_id` is a deliberate no-op, so a *pytest*
explicit-`p2p` id **can** name a file outside `tests.paths`. Harmless here: `not_run` is
structurally empty on pytest, and such an id whose file the submission deleted already
exits 4 and falls to `state.environment` today, unchanged by this plan.)

**(e) What is left reachable is a count problem, not an id problem.** The executed set can
still shrink silently through *configuration the restore does not cover*: a root
`conftest.py` `collect_ignore`, an `addopts` marker filter, a `skipif` injected into an
imported source module, or — on node — a tracked `vitest.config.ts` / `jest.config.js` /
`package.json` at the repo root whose `exclude`/`testMatch`/`setupFiles` the submission
edits. All exit 0. The only detector in the record today is `p2p_deselected` vs
`p2p_deselect_requested`, and `_check_p2p`'s own comment records it as **vacuous** on any
task whose config deselects (measured on `pallets/click`: `addopts = "-m 'not stress'"`
reports **30,007** deselected against **7** requested, so no stale id can drag the total
under the floor). Closing that honestly is an executed-**item count** against an
oracle-stored baseline — (b)'s cost — filed as its own `TASKS.md` item (§7).

### 1.6 Reachability on today's corpus

Zero. Of the nine gated manifests in `~/.cache/bakeoff-probe/taskset/`, one declares an
explicit `tests.p2p` (`chimera-228-equinox-numeric`) and it is a **pytest** task; the two
node tasks (`ufo-214-without-trailing-slash-query`, vitest;
`yaml-474-single-newline-empty-value`, jest) both declare `p2p: []`. No stored event log
holds a node run at all. The version constants still move (§6) for the reason the 4→5 and
6→7 bumps moved: the ladder's *meaning* changes on an input it already accepted, and the
resume gate in `scripts/grade.py` keys on `(run_id, GRADER_VERSION)` alone.

---

## 2. Design

### D1a — `_selected` means "what this argv asked to RUN", and the subtraction lands at the source

The blocking fix for §1.4. In `preflight._Runner.pass_to_pass`, the explicit branch
becomes:

```python
        if tests.p2p:
            result = self.run(self.adapter.p2p_args(
                selected=tuple(tests.p2p), scope=(),
                deselected=tuple(extra_deselect), ignored=tuple(ignore)))
            # MINUS what this same argv deselects. `_selected` is "what the
            # last invocation asked for BY ID", and a quarantined id was asked
            # NOT to run: `p2p_args` folds `deselected` into a negative
            # lookahead inside the single `-t`, so those tests are SKIPPED,
            # carry no terminal status, and `executed_names` -- which yields
            # only `passed`/`failed` -- never sees them. Left unsubtracted they
            # fall straight through `verify_selected` into `Outcome.not_run`,
            # and on this branch the quarantine is a SUBSET of `tests.p2p` by
            # construction (`derive_quarantine` derives it from two
            # `pass_to_pass` runs over that same list). The grader's `did not
            # run` branch would then fire on every healthy node run of any task
            # carrying one flake -- `not_graded` on every cell of every arm,
            # permanently, in an append-only file.
            #
            # `ignore` is deliberately NOT subtracted. It has exactly one
            # caller -- preflight's p2p run at the START state -- which reads
            # no `not_run` at all, and the grader never passes it. Subtracting
            # it here would be a rule with no reader.
            self._selected = tuple(
                node_id for node_id in tests.p2p
                if node_id not in extra_deselect
            )
            return result
```

**Chosen over the grader-side `stale = outcome.not_run - set(quarantined)`** for two
reasons. First, `_selected`'s docstring already claims it is *"what the last invocation
asked for BY ID"* — the current line does not satisfy that claim, so the grader-side patch
leaves a field lying and the next reader of `not_run` re-introduces the same defect one
call site over. Second, this repo's standing rule is that a second copy of a rule is a
second thing that can be wrong about what happened, inside the check that exists to be
right about it.

**Cost: none outside the grader.** `grader.py:1313` is the only call site passing
`extra_deselect`; every preflight and oracle call leaves it `()`, so the expression is
identity there. **`PREFLIGHT_VERSION` does not move**: no preflight verdict changes,
because preflight reads `not_run` on the f2p run only and never on a p2p run (§7's second
`TASKS.md` item).

### D1b — the branch, mirrored from `_check_f2p`, placed before `KIND_PASSED`

`_check_p2p` gains, **immediately after** `state.p2p_deselected = ...` and **immediately
before** `if outcome.kind == KIND_PASSED:`:

```python
    # BEFORE the KIND_PASSED branch, for the reason `_check_f2p` states one
    # function up and for a sharper one here: a PARTLY stale selection runs the
    # ids that still match, they pass, and the report that reaches this point
    # is green at exit 0 -- so a green-first ladder absorbs it and the record
    # says `resolved: True` over a regression check part of which never ran.
    # It also precedes KIND_FAILED and the timeout branch; see D4.
    #
    # `not_run` here excludes the quarantine, because `pass_to_pass` subtracts
    # `extra_deselect` from `_selected` on this branch -- a quarantined id is
    # skipped by the same argv's negative lookahead and would otherwise be
    # reported as not-run on every healthy node run of a task with one flake.
    #
    # ENVIRONMENT, never a `GradeFailure`. `not_run` is non-empty only on the
    # explicit-`tests.p2p` branch (`pass_to_pass` resets `_selected` to `()` on
    # the deselect one) and only on a framework that writes a report (pytest's
    # `report_path()` is `None`). Every id on that branch is validated at LOAD
    # time to live under `tests.paths` -- `node_adapter.validate_node_id`,
    # applied to `(*f2p, *p2p)` in `tasks.py` -- and check 2 restores
    # `tests.paths` to the start state before this check runs. So a deleted or
    # renamed test file is NOT an available explanation. What is left is: a
    # stale manifest; a rename the harness cannot see; a selection argv this
    # harness built wrong; or a runner CONFIG the submission edited outside
    # `tests.paths` and the restore therefore did not put back (a root
    # `vitest.config.ts` / `jest.config.js` / `package.json` `exclude`,
    # `testMatch` or `setupFiles`). The fourth is submission-caused and the
    # ruling is unchanged BECAUSE the grader has no channel that separates it
    # from the other three: P2P_REGRESSION is the claim that the model's patch
    # broke a passing test, permanent, in an append-only store, over an
    # ambiguity the model never saw. `resolved: None` is still strictly
    # harsher than what HEAD does with that submission, which is grade it
    # `resolved: True`.
    if outcome.not_run:
        state.not_run_node_ids = tuple(sorted(outcome.not_run))
        state.environment(
            "p2p",
            "these declared p2p ids did not run: "
            + ", ".join(sorted(outcome.not_run)),
            result,
        )
```

The message is built from `outcome.not_run` and **not** from `state.not_run_node_ids`, so
the field assignment is independently removable — which is what lets §5's second mutation
anchor test the field rather than the line. Do not "simplify" the duplicate `sorted(...)`.

### D2 — the ids are recorded as a tuple, not only inside a message

`GradeRecord` gains **one** field, written by **both** checks:

```python
    #: Declared f2p or p2p node ids the run did not execute -- `Outcome.not_run`,
    #: which is "requested, and no terminal status came back". `None` is NOT
    #: MEASURED (the ladder never got here, or the framework reports no
    #: executed ids -- pytest, where the exit code carries this instead); a
    #: non-empty tuple is an observation. It never round-trips as `()`: the
    #: branch that writes it only runs when the set is non-empty.
    #:
    #: On the p2p side the quarantine is already subtracted, upstream, by
    #: `preflight._Runner.pass_to_pass` -- a quarantined id was asked NOT to
    #: run, so reporting it here would be a defect, not evidence.
    #:
    #: `environment_error_check` says which check produced it, and ONE field is
    #: enough because only one check can ever write it -- both branches go
    #: through `state.environment`, which raises `_Stop`, so an f2p `not_run`
    #: means `_check_p2p` never ran at all.
    #:
    #: Beside the message rather than instead of it, for the reason
    #: `f2p_failed_node_ids` exists: a node `fullName` is free text and may
    #: contain ", ", so `environment_error`'s joined form is not losslessly
    #: splittable, and a reader counting how often a task set's manifests went
    #: stale would be grepping prose. GRADER_VERSION 7 -> 8 moved for exactly
    #: this shape -- a well-formed verdict beside a lying evidence field.
    not_run_node_ids: tuple[str, ...] | None = None
```

placed after `p2p_failed_node_ids` in `GradeRecord`, and added to `_TUPLE_FIELDS` so `None`
and `()` survive the JSON round trip distinctly.

`_check_f2p`'s existing branch gains the same one-line assignment, so a reader of the field
never has to know that one check filled it and the other only wrote prose.

**Rejected: two fields (`f2p_not_run_node_ids` / `p2p_not_run_node_ids`).** They cannot both
be non-`None` on any line — `state.environment` raises — so the second is a column that is
`None` on every row ever written, and `environment_error_check` already carries the
discriminator, typed.

**Rejected: no field, ids only in `environment_error`.** See the docstring.

### D3 — `p2p_failed_node_ids` stays `None` on this branch

Nothing failed. `()` there is the claim "the p2p run reported no failures", a different and
unearned statement about a run that did not finish its selection.

### D4 — the branch outranks `KIND_NOTHING_RAN`, `KIND_FAILED` **and** the timeout branch

The insertion point puts it ahead of all three. Each is a deliberate decision:

* **vs `KIND_NOTHING_RAN`.** A wholly-stale explicit selection now reads `ENVIRONMENT_ERROR`
  where it read `SCOPE_COLLECTED_NOTHING`. Both are `resolved: None`, so nothing said about
  any model changes; what changes is that the row names the cause.
  `SCOPE_COLLECTED_NOTHING` means "the declared scope collected nothing", and on the
  explicit branch there is no scope — the *selection* matched nothing, which is a stale
  manifest. This is **not** made unreachable by `derive_quarantine`'s total-cover refusal:
  a *partial* quarantine plus partial staleness reaches a wholly-empty effective selection
  without that guard firing, so the relabel is live rather than theoretical.
* **vs `KIND_FAILED`.** A run in which one declared id genuinely ran and failed while
  another went stale moves from `P2P_REGRESSION` (`resolved: False`) to `not_graded`. The
  failing id *did* run, so that one piece of evidence is unambiguous — and it is still
  downgraded, because the check's claim is "the declared p2p set still passes" and a
  partially-executed selection is not the run that claim describes. Reporting
  `P2P_REGRESSION` on it publishes a verdict about a set the grader only partly measured,
  and the ladder's `grade_failure` names the *first* rung that failed, which would then
  mean "first rung that failed, out of the subset we managed to run". `_check_f2p` sets the
  same precedent for the same reason.
* **vs `code == _TIMEOUT_EXIT`.** Same argument, and a timeout that also lost part of its
  selection is doubly not a statement about the model.

Pinned by §4.2(2), (4) and (7).

### D5 — interaction with round-2 item 1 (node file selection, K argvs)

Item 1 turns `select_args`/`p2p_args` into `select_argvs`/`p2p_argvs` returning
`list[list[str]]`. `_Runner.run` then, **per group**, `rm -f`s the report path, execs, and
**reads that group's report**; step 3 merges the list of reports (`_MergedResult`). So **K
groups produce K `cat` reads**, one per group — not one per check.

Item 1 touches neither `_selected` nor `verify_selected`, so **this item is additive over
item 1 in either landing order**, and item 1's own §7 already names this item as out of its
scope. The one constraint it imposes is on §4's fixtures, and §4.1 states it: every
declared id in them shares a single file, so K = 1 for both check 5 and check 6 and the
ordered `cat` rule still lines up.

(An earlier draft claimed item 1 removes a cross-file masking defect. It does not:
`verify_selected` already keys on `f"{path}::{name}"` at HEAD, so cross-file masking is not
a HEAD defect and item 1 removes none.)

### D6 — what is NOT built: the deselect-branch superset check

Refused, with §1.5 as the measurement. The residue — an executed-**item count** against an
oracle-stored baseline, which would catch a config-level silent shrink — is filed as a new
`TASKS.md` item (§7) rather than built here, because it needs a new `Oracle` field and an
`ORACLE_VERSION` bump that re-derives every cached quarantine.

**Rejected: give pytest a JUnit report so `executed_names` works there.**
`pytest_adapter.report_args` refuses one with a recorded reason (`classname` → node id is
ambiguous), the parser would become a second thing that can be wrong about what happened
inside the check that exists to be right about it, and it would change the graded argv,
which `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` gates.

**Rejected: a `--collect-only` probe beside the graded run.** It doubles the per-check cost,
and "collected" is not "executed" — a `skipif` added by the submission collects and does not
run, which is exactly §1.5(e)'s case.

---

## 3. Exact changes

### 3.1 `bakeoff/src/bakeoff/preflight.py`

* `_Runner.pass_to_pass`, explicit branch: replace `self._selected = tuple(tests.p2p)` with
  D1a's generator expression **and its comment block, verbatim**.
* `pass_to_pass`'s docstring: the paragraph beginning *"`_selected` follows that branch and
  the DESELECT one resets it to `()`"* gains a sentence — on the explicit branch it is the
  declared list **minus `extra_deselect`**, because a deselected id was asked not to run.
* **`_Runner.__init__`'s `_selected` attribute docstring** — the `#:` block opening *"What
  the last invocation asked for BY ID"*, whose enumeration reads *"Written by `select` and
  by `pass_to_pass`'s explicit branch"*. Add one clause: **"… minus what the same argv
  deselects"**. Nothing there becomes false without it, but by this repo's convention the
  attribute docstring is where the next reader looks for the field's invariant, and the
  method docstring above is not a substitute for it. (Review 2, note (a).)
* Nothing else. `PREFLIGHT_VERSION` does **not** move (D1a).

### 3.2 `bakeoff/src/bakeoff/grader.py`

* `GRADER_VERSION`: read the current literal and add one (§6). Prepend §6's `N -> N+1`
  block to the existing comment stack, in the same style.
* `_State`: new field after `p2p_failed_node_ids`:
  ```python
    #: Declared ids the run did not execute, from `Outcome.not_run`. Written by
    #: `_check_f2p` and `_check_p2p`; only one of them can ever reach it,
    #: because both write it on a `state.environment` path and that raises
    #: `_Stop`. `environment_error_check` says which.
    not_run_node_ids: tuple[str, ...] | None = None
  ```
* `_State.result()`: add `not_run_node_ids=self.not_run_node_ids,` after
  `p2p_failed_node_ids=...`.
* `LadderResult`: add `not_run_node_ids: tuple[str, ...] | None = None` after
  `p2p_failed_node_ids`.
* `_check_f2p`: inside the existing `if outcome.not_run:` block, insert
  `state.not_run_node_ids = tuple(sorted(outcome.not_run))` **immediately after the
  existing nine-line comment block and immediately before `state.environment(`** — the
  comment is written to sit on the `state.environment` call and must keep doing so. Leave
  the message's `", ".join(sorted(outcome.not_run))` unchanged (D1b's anchor argument).
* `_check_p2p`: insert D1b's block verbatim, between
  `state.p2p_deselected = adapter.parse_deselected(...)` and
  `if outcome.kind == KIND_PASSED:`.
* `_check_p2p`'s docstring: add a paragraph naming the new branch, the quarantine
  subtraction it depends on, and D4's three-way ordering claim.
* `build_grade_record`: add `not_run_node_ids=ladder.not_run_node_ids,` after
  `p2p_failed_node_ids=ladder.p2p_failed_node_ids,`.

### 3.3 `bakeoff/src/bakeoff/grade_schema.py`

* `GRADE_SCHEMA_VERSION`: `"1.3.0"` → `"1.4.0"`, with §6's paragraph appended to the
  existing comment stack.
* `_TUPLE_FIELDS`: append `"not_run_node_ids"`.
* `GradeRecord`: add D2's field, with D2's docstring, after `p2p_failed_node_ids`.
* Module docstring, the "Null semantics" list: add a bullet for `not_run_node_ids` — `None`
  is not measured (and is what every pytest line carries, because the exit code answers this
  there instead), a non-empty tuple is an observation, `()` is unreachable by construction.

### 3.4 Nothing else in `src/`

`oracle.py`, `runners/*` and `tasks.py` are **not** edited.

---

## 4. Tests, by name

All in `bakeoff/tests/test_grader.py` unless stated.

### 4.1 Fixture work first

`_node_task` gains a `p2p=()` parameter forwarded to `_task(f2p=f2p, p2p=p2p)`. Nothing
else about it changes.

New helper `_run_node_p2p_with(*, p2p, p2p_report, f2p_report=None, oracle=None,
f2p=("tests/a.test.js::does a thing",))`, carrying these two docstring points:

> **The `cat` rule is served a LIST, in order.** The f2p and the p2p invocations read the
> **same** `REPORT_PATH` — `_Runner.run` writes a fixed path so the gated argv equals the
> graded argv, and deletes the file before each invocation. So the fake's `cat` rule takes
> a list of results: the first read is check 5's, the second is check 6's. One report for
> both would make check 5 read check 6's ids and refuse before check 6 ever ran — a fixture
> bug that looks exactly like the branch under test firing.
>
> **Every declared id in these fixtures shares one file**, so each check issues one runner
> group and therefore one `cat`. That is a constraint, not a coincidence: after round-2
> item 1, `_Runner.run` reads one report **per group**, and a multi-file id list would
> consume the ordered list above in the wrong order.

Default `f2p_report`: every declared f2p id present with `"status": "passed"`, so check 5
passes and control reaches check 6. Report shape is `_run_node_f2p_with`'s verbatim —
`{"testResults": [{"name": "/repo/" + <file>, "status": ..., "assertionResults":
[{"status": ..., "fullName": ...}]}]}`.

### 4.2 The branch

Ids used below: `A = "tests/b.test.js::keeps working"`,
`B = "tests/b.test.js::renamed away"`, both in one file (§4.1's constraint).

1. **`test_a_p2p_id_that_never_ran_is_the_graders_problem_not_the_models`**
   Node task, `p2p=(A, B)`; the p2p report holds a `passed` verdict for `A` and nothing for
   `B`. Asserts `grade_failure is None`, `not_graded_reason == "environment_error"`,
   `environment_error_check == "p2p"`, `"did not run" in environment_error`,
   `B in environment_error`, and `not_run_node_ids == (B,)`.

2. **`test_a_partly_stale_p2p_selection_is_not_absorbed_by_a_green_report`**
   The ordering pin against `KIND_PASSED`, and the anchor for §5's first mutation. Same
   fixture as (1); asserts `result.resolved is None` and `_check(result, "p2p").status ==
   "fail"`, with a docstring stating that the ids that still match run, pass, and produce a
   report that is green at exit 0 — so a ladder testing `KIND_PASSED` first would return
   `resolved: True` over a regression check part of which never ran.

3. **`test_a_quarantined_p2p_id_is_not_reported_as_not_run`**
   **The blocking-finding pin (§1.4).** Node task, `p2p=(A, B)`, `oracle=_oracle(
   quarantined=(B,))`; the p2p report holds a `passed` verdict for `A` and nothing for `B`
   — which is what the negative lookahead actually produces. Asserts
   `not_graded_reason is None`, `resolved is True`, and `not_run_node_ids is None`.
   Docstring: `p2p_args` folds the quarantine into the same `-t` as a negative lookahead,
   so a quarantined test is skipped, carries no terminal status, and is invisible to
   `executed_names`; without D1a's subtraction every healthy node run of a task with one
   flake would grade `not_graded`.

4. **`test_a_node_p2p_selection_that_wholly_missed_names_its_cause`**
   D4 vs `KIND_NOTHING_RAN`. Neither declared id present in the p2p report. Asserts
   `not_graded_reason == "environment_error"` (**not** `"scope_collected_nothing"`),
   `resolved is None`, and `not_run_node_ids == (A, B)` sorted — with a docstring stating
   that both verdicts are `resolved: None`, so nothing said about the model changes, and
   what changes is that the row names the cause.

5. **`test_a_node_p2p_id_that_DID_run_and_failed_is_still_the_models_failure`**
   The other half, so (1) cannot be a blanket refusal of every node p2p run. Both declared
   ids present, `B` `failed`. Asserts `not_graded_reason is None`,
   `grade_failure == "p2p_regression"`, `p2p_failed_node_ids == (B,)`, and
   `not_run_node_ids is None`.

6. **`test_a_p2p_id_that_failed_beside_one_that_never_ran_is_still_not_graded`**
   D4 vs `KIND_FAILED`. Report holds `failed` for `A` and nothing for `B`. Asserts
   `not_graded_reason == "environment_error"`, `grade_failure is None`,
   `p2p_failed_node_ids is None` (D3), `not_run_node_ids == (B,)`. Docstring carries D4's
   argument: the failing id did run, and the verdict is downgraded anyway because a
   partially-executed selection is not the run the check claims to have made.

7. **`test_the_deselect_branch_reports_no_p2p_ids_as_not_run`**
   Today's default `_task()` (pytest, `p2p=()`), a clean `FakeEnv()`. Asserts
   `result.resolved is True` and `result.not_run_node_ids is None`. Pins that the branch
   cannot fire where no id list is declared — the leftover-`_selected` hazard
   `pass_to_pass`'s reset exists for.

8. **`test_a_pytest_p2p_run_never_reports_ids_as_not_run`**
   The framework half of (7). A pytest task with an explicit `p2p=("tests/test_calc.py::
   test_b",)` and the **default** `FakeEnv()` — **no rule**. Docstring must say why:
   `test_grader.py`'s `is_p2p(argv)` matcher is *pytest in argv **and** `--deselect` in
   argv*, and on the explicit branch with an empty quarantine `pytest_adapter.p2p_args`
   emits `list(selected)` and **no `--deselect`** — so `is_p2p` would never fire and an
   `is_f2p` rule would answer check 6 as well. `FakeEnv`'s documented default (unmatched
   argv → exit 0, empty output) is what makes `resolved is True` reachable here. Do not
   "fix" the matcher. Asserts `not_run_node_ids is None` and `resolved is True`, recording
   that `report_path()` is `None` on pytest, so `_Runner.classify` never asks
   `verify_selected` and the exit code carries this instead (exit 4, `ERROR: not found:`).

### 4.3 The evidence field

9. **`test_the_ids_that_did_not_run_are_recorded_as_a_tuple_not_only_in_a_message`**
   The anchor for §5's second mutation. One declared p2p id whose `fullName` **contains
   `", "`** — `"tests/b.test.js::formats a, b and c"` — absent from the report. Asserts
   `not_run_node_ids == ("tests/b.test.js::formats a, b and c",)` (length **1**) while
   `environment_error.split(", ")` yields more than two parts, so the joined message is not
   losslessly splittable and the tuple is not redundant with it.

10. **`test_only_one_check_can_write_the_ids_that_did_not_run`**
    `_run_node_f2p_with(report_shape="t_nomatch")` (the existing f2p helper). Asserts
    `environment_error_check == "f2p"`,
    `not_run_node_ids == ("tests/a.test.js::does a thing",)`, and
    `_check(result, "p2p").status == "skipped"`.

### 4.4 `bakeoff/tests/test_grade_schema.py`

11. **`test_the_grade_schema_version_moved_with_what_the_record_means`** — existing test.
    Change the literal to `"1.4.0"` and append to its docstring: *1.3.0 -> 1.4.0 adds
    `GradeRecord.not_run_node_ids`, a value a reader of a 1.3.0 line could not have met —
    on such a line the ids a run failed to execute exist only inside `environment_error`'s
    prose, so its absence is the writer's vocabulary and not a measurement.*

12. **`test_a_1_3_0_line_loads_with_no_not_run_ids_rather_than_an_empty_tuple`**
    Modelled on `test_a_1_2_0_line_loads_with_no_framework_rather_than_a_fabricated_one`:
    `_record().to_dict()`, set `grade_schema_version` to `"1.3.0"`,
    `del data["not_run_node_ids"]`, write, `load_grades`. Asserts `malformed == 0` and
    `records[0].not_run_node_ids is None` — never `()`, which would be the claim that the
    run executed every id it asked for.

13. **Two concrete edits to existing tests** (read the helper before editing):
    * `test_grade_schema.py`'s `_record()` base dict gains
      `not_run_node_ids=("tests/b.test.js::x",)`. That is what makes
      `test_round_trip_preserves_every_field_and_type` (a whole-record `back == rec`
      compare) cover the field, and what makes (12)'s `del` meaningful.
    * `test_round_trip_keeps_none_distinct_from_empty` gains `not_run_node_ids=None` in its
      override kwargs plus `assert back.not_run_node_ids is None`. That test is **three
      hard-coded assertions**, not a sweep — adding the field to `_TUPLE_FIELDS` does not
      reach it on its own.

### 4.5 Untouched and expected green

`test_grading_p2p_with_no_extras_is_the_argv_preflight_validated`
(`tests/test_preflight.py`) — no argv changes here, and D1a touches `_selected` rather than
any emitted argument, so the identity gate must stay green **without being edited**. If it
goes red, this plan was implemented wrong.

Also expected green without edits: every existing `test_preflight.py` test over
`pass_to_pass`, since `extra_deselect` is `()` at every preflight call site and D1a's
expression is identity there.

---

## 5. Mutation anchors

Three entries appended to **`MUTATIONS`** in `bakeoff/scripts/mutation_check.py` (the list
is named `MUTATIONS`; there is no `GUARANTEES` identifier in that file), in the existing
6-tuple shape `(label, file, find, replace, test selector, marker)`. The grader needles
carry the `"p2p"` argument line because `_check_f2p` holds a byte-identical
`if outcome.not_run:` block; the `"f2p"` one keeps them apart.

```python
    (
        # A `-t` pattern naming a test that no longer exists exits 0 with every
        # test reported skipped (measured 2026-09-01, vitest and jest both). A
        # PARTLY stale p2p selection therefore runs what still matches, passes,
        # and arrives here as a green report -- so without this branch the
        # record says `resolved: True` over a regression check part of which
        # never ran.
        "grader: absorb a partly-stale p2p selection into a green report",
        "src/bakeoff/grader.py",
        '    if outcome.not_run:\n'
        '        state.not_run_node_ids = tuple(sorted(outcome.not_run))\n'
        '        state.environment(\n            "p2p",',
        '    if False:\n'
        '        state.not_run_node_ids = tuple(sorted(outcome.not_run))\n'
        '        state.environment(\n            "p2p",',
        # BOTH ordering pins under one anchor: the `if False:` mutation makes
        # 4.2(2) (vs KIND_PASSED) and 4.2(6) (vs KIND_FAILED) red alike, so one
        # anchor proves both of D4's claims. This is the first selector in the
        # file to use an `or`, and the mechanism handles it -- but THE QUOTES
        # ARE REQUIRED: `run()` splits on " -k ", strips one layer of quoting
        # off the remainder, and passes what is left as a SINGLE `-k`
        # argument, so an unquoted expression would be split on the space and
        # select nothing. (Review 2, note (c).)
        'tests/test_grader.py -k "partly_stale_p2p_selection or '
        'failed_beside_one"',
        "not integration",
    ),
    (
        # A node `fullName` is free text and may contain ", ", so the joined
        # message is not losslessly splittable back into ids. Without the
        # tuple, a reader counting how often a task set's manifests went stale
        # is grepping prose -- the well-formed-verdict-beside-a-lying-evidence-
        # field shape GRADER_VERSION 7 -> 8 already moved for.
        "grader: report the stale p2p ids only in a message",
        "src/bakeoff/grader.py",
        '        state.not_run_node_ids = tuple(sorted(outcome.not_run))\n'
        '        state.environment(\n            "p2p",',
        '        state.environment(\n            "p2p",',
        "tests/test_grader.py -k recorded_as_a_tuple",
        "not integration",
    ),
    (
        # `p2p_args` folds the quarantine into the same `-t` as a negative
        # lookahead, so a quarantined test is SKIPPED, carries no terminal
        # status, and is invisible to `executed_names`. Without the
        # subtraction it falls through `verify_selected` into `not_run`, and
        # the grader's branch above then grades `not_graded` on every cell of
        # every arm of any node task with one flake -- permanently, in an
        # append-only file.
        # The label names BOTH files on purpose: the mutated file is
        # `preflight.py` while the defect it guards is the grader's verdict,
        # so an operator scanning labels for preflight coverage has to be able
        # to find it. (Review 2, note (b).)
        "preflight/grader: report a quarantined p2p id as one that did not run",
        "src/bakeoff/preflight.py",
        "            self._selected = tuple(\n"
        "                node_id for node_id in tests.p2p\n"
        "                if node_id not in extra_deselect\n"
        "            )",
        "            self._selected = tuple(tests.p2p)",
        "tests/test_grader.py -k quarantined_p2p_id_is_not_reported",
        "not integration",
    ),
```

The second anchor's value depends on D1b building the message from `outcome.not_run` rather
than from the field. If an implementer "simplifies" that duplicate `sorted(...)`, the
anchor does **not** die loudly — `mutation_check.run()` reports `caught = result.returncode
!= 0`, and a `TypeError` out of `", ".join(None)` is a non-zero pytest exit, so the harness
still prints `CAUGHT`. It degrades silently from *"proves the field is recorded"* to
*"proves the line is executable"*, which is the worse failure. The instruction stands: do
not simplify it.

`scripts/mutation_check.py` **runs solo**; nothing else may touch the tree while it does.

---

## 6. Version constants

**`GRADER_VERSION` — read the current literal, add one.** It is `"9"` at `8ab0b08`
(item 3). Round-2 item 1 also bumps it, and item 1 lands ahead of this one, so the landing
value is **at least 10** — no number is predicted here on purpose; read
`bakeoff/src/bakeoff/grader.py` at implementation time. Prepend, in the file's existing
`N -> N+1` style:

> `N -> N+1`: `_check_p2p` gains the `did not run` environment branch `_check_f2p` has
> carried since 5 -> 6, both checks now record the ids in
> `GradeRecord.not_run_node_ids`, and `preflight._Runner.pass_to_pass` subtracts
> `extra_deselect` from `_selected` on the explicit branch so a quarantined id is not
> reported as one that did not run. Under N, a node task with an explicit `tests.p2p` whose
> declared ids had *partly* stopped matching ran what still matched, passed, and reached
> `state.passed("p2p")` at exit 0 — a `resolved: True` verdict over a regression check part
> of which never ran, invisible to the exit code, to `p2p_deselected` (nothing was
> deselected — the ids were selected and matched nothing) and to `p2p_failed_node_ids`.
> That is a change to what a check MEANS on an input the ladder already accepted, so the
> version moves whether or not anything was graded under N.
>
> Two vocabulary changes ride with it, and neither touches a stored line. A wholly-stale
> explicit selection now reads `environment_error` where it read
> `scope_collected_nothing`; and a run where one declared id failed while another went
> stale now reads `environment_error` where it read `p2p_regression` — both are the
> partially-executed-selection ruling, and the second is the only one that changes a
> `resolved` value (`False` → `None`).
>
> It costs a full re-grade into a fresh `v<N+1>` artifacts directory. **No verdict on
> today's corpus changes**: `not_run` is structurally empty on pytest (`report_path()` is
> `None`, so `_Runner.classify` never asks `verify_selected`), every stored run is a pytest
> one, and of the gated node manifests neither declares an explicit `tests.p2p`. It has to
> land with the code that makes the divergence possible rather than with the first run that
> exercises it, because the resume gate in `scripts/grade.py` keys on
> `(run_id, GRADER_VERSION)` alone.

**`GRADE_SCHEMA_VERSION`: `"1.3.0"` → `"1.4.0"`.** Unhedged, unlike `GRADER_VERSION`, and
deliberately so: **no item ahead of this one in the queue moves it — checked.** Items 2, 3,
4, 5, 6 and 7 each state explicitly that it does not move, and item 1 does not touch
`grade_schema.py` at all. Comment paragraph:

> 1.4.0 adds `GradeRecord.not_run_node_ids`: the declared f2p or p2p ids a run did not
> execute. On a 1.3.0 line those ids exist only inside `environment_error`'s prose, and a
> node `fullName` may itself contain `", "`, so the joined form cannot be split back — the
> field's absence on such a line is the writer's vocabulary, not a measurement, which is the
> same reason `SCHEMA_VERSION` moves for additive bumps.

**Not moved, and why:** `ORACLE_VERSION` (§1.5(b)/D6 — no oracle field is added);
`PREFLIGHT_VERSION` (D1a is identity at every preflight call site, and preflight reads
`not_run` on the f2p run only, so no cached verdict changes); `SCHEMA_VERSION` (no
`RunRecord` field moves).

---

## 7. Docs

* **`docs/superpowers/specs/2026-08-17-offline-grader-design.md`**
  * The check-6 paragraph (the block beginning *"Check 6's claim is 'the repo's declared
    suite still passes'"*): append a paragraph stating that on the explicit-`p2p` branch a
    declared id that produced no terminal status — **the quarantine excluded** — routes to
    the environment path ahead of the pass, fail and timeout branches, with §1.2's measured
    shape, D1b's four-cause list (including the config-outside-`tests.paths` case), and
    D4's ordering ruling in one sentence each.
  * The `GradeRecord` field listing: add
    `not_run_node_ids: tuple | None   # declared ids with no terminal status, quarantine
    excluded; None on pytest, where the exit code carries this`.

* **`TASKS.md`** — four edits.
  1. Tick the item-8 bullet and **rewrite it**, because its second half is stale:
     `pass_to_pass` has written `_selected` on the explicit branch since `59f4488` (§1.3).
     The replacement records what landed, on which branch it can fire, the quarantine
     subtraction it depends on, and that it is unreachable on today's corpus.
  2. New **P2** item, worded from §1.5(e):

     > **A p2p sweep that silently collects fewer tests than the baseline is invisible on
     > the deselect branch.** `tests.p2p: []` declares no id list, so `_check_p2p`'s
     > `not_run` branch cannot fire there by construction, and pytest reports no executed
     > ids to compare against anything — `report_path()` is `None` and `report_args()`
     > returns `[]` deliberately, because a JUnit report needs `classname` mapped back to a
     > node id and a dotted segment is a package or a class with nothing in the XML saying
     > which. Two of the three ways the set could shrink are already closed:
     > `_check_test_restore` puts every tracked file under `tests.paths` back to the start
     > state and the deselect branch's scope is a subset of that, so a deletion or rename
     > cannot shrink it; an import break exits non-zero and is routed. What remains is
     > *configuration the restore does not cover* — the task's own root `conftest.py`
     > `collect_ignore` or `addopts` marker filter, a `skipif` injected into an imported
     > source module, **or a tracked runner config the SUBMISSION edits outside
     > `tests.paths`** (`vitest.config.ts`, `jest.config.js`, `package.json`:
     > `exclude` / `testMatch` / `setupFiles`) — all at exit 0. The only detector in the
     > record is `p2p_deselected` against `p2p_deselect_requested`, which `_check_p2p`'s own
     > comment records as vacuous on any task whose config deselects (`pallets/click`:
     > 30,007 measured against 7 requested). Closing it means an executed-**item count**
     > with a baseline stored on the `Oracle` — a new field and an `ORACLE_VERSION` bump
     > that re-derives every cached quarantine at two full suite runs per task, which is
     > why it is its own item.
  3. New **P2** item for the preflight half (review finding 8):

     > **`preflight` is blind to a declared p2p id that does not run, and the grader is
     > not.** `preflight` refuses a task whose f2p ids did not run
     > (`if not_run and red_outcome.kind != KIND_LOAD_ERROR`), and makes no such check on
     > either p2p run — the p2p-after site reads only `runner.classify(after_p2p).kind !=
     > KIND_PASSED` and never looks at `not_run`. That is the same f2p/p2p asymmetry
     > round-2 item 8 closed in the grader, one layer earlier, and it matters more here:
     > the repo is pinned at `base_sha` and the test half is committed, so a declared p2p
     > id cannot go stale over time — the reachable causes are a manifest authoring error
     > and a submission-caused config change. For the first, the gate could refuse the task
     > for free, before an image, a proxy or a token; instead item 8's branch fires at
     > grade time on **every run of every arm** of that task, after the money is spent.
     > `CLAUDE.md`: *"a precondition, not a run criterion, because by the time a record
     > exists the tokens are spent"*, and *"a task must be shown to discriminate, per task,
     > in its own image"*. The proof that it is open: item 8's own verification vehicle B
     > deliberately gives `ufo-214` an invented p2p id and the gate returns **GO**. Closing
     > it is one `if` at the p2p-after classification site plus a `PREFLIGHT_VERSION` bump,
     > which invalidates every cached preflight verdict — hence its own item.
  4. If the round-2 ledger in `TASKS.md` or `.superpowers/broaden/round2/` tracks item
     state, mark item 8 done there in the same commit.

* **`CLAUDE.md`** — no edit, per review 1's ruling on Q1. Its grader bullets name fix
  *shapes* a future editor must not undo, and this branch's shape is already stated one
  function up by `_check_f2p`'s comment. The preflight asymmetry (§7.3) is a `TASKS.md`
  fact, not a `CLAUDE.md` one. **Do not collide with item 1's `CLAUDE.md` sentence about
  the node argv**, which lands ahead of this item.

---

## 8. Verification

Run from `bakeoff/` with `.venv/bin/python`.

**Read every baseline immediately before making the change**, never from this plan: items
1, 2, 4, 5, 7 and 6 all land ahead of item 8 and each adds tests and mutation anchors. The
absolute numbers from round start (`1539 passed, 62 deselected`; 160 anchors) are already
stale at `8ab0b08`.

1. **Unit.** Record `.venv/bin/python -m pytest tests/ -q` before the change; after it,
   expect **+11 passed** (§4.2's eight, §4.3's two, and §4.4(12); §4.4(11) and (13) edit existing tests) and the same
   deselected count. `tests/test_preflight.py -k argv_preflight_validated` must be green
   with no edit (§4.5).
2. **Mutation.** Record `.venv/bin/python scripts/mutation_check.py`'s `N/N` before the
   change; after it, expect **+3** on both sides. **Runs solo.** A stale-anchor failure
   means a needle in §5 does not match the source byte-for-byte — fix the needle, never the
   source, and never the test.
3. **Logger gate.** `.venv/bin/python scripts/verify_logger.py` → `GATE PASSED`. Needs a
   Docker daemon; reports `GATE INCOMPLETE` and exits 1 without one.
4. **Integration A — the no-regression proof on a real pytest task.**
   ```
   .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
       --task-set ~/.cache/bakeoff-probe/taskset --tasks werkzeug-3037-duplicate-rule-error
   .venv/bin/python ~/.cache/bakeoff-probe/self_grade.py \
       ~/.cache/bakeoff-probe/taskset werkzeug-3037-duplicate-rule-error \
       ~/.cache/bakeoff-probe/eventlog-r2i8 <extra.diff>
   .venv/bin/python scripts/grade.py --event-log ~/.cache/bakeoff-probe/eventlog-r2i8
   ```
   `<extra.diff>` is authored for this run, **not** the existing
   `~/.cache/bakeoff-probe/extra.diff` (which is tomlkit's). Build it as
   `task.solution_diff` verbatim **plus a hunk deleting `tests/test_routing.py`** — the
   task's entire declared `tests.paths`, which really is that single file. Recipe:
   materialize the task, apply `solution_diff`, `git rm tests/test_routing.py`,
   `git add -A`, `git diff --cached <start_sha>`. Do not hand-write the deletion hunk.

   **Expected: the `-extra` run grades identically to the `-reference` run,
   `resolved: True`**, with `agent_modified_tests: True` and `not_run_node_ids: null`.
   That is the evidence for §1.5(c) and for the ruling: check 2's `git rm -r` +
   `git checkout <start_sha> -- tests/test_routing.py` puts the file back, so "the agent
   deleted a p2p test" never reaches check 6. If the `-extra` run grades anything else,
   **stop and re-plan** — the restore claim the ruling rests on is wrong.

5. **Integration B — the branch itself, on a real node task.** No gated manifest is node ×
   explicit-`tests.p2p` (§1.6), so make a scratch copy —
   `cp -r ~/.cache/bakeoff-probe/taskset ~/.cache/bakeoff-probe/ts-r2i8` — and edit **only**
   `ufo-214-without-trailing-slash-query/task.yaml`'s `tests.p2p`, from `[]` to two ids:

   * one **real** test in `test/trailing-slash.test.ts` (read the file in the materialized
     tree for its exact `fullName`), and
   * one `<same file>::<a name no test has>`.

   Two constraints on those ids, both refusals the loader makes:
   * the file half must be **under `tests.paths`**, or `node_adapter.validate_node_id`
     refuses the manifest at load time and the run proves nothing;
   * **neither may collide with `tests.f2p`** — `tasks.py` refuses an f2p/p2p overlap
     (*"are declared as both f2p and p2p"*), and `ufo-214`'s single f2p id lives in
     `test/trailing-slash.test.ts`, the same file the real p2p id is drawn from.

   **Why the gate lets this through, and it is not luck:** `preflight` reads only
   `runner.classify(after_p2p).kind != KIND_PASSED` on its p2p-after run and never looks at
   `not_run` (§7's third `TASKS.md` item). The invented id is skipped, the rest pass, the
   run is `KIND_PASSED`, and the manifest gates **GO**. Record that in the step so the next
   reader does not delete it as flaky.

   Then preflight and self-grade that scratch task set as in (4). **Expected:** the
   reference run grades `not_graded_reason: environment_error`,
   `environment_error_check: "p2p"`, and `not_run_node_ids` naming exactly the invented id.

6. **Integration C — the blocking fix, on the same scratch task.** Re-run B with the
   manifest's second id changed from an invented name to the **real** name of a *second*
   existing test in that file, and with the oracle forced to quarantine it. If forcing the
   quarantine through `derive_quarantine` is impractical offline, the §4.2(3) unit test is
   the pin and this step may be recorded as not run — say which, in the commit message.
   Do **not** record it as passed.

7. **Graph.** `graphify update .` after the source edits.

Revert the scratch task set afterwards; it is a probe artifact and does not enter the repo.

---

## 9. What this does NOT do

* **The deselect-branch superset check.** Refused with §1.5's measurement; the residue is
  filed as a `TASKS.md` P2 item (§7.2).
* **The symmetric blindness in `preflight`.** `preflight` refuses a task whose **f2p** ids
  did not run and makes no such check on either p2p run. That is the same asymmetry, one
  layer earlier, and closing it here would widen this item into a `PREFLIGHT_VERSION` bump
  that invalidates every cached preflight verdict. It is **deliberately left open** and
  filed as its own `TASKS.md` P2 item (§7.3) — which is also why §8.5's scratch manifest
  gates GO at all.
* **`p2p_missing_node_ids`.** Not added. The brief's candidate name belongs to the refused
  half — it would name ids missing from a baseline that does not exist. What *is* added is
  `not_run_node_ids`, which names ids the run **requested** and did not execute.
* **Any `Oracle` field or `ORACLE_VERSION` move.** No cached quarantine is invalidated.
* **Any `PREFLIGHT_VERSION` move.** D1a is identity at every preflight call site.
* **Any change to `oracle.py`, `tasks.py` or `runners/*`.**
* **Any argv change.** `select` / `pass_to_pass` / the adapters emit exactly what they emit
  today — D1a edits `_selected`, not an emitted argument — so
  `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` stays green without
  being edited.
* **`SCHEMA_VERSION`.**
* **`grade.py`'s end-of-batch summary.** It is not taught to count this new environment
  cause; the P3 double-count item already open there is a separate decision.
* **Any pytest behaviour.** `not_run` is structurally empty there; every pytest verdict is
  byte-identical.
* **Round-2 item 11** (same-file duplicate `fullName`) and **item 1** (K argvs). This item
  reads whatever `Outcome.not_run` holds; D1a changes only which ids were *requested*, not
  how the report is read.

---

## 10. Rulings adopted from review 1

All five open questions were ruled by review 1 and are adopted, not re-argued.

* **Q1 `CLAUDE.md`** — no edit (§7). Do not collide with item 1's node-argv sentence.
* **Q2 D4's relabel in the `GRADER_VERSION` note** — stated, and widened to both relabels
  (§6).
* **Q3 `_check_f2p` also writes the field** — kept (D2, §3.2).
* **Q4 the scratch manifest** — accepted; no node fixture task is committed. Both loader
  constraints and the reason the gate passes it are written into §8.5.
* **Q5 does the fixture survive item 1** — yes, but because every declared id in it shares
  one file (K = 1), not because the report is read per check. Written into §4.1 and D5.

Review 1 also upheld, and this revision does not re-open: the refusal of the structural
superset check (D6/§1.5), and routing uniformly to `state.environment` (D1b).

---

## Review 1 → changes

| # | finding | change |
|---|---|---|
| 1 | **BLOCKING** — quarantined ids fall through `verify_selected` into `not_run`, so the new branch would grade `not_graded` on every healthy node run of a task with one flake | New **§1.4** states the mechanism with the five measured links; new **D1a** fixes it at the source (`pass_to_pass` subtracts `extra_deselect` from `_selected`) with the grader-side alternative argued and rejected; **§3.1** is a new subsection; new test **§4.2(3)** `test_a_quarantined_p2p_id_is_not_reported_as_not_run`; new **third mutation anchor**; D1b's comment and D2's docstring both note the subtraction; §6's note and §9 updated. **D4's completeness claim punctured**: a partial quarantine plus partial staleness reaches a wholly-empty selection without `derive_quarantine` raising. `PREFLIGHT_VERSION` shown not to move (`grader.py:1313` is the only `extra_deselect` caller) |
| 2 | line anchors were the working tree's, not HEAD's | Re-baselined on **`8ab0b08`** (item 3 landed, so the four disputed anchors are now correct); a header paragraph states line numbers are advisory and function names are the contract, naming the six items that land ahead |
| 3 | `GUARANTEES` → `MUTATIONS`; stale counts | §5 renamed and states there is no `GUARANTEES` identifier; §8.1/§8.2 replaced absolute baselines with "record before the change, expect +11 tests / +3 anchors" |
| 4 | D5's per-check `cat` claim is wrong (item 1 reads per **group**) | D5 rewritten to per-group; the surviving reason (K = 1 because every fixture id shares a file) moved into **§4.1's helper docstring as a constraint**; the cross-file masking sentence dropped as inaccurate |
| 5 | predicted `GRADER_VERSION` `10` already wrong (item 1 also bumps) | §6 predicts no number — "at least 10, read the literal". Added the checked clause that **no item ahead moves `GRADE_SCHEMA_VERSION`**, making the asymmetry deliberate |
| 6 | D1's cause list not exhaustive — a submission-edited runner config outside `tests.paths` | Fourth cause added to D1b's comment with the "ruling unchanged because the grader cannot tell it apart" clause; mirrored in §1.5(e), §7's spec paragraph and §7.2's `TASKS.md` bullet |
| 7 | branch also outranks `KIND_FAILED` and the timeout branch, unargued and unpinned | **D4 rewritten for all three branches** with the partially-executed-selection argument; new test **§4.2(6)** `test_a_p2p_id_that_failed_beside_one_that_never_ran_is_still_not_graded`; the `False` → `None` relabel named in §6's note |
| 8 | preflight's symmetric blindness neither closed nor filed; §9's reason did not describe it | New **§7.3** `TASKS.md` P2 item with the cost-of-lateness argument and the two `CLAUDE.md` quotations; **§9's bullet rewritten** to say it is deliberately left open and why; §8.5 now records that this blindness is what lets the scratch manifest gate GO |
| 9 | §4.2's pytest fixture — `is_p2p` requires `--deselect`, which the explicit branch does not emit | §4.2(8) written out: **no rule**, `FakeEnv`'s documented default, with a docstring instruction not to "fix" the matcher |
| 10 | `test_round_trip_keeps_none_distinct_from_empty` is three hard-coded assertions, not a sweep | §4.4(13) replaced with the two concrete edits: field added to `_record()`'s base dict, and an explicit override + assertion in that test |
| 11 | second anchor would not "die with a TypeError" — it degrades silently | §5's closing paragraph rewritten to the silent-degradation argument (`caught = returncode != 0`); the instruction stands |
| 12 | `_check_f2p` insertion point ambiguous | §3.2 now says **immediately after the existing nine-line comment block and immediately before `state.environment(`**, with the reason |

**Disputed: none.** All twelve adopted as written; findings 1, 4 and 7 are addressed by
going further than the fix as described (finding 1 at the source rather than in the grader,
so `_selected` stops lying; finding 4's constraint written into the fixture contract rather
than only into D5; finding 7 pinned by a test as well as argued).

---

### Review 2

**APPROVED — 12 / 12 addressed, 0 open findings**, re-reviewed against HEAD `2ddd68a`
(`git diff --stat 8ab0b08 2ddd68a -- bakeoff/` is `HARVESTING.md` only, so every `src/`
anchor this plan cites is valid at HEAD). Review 2 independently re-verified the blocker's
resolution — that `p2p_args` folds `deselected` into the negative lookahead
(`node_adapter.py:268-269`), that `executed_names` yields only `("passed", "failed")`
(`:97`, `:400-401`), that after D1a a quarantined id is no longer in `requested` so
`verify_selected` cannot return it, that `grader.py:1313` is the sole `extra_deselect`
caller (every other `pass_to_pass` site enumerated), and therefore that
`PREFLIGHT_VERSION` does not move. It also confirmed anchor 3 fires on revert and that
§4.2(6) is mutation-covered by anchor 1 even before note (c) widens the selector. None of
that is re-opened here.

Three non-blocking implementer notes were raised. **All three are folded into the plan
body above**, so an implementer transcribing §3 and §5 picks them up without reading this
section:

| note | where it landed |
|---|---|
| **(a)** §3.1 updated only the `pass_to_pass` **method** docstring; `_Runner.__init__`'s `_selected` **attribute** docstring is the prose that actually carries this invariant (*"What the last invocation asked for BY ID"*, *"Written by `select` and by `pass_to_pass`'s explicit branch"*), and by this repo's convention it is where the next reader looks | **§3.1** gains a bullet requiring the clause *"… minus what the same argv deselects"* on that `#:` block |
| **(b)** anchor 3's label read `"grader: …"` while the mutated file is `src/bakeoff/preflight.py`, so an operator scanning labels for preflight coverage would not find it | **§5** anchor 3 relabelled `"preflight/grader: report a quarantined p2p id as one that did not run"`, with a comment saying why the label names both |
| **(c)** anchor 1's selector `-k partly_stale_p2p_selection` does not select §4.2(6), though its `if False:` mutation makes that test red too — one anchor could prove both ordering claims | **§5** anchor 1's selector widened to `-k "partly_stale_p2p_selection or failed_beside_one"`, with a comment recording that this is the file's first `or` selector and that **the quotes are required** — `run()` splits on `" -k "`, strips one layer of quoting, and passes the remainder as a single `-k` argument, so an unquoted expression would split on the space and select nothing |

Note (c) changes no count: §8.2 still expects **+3 anchors**, and §8.1 still expects
**+11 passed**, because it widens an existing selector rather than adding a test or an
entry.
