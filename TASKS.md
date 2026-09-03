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

- [x] **A cross-file duplicate `fullName` was refused even though
  `<file>::<fullName>` is unambiguous.** Closed 2026-09-03 (round 2 item 1).
  A node selection and a node deselection are now one argv **per file** —
  measured, one positional plus one `-t` runs the named tests of that file
  only, while two positionals plus one union `-t` ran a test declared for
  neither pairing — so the file half of the id reaches the runner. Both
  refusals are gone: `validate_id_set` is a no-op and preflight's
  `duplicate_full_names` is evidence rather than a problem. Two narrower
  rules replaced them: a **vitest** tree whose executed files include one
  repo-relative path contained in another's is refused as
  `ambiguous_file_filters` (that positional is a substring filter no
  anchoring reaches), and the same-file case below stays open. jest also
  stopped emitting `--testPathIgnorePatterns` at all, which replaced a
  repository's own ignore list on the one run that used it.
  `PREFLIGHT_VERSION` 15, `GRADER_VERSION` 10, `ORACLE_VERSION` 5.

- [x] **The sqlglot ssh-submodule refusal blocks a suite that never reads the
  submodule, and there is no manifest lever to say so.** Shipped 2026-09-02
  (round 2 item 2) as top-level `submodules_unneeded: ["<path>"]`. The gitlink
  stays in the index and the tree exactly as at `base_sha`, the directory is
  never populated, its url scheme is never checked, a readable `.gitmodules`
  is not required for it, and no pruned mirror is built. It relaxes four of
  the six submodule refusals and **neither of the two that protect grading**:
  a `strip_paths` entry covering the path, and a reference diff touching it,
  are refused with the key exactly as without it. Preflight stops calling a
  declared path `stale` and instead asserts what the declaration promises —
  marker `-`, the directory present and EMPTY, re-read after the suite,
  because git does not descend into a gitlink path in any state and `git
  status --porcelain` cannot see a file written in there. `container.snapshot_diff`
  now seeds its scratch index with `git read-tree <base_sha>`: without it an
  uninitialised gitlink is a phantom `deleted file mode 160000` on a clean
  tree (measured 239 bytes on `tobymao/sqlglot`) and the offline grader
  refused every such submission as `SUBMODULE_GITLINK_UNGRADABLE`. That one
  line also removed a pre-existing phantom — a file tracked at the start
  state that also matches `.gitignore` was reported deleted in every
  submission and checkpoint (`eemeli/yaml` carries 15, `bidict` 1).
  `PREFLIGHT_VERSION` 15 → 16, `SCHEMA_VERSION` 3.8.0 → 3.9.0; `start_sha`,
  `ORACLE_VERSION`, `GRADER_VERSION` and `GRADE_SCHEMA_VERSION` do not move.

- [ ] **The unneeded-submodule gate records no collected-test count, so it
  cannot tell a guarded suite from a silently shrinking one.** What
  `submodules_unneeded` proves is narrower than its name: the declared f2p ids
  are red-before/green-after and the p2p sweep is green *with the directory
  empty*. A repository that carries an optional submodule guards on its
  presence — measured on `tobymao/sqlglot`, `tests/sqlglot/__init__.py` and
  `tests/test_integration_loader.py` both sit behind `os.path.isdir(...)` and
  append nothing when the directory is empty — so the suite silently
  *shrinks* rather than failing, and every verdict the gate records is
  consistent with that. Closing it means recording a collected-test count for
  the p2p sweep, which is a runner-adapter change across pytest, vitest and
  jest and belongs with that work rather than bolted onto a manifest key. It
  is a bound on external validity, not a correctness gap: every arm sees the
  identical tree.

- [ ] **What an agent writes inside an uninitialised submodule directory is
  invisible at run time.** Measured 2026-09-02 after the snapshot-index seed:
  a file the agent creates under a `submodules_unneeded` path diffs to **0
  bytes** and names nothing, while `ls -A` sees it — git does not descend into
  a gitlink path in any state, and the seeded index does not either. So the
  §5.6 submission and every §5.5 checkpoint are silent about it. Within item 2
  the only enforcement is preflight's two `ls -A` reads, which refuse the
  *task* rather than scoring a *run*: a task that gates clean and an agent
  that writes in there mid-run are two different moments. Capturing it at run
  time is round-2 item 17's job
  (`docs/superpowers/plans/2026-09-03-round2-17-submodule-dirty-capture.md`):
  an `ls -A` over uninitialised gitlink directories, recorded in
  `submodules_dirty` and refused by the grader. Filed here because item 17 had
  not landed when item 2 shipped; delete this entry when it does.

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

