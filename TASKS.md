# Pending Tasks

Single list of open work for the LLM bakeoff eval. **This file is the backlog.**
`tasks/todo.md` is the opposite — a completed-work review log, one section per
finished task. Nothing here is done; move it there when it is, and take the
evidence with it.

Spec: [docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md](docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md)
Harness plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

---

## READINESS — answer this before reading anything else

There are 40-odd open items below. **Five of them block collection.** The rest
block *publication*, or are recoverable from stored artifacts at any time. This
section exists so that distinction does not have to be re-derived.

### Can I do a real-issue test run? — **Done. 2026-08-13.**

Four arms, `pallets/click` #3360, on the image that fixes the stale `.pyc`.
4/4 records, no stranded cells, no uninterpretable rows, `wire_unattributed: 0`
on every arm, all four resolved by a hand-run of the oracle. Evidence in the
next section. Nothing is pending for this.

**Re-run that evening on schema 3.7.0, and the result inverted.** Same task,
same four arms, N=1 each: sonnet-runtime and kimi **resolved** (1623/1623 on the
full suite, no regressions); gemma burned all 40 turns for a 238 b diff that
fixes nothing; nemotron wrote *"Let me check the formatting.py"*, emitted
`end_turn` with no tool call, and stopped after 2 turns and 108 output tokens
with a **0-byte diff**.

So the morning's "ceiling task" reading was a single sample, and so is this one.
The honest statement is the one neither run supports on its own: **at
temperature 1.0 and N=1 the between-run variance on this task is larger than the
between-arm spread.** 4/4 and 2/4 on the same cells, hours apart. That is
calibration evidence for §3.5's repeat count, not a capability ranking — and it
is the strongest argument in this file for why N=1 cannot ship.

The task *can* discriminate. Whether it does on any given run is a coin flip,
which is a different problem from a ceiling task and needs the same fix: repeats.

### Can I start the large-scale eval? — **No. Four things, in dependency order.**

| # | blocker | why it blocks | where |
|---|---|---|---|
| 1 | **The dataset.** 1 task of ~80. It discriminates 2/4 on one run and 0/4 on another | nothing downstream can start; the calibration pilot needs it too | Out of scope §1 |
| 2 | **Caps are unset.** 40 turns is a placeholder and is already binding — nemotron used 40/40 on 2026-08-13, mid-verification, and its diff resolved anyway | §5.4 derives caps from the pilot at ~p95×2; a cap tuned to the incumbent scores a style difference as capability | P3 |
| 3 | **Credential refresh.** Measured 1 h ⇒ **~20 cells per login**; 3,200 cells ⇒ ~160 re-logins | the matrix runs, but never unattended | P1 |
| 4 | **No run-level retry.** `attempt_number` has no caller | every infra hiccup leaves a permanent hole; today's work bounds it to ~3 cells/arm and names them, but cannot fill them | P1 |
| ~~5~~ | ~~**The offline grader.**~~ **Closed 2026-08-17.** | — | `bakeoff/src/bakeoff/grader.py`, `oracle.py`, `grade_schema.py`, `scripts/grade.py` |

1 and 2 are one project: harvest the set, run the pilot, read the caps off it.

**Blocker 5 closed 2026-08-17.** `scripts/grade.py` grades a stored event log
offline and appends one `GradeRecord` per run to `<event-log>/grades/grades.jsonl`,
beside the log and never into it. The nine-check ladder is `grader.py`; the oracle
is the manifest's declared `f2p`/`p2p` and only the flake quarantine is derived
(`oracle.py`, two identical p2p runs at the reference state, symmetric difference).
Design: [specs/2026-08-17-offline-grader-design.md](docs/superpowers/specs/2026-08-17-offline-grader-design.md).
What it does **not** yet have is a §10.3 reporting view over `grades.jsonl` —
that is still to write, and it is publication work rather than collection work.

**A verdict set graded off the collection machine carries the `image_matches_run`
caveat.** `GradeRecord.image_matches_run` is `True` only when the task image the
grade ran in is the image the run itself used. Grading later, elsewhere, or after
a base-image rebuild sets it `False` (or `None` when it could not be determined),
and a §10.3 report that aggregates such grades is reporting "this submission
passes in *an* image built from this manifest", not "in the image that produced
it". Filter on the field or state the caveat; do not silently mix the two.

### Sizing, measured 2026-08-13 rather than assumed

Mean cell wall clock across the four arms was **150 s** (89.6 / 147.5 / 109.2 /
255.7) plus ~25 s of harness overhead. The driver is strictly sequential.

| shape | cells | sequential | logins at 1 h |
|---|---|---|---|
| 80 tasks × 4 arms × N=10 | 3,200 | **6.5 days** | ~160 |
| 80 tasks × 4 arms × N=3 | 960 | 1.9 days | ~48 |
| 30 tasks × 4 arms × N=3 | 360 | **18 hours** | ~18 |

The last row is the smallest shape that yields a defensible scorecard. It needs
blockers 1 and 2 (5 is now closed), and makes 3 tolerable. It will not support
pass^k or tight intervals.

### Do before collection, cheap, not blocking

Both done at schema 3.7.0, 2026-08-13. See `tasks/todo.md` for what they cost
and what measuring the first one exposed.

**Read every pre-3.7.0 record with three caveats.** They are permanent — the log
is append-only.

Counts below are scoped and stamped: the 25 records in the 7 event logs under
`~/.cache/bakeoff`, as of 2026-08-14. That scope matters — the sibling
`~/.cache/bakeoff-*` directories are test and gate fixtures, not collected data,
and the repricing item in P1 below counts a different archive again. The counts
have already drifted once, downward, because three event logs were deleted: the
event log is append-only, the *directory* of event logs is not.

- `host.contention_flag: false` is a dataclass default, not a measurement, on
  the **9** pre-3.7.0 records. It is a real measurement from 3.7.0 on, and the
  earlier form of this line — "all 24 stored records carry it" — is now false as
  well as stale: 3.7.0 and later carry 12 `false`, 3 `None` and one **`true`**.
- `artifacts.*` paths resolve to the **last** run of that cell, not necessarily
  to the record holding them: an unstamped artifacts root plus a per-cell
  `rmtree` meant a later matrix deleted an earlier run's files. **5** stored
  records provably disagree with the file they point at (was 12; the drop is
  deleted logs, not a smaller blast radius — all 25 survivors are checkable).
- `run_id` names no collection episode, so **5** ids appear in more than one
  event log — `11ab9cf6527a188f` in **six**, and this caveat alone is not confined
  to pre-3.7.0: that id collides at 3.8.0 too. An offline pass merging logs must key
  on `(collection_id, run_id)` and treat `collection_id: ""` as unknown rather
  than as a shared episode.

- [ ] **Artifacts now accumulate per invocation.** The stamped root is the fix
  for the deletion above, but nothing reclaims the trees, and a 3,200-cell
  matrix keeps every one. Size it and decide a retention policy. (P3)

### Blocks the number, not the collection

Candidate cache pricing — gemma and kimi both priced `null` again on
2026-08-13. Every P3 decision. The repricing of the pre-3.0.0 archive.

### The pruned-mirror cache has its own open list

Shrunk to the remainder on 2026-08-14: the deliberately unanchored
`materialize` alternates assertion (kept — deleting it fails no test, by
design); `ensure_mirror`'s full mirror still at the umask's mercy while the
pruned one is now 0700; and `images.py`'s `git archive` reading the full
mirror outside the new repo lock (harmless while a fetch never removes
`base_sha`, recorded so "the mirror race is closed" stays qualified). The
six prose defects, the lock, the streaming sweep, the utf-8 decode, the
cache mode and the two uncovered IO paths all closed — the ledger and the
traps are in [HANDOFF.md](HANDOFF.md).

### Read every pre-2026-08-13 figure with the pyc caveat

Keyed on `versions.container_image_digest`, which is what `--allow-mixed-images`
gates on (`matrix.py:204`, enforced at `run_matrix.py:393`): `sha256:1521e391…`
is pre-fix, `sha256:8942bd48…` is post. 9 of the 25 stored records are pre-fix.
The heading says "figure" advisedly — this caveat is about published numbers —
and the digest extends it to records, which is a broadening, not a correction.

