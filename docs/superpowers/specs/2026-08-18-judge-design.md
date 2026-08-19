# The judge — design

The second grading channel: a neutral-family LLM that **ranks** submissions the
deterministic ladder has already **gated**. Spec §4.2.3 and §4.3 define the
protocol; this file is how it gets built, what it stores, and which mistakes
are silent.

Nothing here exists yet. `grader.py` is the whole of grading today.

---

## What it is

Two channels, and the separation is the load-bearing part of the whole design:

> **Deterministic checks gate; the judge ranks.** They are never averaged into
> a single number, and a run failing the deterministic gate receives no quality
> score at all — the quality of incorrect code is not a meaningful quantity,
> and scoring it would let the judge rescue a broken patch. (§4.2.3)

So the judge is not a grader in the sense `grader.py` is. It never produces
`resolved`, never contributes to pass@1, and cannot promote a `GradeFailure`
into a pass. It answers a different question — *among submissions that already
work, which is better, and how* — and it answers it in units that are useless
for gating and valuable for a purchasing decision.

Today's grading path, for contrast: `grader.py` runs nine checks in
`CHECK_ORDER` — `patch_non_empty`, `test_restore`, `build`, `typecheck`,
`f2p`, `p2p`, `lint`, `secret_scan`, `destructive_scan` — and `GradeRecord`
carries `resolved` plus the first rung that failed. There is no LLM anywhere in
`grader.py`, `oracle.py`, `grade_schema.py` or `scripts/grade.py`, and this
design does not put one there.

### Where it sits on disk

```
<event-log>/                    immutable, append-only, written by the harness
  runs/<run_id>.json
  index.jsonl
  grades/grades.jsonl           derived view 1 — the deterministic ladder
  judgments/judgments.jsonl     derived view 2 — this document
  judgments/payloads/<id>.json.gz
```

`judgments/` sits **beside** the log for the same reason `grades/` does: a
record is immutable once written, so a verdict is a derived view, and a
re-judge under a different model, prompt version or rubric is a **new line**
whose disagreement with the old one is the finding. It is a sibling of
`grades/`, not a child — the judge consumes `GradeRecord` and must not be
mistaken for part of it.

---

## What this is blocked on, and it is not code

**κ calibration is a people dependency (OPEN-5, unresolved).** §4.3 requires
engineers to grade a ~20% gold subset blind, with Cohen's κ reported against
it: κ ≥ 0.6 acceptable, ≥ 0.8 strong, and **below 0.6 the Tier B number is
directional only and must not carry the decision.**

Building the judge without that subset produces numbers that are *by the
project's own rule* not allowed to decide anything. That is not a reason to
delay the code — the judge has to exist before anyone can grade against it —
but it is a reason not to schedule the judge as though it unblocks a decision.
Whoever picks this up should read OPEN-5 first and find out who is grading.

**The current task set gives the judge no work.** Both `trucking-*` tasks carry
executable oracles, so the ladder already answers `resolved` outright. The
judge earns its place on Class C tasks (transcript + untested PR, reference
diff only) and on ranking among runs that all passed. Building it against the
present two-task set would exercise the plumbing and measure nothing.

---

## The units

### `similarity.py` — deterministic context, explicitly not a score

§4.2.1 lists three things computed and **passed to the judge as context**:
file overlap with the reference diff, symbols touched, diff-size ratio.

> Similarity to the reference is not correctness — valid solutions legitimately
> differ — so these inform judgment and never contribute to a score.

Write them here, not in `judge.py`, and give them their own tests. They are
pure functions over two diffs and are the only part of this design that can be
verified without a model call.

The failure mode to design against: someone later sums them into a number.
`SimilarityContext` therefore has no `score` field, no `__lt__`, and no
aggregate — three independent facts, handed over as three fields.

### `judge.py` — the two outputs

**Output (a), rubric profile.** Five orthogonal dimensions, 3-point anchored
scale (0 fails / 1 partial / 2 meets). Short scales are deliberate — 1–10
scales show poor inter-rater agreement:

