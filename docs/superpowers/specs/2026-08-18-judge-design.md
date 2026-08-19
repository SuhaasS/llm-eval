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