**The heading's date is local; `started_at` is UTC**, which puts three of those
nine on 2026-08-13Z though they ran 17:03, 17:04 and 18:07 on 2026-08-12 local.
Nothing in the record says which zone it is in, and reading one against this
prose is exactly the UTC/PDT misread `CLAUDE.md` records costing a session.

The base image digest changed with that fix, so nothing before it is
environment-comparable, and the defect could hand an agent its own pre-fix
code. Re-measure rather than mix; `--allow-mixed-images` exists and should not
be used across that boundary.

### Read every record written before 2026-08-14 with the run-tree caveat

Keyed on `versions.harness_commit`, not on the date in this heading — 2026-08-14
is both the day the prune landed and the day the clean records were written, so
a same-day record has no date-visible side. Of the 25 stored records, 21 are at
`b4301ce8`, `dd6c4368` or `715d206f`; the 4 at `1d6bafdd` ("fix: the run tree
contained the answer") are clean. Every record carries `-dirty`, so the field
names the last *commit*, not the working tree — `1d6bafdd-dirty` certainly has the
prune, and nothing earlier can, because `pruned_mirror_path` did not exist before
that commit (2026-08-14 10:33:07 local) and nothing under `~/.cache/bakeoff` was
written between then and the first clean collection at 13:53.

**The reference fix was reachable in the agent's own repository.** `materialize`
cloned run trees from the full upstream mirror, so `refs/heads/main` sat 181
commits ahead of the start state on `pallets/click` and
`git log main --grep=3360` named the merged PR. `HEAD` was detached at
`start_sha`, so `git log` and `git diff` looked clean; `git log --all`,
`git branch` and `git show main` did not. Closed by `ensure_pruned_mirror`.

This is **not** evidence that any stored run used it — no record captures the
agent's git invocations, so for those 21 it cannot be checked either way. None
of `RunRecord`'s 58 fields (51, 54 or 58 present per record, by schema version)
describes the run tree's object store, and kept trees exist only for the 08-14
collection. It is a confound that cannot be ruled out retrospectively, and the
log is append-only. It is also *differential*: an arm that greps history while
orienting was advantaged over one that did not, which is a §6.4 problem rather
than a uniform bias an offline pass could subtract.

**Distinct from the leak above, which stays unfalsifiable for those 21: the two
prune-cache defects fixed in `ad193f2` affected zero stored records.** One raised
before any run started — `materialize` is called at `run_matrix.py:211`, ahead of
`execute_run` at `:231`, so a `TaskError` there produces no record at all, not
even a CRASHED row. The other needed a write into a pruned mirror, which existed
only from `1d6bafdd`, and all four records under it were checked from their kept
trees: `rev-list --count HEAD` is 3,131 and so is the store's total commit-object
count, so no commit outside the start state's history survived — which is what
`git log --all` would need. That is direct evidence about what the agent could
see; the first defect's argument is a code path and needs no timeline at all,
and only the scoping of the second to `1d6bafdd` rests on the clock above.

- [ ] **Pruned mirrors accumulate too.** One bare mirror per `(repo, base_sha)`,
  ~5 MB for click, ~400 MB at 80 tasks. Same retention question as the
  artifacts item above, and it belongs in the same policy. (P3)

---

## Where things stand — 2026-08-13

**The harness works and the record is honest.** Tasks 1–11 complete, Task 12's
offline half done, Gate 0 (every CAPTURE-class observability gap) closed at
schema 3.3.0. Gate 1's **harness half** closed at schema 3.5.0 — a task is now
a directory on disk, validated before anything is spent, and scheduled by a
resumable driver. The credential-expiry blind spot closed at **schema 3.6.0**, and the provider
finish reason, the `host` block and collection identity at **schema 3.7.0**.
`verify_logger.py` PASSED on 3 of 3 consecutive runs, unit suite 474,
integration 37, `mutation_check.py` **91/91**.

**That `verify_logger.py` line used to be worth less than it looked.** Until
2026-08-13 the gate failed on 2 of 3 runs from a stale-`.pyc` defect (see item
4 below), so a recorded PASS was partly luck. It is deterministic now, and the
3-of-3 is the claim.

**A seventh harness defect, found while verifying that work, and this one
destroyed records rather than misattributing them.** `git add -A` runs
concurrently with the agent, and Claude Code's Write is atomic
(`calc.py.tmpXXXX`, created then renamed). A rename landing between git's
readdir and its stat makes git exit 128, and because the checkpoint recorder is
called from inside the agent's stdout loop the exception unwound past the
container into `execute_run`'s catch-all: **CRASHED, zero turns, zero tokens,
no diff, for a run that was working.** Measured once in seven offline arms.
Fixed three ways — the race is retried, the residual failure is contained, and
`checkpoint_error` names the gap, because containment alone swaps a loud wrong
record for a quiet one. At N=10 over 2,400 runs this would have shown up as an
arm-correlated crash rate.

**Gate 1's exit criterion is met: one real issue, end to end, on all four
arms** (`pallets/click` #3360). Re-run 2026-08-13 on the image that fixes the
stale-`.pyc` defect; the 2026-08-12 figures are superseded and are kept below
only as the before/after. Four records, no stranded cells, no uninterpretable
rows, every arm isolated with full wire attribution, `wire_unattributed` 0 on
all four.

| arm | turns | tools | wall | cost | diff | terminated_by |
|---|---|---|---|---|---|---|
| claude-sonnet-5-runtime | 16 | 15 | 89.6 s | $0.329 | 791 B | agent_finish |
| gemma-4-31b | 23 | 22 | 147.5 s | unpriced | 2,772 B | agent_finish |
| kimi-k2-5 | 24 | 23 | 109.2 s | unpriced | 2,091 B | agent_finish |
| nemotron-3-super-120b | 40 | 40 | 255.7 s | $0.203 | 13,552 B | **turns** |

Superseded 2026-08-12 run, taken while the stale `.pyc` was live — every one of
these figures was produced in an environment that could hand the agent its own
pre-fix code: sonnet 16/98.9 s/$0.349/618 B, kimi 23/62.3 s/1,485 B, nemotron
40/101.3 s/$0.180/2,493 B, gemma **40 turns (cap)**/369.5 s/1,215 B. Gemma is
the visible move — 40 turns and the cap, down to 23 and `agent_finish`.

**All four resolved it**, by a hand-run of the oracle over the four stored
diffs — every submission applied cleanly, and f2p and p2p both pass after
restoring the test files (§4.2.1 check 2). That pass was run by hand, not by
the harness, which still never grades.

**Three of the four properly finished, and it is checkable from the wire log.**
Raw provider `finish_reason` runs *n−1* × `tool_calls` then exactly one `stop`
on sonnet, gemma and kimi, with a final message reporting verification already
done — the opposite of the quit-mid-plan signature, which is prose promising a
*next* action with no call attached. Nemotron shows 40 × `tool_calls` including
the last, with a tool call in flight and the text *"Let me also verify … by
checking our change:"* — it was truncated by the turn cap mid-verification, not
an early stop, and its diff resolves anyway. Every `tool_use` id issued was
answered by a `tool_result` in the next request (15/22/23/39, zero unmatched),
so no call was silently dropped. **None of this is in the record** — see the
`finish_reason` coercion item in P2.

Two arms left scratch files in the submission — nemotron
`src/click/formatting.py.fixed` (most of its 13,552 B) and gemma
`reproduce_issue.py`. Model behaviour, correctly captured by §5.6, and a reason
diff size measures tidiness as well as the fix. **No `__pycache__` in any
diff**, which is the pyc fix holding.

**Read it as a proof of the path, not as a result.** N=1 per arm on one task
is not a rate, and by §3.5's own drop rule a task all four models solve is a
**ceiling** task with no discriminating signal — this one would be dropped
during the calibration pilot. It is the right task to prove Gate 1 and the
wrong task to keep in the frozen set.

**Phase 0c is GO on the fixture task**, twice: a four-arm N=3 on 2026-08-12
(`20260812T175513Z`, 12/12) and a four-arm N=1 confirming Gate 0 live
(`20260812T222630Z`, 4/4). Every run lands the same 157-byte diff. **Both
predate the stale-`.pyc` fix**, and the fixture's one-line change is exactly
the same-size edit that defect hides — so read them as evidence the loop runs,
not as evidence of what any arm can do.

**Read both the way 2026-08-11 taught us.** Run A that day was GO and run B
immediately after was NO-GO on Nemotron alone. One GO is one observation, not a
rate, and the N-sizing item below is unchanged by either of these.

### What is proven, and what is not

Proven end to end, on every arm: the loop, both transports, container isolation,
per-turn checkpointing, wire capture, the §5.2 config dump, and — since Gate 0 —
that the record describes the request the *provider* answered rather than the one
Claude Code sent.

Not proven by anything yet: **any statement about relative model quality.** One
fixture task, one 157-byte diff, and the harness does not grade. There is no
dataset, no matrix driver and no offline grader, which is what P0 and the
out-of-scope plans below are about.

### Six things every reader of an old number needs

1. **Every cost and turn figure predating schema 3.0.0 is inflated ~2×.** Claude
   Code emits one transcript record per content block with the whole `usage`
   repeated in each, and `parse_trajectory` counted records. The re-derived parse
   matches the wire log on 16/16 live runs where the stored records did not. The
   archive keeps its figures — the log is append-only — so see the repricing item
   in P1 before quoting any of them.
2. **Every dollar figure predating 2026-08-11 uses the old $3/$15 Sonnet book.**
   `Versions.pricing_basis` says which book produced a given `cost_usd`.
3. **Every capability figure from before 2026-08-12 was taken where the agent
   could not run tests.** The image shipped no `pytest` and the fixture was not
   importable, so §3.3's "runs tests, sees failures, self-corrects" terminated
   after "edits" and every arm was scored on one unverified guess. Fixed; the two
   GO runs above are the only data taken after it.
4. **An eighth harness defect, found 2026-08-13 while testing the seventh, and
   this one corrupts the self-correction loop itself.** CPython invalidates a
   `.pyc` on (source mtime in whole seconds, source size) and **both halves are
   ordinary**: an operator swap, an off-by-one or a boolean flip preserves byte
   count, and an agent edits and re-runs the suite inside the same second.
   Measured in the eval image — fix applied, source correct on disk, pytest
   still red, pyc header reporting `mtime=1786605617 size=32` on both sides.
   §3.3's loop is "runs tests, sees failures, self-corrects"; this feeds it the
   pre-fix behaviour, so a correct edit reads as wrong and the agent corrects
   away from the answer. It was already making `verify_logger.py` fail on **2
   of 3** consecutive runs, and the first live smoke run shipped a diff whose
   first hunk was a binary `calc.cpython-312.pyc`. Closed with
   `PYTHONDONTWRITEBYTECODE=1` in the image. **Every capability figure taken
   before 2026-08-13 was taken with this live**, on top of the Phase 0c caveat
   below. The checkable form of that boundary is
   `versions.container_image_digest == sha256:1521e391…`, not the date — see the
   pyc caveat above, where the two disagree by three records under UTC.

5. **Every record from before `1d6bafdd` was taken with the merged fix readable
   in the agent's own repository.** The one item on this list that is not about
   a number being wrong — it is about the task being easier than it looks, and
   *differentially* so, since only an arm that runs `git log --all` collects it.
   Keyed on `versions.harness_commit`; 21 of 25 stored records are exposed. See
   the run-tree caveat above, including why the two prune-cache defects found
   while fixing it affected zero records.

6. **Eight of eight "model failures" so far have been harness defects** —
   Sonnet's beta header, Kimi's tool-id mangling, Gemma's `propertyNames`,
   Gemma's colliding ids, Gemma's `max_tokens` rejection, Gemma's
   `reasoning_effort` requirement, the `git add -A` checkpoint race, and the
   stale `.pyc`. Each looked deterministic and total beforehand, and the last
   two were found while verifying a fix for something else. The run-tree leak in
   item 5 is not on this list — it was found by reading the materialization path,
   not by investigating a failure — which is the point: it would never have
   surfaced as one. Weigh that base rate before reading the next total failure as
   capability.

### The road to a published result

Four gates. Each blocks the next; the section numbering below follows them.

| gate | what it unlocks | state |
|---|---|---|
| **0 — capture** | the record can describe a run honestly | **done for the unrecoverable half** (3.3.0, extended 3.6.0 and 3.7.0); ~6 fields still structurally unpopulated, listed below |
| **1 — a real task** | one real issue, end to end, on all four arms | **harness half done** (3.5.0, image fixed 2026-08-13); the dataset is the rest |
| **2 — scale** | a 2,400-run unattended matrix | **P1 below.** Binding constraint measured 2026-08-13: ~20 cells per credential window |
| **3 — numbers** | a scorecard anyone can defend | **P2 + P3 below**, plus the offline grader |

**Gate 0 is done in the sense that matters and not in every sense**, and the
distinction is worth keeping straight. What is closed is the *unrecoverable*
half: no observation the harness can make is now lost at write time, and a
credential failure that used to read as a quiet model failure is labelled and
stops the run. What is still open is a set of fields that are **structurally
empty on every record**, measured across the four live runs of 2026-08-13:

| field | why it is empty | class |
|---|---|---|
| ~~`host.cpu_pct_p95`, `host.mem_peak_mb`, `host.contention_flag`~~ | closed 3.7.0 — `HostSampler` samples `docker stats` for the life of the agent; `contention_flag` is `bool \| None` and narrow by design | **was capture** |
| `time.retry_backoff_ms` | the proxy retries up to 3× and nothing records the backoff, so retry latency hides inside `inference_ms` | **capture** |
| `tokens.reasoning` | not representable on the candidate adapters, and the provider returns 0 anyway | **capture**, only bites if thinking is turned on |
| `tokens.cache_write_1h` | LiteLLM's Converse bridge drops Bedrock's `cacheDetails` | **capture**, harmless while the TTL is hardcoded 5m |
| `truncation_events`, `tool_calls.malformed`, `tool_calls.errored`, `p2p_regressions`, `failure_class` | need the oracle or raw completions; §6.4 puts both offline | **derivation, by design** |

The rest of the 29 empty fields on those runs are honest zeros — no crash, no
scanner error, no unattributed call, a clean parse. `wire_unattributed: 0` in
particular is a *measurement*, and it is what licenses trusting the
wire-derived fields.

Gate 0 was urgent because a capture gap is unrecoverable: the observation is
never written and no offline pass can invent it. Everything left is either a
prerequisite (Gates 1–2) or a **derivation** gap — the observation is sitting in
`wire.jsonl.gz` and simply is not surfaced — which can be closed at any time,
including after Phase 4. Each P2 item says which it is.

---

## P0 — Blocking a DISCRIMINATING task set (Gate 1)

**Renamed 2026-08-13.** A real-task run is no longer blocked — one ran that day
on all four arms and is recorded above. What is blocked is a task set that can
tell the arms apart: the set holds one task, whose outcome swung 4/4 to 2/4
between two N=1 runs on the same day, so the section
title used to promise something already delivered while the actual gap went
unnamed.

**The harness half is built** (2026-08-12, schema 3.5.0). The input path exists:
`tasks.py` (manifest, loader, reference split, materialization),
`images.py` (generated per-task Dockerfiles), `preflight.py` (the task gate),
`matrix.py` + `scripts/run_matrix.py` (the collection driver). `proxy.py` and
`session.py` are the topology and the §5.2 dump, moved out of `smoke_test.py`
so the Phase 0c gate and the real run are the same environment rather than two
implementations of it.

One real task exists and is validated: `taskset/click-3360-write-usage-empty-args`
— `pallets/click` issue #3360 / PR #3434, 7 fail-to-pass tests, 1,615
pass-to-pass, 1.4 s suite. **What remains for Gate 1 is the dataset itself**
(~80 tasks, §3.5), which is the out-of-scope plan below.

- [x] **Task manifest and loader.** `task.yaml` per §3.7 plus `reference.diff`,
  the merged PR verbatim, split into a test half and a solution half by path —
  a partition, checked as one, so nothing can be dropped from the reference.
  Loud on: a `base_sha` that is not 40 hex, a `base_sha` the repo does not
  contain, a reference with no test half or no solution half, a rename crossing
  the boundary, a duplicate `task_id`, an unknown `--tasks` id.

- [x] **Per-task container images.** Generated from the manifest rather than
  hand-written, because the two most expensive failures are structural and a
  generator cannot emit them: an image left as `USER root` makes Claude Code
  refuse `bypassPermissions` and exit before one event, and an inherited
  `ENTRYPOINT` turns `sleep infinity` into an immediate exit. Task pins beat the
  base image's — measured: click's suite does not *collect* under the base's
  pinned pytest 9.1.1.

- [x] **A per-task precondition that is stronger than "pytest is on PATH".**
  `preflight.py` proves the task is red before the reference fix and green
  after it, inside the pinned image, offline, before the proxy starts. The
  load-bearing detail is pytest's exit code: `1` is "tests ran and failed",
  `2`/`4`/`5` are "the environment is broken", and `returncode != 0` accepts
  the Phase 0c failure as evidence the bug is present.
  It earned its place on the first real task: click's suite needs `less` on
  `PATH`, and without it the pager test closes the borrowed stdout and 189
  unrelated tests error — invisible on macOS, and an agent handed that suite is
  debugging the image.

- [x] **Matrix driver.** Round-major with a recorded seed: every (task, arm)
  once per round, shuffled within the round, so §5.7's randomised-and-
  interleaved holds and repeats are separated by a whole round rather than by
  luck. Resumable across invocations, which is mandatory rather than nice —
  `write_run` opens `"x"` and `run_id` is deterministic, so the smoke script's
  fresh-root-per-invocation trick cannot work for a multi-day matrix.
  **Resume's trap is closed too:** `task_version` is not part of `run_id`, so
  an edited task would leave every cell looking complete and silently mix two
  tasks under one `task_id`. That is a hard refusal, per cell. A differing
  image digest warns and requires `--allow-mixed-images`.

- [x] **The `.gitignore` item, restated as the property that matters.** Checked
  as "running the suite leaves `git status` clean", not as "the repo has a
  `.gitignore`" — a `.gitignore` that does not cover what *this* suite drops
  passes the second and fails the first. Remedy is a manifest-declared
  `gitignore_extra`, applied in the setup commit and therefore visible in
  `start_sha`.

- [x] **`task_set_commit` is populated** from the task-set repo's git state,
  `-dirty` scoped to the set rather than the whole worktree. Schema 3.4.0,
  because an empty string changed meaning: it used to say "no dataset exists",
  and now says "this task did not come from a task set".

- [ ] **Two Gate-1 items are still open and both are the dataset, not the
  harness.** Harvest the remaining tasks (§3.5's ~80) and decide the prompt
  policy below. Everything above runs against as many tasks as exist.

- [ ] **The test half is applied at setup, and that is a methodology choice.**
  A real bug-fix PR carries the test that proves the fix, so at `base_sha` the
  oracle does not exist and §3.3's "runs tests, sees failures, self-corrects"
  loop has nothing to run — the Phase 0c truncation arriving through the
  dataset instead of the image. Applying it makes the task "make this test
  pass", which is what SWE-bench measures and is *not* identical to the
  harvested workflow. The alternative — a hidden oracle applied only at
  grading — makes every task's difficulty depend on the agent's test-writing
  habits, confounding the thing being measured. **Default: apply.** It has to
  be stated on the scorecard, not left implicit.

- [ ] **Nemotron 3 Super quits mid-plan, and the ruling gates N.** Investigated
  2026-08-11 and confirmed to be the model, not the bridge. The failing response,
  read raw off the wire:

  ```
  finish_reason: 'stop'   tool_calls: None   function_call: None
  content: 'Now let me check the test file to see what the expected
            behavior should be:\n'         completion_tokens: 46
  ```

  It narrated its next action and ended the turn without emitting the call.
  Claude Code takes `end_turn` at face value and stops, so the run records
  `terminated_by: agent_finish` with 1 tool call and no diff. Reproduced verbatim
  on 2026-08-12 (108 completion tokens, same signature) — and that call took
  **179.6 s** against 0.5–15 s for every other call in the matrix, which is a
  detail worth keeping.

  **Adapter ruled out, on evidence rather than absence.** `finish_reason` is
  literally `stop`, not an unrecognised value coerced to `end_turn` by the
  translation (see the coercion item in P2); the content holds no tool call
  emitted as text, which is the Nemotron-family failure worth suspecting; `stop`
  not `length`, and 46 completion tokens, so nothing was truncated; ids unique
  and zero `(no content)` turns, so the collision uniquifier provably never
  fired. A successful run says almost the same sentence — *"Now let me check the
  test file to see what's expected:"* — with `finish_reason: tool_calls` beside
  it. Same intent, one sampled with the call and one without.

  **Rate, measured rather than assumed. Pooled: 2/23.** N=10 on this arm alone
  came back 10/10. The interval is the finding — 23 runs cannot separate a 2%
  flake from a 20% one, and it would take ~47 runs to be 95% sure of seeing a 6%
  event at all. **Do not quote a point estimate as the rate.**

  Sampling is a plausible contributor and is not free to change: this arm runs
  `temperature 1.0, top_p 0.95` on NVIDIA's own guidance, and §5.3 keeps the
  *policy* identical across arms rather than the number, so lowering it for
  Nemotron alone would trade a model property for a config confound.

  Two things turn on this and neither can be settled here:
  - **The scoring plan has to rule** whether a quit-without-tool-call is a
    legitimate failure or an exclusion. The offline grader can identify the class
    cheaply: final turn `stop`, no diff, and prose promising an action it never
    took.
  - **N cannot be picked in advance.** The N=3 criterion was chosen so a single
    run could not be read as a rate, and two consecutive N=3 runs then disagreed
    on the verdict. Whatever N Phase 3 uses must be sized off the measured flake
    rate, on a real task, not this fixture.

- [ ] **The request carries `system` twice, and the first copy lands after the
  user message.** Seen in every Gemma wire log from 2026-08-11: the request has a
  top-level `system` field *and* 6 inline `system`-role messages, with
  `messages[1]` being the first of them — so the role sequence opens
  `['user', 'system', ...]`. Not implicated in any measured failure, and
  deliberately not bundled into the tool-call id fix so that run stays
  attributable. Worth a look before Phase 3: it is not the shape any provider
  documents, and an arm that handles it badly would look like a weak model.

---

## P1 — Blocking the unattended matrix (Gate 2)

None of these can appear at N=1. All will appear at N=10, and the credentials
one ends a multi-day run outright.

- [ ] **Both credentials expire mid-run, and no automated refresh exists.**
  Still open — what closed on 2026-08-12 was the *silent burn*, not the refresh.

  **The window, measured — and it is one hour, not eight.** Both credentials
  are frozen once, before the loop, into a proxy container that lives for the
  whole matrix. `derive_mantle_token` presigns with the SigV4 session
  (`SigV4QueryAuth(credentials, …)`), so the bearer token embeds
  `X-Amz-Security-Token` and **cannot outlive that session** whatever its own
  12 h cap says — that cap never binds.

  Measured twice: a login at `2026-08-12T23:54Z` expired at
  `2026-08-13T00:54:38Z`, and one at `2026-08-13T06:29Z` at `07:29:35Z`. Both
  **1:00**. An earlier note in this file said ~8 h; that came from misreading a
  UTC/PDT offset and is wrong.

  That changes the size of the problem. Against a 4–5 day matrix, one hour is
  **~100 re-logins**, not ~12 — so an automated refresh is a hard prerequisite
  for Gate 2 rather than an ergonomic improvement.

  The hour is a property of the **frozen copy**, not of the session: botocore
  returns `DeferredRefreshableCredentials` that would mint a fresh hour from
  the 1 h SSO token by itself, and `freeze_sigv4_credentials` resolving them to
  literal strings for a container that cannot re-resolve is precisely what
  defeats that. Docker cannot change env on a running container, so `aws sso
  login` mid-run changes nothing until the proxy restarts.

  **What now happens instead of nothing.** `proxy.credential_window` reads the
  deadline and prints it; `credential_stop` refuses any cell whose
  `wall_clock_timeout_s + CELL_OVERHEAD_S` reaches past it, before the proxy is
  built and again before every cell; the driver exits **2** and a re-invocation
  resumes exactly there. `freeze_sigv4_credentials` no longer escapes as a
  `TokenRetrievalError` traceback.

  **What is still missing** is the refresh itself, and at one hour it is the
  binding constraint on Gate 2 rather than an inconvenience. Two options, and
  the second is the real one:

  - restart the proxy with freshly frozen env between rounds — cheap to build,
    but it needs a live SSO token, so it only stretches the unattended window
    from 1 h to the SSO token's own lifetime (also 1 h here);
  - give the proxy container a credential path it can refresh through — a
    mounted `~/.aws` plus SSO cache, or a `credential_process`, so botocore
    does the hourly refresh it is already capable of. This is the one that
    actually removes the human from the loop, and it is what
    `freeze_sigv4_credentials` currently exists to avoid.

  Neither is built.

- [ ] **Auth failures were unclassifiable, and that is now fixed but worth
  keeping written down.** Closed 2026-08-12 at schema 3.6.0; kept here because
  every figure taken before it is affected. litellm's `_map_bedrock_exception`
  recognises auth from `"…token…is invalid"` and not `"expired"`, and has no
  403 branch — so an `ExpiredTokenException` arrived as `APIConnectionError` at
  **status 500** and was excluded as `api_5xx`. Every other auth shape, and all
  three mantle arms, arrived as 401/403 and got **no exclusion at all**. And a
  single 401 cools a single-deployment group down for 5 s, after which
  `num_retries: 3` returns `RouterRateLimitError` — a plain `ValueError` with
  no `status_code` — so the run's *last* wire entry said "No deployments
  available" at status `None` and matched nothing. Now `api_auth` /
  `router_no_deployment`, with `router_settings.disable_cooldowns: true`
  removing the masking at the source.

- [ ] **No run-level retry exists, despite the schema being built for it.**
  `attempt_number` and `parent_run_id` are defined and hashed into `run_id`, and
  no caller ever passes a non-default. A throttled run is recorded, excluded, and
  the matrix moves on. Combined with the stale-`.partial` item below, re-running
  a sample needs a new event-log root or a hand-passed `attempt_number` — which a
  2,400-run matrix will need.

  **Bounded and made visible on 2026-08-12, not fixed.** `StreakTracker` stops
  the matrix within ~3 uninterpretable cells *per arm* rather than never (see
  §5.7 below), and `plan_resume` now reports every already-written cell whose
  record carries an exclusion — so the holes are enumerable instead of needing
  a hand-grep of the event log. Filling them still needs `attempt_number`.

- [ ] **§5.7's interleaving and consecutive-failure detection pull against each
  other, and `StreakTracker` is where that is reconciled.** Round-major
  ordering is right for the cache and ordering confounds and is exactly what
  defeats a global consecutive-failure counter: one dead arm's failures are
  never adjacent. Simulated on 20 tasks × 4 arms × 3 repeats with
  `claude-sonnet-5-runtime` dead, the global counter of 5 **never fires** and
  all 60 of that arm's cells are lost; a per-arm counter of 3 fires at cell 4
  having lost 3. Both are counted now, and both are flags
  (`--abort-streak`, `--abort-streak-per-arm`) because stored records already
  carry `api_throttle` exclusions and a heavily throttled run should raise the
  threshold rather than lose the gate. Left here because the *thresholds* are
  unmeasured — they should be set from the observed throttle rate at scale, not
  from the credential case they were tuned for.

- [ ] **The prior-run lookup is unsafe under §5.7 parallelism.** `index.jsonl` is
  appended unlocked and `last_run_id_for` runs before the container starts, so
  two concurrent runs of the same (task, model) read the same prior id and
  neither sees the other. Sequential today, so this is a precondition on
  parallelising the runner, not a live defect.

- [x] **`host` metrics are dead code: `container.stats()` has zero callers.**
  Closed 2026-08-13 at schema 3.7.0. `HostSampler` holds a `stats(stream=True)`
  generator for the life of the agent and records p95 container CPU, sampled
  peak memory, host loadavg, the VM's vCPU count and a peer count.
  `contention_flag` is `bool | None`, `None` on every unsampled run, and claims
  only that another `bakeoff.eval_agent` container shared the host — it is
  deliberately not derived from load, because `docker info` NCPU is 2 against
  `os.cpu_count()` 18, so a loadavg threshold cannot fire and mixing the two
  puts denominators from different machines behind one boolean. `load_p95` and
  `vm_cpus` are recorded so an offline view can decide instead.

  The 22× wall-clock spread below is still unattributable for the runs already
  collected; from 3.7.0 on it is answerable.

- [ ] **Confirm Bedrock service quotas** for the concurrency the plan needs
  (OPEN-3). Wall clock, not cost, is the binding constraint.
  **Wall-clock and turn-count variance is the real problem, and N=3 exposed it.**
  On the *same one-line task*, Nemotron across two N=3 runs:

  | run | turns | wall |
  |---|---|---|
  | A1–A3 | 19 / 15 / 21 | 201.9s / 42.6s / 54.2s |
  | B1–B3 | 24 / 21 / 7 | 52.5s / 98.2s / 9.0s |

  A 22× spread in wall clock (9.0 s to 201.9 s) and 3.4× in turns, on identical
  work. The two do not track each other — B2 took 98.2 s for 21 turns while A1
  took 201.9 s for 19 — so per-turn latency varies independently of how much the
  model chose to do. A single-sample estimate is worthless for capacity planning:
  the original OPEN-3 sizing used one 311 s sample, and 2,400 runs amplifies
  whichever end you picked. Size off the p95 of a repeated measurement, on a real
  task rather than this fixture.

- [ ] **Throttling makes the exclusion rate load-dependent.** No 429s at N=1. At
  scale they are routine, and the exclusion rate then correlates with when and
  how parallel the run was rather than with the model. §5.7's
  randomize-and-interleave is the mitigation and the spec says to *verify* it,
  not assume it.

- [ ] **The per-run repo is created by the harness user; the container writes
  as uid 1000.** On macOS virtiofs ignores ownership, which is why this has
  never bitten. On a Linux host the agent would be unable to write to `/repo`
  and every arm would land no diff. Pre-existing, unrelated to any one task,
  and it becomes real the moment the matrix moves off a laptop.

- [ ] **Durability: five narrow windows in an append-only claim.**
  (a) No `os.fsync` of the runs directory after the rename, so a power loss can
  leave the index line durable and the record's directory entry lost — the exact
  inversion the comment says is impossible.
  (b) A stale `<run_id>.json.partial` from a process killed mid-write poisons
  that run_id permanently; `run_id` is deterministic and nothing increments
  `attempt_number`, so there is no operational retry path. Gate 0's predecessor
  stopped this losing the record (`record.unwritten.json`); it does not clean up
  the `.partial`. Wants a sweep at `EventLog` open, loudly.
  (c) A crash between rename and index append leaves a record `list_runs` sees
  and `last_run_for` does not, so `prior_same_task_run_id` silently names the
  wrong run.
  (d) `read_run` is intolerant where `last_run_for` is deliberately total — a
  torn record file raises out of the reader.
  (e) Wire logs are `flush()`ed but never `fsync`ed, and `proxy_callback._write`
  has no `try`, so an unwritable wire dir raises inside LiteLLM's logging path.

- [ ] **Reprice and recount the archive under 3.0.0.** 219 stored records carry
  inflated `turns_used`/`tokens`/`cost_usd`, and 44 of them also carry Sonnet at
  the old $3/$15 book. The log is append-only, so they keep those figures; the
  transcripts and wire logs are on disk, so a derived view can recompute all of
  them. Needed before any cost or turn-count figure is published, and it is the
  reason `Versions.pricing_basis` and `RunRecord.assistant_records` exist —
  without them a re-derivation cannot tell which records need correcting.

### The pricing hole — two of three candidate arms are unpriceable when warm

- [ ] **Gemma and Kimi both return cache tokens that `costs.py` has no rate for.**
  Kimi first (2026-08-08: `cache_read` on 2 of 3 runs, 33,152 each), then Gemma
  on the 2026-08-12 GO run (`cache_read=18016` across the set: 16512 / 0 / 1504,
  where a 2026-08-08 measurement had recorded gemma returning no cache fields in
  9 runs). Neither appeared because anything on this side changed.

  **Confirmed on the first real task, and it is worse there than on the
  fixture.** Both candidate arms that returned cache tokens priced to `null`
  on the 2026-08-12 real-task run — gemma with `cache_read` 297,824 against
  672,044 input, kimi likewise — with `pricing_error` naming the cause and the
  tokens intact. On a 20k-line repository the cache is warm on essentially
  every turn, so **2 of 3 candidate rows were unpriced while both priced rows
  were Sonnet and Nemotron**. A cost headline computed from that compares
  Sonnet's full distribution against whichever candidates happened to be
  priceable.

  A pricing failure now costs only the price — the tokens survive and the run
  stays repriceable — but **there is no rate to reprice it with**. At N=10 per
  task the cache is warm far more often than not, so most gemma and Kimi rows
  would carry `cost_usd: null`. Any cost headline computed today silently
  compares Sonnet's full distribution against whatever subset of the candidates
  happened to run cold.

  Decide it once, for both arms: the pricing-permissions item below, or a stated
  fallback — report candidate cost as a lower bound from cold runs, or exclude
  the candidates from cost claims and say so on the figure.

- [ ] **Request `pricing:GetProducts` and `pricing:DescribeServices`** on the
  `BedrockModelTester` SSO role. Read-only, free, no data access. It is the only
  way to settle whether AWS publishes cache rates for the candidates, which
  retires the guard properly rather than working around it.
  Checked 2026-08-07: **Bedrock returns no dollar figure on any call** — the API
  and its response headers carry token counts only, so the price book is the sole
  pricing authority and a gap in it must be reported, never estimated. Cost
  Explorer and CloudWatch are also denied to this role, and neither can attribute
  spend to a `run_id` anyway. Application inference profiles with cost-allocation
  tags could attribute per arm but not per run, and would change the model id in
  every deployment.

- [ ] **The ephemeral tier is unobservable on the candidate arms.** Schema 2.2.0
  records `cache_write_5m` / `cache_write_1h` (§3.1), but only Sonnet's
  native-Anthropic route carries the nested `cache_creation` object — LiteLLM's
  Converse bridge emits the flat total and drops Bedrock's `cacheDetails`. Those
  writes are recorded as untiered and charged at the 5m rate. Harmless while
  Claude Code's Bedrock TTL stays hardcoded to 5m (claude-code#32671), and it
  becomes a mispricing the moment that changes or a candidate gets a cache rate.
  Fixing it means a `litellm_patches` intervention to carry `cacheDetails`
  through, which is not worth doing before a candidate has a rate at all.

### Cost is order-dependent, and the policy is still unchosen

- [ ] **Sonnet's cost varies 2.9× on byte-identical work, from scheduling alone.**
  Measured at N=3, three runs of the same one-line fix, same 157-byte diff:

  | run | cache_write | cache_read | cost |
  |---|---|---|---|
  | 1 | 125,853 | 210,149 | **$0.548** |
  | 2 | 118,499 | 217,519 | $0.522 |
  | 3 | 34,166 | 259,646 | $0.217 |

  > **Every absolute number here is inflated ~2× and every dollar figure predates
  > the $2/$10 book.** Run 1's true `cache_write` is 42,305, not 125,853, and it
  > made 5 calls, not 8. The **ratio** survives, because cold and warm runs
  > inflated alike; nothing else does. Re-derive from the stored transcripts
  > before quoting any of it.

  Reproduced across two N=3 runs with the *shape* stable and the *breakpoint*
  not — 2.9× then 2.5×, and which run got the cheap one moved. So it is not a
  fixed warm-up cost that could be subtracted; it depends on cache state at
  dispatch. It lands on one arm only, so Sonnet-vs-candidate cost is confounded
  twice over: by order, and by the presence of caching at all.

  **Both halves of the instrumentation are built.** `cache_state.warm` separates
  the runs directly — on both N=3 sets the single `warm: false` run is the
  expensive one ($0.547 and $0.373 against ~$0.18–0.22) — so "report warm runs
  only" is a one-line filter rather than a reconstruction. `smoke_test
  --interleave` gives the §5.8 ordering, `print_run_order` states whether the
  ordering actually used separated the repeats, and `print_cost` reports the two
  scenarios by name.

  **The reframing matters more than the flag.** The inter-run cache hits are a
  harness artifact: each run is a fresh container, a wiped `CLAUDE_CONFIG_DIR`, a
  fresh repo and a one-shot `claude -p`, so nothing crosses runs but Bedrock's
  server-side cache — and repeats are scheduled 3–5 s apart inside a 300 s TTL
  (6/6 matrices cold on run 1, 10/10 warm after). An interleaved round takes
  1.4–3.9 min, still inside the TTL, so `--interleave` redistributes the write
  cost and **cannot produce a cold run**. A real developer opening a fresh task
  pays the tool-schema write, so `first_task` and `warm_followup` are two
  deployment situations, not a number and its correction.

  **Still open: which one the recommendation quotes.** See the ordering item in
  P3 — silently averaging them is the one option that is not defensible.

- [ ] **`RunRecord.sampling` reflects the bridge, not just the config.** On the
  Responses path all three candidates reported `temperature=1.0`; switching to
  chat-completions made it read `absent`; it returned on nemotron's successful
  run. Gate 0's resolved-params channel now makes the *actual* value observable
  on the `openai/` arms — measured `temperature=1.0` on all three, live — so what
  remains is to pin which bridge each arm uses before the full run, and to note
  that the `bedrock/` Sonnet arm still reports `client_request` because no openai
  param mapping runs on its path.

---

## P2 — Derivation gaps (Gate 3; safe to close after collection)

Every item here is recoverable from stored artifacts at any time, including after
Phase 4 — that is what makes them lower priority than everything above, not their
size. Two exceptions are marked CAPTURE and should ride along with Gate 1.

- [ ] **`snapshot_diff` takes no `--binary`, so a binary change is unappliable.**
  A submission that touched a binary file is recorded as `Binary files a/x and
  b/x differ` with no payload, and `git apply` refuses the whole patch. The
  grader contains this — it drops the binary chunks, names them in
  `GradeRecord.binary_chunks_dropped`, grades the text remainder, and refuses
  outright (`BINARY_HUNK_UNAPPLIABLE`) when *every* chunk is binary — so no run
  is lost. But the containment is a caveat on a verdict, not a fix: the stored
  diff is not a faithful record of what the agent did, and a task whose fix
  legitimately touches a binary fixture cannot be graded at all. Adding
  `--binary` makes the diff self-contained; it also grows every stored
  checkpoint, so measure the size cost on a real collection before flipping it.
  Note that a diff cut with `--binary` re-applies without any of this, which
  means the grader's binary branch stays as defence for records already written.

- [ ] **DERIVATION. Fields that are permanently zero and read as measurements.**
  `retry_backoff_ms`, `ToolCallStats.malformed` (so `malformation_rate` is always
  0.0 and `TOOL_MALFORMATION`/`ADAPTER_FAILURE` can never fire),
  `truncation_events`, `p2p_regressions`, `diff_stats`, `Checkpoint.per_test`,
  `DestructiveEvent.affected_outcome`, `Exclusion.pre_registered` (always
  `True`). Populate or delete — deleting is honest, leaving them is a claim.
  `HostMetrics.contention_flag` was on this list and came off it at 3.7.0: it is
  now measured, or `None`.

  Recoverable from, where it matters: `diff_stats` from
  `checkpoints[].diff_vs_base`, `truncation_events` from wire `finish_reason` /
  `stop_reason`, `p2p_regressions` from the scoring plan. **`retry_backoff_ms` is
  the exception and is a real capture gap wearing a derivation gap's clothes:**
  the proxy retries up to three times per call and nothing records the backoff,
  so retry latency lands inside `inference_ms` with no way to separate it. Gate 0
  added `litellm_call_id` to every wire entry, which is what makes grouping the
  attempts of one logical call possible at all — the timing still is not there.

- [ ] **DERIVATION. `Checkpoint.turn` and `turns_used` count different things
  and are both called "turn".** Measured on the first real-task run
  (nemotron, 2026-08-12): `turns_used` 40, `assistant_records` 80,
  `turns_streamed` 71, and **71 checkpoints numbered 1–71**. Checkpoints are
  indexed by the stdout callback, which fires per assistant event — one per
  content block — while `per_turn` and every §5.4 budget are indexed by API
  call. So §5.5's promise, "the pass rate at any budget K computed post-hoc",
  is computed against the wrong axis unless the grader knows: checkpoint 40 is
  not the state after 40 turns, it is the state after 40 content blocks, which
  on this run was about turn 20.

  This is the 3.0.0 collapse arriving in a place 3.0.0 did not reach.
  Derivable, but only from `agent_stdout.jsonl`: the *n*-th assistant event on
  stdout is checkpoint *n*, and each event carries `message.id`, which is the
  same key `parse_trajectory` dedupes on. So the mapping is exact and the
  artifact that carries it must not be dropped. The cheaper alternative is to
  stamp the API-turn index on the checkpoint at capture time, which needs the
  dedupe to move into `claude_runner`'s callback.

- [ ] **DERIVATION. `TurnRecord` has no absolute timestamp.** Per-turn data,
  checkpoints (`elapsed_ms`) and wire entries (`logged_at`) use three time bases
  that never join, so no per-call latency can be attached to the turn that
  incurred it. Gate 0 made the wire side real — `logged_at` is the proxy's
  capture time now rather than the harness's replay time, and `started_at` /
  `ended_at` carry the call's own boundaries — so the join is newly possible and
  is still not made. Also: for a deduped later content block `inference_ms` is
  computed and then dropped, so `sum(per_turn.inference_ms)` systematically
  undercounts the span it purports to cover.

- [ ] **DERIVATION. `ToolCallStats.malformed` is never populated at harness
  time**, so `TOOL_MALFORMATION` and `ADAPTER_FAILURE` are both unreachable — and
  §6.4 calls the adapter-vs-model distinction the eval's most consequential call.
  Keep `malformed` at 0 at harness time (deciding a call was malformed means
  reading raw completions, which §6.4 puts offline). The Kimi tool-id defect made
  the cost concrete: an adapter failure the harness could never have flagged, and
  it took a live run to find.
  **For the scoring plan:** a pass over `wire.jsonl.gz` where the set of
  `tool_use` ids in a response must equal the set of `tool_result` ids in the
  next request. That single check would have caught the id mangling directly,
  offline and for free.

- [ ] **DERIVATION. The adapter coerces `finish_reason` and the harness reads the
  result as intent.** LiteLLM's openai→anthropic translation maps `stop`→
  `end_turn`, `length`→`max_tokens`, `tool_calls`→`tool_use`, and **anything else
  to `end_turn`**. `assemble_record` treats `end_turn` as the agent deciding it
  was finished (`TerminationReason.AGENT_FINISH`), so an unrecognised provider
  finish_reason is silently recorded as a deliberate stop. The raw value is in
  the wire log (`choices[0].finish_reason`); record it beside the translated
  `stop_reason` so the offline grader can tell a real end_turn from a coerced
  one.
  Worth stating plainly because it reads like a blocker and is not: the Nemotron
  P0 **is** diagnosable today, offline, from `wire.jsonl.gz` — that is where its
  `finish_reason: 'stop'` came from. This makes the class cheap to query in bulk;
  it does not gate the ruling.

- [ ] **DERIVATION. Truncated tool-call JSON is silently repaired.** On the
  non-streaming adapter path LiteLLM closes unmatched brackets in tool arguments
  and returns the result as though the model emitted it — a repaired `Edit` is a
  different edit than the model asked for, and the only trace is a log line.
  Streaming forwards raw `input_json_delta` and does no repair, so this bites the
  occasional non-streaming call rather than every turn. Detect offline by
  comparing the wire log's raw arguments against the transcript.

- [ ] **DERIVATION. §5.2's config dump is an artifact, not a record field.**
  Written to `artifacts_root/<arm>/effective_config.json` and diffed across arms
  by the smoke script. The spec asks for it stored *in the run record*, which
  needs an `Artifacts.effective_config_json` field and a schema bump.

- [ ] **CAPTURE. `TokenUsage.reasoning` is structurally 0 on all three
  candidates.** The adapter's usage translation emits input/output/cache fields
  and has no reasoning field to emit, so a zero there is "not representable", not
  "none used". Worse than not-plumbed-through: measured 2026-08-12, the provider
  does not report it either — gemma returns `reasoning_tokens: 0` at
  `effort=high` while producing 2.4× the output, and on `/v1/responses` returns
  `0` with an explicit `reasoning` block in the same response. So this cannot be
  closed by plumbing alone. It only bites if thinking is turned on (see P3), but
  it must be closed **before** that, or the extra tokens land inside `output`,
  priced correctly and attributable to nothing.

- [ ] **`smoke_bedrock.py --live` skips the four mantle arms that
  `smoke_test.py` runs fine.** The preflight reads `BAKEOFF_MANTLE_TOKEN` from
  the environment; the run calls `derive_mantle_token` and mints one from the SSO
  session. So the gate reports `MISSING BAKEOFF_MANTLE_TOKEN` on four rows and
  `preflight PASS` on the next line, for arms that are fully credentialed — a
  preflight weaker than the thing it gates. Confirmed again 2026-08-12: those
  four rows said MISSING and the live run then completed 3/3 on them.

- [ ] **`eventlog.last_run_id_for` carries a ~70-line design rationale and has
  zero production callers.** It is a two-line delegate; `last_run_for`, the
  function actually on the hot path, documents only `started_at`. Move the
  docstring to the implementation or delete the delegate.

- [ ] **`schema.Exclusion` is the only nested class not routed through `_build`.**
  A record written by a later schema still raises `TypeError` there, which is the
  failure `_build` exists to prevent — and every schema bump since 2.2.0 has
  added nested fields, so the gap is reachable.

---

## P3 — Decisions to settle before numbers are published

These need a call, not code. Most are cheap to make and expensive to make late.

- [ ] **`NotGradedReason` may want a `RECORD_SCHEMA_UNREADABLE` member.** The
  closed set has `RECORD_SCHEMA_TOO_OLD` and nothing for "the version string
  could not be parsed at all", so `grade.py:_schema_refusal` files an
  unparsable `schema_version` under TOO_OLD and says in the *detail* that the
  comparison could not be made. That is the honest half of the claim, but a
  reader counting reasons counts it as age. `_schema_refusal` is the **single
  call site** — adding the member is one enum entry plus the `except ValueError`
  branch there, and nothing else moves. Deliberately not done now: a new member
  changes a closed set that stored grades are already written against, and the
  right time to widen it is before the first collection is graded, not during.

- [ ] **Sonnet 5 pricing.** `PRICE_BOOK` carries Anthropic's introductory $2/$10,
  which runs through 2026-08-31. Whether Bedrock mirrors it is unverified, and
  the eval asks whether a candidate beats *buying Sonnet* rather than whether one
  AWS SKU beats another. Check the pricing page before any cost figure leaves the
  team, and note that `Versions.pricing_basis` is what lets a reader tell which
  book a stored record used.

- [ ] **The 40-turn cap is a placeholder and it is already binding.** On the
  first real-task run Nemotron used all 40 turns and terminated on `turns`,
  with a plausible submission in hand. §5.4 says caps come from the §3.5
  calibration pilot's slowest-converging model at roughly p95 × 2, applied
  identically to all four arms — and that a cap tuned to the incumbent
  converts a style difference into a capability score. Nothing measured has
  set these yet, so no turn-limited result from before the pilot means what it
  looks like.

- [ ] **Sonnet's tokenizer is ~30% denser.** A uniform token cap gives Sonnet
  ~30% less *text* budget than the other arms — a config choice that would be
  scored as a capability difference (§5.4) — and cost-per-task is not directly
  comparable at equal text. Caps derive from the Phase 3 calibration pilot;
  decide the policy there.

- [ ] **Record the Sonnet transport asymmetry as a §6.4 confound.** Sonnet 5
  signs SigV4 against bedrock-runtime while all three candidates go through the
  mantle passthrough. Different code path, different request shape: on runtime,
  beta features ride as an `additionalModelRequestFields.anthropic_beta` *body*
  field rather than an `anthropic-beta` header. Gate 0 added a second visible
  consequence — the runtime arm records `sampling_source: client_request` because
  no openai param mapping runs on its path, while all three candidates record
  `resolved`. The reference arm is not transport-identical to the arms it is the
  reference for, and any Sonnet-vs-candidate delta carries that. It needs to be
  stated wherever the comparison is published, not just known here.

- [ ] **Gemma runs without `top_p` and the other candidates do not.** Its route
  rejects any non-default value (*"'top_p' is not supported with this model"*),
  so gemma omits it exactly as Sonnet does. §5.3's constant across arms is the
  *policy* — lab-recommended unless the route refuses it — not the number, which
  is why nemotron and kimi keep `top_p 0.95`. **This is a real §5.3 divergence
  and must be published with any gemma comparison.**

- [ ] **Thinking is off on every arm, and that is a choice rather than a
  constraint.** Bedrock accepts `reasoning_effort` on all three candidates and
  `'high'` measurably changes output (gemma 240 → 580 completion tokens, kimi
  171 → 717) — the earlier attribution of the rejection to Bedrock was wrong; it
  was litellm's own `_check_valid_arg`. So thinking **is** available and the eval
  is choosing not to use it.

  **Reasoning cannot be enabled uniformly on any single route.** Measured
  2026-08-12 with tools present, which is the only configuration an agentic eval
  cares about:

  | arm | `none` | `default` | `medium` | `high` |
  |---|---|---|---|---|
  | gemma-4-31b | OK | **400** | **400** | **400** |
  | nemotron-3-super-120b | OK | **400** | OK | OK (46 tok) |
  | kimi-k2-5 | OK | **400** | OK (30) | OK (69 tok) |

  Three findings, each closing a door:
  - **Gemma is tools XOR reasoning on chat/completions.** Any non-`none` effort
    with tools is a 400.
  - **There is no "adaptive" value to set.** Anthropic's `adaptive` is an
    Anthropic concept; litellm maps it to a fixed `"medium"`, overridden by
    `output_config.effort` (`"high"` from Claude Code). OpenAI's nearest
    equivalent, `reasoning_effort: "default"`, is rejected on **all three** arms.
    Only a static level can be chosen, so the eval cannot reproduce what Claude
    Code actually requests.
  - **`/v1/responses` gives gemma both** — measured, a `reasoning` block and a
    `function_call` in one response, with **unique** tool ids
    (`call_83c9aab1…` rather than `call_0`), which would retire
    `anthropic_tool_use_id_collision_uniquify` on that arm. But nemotron and kimi
    **cannot use that route at all**: Bedrock answers *"The model
    'nvidia.nemotron-super-3-120b' does not support the '/v1/responses' API"*,
    which is why `use_chat_completions_url_for_anthropic_messages` is set.

  So the best achievable thinking-on configuration is gemma on `/v1/responses`
  and the other two on chat-completions at `high` — reasoning everywhere, at the
  cost of gemma running a different transport from the arms it is compared
  against, and a further `litellm_patches` intervention because
  `_should_route_to_responses_api` reads a process-global flag with no
  per-deployment override.

  **Decision as it stands: thinking off, uniformly**, pinned explicitly to
  `"none"` on all three candidates. It is the only configuration all three can
  share, it is what the whole Phase 0c corpus already is (Sonnet included), and
  turning it on invalidates rather than re-measures that corpus. It also
  **measures a deployment nobody ships.** Sonnet is thinking-off too, so the
  comparison is internally fair — and that has to be stated on the scorecard, not
  left implicit.

- [ ] **Arm-major ordering is the default, inside a 300 s cache TTL.** §5.7/§5.8
  require the opposite. Measured: the first Sonnet run cost 2.9× the others,
  purely from scheduling. Latency is not normalised for cache state at all, so
  repeats 2–3 of each arm get a systematic TTFT advantage. Decide whether
  `--interleave` becomes the default, and whether cost publishes `first_task` /
  `warm_followup` separately rather than a mean.

- [ ] **The cost headline compares non-comparable subsets.** Sonnet is on
  Anthropic list pricing, the candidates on the Bedrock page — defensible, and
  documented. Less defensible: candidate cache multipliers are `None`, so any
  candidate run returning cache tokens gets `cost_usd = None` and drops out of
  the means entirely. Sonnet's warm runs price at 0.1×; the candidates' warm runs
  are unpriced. Either measure the multipliers or state the subset on the figure.

- [ ] **Per-turn snapshotting runs inside the synchronous stdout loop.**
  `every_k_turns=1` by default and never overridden; each turn boundary fires
  three synchronous `docker exec`s while the harness stops draining the agent's
  stdout. That time lands inside `wall_clock_total_ms` — a headline metric — and
  if the pipe buffer fills, the agent itself blocks. The approximate-boundary
  tradeoff is argued in `claude_runner.py`; the wall-clock contamination is not
  mentioned anywhere.

- [ ] **`WebFetch`/`WebSearch` are in the tool schema and dead.** The container
  has no route off the host. A model that reaches for docs is penalised in a way
  a real session would not be, and nothing records that a run failed on a fetch
  attempt. Decide whether that is the intended measurement.

- [ ] **Subagents (`Task`) are never discussed anywhere.** The default tool set
  ships them and the config dir is empty, so runs get built-in general-purpose
  subagents. Not excluded, not measured, not mentioned in the spec.

- [ ] **`Checkpoint.tests_pass` stays `None`** by design — §5.5 requires offline
  grading. Lands with the scoring plan.

---

## Housekeeping

- [ ] **`bakeoff/.env` holds three commented-out static AWS credential lines with
  plaintext values**, annotated in-file as an expired STS session superseded by
  the SSO profile. Inert and gitignored, but a real secret-key / session-token
  pair sitting on disk with no expiry tracking. Delete the lines.

---

## Out of scope here — each needs its own plan

These are the three deliverables the gates above exist to make possible. None is
started.

1. **Dataset construction (Gate 1)** — transcript/PR/Jira join, task harvesting,
   container builds, stratification (§3). The P0 section is the *harness* half of
   this; the dataset itself is the other half.
2. **Scoring (Gate 3)** — **the §4.2.1 deterministic checks are done as of
   2026-08-17** (`grader.py`, `oracle.py`, `grade_schema.py`, `scripts/grade.py`);
   what remains here is offline *checkpoint* grading (the §5.5 cost/quality curve
   — the ladder currently grades the final diff, not each checkpoint), the judge
   protocol and κ calibration (§4.2.3, §4.3). The harness still deliberately does
   not grade; a verdict now exists in `grades.jsonl`, beside the log. Three P2
   items name specific offline passes this should carry.
3. **Analysis and reporting (Gate 3)** — paired cluster bootstrap, pass@1/pass^k,
   Elo, scorecard (§10). Every P3 decision has to be stated on the scorecard, not
   just settled.