1. **Functional equivalence** — accomplishes what the reference accomplished;
   explicitly *not* "is the same code"
2. **Completeness** — every part addressed, no stubs or TODOs
3. **Cross-file consistency** — callers updated, signatures aligned
4. **Scope discipline** — no unrelated edits or gratuitous refactors
5. **Convention adherence** — matches surrounding idiom and error handling

Plus unscored binary flags: `introduced_stub`, `left_debug_artifacts`,
`wrote_tests`.

Diagnostic, not decisive. The rubric profile is reported per model as a
profile; it is not summed into a rank.

**Output (b), pairwise preference.** Blind, position-randomized, ties
permitted, round-robin across all four arms. Win rates convert to Elo,
following GDPval.

**The pairing rule is the subtle one.** Compare sample *i* of one model against
sample *i* of another **on the same task**. Both are random draws from their
respective distributions, so the comparison is unbiased.

> Never pair a draw against another model's best, median, or a single canonical
> run; that compares a draw to a statistic and skews the win rate. (§4.2.3)

Where one side failed the deterministic gate and the other did not, **the
pairwise is skipped and recorded as a gate-decided win** — the objective result
already settles it, and asking a judge would let it disagree with a fact.

**Sonnet 5 is an arm, not the answer key.** It takes the same absolute rubric
and the same round-robin as every candidate. The ground-truth anchor is the
merged PR diff (§3.2) — human-authored, reviewed, shipped. Any implementation
that treats a Sonnet run as a reference has changed what the eval measures.

### What the judge receives, and what it must never receive

Receives: task prompt, reference diff, candidate diff, deterministic check
results, rubric items seeded from human correction turns (§3.3), and the
`SimilarityContext` above.

**Never receives: model identity, cost, timing, or any prior verdict.**

Each of those is a distinct leak and each needs its own guard, because the
payload is assembled from records that carry all four:

- `RunRecord.model` and the arm name are obvious. Less obvious: `bedrock_model_id`,
  the `sampling` block, `finish_reasons`, and `artifacts.*` paths that embed the
  arm name in a directory component. A payload builder that copies
  `record.artifacts` verbatim leaks the model in a file path.
- Cost and timing identify arms almost as well as names do — Sonnet's
  `cache_read` signature and the candidates' `cost_usd: null` (pricing_error)
  are both fingerprints.
- A prior verdict in the payload turns three independent votes into one vote
  and two confirmations.

The guard is a **whitelist payload builder**, never a redactor: build the
payload field by field from named sources, so a new `RunRecord` field is absent
by default rather than leaked by default. `SCHEMA_VERSION` moves for additive
fields precisely because readers cannot be trusted to notice them.

### `judge_schema.py` — `JudgeRecord`

Per §6.3, stored separately and linked by `run_id`:

```
JudgeRecord:
  judgment_id, judged_at
  judge_model_id                  # PINNED. e.g. "openai.gpt-5.6-sol", never an alias
  judge_prompt_version            # integer, moves on any prompt text change
  judge_prompt_sha                # digest of the exact rendered prompt
  judge_sampling                  # temperature, top_p, max tokens — as sent
  rubric_version
  kind                            # "rubric" | "pairwise"

  # rubric
  run_id, task_id, dimension_scores{5}, flags{3}

  # pairwise
  run_id_a, run_id_b, task_id, sample_index
  position_assignment             # which run_id was shown as A
  verdict                         # "a" | "b" | "tie" | "gate_decided"
  gate_decided_by                 # null unless verdict == "gate_decided"

  # both
  full_reasoning_text
  input_payload_path              # gz, beside the jsonl
  input_payload_sha
  vote_index                      # 0..2 — three samples per comparison
  grade_version_seen              # the GradeRecord this verdict was gated on
  graded_against_task_set_commit
```

`input_payload` is **required**, not optional, and §4.3 says why: *"because
full judge inputs are logged, a re-judge is a re-score, not a re-run."* The
tokens for the original run are already spent; a verdict whose input cannot be
reconstructed forces a re-collection to re-score.