- [ ] **No neutral judge model is invokable, so the judge channel cannot
  produce a single number.** §4.3 makes a neutral family MANDATORY, and
  `assert_neutral_judge` refuses one from the four compared families. Measured
  2026-08-19 against `AWSReservedSSO_BedrockModelTester_cd3c44c7ea0e8a37`: the
  bedrock-mantle catalog lists 40+ models, and the principal can invoke exactly
  the four eval arms. Everything else answers

      401 access_denied — not authorized to perform: bedrock-mantle:CreateInference
      on arn:aws:bedrock-mantle:us-east-1:610746058075:project/default

  including the pinned default `openai.gpt-5.6-sol`. The denial is per MODEL and
  not per route or per action: the same bearer token, the same
  `.../v1/chat/completions`, the same instant returns 200 for
  `moonshotai.kimi-k2.5` and 401 for `openai.gpt-5.6-sol`. Twelve neutral
  candidates were probed and all twelve were denied — `openai.gpt-5.6-luna`,
  `-terra`, `gpt-5.5-2026-04-23`, `gpt-5.4-2026-03-05`, `gpt-oss-120b`,
  `gpt-oss-20b`, `xai.grok-4.3`, `deepseek.v3.2`, `zai.glm-4.7`,
  `qwen.qwen3-coder-next`, `mistral.mistral-large-3-675b-instruct`,
  `minimax.minimax-m2.5` — as were `anthropic.claude-opus-4-8`,
  `claude-haiku-4-5` and `moonshotai.kimi-k2-thinking`, which are non-neutral
  anyway and are listed here only because they bound the entitlement: it is
  scoped to the four arm ids, not to a vendor or a family.

  This is an IAM change, not a code change, and it is a **prerequisite for any
  Tier B number** — the driver runs to completion without it, writing
  gate-decided pairs and zero judged ones, which reads as a judge that agreed
  with the ladder rather than as a judge that was never asked. Do not close it
  with `BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE=1`: the only entitled models ARE the
  compared arms, so the override buys a Claude judge rating the Sonnet arm,
  which is the exact self-preference §4.3 cites (33.7% self-rated against
  14.13% independent). Note also that this interacts with OPEN-5: even once a
  neutral judge is entitled, §4.3 forbids publishing any judge number without
  the κ that was in force.