Store the payload gzipped beside the jsonl and record its sha, rather than
inline — payloads carry two full diffs and would dominate the line file.

### `scripts/judge.py` — the batch driver

Same shape as `scripts/grade.py`: reads an event log, reads `grades.jsonl`,
writes `judgments.jsonl`, never opens the event log for writing. Resumable —
a comparison already judged under this `(judge_model_id, judge_prompt_version,
rubric_version)` is skipped unless `--re-judge`.

Order of operations per task-sample:

1. Load the four runs' `GradeRecord`s.
2. Drop any run whose `resolved is None` — excluded runs are not observations
   of the model and must not be ranked. (Five such rows exist in
   `trucking-pilot-v2`; a judge that ranked them would rank a credential
   failure.)
3. For each of the 6 pairs: if exactly one side passed the gate, record
   `gate_decided` and make no model call. If neither passed, record nothing.
   If both passed, run three votes with independent position randomization.
4. Majority vote over the three. Ties permitted at the vote level and at the
   majority level.

---

## Model configuration

§4.3 requires a **neutral family, mandatorily**: the compared families are
Anthropic, Google, NVIDIA and Moonshot, and the judge must sit outside all
four. A Claude judge inflates Sonnet 5; a Gemini judge inflates Gemma 4.
Self-preference bias is measured — one case had a model rate its own backbone
at 33.7% faithfulness where an independent judge said 14.13%.

**Measured 2026-08-18** against `https://bedrock-mantle.us-east-1.api.aws`,
the same endpoint and the same derived bearer token the candidate arms already
use, so the judge needs no new credential path:

| available | note |
|---|---|
| `openai.gpt-5.6-luna`, `-sol`, `-terra` | GPT-class, outside all four families — §4.3's stated preference |
| `openai.gpt-5.5`, `openai.gpt-5.4` | older, pin only for a drift check |
| `qwen.qwen3-235b-a22b-2507` | the model AA-LCR uses for equality-checking; the §4.3 drop-in if residency policy changes |

**Pin one variant and record it.** `luna`/`sol`/`terra` are three models, not
three names for one, and `judge_model_id` is what makes a verdict
reproducible. A floating alias is a moving oracle in exactly the way an
unpinned `image.pip` version is — the same submission scores differently on two
passes and the record cannot say which model answered.

**The residency tradeoff is already accepted and recorded** (§4.3): a
GPT-class judge sends candidate diffs to a third party, the same class of
objection that ruled out OpenRouter for production traffic in §2, at judging
volume rather than for everything. Reviewed and accepted, with Qwen as the
swappable alternative. Note for this codebase specifically: the mantle
endpoint is AWS-side, which is a materially different posture from calling
OpenAI directly — worth re-confirming with whoever accepted the original
tradeoff, since the thing they accepted may not be the thing being built.

---

## Invariants

Enforced in code and asserted by tests. Each is silent when broken.

- **The judge never rescues a failed gate.** No code path may write a
  `JudgeRecord` for a run whose `GradeRecord.resolved` is `False`, and no
  downstream view may combine a rubric score with `resolved` into one number.
  This is §4.2.3's central rule and the reason the two channels exist.
- **An excluded run is never judged.** `resolved is None` means the row is not
  an observation of the model. Ranking it ranks the infrastructure.
- **The payload is built by whitelist, not by redaction.** A new `RunRecord`
  field must be absent from the judge payload by default. Redaction fails open;
  whitelisting fails closed.
- **Position is randomized per comparison and recorded.** `position_assignment`
  is stored, not derived, so a position-swap probe can be run over a subset
  after the fact without re-judging everything.
- **Three votes are three independent calls.** Same payload, independent
  position assignment, no shared context. A single call asked to produce three
  opinions is one vote wearing three hats.
- **`kappa_in_force` travels with any published number.** §4.3: below 0.6 the
  number is directional only. A view that reports Elo without the κ that was in
  force at judging time has dropped the caveat that makes it honest.
- **A verdict names the grade it was gated on.** `grade_version_seen`. A
  re-grade under a new `GRADER_VERSION` can flip `resolved`, which changes
  which pairs are gate-decided — so a verdict is only interpretable against the
  grade generation it saw.
- **Judgments are append-only.** No update, no delete. A re-judge is a new line
  and the disagreement is the finding.
- **Subsample samples, never pairs.** If cost binds, drop sample indices.
  Dropping pairs breaks the Elo graph's connectivity; subsampling only widens
  intervals. (§4.2.3)

---

## Cost and scale

Round-robin over 4 models is 6 pairs per task-sample. At 60 tasks × 10 samples
× 3 votes that is **~10,800 judge calls**, plus 4 rubric calls per task-sample
(2,400) if the rubric runs at full N.

Two levers, in order: run the rubric at a lower N than the pairwise (it is
diagnostic, not decisive), and subsample sample indices for the pairwise. Both
preserve the Elo graph. Neither drops a pair.

Gate-decided pairs cost nothing, which means the judge gets cheaper exactly
when the task set discriminates — on a task where two arms fail the gate, four
of the six pairs are free.

---

## Out of scope, on purpose

- **Absolute quality scoring.** §4.2.3 restricts the judge to equality-checking
  against a reference or blind pairwise. "Rate this diff 1–10" is the
  open-ended question whose κ ≈ 0.32 motivated reference-anchoring in the first
  place.
- **Judging the trajectory.** The judge sees the submission, not the transcript.
  Tool-call quality and turn efficiency are already measured deterministically
  in the `RunRecord`.
- **Any write into the event log or into `grades.jsonl`.** Two derived views,
  two files, no cross-writes.
- **Rescuing an ungraded run.** If `grade.py` could not grade it, the judge has
  no gate to defer to and does not run.

---

## Testing

- `similarity.py` is pure and gets ordinary unit tests, including the case that
  motivates it: a correct fix with zero file overlap with the reference must
  produce low similarity and must not be penalized anywhere downstream.
- The payload builder gets a **leak test**: construct a `RunRecord` with every
  field populated, build a payload, and assert the model name, the arm name,
  every cost field, every timing field and every artifact path are absent from
  the serialized bytes. Add a companion test that a newly added `RunRecord`
  field does not appear — the whitelist's whole point.
- The pairwise driver gets a fake judge (deterministic, no network) to pin
  ordering, gate-decided handling, exclusion dropping, and majority-vote
  arithmetic including ties.
- Position randomization gets a seeded test asserting both assignments occur
  and that `position_assignment` matches what was sent.
- One integration test with a real judge call, marked `integration`, pinned to
  the recorded `judge_model_id` — the offline gate stays offline.

---

## Open questions

- **OPEN-5 (blocking for any published number):** who grades the ~20% human
  gold subset, how many people, hours allocated. Without it κ is unmeasurable
  and every judge-derived number is directional by default.
- **Which GPT-5.6 variant.** `luna`/`sol`/`terra` differ; pick by measuring
  agreement against the human subset, not by name.
- **Rubric N.** Whether the rubric profile runs at full N or a subsample.
  Diagnostic output at N=10 is probably over-buying.
- **Residency re-confirmation.** §4.3 accepted a third-party judge; the mantle
  endpoint is AWS-side. Confirm the accepted tradeoff still describes what is
  being built.

---

## Amendments (2026-08-19)

Four things this document specified that the code now does differently, and
each one was found by building it. The superseded passages are left standing
above rather than edited in place: what changed and why is the useful record,
and a spec that silently rewrites itself teaches the next reader nothing.

### A comparison is two forced positions, not three random ones

*Supersedes step 3 and step 4 of "Order of operations per task-sample", the
invariant **Three votes are three independent calls**, and the invariant
**Position is randomized per comparison and recorded**.*