- [ ] **`JUDGE_SAMPLING`'s `temperature: 0.0` is refused before the request
  leaves the process, for any gpt-5-family judge.** litellm 1.95.0 routes
  `openai/openai.gpt-5.6-sol` through `gpt_5_transformation`, which allows a
  non-1.0 temperature only when `_supports_reasoning_effort_level(model,
  "none")` is True. That reads `supports_none_reasoning_effort` out of
  `litellm.model_cost`, and a Bedrock vendor-namespaced id is not in the
  built-in map, so it returns False (documented "safe fallback for unknown
  models") and the call raises

      litellm.UnsupportedParamsError: gpt-5 models (including gpt-5-codex)
      don't support temperature=0.0. Only temperature=1 is supported.

  Measured 2026-08-19: 4 of 4 paid units in a real pass errored this way, at 0.0 s
  each, having made no request. Two non-fixes were probed and both failed.
  Adding `reasoning_effort: "none"` alone does nothing — the guard is on
  `supports_none`, and the effort value is only consulted after it. Declaring
  the capability in the deployment's `model_info` does nothing either: the
  transformation is handed a model STRING and reads global `litellm.model_cost`,
  with no router context to consult. `litellm.drop_params` is not an option in
  either form — it drops `temperature` silently and the judge then runs at the
  route's default, which destroys the determinism the whole protocol rests on
  ("at temperature 0 these two answers ARE the judge", `VOTE_POSITIONS`).

  So the only mechanism that works is a global `litellm.register_model` for the
  judge id, and that is a design decision rather than a typo fix: `judge.py`
  deliberately never imports `litellm_patches` because it mutates litellm
  process-wide on import, and a `register_model` at router-build time is the
  same class of mutation in a module the payload builders and leak tests import.
  Deferred until the entitlement above is settled, because the right shape
  depends on which model gets entitled — a non-gpt-5 neutral judge (deepseek,
  qwen, mistral, grok) hits no such guard and needs no change at all.

- [x] **`load_task_set` validates every manifest in the task-set root before
  `--tasks` filters, so one broken sibling manifest blocks every other task's
  gate and grade.** Measured 2026-09-02, twice independently: a sibling's
  `image.env` key typo (an unrelated in-progress manifest in the same
  directory) blocked `run_matrix.py --preflight-only --tasks <unrelated-task>`
  and `grade.py --taskset ...` entirely, with the printed error naming the
  broken sibling rather than the task that was actually asked for. Plausibly
  intentional for a committed, curated task set — every manifest there should
  always be valid — but a sharp edge for concurrent drafting in one shared
  directory. Either a `--tasks` selection should load only the named
  manifests, or the error should name the offending sibling AND say
  explicitly that it is not the selected task.
  **Resolved:** the third remedy, gated on committed-ness. Refusals are
  collected rather than raised where found; a `--tasks` selection loads past a
  sibling that fails to load only when that sibling is not itself selected AND
  is not tracked in the task set's enclosing revision (untracked, ignored, or
  in no repository at all), with a WARNING naming both the sibling and the
  selection. A tracked-and-broken sibling, or any run with no `--tasks` at
  all, still refuses — with the improved text on every path: the offending
  manifest, why it is fatal, and (when applicable) the selection it is not
  part of. `grade.py` gaining a `--tasks` flag was designed and deferred (see
  the round-2 plan's D6/Q2): every way to let it proceed past an unreadable
  manifest stamps a false `TASK_NOT_FOUND` into the append-only grades file at
  exit code 0.

- [ ] **`run_matrix.py --preflight-only` rebuilds every base image
  unconditionally before `resolve_tasks` runs, so `preflight.py`'s
  base-tag-mismatch refusal cannot be exercised through the one command the
  docs name for running the gate.** `prepare_bases` calls
  `images.build_base_image` with no existence check, and a cache-hit
  `docker build -t <tag>` silently retags the correct image back onto a
  manually mutated tag before preflight's own `python --version` read-back
  ever runs inside it. Measured 2026-09-02: mutating
  `bakeoff-eval-agent:base-python-3.13` to point at the 3.11 image, then
  running the documented gate command, printed `preflight PASS` with
  `python_observed: Python 3.13.15` — the mutation was undone before the
  read-back could see it. The refusal code itself is real and was verified
  working when it was written; record this as "defence exists, not reachable
  via the driver," not as dead code — exercising it needs either mutating
  between `prepare_bases` and `resolve_tasks` inside one process (not
  triggerable from outside) or calling `preflight()` directly.

- [ ] **Files an `image.build` step writes INTO `/repo` are discarded by the
  runtime bind mount, and which side should own the fix is still an open
  decision, not a bug to patch quietly.** Measured 2026-09-02
  (`pytest-dev/pytest`, whose editable install generates
  `src/_pytest/_version.py` at build time via setuptools_scm): the generated
  file exists only in the image's build-time scaffold copy of `/repo`, because
  at run time the materialized run tree — built separately by `materialize()`,
  which never sees anything `image.build` wrote — is bind-mounted over `/repo`
  in full. A runner that needs such a file has to regenerate it itself every
  invocation (this task's `tests.runner` does, via a heredoc reproducing the
  build-time content verbatim). Decide whether `image.build` should instead
  run against the materialized tree (which would invert the current
  build-before-materialize order), or whether the scaffold/tree split should
  simply be documented as the contract every such task has to work around.

### The judge channel — parked by the round-2 review (2026-08-20)

Ten defects were fixed on `judge-review-fixes`; these are what the same review
found and left. All of them are offline and none blocks a collection — the
judge runs after one — which is why they sit here rather than above.

- [ ] **`_stored_payload_path`'s check is a bare `assert` and `python -O`
  deletes it.** `scripts/judge.py:568`. The check is load-bearing: it is what
  keeps "the line stores the file that was written" true by construction, and
  under `-O` a driver run would file relative paths naming files nobody can
  find, silently, on every line. Give it a dedicated exception in the shape of
  `GateInvariantError` and raise it. The same sweep should look for other bare
  asserts on the judge's write path.

- [ ] **A split key/value secret passes BOTH payload scans.**
  `judge_schema.scan_payload` walks raw strings and then canonical JSON, and
  `scanners._SECRET_PATTERNS["generic_api_key"]` needs the value to follow the
  key through `[:=]\s*['"]?` with nothing in between. A payload dict
  `{"AWS_SECRET_ACCESS_KEY": "<40 chars>"}` yields the key and the value as two
  separate strings on the raw walk, and `json.dumps` puts the key's closing
  quote between them on the canonical one — so neither layer matches, and the
  redundancy that exists so neither scan is the only one agrees. Both scans
  fail in the same direction, which is the direction that sends the credential.

- [ ] **`_vote_line` and `_gate_decided_line` have no pre-append gate
  re-assert.** `_rubric_line` re-asserts its gate immediately before the append
  (`scripts/judge.py:1000`); the two pairwise writers do not
  (`:1082`, `:1159`). The gate is checked earlier for all three, so nothing is
  wrong today — what is missing is the guarantee that a future edit moving work
  between the check and the append cannot write a line the gate would refuse.
  Cheap, and it makes the three writers say the same thing.

- [ ] **The mantle 401-refresh mints outside `build_lock`.**
  `src/bakeoff/judge.py:1691-1723`. The router rebuild takes the lock; the
  `derive_mantle_token` call above it does not. At expiry every concurrent
  worker 401s within the same second, and each mints its own token and bumps
  `auth_refreshes` — a thundering herd at the credential endpoint, and a
  counter documented as "credentials this pass replaced" reporting one per
  worker instead of one. Single-flight it (mint under the lock, or a
  `token_generation` counter each caller checks after acquiring), and the
  count becomes the thing its docstring claims.

- [ ] **Six reuse duplications in the judge channel.** Each is one fact spelled
  twice, and the failure mode is the copies drifting apart: `_AUTH_STATUS`
  declared in both `judge.py:1380` and `codex_judge.py:212`; the two backends'
  lazy call wrappers built from near-identical scaffolding; the reasoning-effort
  normalization rule spelled in three places; the persist block copied between
  `_rubric_line` and `_vote_line`; the resume-key layout written out in two
  places; and `append_judgment`/`load_judgments` (`judge_schema.py:397,419`) a
  copy of `append_grade`/`load_grades` (`grade_schema.py:390,411`). None is a
  bug now. Fold them where a shared helper does not couple two modules that are
  deliberately independent — the two schema files are the case to think about
  rather than merge on sight.

- [ ] **The κ caveat is print-only and does not travel with the summary.**
  `_print_kappa_caveat` puts OPEN-5's "no number here is calibrated" on the
  terminal (`scripts/judge.py:4913`), but `summarize`'s returned dict carries
  no `kappa_in_force`. A library caller — anything that consumes the summary
  and renders its own report — gets every rate and none of the caveat, which is
  precisely the reading the caveat exists to prevent. Put the flag in the dict
  beside the numbers it qualifies; the printout keeps reading it from there.

- [ ] **M2. A JSON-SHAPED preamble still burns the unit at temperature 0.** The
  extractor now rescans after a `json.JSONDecodeError`, which covers braced
  prose. It does not cover a brace group that parses and is the wrong shape —
  an example object, an echoed schema — because that path raises on the shape
  rather than on the decode, and the scan never resumes. At temperature 0 every
  retry reproduces the reply, so a valid verdict later in the same text is lost
  on every attempt. Continue the scan on a shape refusal too, and raise only
  when no group in the reply yields a verdict.

- [ ] **M3. `--only-task` filters on `grade.task_id`; cells key on
  `record.task_id`.** `scripts/judge.py:1830` vs `:1858`. The two agree on
  every collection anything has produced, and nothing pins that they must — so
  a grade line whose `task_id` diverges from its record's would be selected
  under one id and filed under another, and the pass would judge a task the
  operator did not name while reporting the one they did. Assert the agreement
  where the record is read, or filter on the record's field and let the grade
  line's be the cross-check.

- **Confirm on a LIVE cell that `CI=1` in a task image does not change Claude
  Code's behaviour.** Broadening 3 bakes that variable into a property-based
  task's image and it reaches the agent's own process, which is the point.
  Measured offline: `claude` 2.1.220 reads `CI` in exactly two places — the
  bundled `supports-color` colour-depth block (`"CI" in env` ×1, `CI_ENVS` ×3)
  and an environment-name function (`process.env.CIRCLECI` …); the eight
  `isCI` hits are seven zod `isCIDR` plus one `isCI:Yt(!1)` field. Both should
  be inert without a TTY, and `scripts/smoke_test.py --mode offline` against an
  image carrying `ENV CI=1` passes (broadening 3's Final verification). What
  that leaves open is a live cell: turn counts, tool-call counts and
  `terminal_finish_reason` against the same cell without the variable. Do it
  before the first property-based task is collected, not after — a behaviour
  difference on one task's image is a §6.4 confound only that task's arms
  carry, and `RunRecord.config_digest` will not move, because it covers
  `_eval_env` and `PASSTHROUGH_ENV` and not the image's ENV. That is correct
  (the image is pinned by `container_image_digest`) and it does mean the
  difference is invisible in a digest comparison.

- [x] **Two evidence families disagree about what an unreachable check writes.**
  Broadening 3's `image_env_*` / `hypothesis_*` keys are written on *every*
  path — `declared` with a value, the rest as explicit `null` — on the
  pre-container early return at ~~`preflight.py:507-523`~~ (that citation was
  stale before this was written: it pointed inside `_gitlink_paths`' docstring,
  not at the seed block). `stripped_paths` and
  `stripped_paths_present`, added by broadening 1, are simply **absent**
  there — and so is `suite_timeout_s`, added by broadening 4: no suite ran
  under any bound on this path, but that is indistinguishable from a gate too
  old to have the key. Both encode "the gate did not look"; only one family
  says so, and a reader of a cached `preflight.json` cannot tell an absent
  key from a verdict written by an older gate. Make them consistent by moving
  the strip's two keys (and `suite_timeout_s`) before the guard as `null`s —
  the new family is the shape to copy, not the other way round. Cheap, and it
  needs a
  `PREFLIGHT_VERSION` bump because it changes what a cached verdict contains.
  **Resolved, and the item under-counted the defect by a factor of six.** The
  inconsistent family is **twenty** keys, not three — taken by AST off every
  `evidence[...] =` statement in `preflight()`. Four of them
  (`grading_build_exit`, `grading_typecheck_exit`, `grading_lint_exit`,
  `scope_prefixes_absent`) are absent from the *ordinary healthy GO* verdict,
  which is the blob a task author reads most, so the three-key fix would not
  have fixed the reader's problem on the two blobs a reader is most likely to
  diff. Closed by a schema rather than by moving keys: `preflight.EVIDENCE_KEYS`
  lists all 42, `_evidence_seed()` fills every path from it,
  `PreflightResult.__post_init__` refuses a key set that is not it, and
  `test_evidence_keys_lists_exactly_what_preflight_writes` derives the tuple
  from the source by an AST walk so it cannot go stale the next time an item
  adds a key. New key `early_return` names the pre-container refusal, because
  under a uniform schema "many nulls" stops being a proxy for "no container
  started". `PREFLIGHT_VERSION` 16 → 17; no verdict moves.

- [x] **Re-screen the `HARVESTING.md` corpus at 3.11 and 3.13.** Broadening 5
  gave a manifest `image.python: "3.11" | "3.12" | "3.13"`, closed and
  enforced at load time, but the whole screen at `docs/BUILDING-A-TASK-SET.md`
  §2 was run in `python:3.12-slim-bookworm` only — so a candidate excluded for
  a suite failure with an unrelated-looking cause (a `SyntaxError` on newer
  syntax, a removed stdlib module, a C extension with no wheel) has never been
  tried against the interpreter that would actually fix it. No repository has
  been shown to be reopened by this key today. Re-run the screening command
  against `python:3.11-slim-bookworm` and `python:3.13-slim-bookworm` for the
  candidates `HARVESTING.md` already excluded, and report which pass — the key
  is built and gated, but claiming a yield improvement without this
  measurement would be the "report the gap, never estimate it" rule broken.
  **Result:** measured 2026-09-02, zero repositories reopened — all eight
  re-screened candidates show the identical blocker (or, for pygments, the
  identical green) on 3.11, 3.12 and 3.13; see
  `~/.cache/bakeoff-probe/reports/r2-19-rescreen.md` and `HARVESTING.md`.

- [ ] **No property-based determinism check exists for the node frameworks.**
  Broadening 3 refuses a pytest task whose declared tests import `hypothesis`
  with no `image.env: {CI: ...}` declared beside it, closing a gate a lucky
  draw could otherwise pass through. Nothing equivalent exists for `fast-check`
  or `jest-fuzz`: `node_adapter.py`'s `hypothesis_interpreter` returns `None`
  for both node frameworks, and the docstring beside it — and
  `test_runners.py`'s beside that — both cite this file as where the follow-up
  is tracked. A node task with a seeded-by-clock property suite gates on
  whichever draw preflight happens to run, exactly as a pytest one did before
  broadening 3 closed it there.

- [ ] **Two node ids with the same `fullName` in the SAME file are unguarded,
  and it is a different gap from the cross-file one — whose refusal round 2
  item 1 REMOVED rather than kept.** `-t` matches `fullName` alone, so a
  JS/TS suite that genuinely has two
  identically-titled tests in one file is indistinguishable to a selection or
  a deselection targeting either one — but there is no way to *represent* that
  as two different declared ids in the first place, since a node id is exactly
  `<file>::<fullName>` with no positional index: both same-file duplicates
  collapse to the identical string. `node_adapter.validate_id_set` (the
  loader, now a no-op) and preflight's `duplicate_full_names` check (the real
  report, now evidence) both compare `(fullName, path)` pairs and only flag a
  collision when the `path` differs — a same-file pair, where `path` agrees,
  passes both by construction. The per-file grouping does not help here
  either: both tests live in the one file that one argv names. A repo with
  this shape reads as clean at every gate and silently over-selects or
  over-deselects whichever test the manifest names.

- [ ] **A node check is several commands and the record does not say how
  many.** Since round 2 item 1 a node p2p deselect run is 1 + K invocations
  (K = files holding a deselection) and a node selection is one per file, each
  carrying its own `timeout <suite_timeout_s>` prefix. So
  `GradeRecord.suite_timeout_s: 600` is true of every command and is NOT the
  check's wall-clock bound, which is 600 × (1 + K) — and K is a function of
  the f2p set and of the quarantine, so two runs of the same task can differ.
  Nothing in `RunRecord` or `GradeRecord` records K. Splitting the budget
  across groups was rejected for the opposite reason: it would make the gated
  bound a function of the quarantine, so two tasks declaring the same number
  would get different ones. `SCHEMA_VERSION` did not move for this.

- [ ] **`_check_p2p` has no `not_run` branch, and the data it would need is
  structurally absent too.** `grader.py`'s `_check_f2p` reads
  `outcome.not_run` and routes a non-empty set to `state.environment(...)`
  rather than `state.fail(...)`, so a node id that stopped matching (M1:
  `-t` matching nothing exits 0 with every test skipped) is not stamped on
  the model. `_check_p2p`, forty-odd lines below, has no such branch — and it
  would not fire if added, as written: `preflight._Runner.classify` only
  fills `Outcome.not_run` when `self._selected` is set, and `_selected` is
  written by `_Runner.select` alone; `_check_p2p` calls `pass_to_pass`, which
  never touches it. So a p2p id that silently stopped matching is invisible
  on both counts — no check reads it, and no code path populates it.

---

## P3 — Decisions to settle before numbers are published

- **Grade summary can double-count across grader versions.** `grade.py`'s
  end-of-batch summary audits the last line per `(run_id, grader_version)`,
  so after a `GRADER_VERSION` bump the same run appears once per version in
  the per-model rows (printout only — nothing stored is wrong). Filter rows
  to the current version and warn with the other-version count, or key rows
  by `(model, grader_version)`. Found at final branch review, parked.

- **A same-version re-grade is invisible to the judge's supersession rule.**
  `summarize`'s rule 2 decides which of a comparison's two kinds of line wins
  by reading `grade_version_seen` → `GRADER_VERSION` (`_supersedes`,
  `scripts/judge.py`). That moves only when the grader version moves, and
  `grade.py` skips an already-graded run unless `--re-grade`, whose re-grade
  then appends at the SAME version — so the most common deliberate re-grade,
  one run to pick up a repaired oracle or image, leaves the votes in front even
  when the newer grade flipped the pair. The fix is a `graded_at` (or a
  per-line grade-line ordinal) on the judgment line, which makes two lines at
  one version orderable — a stored-shape change and a `JUDGE_SCHEMA_VERSION`
  bump, hence a decision rather than a patch. The blind spot is named in
  `_supersedes`'s docstring and in rule 2's prose until it is closed.

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

- [ ] **Relative `.gitmodules` urls.** git resolves `../toml-test.git` against
  the superproject's own remote. The derivation could resolve it against
  `task.repo_url` instead, which is well-defined and offline — but the
  resolution rules (`./`, `../` chains, a trailing `.git`, a `repo_url` with
  or without a trailing slash) need measuring before they are written, and the
  refusal is correct in the meantime. The likeliest thing to block a real
  repository. A deferral, not a defect (broadening 6).

- [ ] **Nested submodules.** `submodule update --init --recursive` plus one
  pruned mirror per `(inner url, inner gitlink)`, and preflight's
  `git submodule status --recursive`. Refused today because the untested path
  leaves the inner directory empty, which reads as clean. A deferral, not a
  defect (broadening 6).

- [ ] **A pure-gitlink submission is refused; a pure-gitlink *edit* is
  invisible.** The grader catches the agent who COMMITS inside a submodule
  (`SUBMODULE_GITLINK_UNGRADABLE`, read out of the submission's own chunks).
  It cannot catch the agent who edits and does not commit: `git add -A` stages
  nothing for a submodule, so the submission diff is **zero bytes** and the
  ladder stops at `EMPTY_PATCH` — a `GradeFailure` that stamps
  `resolved: False`. That is byte-identical to an honest empty run (a model
  that read the repo, concluded nothing needed changing and stopped), and no
  field distinguishes them, because the harness never observed the edit.
  Refusing tasks whose *reference* fix touches submodule content keeps this
  off the tasks where it would be the expected path, but it stays reachable on
  any task with a submodule, since what an agent chooses to edit is not
  something a manifest can constrain. Closing it means capturing per-submodule
  state at checkpoint time — a change to what a run RECORDS, not to how one is
  graded — so it needs its own plan. Measured 2026-09-01 (broadening 6, M8).

- [ ] **A pure gitlink rename is invisible to the grader's gitlink refusal.**
  `grader._chunk_is_gitlink` reads the chunk header for a `160000` mode line
  (`new file mode`, `deleted file mode`, `old mode`/`new mode`, `index …
  160000`); a rename chunk with 100% similarity carries none of those, so a
  submission that only moves a submodule directory applies `--index` green and
  is graded against the old content. Not measured on a real submission —
  recorded here so the gap is in the backlog and not only in the docstring.
  Closing it means reading the `similarity index`/`rename from` header and
  checking the destination's mode in the index. Measured 2026-09-02
  (broadening 6 fix wave).

- [ ] **The submodule's `remote remove` is `check=False` and the `reflog
  expire` beside it is `check=True`; nothing measured the asymmetry.** Both
  calls in `tasks._init_submodules` are leak guards over the same object — a
  host cache path, in `.git/config` and in `logs/HEAD` respectively — so
  tolerating a non-zero exit on one half means a leak that guard exists to
  remove can survive in silence. **Leave the code as it is.** The tolerant
  call is the one whose failure is ordinary ("origin does not exist" on a
  submodule git chose not to give a remote), and tightening it without first
  measuring which exit codes git actually produces there would trade a quiet
  leak for a loud false refusal mid-matrix. What is missing is the
  measurement, not the fix (broadening 6, final review).

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

- [ ] **`max_turns` and `wall_clock_timeout_s` still parse with a bare
  `int(...)`.** `int("forty")` raises a `ValueError` out of `load_task` with no
  manifest path in it, and `wall_clock_timeout_s: true` becomes 1 (`bool` is an
  `int` in Python). `tasks._positive_int`, added for broadening 4, exists and is
  applied to `suite_timeout_s` only; extending it to the other two could refuse
  a manifest that loads today, so it is its own change.

- [x] **Preflight's observed suite duration is not recorded, and it is the
  figure two separate readings need.** Closed 2026-09-03 (round 2 item 7). The
  bounded-command count in the older wording above was wrong by one: the gate
  makes **nine** bounded commands per pytest task (8 on a node one), not 8 —
  the five suite runs and up to three `grading.*` argvs, plus the bare pytest
  collection `PREFLIGHT_VERSION` 13 added, which carries the same `timeout`
  prefix and was left out of every count in `HARVESTING.md`,
  `docs/BUILDING-A-TASK-SET.md` and the click manifest's own comment.
  Reading (b)'s grader-side half was also already there: `CheckResult.duration_s`
  is filled from every rung that ran a command (`grader._timing`, three call
  sites) — what was actually missing was the *reference* measurement, not a
  new `GradeRecord` field. So this closes with no `GradeRecord` field and no
  `GRADE_SCHEMA_VERSION` / `GRADER_VERSION` / `ORACLE_VERSION` move: `preflight`
  now writes `bounded_run_durations_s` (one entry per bounded invocation,
  `null` for a run that did not happen) and `bounded_run_duration_max_s` into
  its own evidence, seeded by `_evidence_seed()` and enforced by
  `PreflightResult.__post_init__` the same way item 5's outer schema is.
  `run_matrix` prints the slowest beside the bound on every task's line — PASS,
  NO-GO or cache hit — and totals the gate at the end, bounded runs only.
  `PREFLIGHT_VERSION` 18.

- [ ] **`fresh_tree`'s husks are collected under two of six keys, and the empty
  key directories are never collected.** `container._sweep_stale_trees` runs at
  the leaf level, so it only ever reaches a husk under a key that is allocated
  under again: `preflight-tree/<task_id>` and `grade-preflight-tree/<task_id>`
  are revisited on every invocation and are bounded at one day of crashes; a
  husk under `grade-tree/<run_id>`, `oracle-tree/<task_id>`, a per-cell
  `tree/` or a per-run `claude-config/` survives until the operator's `rm
  -rf`. Bounded by work-done-and-crashed rather than by invocations, which is
  the acceptable shape, but it is not zero. The key directories themselves are
  never removed at all — ~960 empty inodes for an 80 × 4 × 3 matrix. Sweeping
  the key level is **refused**, not deferred: `fresh_tree` opens with
  `parent.mkdir(parents=True, exist_ok=True)`, which does not refresh an
  existing directory's mtime, so an age-guarded key sweep would `rmtree` a key
  a concurrent allocator has just passed through and is about to put a leaf
  under, whose `mkdir` then raises `FileNotFoundError`. Any fix here needs a
  different mechanism — a retry around the leaf `mkdir`, or a lock — not a
  wider sweep. (Round 2 item 3, 2026-09-03.)

---

## Housekeeping

- [ ] **`bakeoff/.env` holds three commented-out static AWS credential lines with
  plaintext values**, annotated in-file as an expired STS session superseded by
  the SSO profile. Inert and gitignored, but a real secret-key / session-token
  pair sitting on disk with no expiry tracking. Delete the lines.

- [ ] **`images.py` carries two dead pieces from broadening 5's single-runtime
  signature.** `_DEFAULT_PYTHON` (a module constant, restated from
  `tasks._DEFAULT_PYTHON` for the import-cycle reason its own comment gives)
  used to be `build_base_image`'s default argument; broadening 7 made the
  runtime and version explicit at every call site, so the default is never
  exercised. It is not fully unread, though — `test_images.py`'s
  `test_every_copy_of_the_default_version_says_the_same_thing` still asserts
  `images._DEFAULT_PYTHON == tasks._DEFAULT_PYTHON`, which is the drift pin
  the constant exists for, so removing it would need that test rewritten
  first, not just deleted. `build_base_image`'s `tag: str | None = None`
  parameter is the other half: every caller in `bakeoff/src`, `scripts/` and
  `tests/` passes only `(repo_root, runtime, version)`, so the parameter's
  default is exercised on every call and its non-default branch never is.
  Left in place when found (broadening 7, Task 5) to keep that diff to its
  subject.

- [ ] **A stored record graded before `SCHEMA_VERSION` 3.9.0 carries the
  tracked-but-ignored phantom, and its verdict was computed over it.**
  Measured 2026-09-02 (code review of round-2 item 2, `df699a8`, finding 1):
  `~/.cache/bakeoff/trucking-dry3/runs/71212309ce6538ea.json`
  (`trucking-2-stale-job-reaper`, schema 3.8.0, `turns_used 1`,
  `destructive_events []`) has 2,902 `deleted file mode` chunks in
  `artifacts.final_diff` (19.8 MB) and in checkpoint 1's `diff_vs_base`,
  matching exactly the 2,902 files tracked under
  `lib/python3.12/site-packages/` at its `base_sha` `66609e41` while
  `.gitignore` names `lib/` (the two sibling shas track 0 such files). It was
  graded: `trucking-dry3/grades/grades.jsonl` carries `resolved: False`,
  `GRADER_VERSION 2` — the ladder applied those 2,902 fabricated deletions to
  the grading tree before running the suite, so the `False` verdict is not
  trustworthy evidence about the agent's actual submission. Re-grading this
  one record (and auditing the other 97 `trucking-*` records under
  `~/.cache/bakeoff` for the same shape, since only this one was checked in
  full) is unstarted.

- [ ] **`mutation_check.py` has two pre-existing anchors whose `old` string is
  not unique in its file, so a mutation lands on whichever match comes first
  rather than the one the entry's comment names.** Measured 2026-09-02 (code
  review of round-2 item 2): `mutation_check.py:1024` ("collection: stop the
  record naming which collection produced it") has its `old` occurring **2×**
  in `src/bakeoff/runner.py`, and `mutation_check.py:1833` ("runners: read a
  report that was never written as a clean run") occurs **3×** in
  `src/bakeoff/runners/node_adapter.py`. Both are CAUGHT today, so this is
  latent rather than broken, but it is silent: `mutation_check.py` applies
  `original.replace(find, replace, 1)` with no uniqueness check. `tasks.py`'s
  `    if unknown:` is now at 3 occurrences too, so the next anchor reaching
  for a short `old` string there has a 1-in-3 chance of being silently
  misplaced. Close the class with one guard —
  `assert original.count(find) == 1` before the replace, reported as a
  distinct failure kind from MISSED — and retarget the two named anchors with
  longer, unique `old` strings.

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