Three votes at independently drawn positions was written for a judge that
varies. This one does not: it runs at temperature 0, and at temperature 0 a
judge is completely described by its answer under each of the two orders.
Everything a third call can return is a copy of one of the first two, so
majority-of-three over drawn positions carries the statistical content of a
single vote — measured, and distributionally identical — while costing three
times as much and settling nothing.

Every comparison is therefore shown in **both** orders, one vote each, with the
position an argument the driver forces rather than a draw. Three properties
follow, and the third is the one worth the change on its own:

The pass becomes reproducible. The same collection judged twice buys the same
two prompts, so a re-judge is a re-run of the same question rather than a
second sample of a different one.

The bill drops by a third. "Cost and scale" above computes 60 tasks × 10
samples × 6 pairs × 3 votes = ~10,800 pairwise calls; the same collection is
now ~7,200.

And §4.2.3's position probe stops being a separate experiment. Under drawn
positions, position consistency was an inference across comparisons and was
unmeasurable on any single one — a comparison could easily hold two votes in
the same order, which says nothing about position at all. Under forced
positions every comparison holds one vote in each order, so consistency is read
directly off the file: a 1–1 split is the judge preferring what it saw first,
and `majority` reports it as the tie it is rather than breaking it toward
whichever vote was drawn twice.

`JUDGE_PROMPT_VERSION` now moves on **protocol** changes as well as prompt-text
changes, and 1 → 2 is that move. The bump is required rather than tidy: a v1
line carries `vote_index` 0 or 1 too, drawn under a random position, and the
resume key holds the index and the version and not the position. Left at 1, a
v1 line at index 0 answers the v2 unit for index 0 and the driver skips it —
producing a clean-looking resume over a comparison holding one random-position
vote where the protocol says two forced ones, and every position-consistency
figure taken off that file computed over votes nobody bought together.

**The bump also re-buys every rubric line, and an operator who is not told
discovers it on the bill.** `_resume_key` keys a rubric line on
`("rubric", run_id, judge_model_id, judge_prompt_version, rubric_version)`, so
bumping the prompt version makes every rubric call in an already-judged
collection a fresh unit. That is correct — a rubric profile answered under one
prompt generation must not be pooled with one answered under another — but the
protocol change is a *pairwise* change, and nothing about it suggests the
rubric is about to be paid for a second time. Plan a re-judge accordingly, or
pass `--no-rubric`.

### "Win rates convert to Elo" means Bradley–Terry, fit and reported on the Elo scale

*Amplifies "Output (b), pairwise preference".*

The sentence above admits two readings, and the obvious one is wrong. Elo as
ordinarily implemented is a sequential K-factor update: ratings walk as
comparisons arrive, so the result depends on the order they arrive in. There is
no natural order here — the comparisons are a set, not a stream — so whatever
order the code happens to iterate in becomes an input to the published table.
Measured on the ladder fixture at 150 comparisons per pair with the arm names
sorted against strength, **40 of 40 seeds printed a wrong ranking**, and the
fixture in the tests prints the order exactly backwards, weakest arm on top by
740 points. Renaming the arms, which says nothing about any of them, moved
ratings by more than 200 points.

The implementation is the maximum-likelihood Bradley–Terry fit instead, by
MM/Zermelo iteration. The likelihood is a function of the win counts alone, so
a shuffle, a relabelling and a second pass over one collection all produce one
table. Ratings are then printed on the Elo scale purely so a reader keeps the
intuition they already have: `ELO_SCALE = 400`, applied to log10 of the fitted
strength, so 400 points is a 10:1 strength ratio and ~70 points is 60/40 — and
`ELO_ANCHOR` puts the mean of the table at 1000. The
anchor is presentation only; Bradley–Terry identifies differences between
strengths and nothing else, so the overall level is free and would otherwise
wander with the arm set.

One prior, and it is recorded because it can reorder: **one virtual tie per
unordered pair that has at least one real comparison** (§D8). Without it an arm
that never lost has an MLE of +∞ and a winless arm −∞, and the iteration either
burns its cap chasing one or prints an `inf` formatted as a rating. Half a
point each way per *played pair* is the lightest thing that bounds both, and at
this bakeoff's size it moves arms within ~50 points of each other by under a
point. It is not uniform shrinkage, though: the prior is per pair rather than
per arm, so an arm whose comparisons sit in sparse pairs is pulled harder than
one whose comparisons sit in dense pairs, and uneven shrinkage can cross two
arms rather than merely compress them. Equal per-pair counts are not a property
the fit may assume — a resumed batch, an arm added mid-collection and dropped
comparisons all skew them.

### What the summary actually prints

*New. The document specified the record and the driver and left the reader
undescribed.*

`position_consistency` is six keys per generation, not a rate:
`measurable`, `consistent`, `single_position`, `position_unrecorded`, `rate`
and `rate_ci95`. The three counts beside the rate are the denominator's own
story — comparisons the probe could read, comparisons that hold one vote and
so say nothing about position, and lines that never recorded an assignment —
and a rate printed without them invites a reader to divide by the wrong total.

Every rate and every rating carries a **task-clustered bootstrap 95%
interval**, per §10.3 ("any metric reported without a confidence interval is
not reported") and §4.4, which says which interval: resampled over tasks,
because comparisons inside one task are correlated and effective sample size
tracks the task count rather than the comparison count. Resampling comparisons
independently prints a band several times too narrow, which is worse than
printing none.

Two degenerate cases print rather than pretend. A rate swept to 0% or 100%
gives every resample the same value, so the bootstrap returns zero width where
the evidence is thinnest; those widen through a Wilson interval computed on the
**task** count behind that rate, and a rate resting on a single task is left
degenerate and marked, since a Wilson band on n=1 would dress one task up as a
measurement of the population. A zero-width *rating* band has no such
correction available and prints `--`, the same dash the table already uses for
an arm the judge never voted on: a refusal, not a value.

### §4.3's "mandatory" is enforced, and the enforcement has edges

*Amplifies "Model configuration".*

A neutral judge family is checked in code, at the driver, before the collection
is read and ahead of the resume — so a re-judge cannot inherit a compared-family
judge from the first pass. Two match rules, because each covers what the other
cannot: a vendor-namespace prefix (`anthropic.`, `google.`, `nvidia.`,
`moonshot.`) catches `anthropic.opus-6`, a compared family under a model name
that never says "claude"; a family token (`claude`, `gemma`, `gemini`,
`nemotron`, `kimi`) catches `bedrock.claude-sonnet-5`, Claude itself under a
neutral vendor namespace, which is what a re-host looks like. A guard missing
either half admits a biased judge whose table is indistinguishable from a clean
one.

The escape is an environment variable, `BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE=1`, and
not a flag — a flag beside the ordinary options is how a mandatory rule becomes
a default, and can be added to a wrapper script by somebody who read the
refusal as an obstacle. It exists because measuring how much a Claude judge
inflates Sonnet 5 on this endpoint is a real experiment, and because a rule with
no escape gets deleted rather than obeyed. Under the override the pass is
admitted loudly: a `NON-NEUTRAL JUDGE ADMITTED` warning on the terminal while
the run is still cheap to stop, and the same text recorded beside the numbers
for whoever reads them later.

Two limits, stated because an unstated limit stops being reviewed:

The token list is substring-matched and deliberately over-wide. A genuinely
neutral judge whose id happens to carry one of those tokens is refused, and its
only route through is the override — which stamps a false non-neutral record
onto honest numbers, and one a later reader cannot tell from a real
self-preference probe. The direction is still right, since a false negative
publishes a biased table nothing downstream can detect and tells nobody, but
"the message answers it" is not true and pretending otherwise is how the width
stops being examined. An exact-id allowlist is the obvious future fix.

And `live_completion` is unguarded. The refusal lives at the driver, so the
integration test — which calls the completion seam directly, passing its own
model id — reaches the wire without passing through it. That is harmless there,
since the id it passes is the pinned neutral default, but it is a hole in the
claim that the guard is unconditional: anything that calls the seam directly is
outside it, and the next caller to do so need not be a test.
