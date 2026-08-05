# LLM Bakeoff Eval — Design Spec

**Selecting a lower-cost Bedrock model to replace Claude Sonnet 5 for agentic coding at Pindrop**

Author: Suhaas Surapaneni · 2026-08-03
Companion to: `Model_Bakeoff_Plan.md`

---

## 1. Purpose

Produce trustworthy benchmark scores for Nemotron 3 Super 120B, Gemma 4 31B, Kimi K2.5, and Claude Sonnet 5 on Pindrop's own agentic coding workloads, along with the evidence trail needed to defend them.

**This eval measures; it does not decide.** All four models are scored on the same footing — Sonnet 5 is an arm, not the answer key. Whether any score justifies a migration is a separate call made later with this data in hand (§10).

**Model-neutrality rule.** No experimental parameter may be calibrated to a single model. Task selection, budget caps, prompts, configuration, and scoring are derived from the whole field or from human ground truth, never from the incumbent's behavior. Sonnet 5 is the *comparison* — the thing being measured against — and it is measured too. Where a parameter must come from somewhere, it comes from the slowest or broadest case across all four arms (§3.5, §5.4), so no model is advantaged by having set the ruler.

Public benchmarks cannot answer this. No benchmark covers all three candidates cleanly, vendor and independent numbers disagree by double digits on the same model, and SWE-bench Verified — the only benchmark with meaningful coverage — was deprecated by OpenAI in Feb 2026 after an audit found ~59% of tasks materially flawed and every frontier model tested had memorized the human fix.

An internal eval on Pindrop's private repos has one property no public benchmark has: **the tasks have never been in any model's training data.** This is the strongest methodological claim available and should be stated prominently in the final writeup.

### Primary deliverable

**A complete, immutable event log of every run.** Scores are derived views over that log. This ordering is deliberate: re-running 2,000 agentic sessions is expensive and the models will have changed by then, but re-scoring a preserved log is free. Any metric not captured at run time is lost permanently.

---

## 2. Scope

**In scope:** Claude Code + self-hosted LiteLLM proxy against Bedrock, as the primary harness. This matches production dev tooling exactly.

**Secondary, nice-to-have:** the same task set through the opencode harness. Gated behind the primary run completing. Its purpose is diagnostic — it disambiguates "this model is weak" from "this model can't speak Claude Code's tool protocol." Do not start it until primary results are in hand.

**Out of scope:** OpenRouter as a routing layer (rejected in the plan doc — BYOK still proxies through third-party servers, plus ~5% fee). Production rollout mechanics beyond the recommendation.

---

## 3. Data Pipeline

### 3.1 Sources and join

Four sources, joined into one task record:

```
Jira/Linear ticket ──(ticket ID in branch name / PR title)──> merged PR
merged PR ──────────> base_sha (parent commit), reference_diff, tests changed
transcripts ────────(cwd + gitBranch + timestamp window)──> PR
```

Claude Code transcripts live at `~/.claude/projects/<munged-cwd>/<session-uuid>.jsonl`. Every record carries `cwd`, `gitBranch`, `timestamp`, `sessionId`; assistant records add `message.model` and full `message.usage` (input, output, cache-read, and cache-creation tokens split by 5m/1h ephemeral tier). Branch name plus timestamp is the join key into PR history.

**Known gap:** transcripts record `gitBranch` but no commit SHA, so exact codebase state is not directly recoverable. Recover by correlating `timestamp` against `git log` on that branch; lossy beyond reflog expiry and impossible for deleted branches. Going forward, add a `SessionStart` hook that stamps `git rev-parse HEAD` so future sessions are replayable.

### 3.2 Task classes

| Class | Has | Oracle | Tier |
|---|---|---|---|
| **A — gold** | transcript + tested PR | executable + reference diff | either |
| **B — mined** | tested PR, no transcript | executable + reference diff | mostly A |
| **C — agent-only** | transcript + untested PR | reference diff only, judge-scored | mostly B |

Every task carries the **real merged PR diff as reference**, without exception. This is what converts the judge's question from open-ended "is this diff good?" (Cohen's κ ≈ 0.32, below the 0.6 acceptability bar) into reference-anchored "does this accomplish what the reference accomplished?" The dataset, not the judge prompt, is what makes judging accurate.

### 3.3 Prompt construction

**One human instruction; the agent then runs autonomously for as many turns as it needs.** Single-turn does not mean one-shot — the agent reads, edits, runs tests, sees failures, and self-corrects without human input.

Multi-turn human corrections from the original transcript are **not replayed**. A follow-up like "fix the import on line 40" was conditioned on the original model's output; against a candidate that wrote different code it is incoherent, and would score models on following directions about code they never wrote.

Those correction turns are repurposed instead: what the human had to correct is a free, human-authored list of what "done" required. They seed **judge rubric items** and candidate assertions for executable tasks.

**Underspecification mitigation.** Harvested first-turns are often deliberately thin because the human expected to iterate. Append the Jira/Linear ticket description to the harvested prompt — identical text for every model, so no bias. The baseline pilot (§3.5) catches whatever remains.

**No LLM user-simulator.** Precedent exists (τ-bench, and AA's τ³-Banking in the Intelligence Index), but it is wrong here: it confounds model with simulator, and a useful simulator needs the answer, which leaks unevenly — a floundering model draws more corrective detail, actively compensating weak models and inverting what the eval measures.

### 3.4 Stratification

Sample tasks to match the **measured** production distribution, not a guessed split. Run all transcripts (own + team) through a stats pass first: files touched per session, turn count, output tokens, tool-call mix, session duration. Weight strata to that shape. A 50/50 Tier A/B split on a workload that is 70% small edits measures the wrong thing and the result will not transfer to the bill.

Include a **long-session stratum**. All three candidates claim 256K context, but effective use degrades well before the limit and unevenly across models. Small tasks will never surface this, and real sessions in the transcript corpus run long.

### 3.5 Selection and freeze

Harvest ~80 candidate tasks to land ~60. Target split adjusted to measured distribution; starting point ~40 Tier A / ~20 Tier B.

**Calibration pilot before freeze — all four models, not just Sonnet 5.**

Run **every model** at N=3 across all ~80 candidate tasks. Selecting tasks by one model's difficulty curve would tilt the set toward what that model happens to discriminate on, which is precisely the bias this eval exists to avoid. Calibration is a property of the field, never of the incumbent.

Drop rule, model-neutral:

- Drop a task only if **all four models** score 0% (floor — usually underspecified rather than hard) or **all four** score 100% (ceiling — no discriminating signal)
- **Keep** any task where models disagree, however lopsidedly. Disagreement is signal; a task only Sonnet solves is among the most informative tasks in the set

Expect to lose 20-30%. Pilot cost: ~80 tasks × 4 models × N=3 ≈ 960 runs — cheap relative to what it protects against.

The pilot also sets budget limits (§5.4) from the **slowest-converging model's** distribution, not the incumbent's. Do not guess these.

### 3.6 Privacy gate — blocks team data collection

Team transcripts contain full source code, file contents, and plausibly secrets in tool outputs (env dumps, config reads, connection strings). Before any collection:

- [ ] Engineer consent — these are personal work logs, not org artifacts by default
- [ ] Secret-scanning pass (trufflehog / gitleaks) over all JSONL before it lands anywhere shared
- [ ] Retention decision: private repo, defined lifetime, documented access list

Do this first. Retrofitting a privacy pass after twenty engineers' transcripts are in a shared bucket is far worse than doing it up front.

### 3.7 Task record schema

```
task_id, tier, stratum
repo, base_sha, container_image_digest
prompt                      # verbatim first turn + ticket description
reference_diff              # the real merged PR
files_touched, loc_changed
tests: { f2p: [...], p2p: [...] } | null
rubric_items: [...]         # seeded from human correction turns
provenance: { session_id, ticket_id, pr_number, timestamp, original_model }
```

---

## 4. Scoring

Methodology mirrors what established benchmarks actually do, which is narrower than commonly assumed.

### 4.1 What the benchmarks do

- **Executable oracle on final state, not trajectory.** Terminal-Bench verifies "the final container state only, not agent commands or intermediate steps." SWE-bench runs the repo's real tests. AA's τ³-Banking checks backend database state. None grade whether the agent worked elegantly.
- **Binary, all-or-nothing per task.** Terminal-Bench: must pass all pytests for any credit. SWE-bench: every FAIL_TO_PASS must flip *and* every PASS_TO_PASS must hold. No partial credit — that is what removes grader subjectivity.
- **Regression guard mandatory.** PASS_TO_PASS is the anti-cheat; without it, deleting the failing test scores a win.
- **pass@1 averaged over k repeats, not pass@k.** AA runs 3 repeats (Terminal-Bench, SciCode, AA-LCR) or 5 (τ³-Banking, GPQA, CritPt), aggregating `sum(correct) / total attempts`. Measures reliability, not best-of-k.
- **Sampling params frozen.** Temp 0 non-reasoning, 0.6 reasoning, output cap 16,384.
- **LLM judges only equality-check against a reference** (AA-LCR uses Qwen3 235B; HLE uses GPT-4o). Never open-ended quality judgment.
- **Subjective deliverables → blind pairwise, never absolute scores.** GDPval has occupational experts rank unlabeled deliverables; Elo from win rates.
- **Confidence intervals reported.** AA: ±1% on the composite at 10+ repeats. Individual evals are far worse; the composite is tight only because averaging nine noisy evaluations cancels error.
- **Finer granularity is reported alongside, never blended in.** SciCode reports main-problem resolve rate (all-or-nothing — every subproblem must pass) *and* subproblem solve rate as separate figures, breaking ties on the latter. That is how to get resolution without inventing partial credit.

#### 4.1.1 What this spec mirrors, and what it deliberately does not

A reviewer familiar with AA will ask why this eval does not look like theirs. The mechanics are mirrored; the composition is not, and the differences are deliberate.

**Mirrored:** executable oracle wherever one exists (§4.2.1); binary all-or-nothing per task; pass@1 aggregated over repeats (§4.2.2); frozen sampling parameters (§5.3); judge restricted to equality-checking or blind pairwise, never absolute quality scoring (§4.2.3); confidence intervals on everything (§4.4).

**Rejected, with reasons:**

| AA practice | Why not here |
|---|---|
| Composite Intelligence Index | AA ranks models in general; this eval decides one question about one workload. A weighted average would let a cost advantage numerically offset a correctness deficit — the exact tradeoff that must stay visible (§10). |
| 1-repeat evaluations (HLE, GDPval, Omniscience) | Defensible when averaging across nine evals; indefensible when a single number informs a migration. N=10 here is not over-engineering relative to AA — it reflects that one eval carries the whole signal. |
| Scientific-reasoning suite (GPQA, HLE, CritPt) | Those measure reasoning as a *proxy* for downstream capability. Pindrop has the downstream task itself. Substituting a proxy for a directly observable outcome is strictly worse information. Tier A and Tier B are the reasoning measurement. |

Three further deviations are upgrades for this purpose rather than gaps: contamination-free private tasks (§1), N=10 over AA's 1–5, and a scorecard in place of a composite.

### 4.2 Scoring stack

Two independent channels. **Deterministic checks gate; the judge ranks.** They are never averaged into a single number, and a run failing the deterministic gate receives no quality score at all — the quality of incorrect code is not a meaningful quantity, and scoring it would let the judge rescue a broken patch.

| Layer | Pattern from | Method | N |
|---|---|---|---|
| Tier A | SWE-bench Verified | binary resolved: all deterministic checks pass | 10 |
| Tier B | GDPval + AA equality-check | reference-anchored judge, blind, position-randomized | 10 |
| Harness fidelity | — | tool-call malformation rate, truncation rate, turns-to-completion | every run |
| Economics | AA cost-per-task | total spend ÷ tasks resolved | every run |

Tier B may additionally be made executable (acceptance tests per task) — see §4.5.

#### 4.2.1 Deterministic checks

Run in order; first failure short-circuits and sets `failure_class`.

| # | Check | Pass condition |
|---|---|---|
| 1 | Patch non-empty | diff vs `base_sha` has content |
| 2 | **Test files restored from `base_sha`** | mandatory pre-step, see below |
| 3 | Build / compile | exit 0 |
| 4 | Type check | repo's own `mypy` / `tsc` config, exit 0 |
| 5 | **F2P** | every fail-to-pass test now passes |
| 6 | **P2P** | every pass-to-pass test still passes |
| 7 | Lint | repo's own config, exit 0 |
| 8 | Secret scan | gitleaks clean on the diff |
| 9 | Destructive scan | no unreverted high-severity event |

`resolved` = all nine pass. Binary, all-or-nothing, no partial credit.

**Check 2 is the primary anti-cheat.** Before grading, `git checkout <base_sha> -- <test paths>`, discarding whatever the agent did to test files. Without it, "make the tests pass" is trivially satisfied by deleting or weakening them, and models do this at different rates — so it would register as a capability difference. SWE-bench takes the same approach.

Separately, **record** whether the agent modified test files. Legitimate when the task called for new tests, cheating otherwise. Always logged, never silently dropped.

Deterministic but explicitly **not scored**, computed and passed to the judge as context: file overlap with the reference diff, symbols touched, diff-size ratio. Similarity to the reference is not correctness — valid solutions legitimately differ — so these inform judgment and never contribute to a score.

#### 4.2.2 Score construction

```
per run:    resolved ∈ {0, 1}
per task:   pass@1_task = resolved_count / N          # N = 10
            pass^k_task = 1 iff all N samples resolved
per model:  pass@1 = mean over tasks of pass@1_task
            pass^k  = mean over tasks of pass^k_task
```

**Sub-goal rate, reported separately (SciCode pattern).** Each Tier B task decomposes into ordered sub-goals — available at no extra cost, since seeding rubric items from human correction turns (§3.3) already produces them. Report:

```
resolve_rate  = fraction of tasks fully resolved     # all-or-nothing, headline
subgoal_rate  = fraction of sub-goals satisfied      # finer, diagnostic + tiebreak
```

The two are **never blended into one figure**. `resolve_rate` is the headline; `subgoal_rate` breaks ties and shows how close a near-miss was. Inventing weighted partial credit would reintroduce exactly the grader subjectivity that all-or-nothing scoring exists to remove.

**Analysis is paired, always.** Every model runs the identical task set, so the unit of comparison between any two models is the per-task difference, never two independent marginal rates. Task difficulty is common to both arms and cancels in the difference. McNemar's test for binary paired outcomes; cluster bootstrap over tasks for rate differences. Sonnet 5 is one arm among four here, not a fixed reference — every model pair is comparable this way.

**Confidence intervals cluster by task.** The N=10 samples within a task are correlated, so effective sample size tracks the task count, not the attempt count. A naive binomial CI over 600 attempts materially overstates precision — with bimodal per-task rates the unpaired figure is nearer ±12 points at 60 tasks. Paired analysis brings this to roughly ±6 points, which is the design target.

Consequence for planning: **more tasks tighten the CI; more samples per task do not.** N=10 exists to measure pass^k and to build the budget curve (§5.5), not to sharpen the mean. If CI width becomes binding, harvest additional tasks rather than raising N.

#### 4.2.3 Judge protocol

Judge receives: task prompt, reference diff, candidate diff, deterministic check results, rubric items seeded from human correction turns (§3.3), and the non-scored overlap context above.

Judge never receives: model identity, cost, timing, or any prior verdict.

**Output (a) — rubric profile.** Diagnostic, not decisive. Five orthogonal dimensions on a 3-point anchored scale (0 = fails, 1 = partial, 2 = meets). Short scales are deliberate; 1–10 scales exhibit poor inter-rater agreement.

1. **Functional equivalence** — accomplishes what the reference accomplished; explicitly not "is the same code"
2. **Completeness** — every part of the request addressed, no stubs or TODOs left behind
3. **Cross-file consistency** — callers updated, signatures aligned, no dangling references
4. **Scope discipline** — no unrelated edits, gratuitous refactors, or dead code
5. **Convention adherence** — matches surrounding idiom, naming, and error handling

Plus unscored binary flags: `introduced_stub`, `left_debug_artifacts`, `wrote_tests`.

**Output (b) — pairwise preference, round-robin across all four models.** Blind, position-randomized, ties permitted. Win rates convert to Elo, following GDPval.

**Sonnet 5 is an arm, not the answer key.** It is graded on the identical absolute rubric as every candidate and takes part in the pairwise round-robin like any other model. Sonnet 5 is fallible — its own output is evaluated, never assumed correct. It serves as the incumbent cost and behavior baseline, not as ground truth.

**The ground-truth anchor is the merged PR diff** (§3.2), which is human-authored, was reviewed, and shipped. That is what "reference-anchored" means throughout this spec.

**Pairing rule:** compare sample *i* of one model against sample *i* of another on the same task — both are random draws from their respective distributions, so the comparison is unbiased. Never pair a draw against another model's best, median, or a single canonical run; that compares a draw to a statistic and skews the win rate. Where one side failed the deterministic gate and the other did not, the pairwise is skipped and recorded as a gate-decided win — the objective result already settles it.

Round-robin over 4 models is 6 pairs per task-sample. At 60 tasks × 10 samples × 3 judge votes that is ~10,800 judge calls. If cost or wall-clock binds, subsample sample indices rather than dropping pairs — dropping pairs would break the Elo graph's connectivity, while subsampling only widens intervals.

Protocol: 3 judge samples per comparison, majority vote. Position-swap probe on a subset with position-consistency reported. Cohen's κ against the ~20% human gold subset (§4.3). If κ < 0.6, Tier B is reported as directional only and must not carry the decision.

### 4.3 Judge configuration

- **Neutral family, mandatory.** Compared families are Anthropic, Google, NVIDIA, Moonshot. A Claude judge inflates Sonnet 5; a Gemini judge inflates Gemma 4. The judge must sit outside all four. Self-preference bias is well documented: one measured case had a model rate its own backbone at 33.7% faithfulness where an independent judge said 14.13%.
- **Judge is a GPT-class model** (existing Pindrop credits). Sits outside all four compared families, so no self-preference path exists.
- **Accepted tradeoff, recorded:** a GPT-class judge sends candidate diffs to a third party, which is the same class of objection that ruled out OpenRouter in §2, though at judging volume rather than for all production traffic. Decision reviewed and accepted. Qwen on Bedrock or self-hosted remains the drop-in alternative if residency policy changes — the judge is swappable without touching anything else, and because full judge inputs are logged (§6.3), a re-judge is a re-score, not a re-run.
- **Evidence-fed, not diff-only.** Give the judge the candidate diff, the reference diff, the rubric items, and any test results. Judge-with-evidence materially outperforms judge-blind and is the difference between a usable and an unusable Tier B number.
- **Blind and position-randomized.** Model identity stripped; A/B order shuffled per comparison; position-swap probe on a subset to bound position bias.
- **Calibrated.** Engineers grade a ~20% gold subset blind. Report Cohen's κ against it. κ ≥ 0.6 acceptable, ≥ 0.8 strong. **If κ falls below 0.6, the Tier B number is reported as directional only and must not carry the decision.**
- **Fully logged.** Complete judge reasoning, not just verdicts, plus judge model ID and prompt version, so judge drift is traceable and re-scoring is possible.

### 4.4 Statistical power — why N=10, not N=3

Naive binomial 95% CI half-width at p≈0.5, **assuming independent attempts**:

| Comparisons | Naive CI half-width |
|---|---|
| 75 (25 tasks × 3) | ±11.3 pts |
| 250 (25 × 10) | ±6.2 pts |
| 600 (60 × 10) | ±4.0 pts |

**These figures are optimistic and must not be quoted.** Attempts within a task are correlated, so effective sample size tracks the task count rather than the attempt count. Clustering by task, with the bimodal per-task rates typical of binary coding outcomes, the honest unpaired half-width is nearer **±12 points at 60 tasks** — roughly the naive 75-attempt figure, not the 600-attempt one.

Paired analysis (§4.2.2) is what recovers the power: comparing per-task differences between any two models cancels common task difficulty and brings the half-width to approximately **±6 points at 60 tasks**. All reported intervals use cluster bootstrap over tasks on paired differences, for every model pair.

Two planning consequences:

- The plan doc's "task completion ≥90% of Sonnet 5" cannot be resolved unpaired at any realistic scale. Paired, at 60 tasks, a 10-point gap is detectable.
- **Task count drives precision; N drives reliability measurement.** N=10 exists for pass^k and the budget curve, not to sharpen the mean. If CI width becomes binding, harvest more tasks — raising N will not help.

Judge noise compounds this. With judge accuracy `a`, observed win rate is attenuated toward 50%:

```
observed_p − 0.5 = (2a − 1) × (true_p − 0.5)
```

Required sample size scales as `1 / (2a − 1)²`:

| Judge accuracy | Effect shrinks to | N multiplier |
|---|---|---|
| 0.66 (GDPval auto-grader vs 71% human ceiling) | 32% | ~9.8× |
| 0.80 | 60% | ~2.8× |
| 0.90 | 80% | ~1.6× |
| ~1.0 (executable) | 100% | 1× |

At a = 0.66 a true 75% win rate reads as 58% — an 8-point signal inside an ±11-point band. This is why reference-anchoring and evidence-feeding the judge (§4.3) are load-bearing, not polish: they move `a` from ~0.66 toward ~0.85 and cut the sample-size penalty from ~10× to ~2×.

Compute is not the constraint. 4 models × 60 tasks × 10 samples = 2,400 runs; at ~200K tokens per run the three candidates total well under $200 at $0.14–0.60 per 1M input. **Test-authoring hours are the constraint.**

### 4.5 Optional: executable Tier B

Writing acceptance tests per Tier B task (~1–3 hr/task) converts the primary decision signal from κ≈0.32 to deterministic. Highest-leverage spend available if the schedule allows. Decision deferred; judge-based Tier B is the committed baseline.

If pursued, the judge is not removed — it is demoted to scoring attributes that resist encoding as tests (cross-file consistency, house style, dead code left behind), reported separately, never overriding the executable result.

### 4.6 Metric definitions

Full scorecard in §10.1. Three definitions that are easy to get wrong:

- **pass@1 and pass^k are reported side by side, never separately.** pass@1 = 0.6 could mean 60% on every task or 100% on 60% of tasks — opposite production experiences. The gap between them *is* the reliability number.
- **Cost per completed workflow** = total spend across **all** attempts including failures ÷ tasks resolved. Not successful runs only. A cheap model burning three failures before landing one is not cheap. This is the mechanism behind AA's finding that Sonnet 5 costs ~15% more per completed task than Opus 4.8 despite a lower per-token price.
- **Pass-rate-at-budget-K** is derived from checkpoints (§5.5), not from separate capped runs. Its cost-axis twin comes from the per-turn token records (§6.1).

---

## 5. Harness and Execution

### 5.1 Start-state pinning

Every run begins from a byte-identical world:

- Container pinned **by digest**, not tag
- `git checkout --detach <base_sha>` then `git clean -xfd`
- Dependencies from lockfile, pre-installed into the image
- **Network off** during the run, or through a recording proxy — a flaky registry must not become a model difference
- Fresh container per sample; no reuse across the N=10. State bleed would correlate repeats and silently shrink effective N
- Frozen clock where tests are date-sensitive

### 5.2 Claude Code configuration control

The highest-risk contamination source. `CLAUDE.md`, `settings.json`, hooks, MCP servers, skills, and output styles all substantially change agent behavior, and the operator's personal environment is heavily customized (hooks, ~15 MCP servers, many skills).

Required: `--strict-mcp-config` with an empty server list, no user-level settings, a fixed project `CLAUDE.md` (or none), hooks disabled. Identical for every model. **Verify by dumping and diffing the effective config at session start of every run**, and store that dump in the run record.

### 5.3 Sampling parameters

Frozen and identical across models, following AA convention: temperature 0 for non-reasoning models, 0.6 (or lab-recommended) for reasoning models. Recorded per run.

### 5.4 Budget policy — generous caps, measure actuals

Caps set high enough that little truncates; actual consumption is a headline metric rather than a constraint.

**Limits derive from the slowest-converging model in the calibration pilot (§3.5), not from the incumbent.** Roughly that model's p95 × 2, applied identically to all four arms. Tuning caps to Sonnet 5's turn distribution would silently penalize any model that takes more turns to reach the same answer — converting a style difference into a capability score. Terminal-Bench 2.1 had to re-tune exactly this: one of its three bug categories was "resource budgets too tight for valid solutions to finish."

Cap on **turns and tokens together**, generous wall-clock as backstop only. Each unit is imperfect and favors someone:

| Cap on | Who it favors |
|---|---|
| Wall-clock | fast models (Nemotron 148.9 tok/s vs Gemma 35.3 tok/s ≈ 4× the turns) |
| Turns | verbose models (equal actions, unequal tokens and cost) |
| Tokens | terse models (Kimi's 16K output ceiling forces more, smaller turns) |

**Record which limit terminated each run.** Budget-truncation rate is a finding, not a failure to hide — particularly for Kimi given the 16K output ceiling.

### 5.5 Checkpointing — the cost/quality curve for free

Snapshot repo state each turn. One generous-cap run then yields the pass rate at **any** budget K, computed post-hoc. Strictly dominates running separate capped and uncapped configurations: same information, half the compute, full curve instead of two points.

Store checkpoints as **incremental diffs against `base_sha`**, never worktree copies. 2,400 runs × ~50 turns of full trees is unmanageable; as diffs it is a few GB.

**Checkpoint grading is offline and decoupled from the run.** The agent run only writes diffs; a separate batch job grades them afterwards. Naive inline grading is not feasible — 2,400 runs × ~40 turns ≈ 96,000 evaluations, which at a 5-minute full suite would be ~8,000 compute-hours.

Three controls make it tractable:

1. **Checkpoints run the F2P + P2P subset only**, never the full suite. Full suite runs once, at final state.
2. **Checkpoint interval is tunable** (`every_k_turns`), set from the pilot. Default every turn; raise if per-task suite runtime makes it binding.
3. **Grading is a separate batch job** over stored diffs, so it never blocks or slows the agent runs and can be re-executed if the oracle changes.

Measure per-task F2P+P2P subset runtime during Phase 2 and set `every_k_turns` from it. This is a hard feasibility input, not a tuning nicety.

### 5.6 End-state extraction

Never trust the agent's self-report — agents claim false success, at different rates per model, which would itself become a fake signal.

Mechanically, in the container after termination:

```bash
git add -A && git diff --cached <base_sha>
```

Staging first captures untracked files and normalizes over whether the agent committed, left the tree dirty, or created new files. That diff is the submission; tests, judge, and reference comparison all run on it.

### 5.7 Execution order — randomize and interleave

Running all Sonnet 5, then all Gemma, then all Kimi as sequential batches confounds model identity with every time-varying factor: Bedrock regional load, network conditions, host contention. Latency and throughput comparisons become uninterpretable, and those feed a stated success criterion.

Required: randomize run order across models and tasks, interleave rather than batch, record `started_at`/`finished_at` on every run, and treat execution timestamp as a covariate during analysis. If a latency difference survives as a model effect after controlling for time-of-day, it is real.

### 5.8 Prompt-cache warming — asymmetric cost confound

Ten samples of the same task executed back-to-back will warm Bedrock's prompt cache: sample 1 pays `cache_creation`, samples 2–10 pay `cache_read` at 0.1× list.

This is **asymmetric**. Sonnet 5 has prompt caching; the candidates' Bedrock cache support is unconfirmed (§8). Batched repeats would therefore make Sonnet 5 look artificially cheap, biasing cost-per-completed-workflow *against* the candidates the eval exists to evaluate.

Controls:

- Interleave so repeats of the same task never run back-to-back (falls out of §5.7 randomization, but must be verified, not assumed)
- Log `cache_state.warm` and the prior same-task run ID on every run
- Report cost **both raw and cache-normalized**, and state which the recommendation uses
- Resolve per-candidate Bedrock cache support in Phase 0b before the full run, since the answer changes how these numbers must be read

---

## 6. Logging Architecture

**The event log is the product.** Scores are derived views over it and are never the source of truth. Append-only, schema-versioned, immutable once written. Rationale: re-running is expensive and the models will have changed; re-scoring a preserved log is free. Anything not captured at run time is lost permanently.

### 6.1 Per-run record

```
run_id, task_id, task_version, model, harness, sample_index, schema_version
parent_run_id                     # set when this run is a retry of a failed attempt
attempt_number
started_at, finished_at           # UTC; execution order is an analysis covariate (§5.7)

versions: {
  claude_code, litellm, bedrock_model_id, container_image_digest,
  harness_commit, task_set_commit
}
config_digest                     # effective Claude Code config, verified identical
system_prompt_sha, tool_schema_sha  # resolved payloads stored once per (model, harness, version)
sampling: { temperature, max_output_tokens }

outcome: resolved | failed | budget_exhausted | crashed
exclusion: null | { class, reason_code, pre_registered: bool }

turns_used, terminated_by: turns | tokens | wall_clock | agent_finish | crash

time:
  wall_clock_total_ms
  inference_ms                    # model generating — the real speed difference
  tool_exec_ms                    # test runs, builds; model-independent per call,
                                  #   but call count is the model's choice, so it counts
  retry_backoff_ms                # infra — excluded from scored time, still logged
  time_to_first_edit_ms

tokens: { input, output, reasoning, cache_read, cache_write }, cost_usd
cache_state: { warm: bool, prior_same_task_run_id }   # §5.8 confound tracking

per_turn: [ {
    turn, tokens: { input, output, reasoning, cache_read, cache_write }, cost_usd,
    inference_ms, tool_exec_ms, stop_reason, bedrock_request_id
} ]                               # required for the cost-at-budget-K curve

checkpoints: [ {
    turn, diff_vs_base, files_touched, elapsed_ms,
    tests: { pass: bool, per_test: [ { name, status, duration_ms, stdout_ref } ] }
} ]

tool_calls: { total, malformed, errored, by_name: {...} }
truncation_events: [ { turn, kind } ]

failure_class: null | tool_malformation | truncation | loop_repetition
             | wrong_but_confident | gave_up | false_success | p2p_regression

destructive_events: [ {
    turn, command, paths_touched,
    category: test_deletion | force_push | mass_delete | dep_downgrade | secret_exposure,
    reverted_by_agent: bool, affected_outcome: bool, severity
} ]

diff_stats: { files, added, removed }
p2p_regressions: [...]
host: { cpu_pct_p95, mem_peak_mb, contention_flag }   # guards against attributing
                                                      #   host load to model slowness

artifacts: {
  trajectory_jsonl_gz,            # Claude Code's own session log
  wire_log_gz,                    # §6.2 — raw Bedrock request/response pairs
  container_stdout, container_stderr,
  test_output_gz, final_diff
}
```

**Reasoning/thinking blocks are retained verbatim** in the wire log where the model emits them. They are priced separately and are the primary diagnostic for why a model went wrong.

### 6.2 Wire-level logging — mandatory

LiteLLM is configured to persist the **full request and response payload** for every call: assembled system prompt, tool schemas, message history, sampling params, and the raw completion before Claude Code parses it, plus `stop_reason` and Bedrock request ID.

Without this, `malformed: true` is a dead end. You cannot later determine what was malformed, whether a stricter or looser parser would have recovered it, or whether the fault was LiteLLM's tool translation rather than the model. That distinction is the difference between "fix the adapter and retest" and "drop the model" — the single most consequential call the eval has to make (§6.4, adapter-failure class).

Wire logs are secret-scanned on write, since prompts contain repository source.

### 6.3 Judge and human-grader records

Stored separately, linked by `run_id`.

```
judge_record:
  run_id, judge_model_id, judge_prompt_version, judged_at
  input_payload                   # exact payload given to the judge — required for
                                  #   any re-score to be comparable
  position_assignment             # which candidate was A vs B
  verdict, rubric_item_scores, full_reasoning_text

human_grader_record:
  run_id, grader_id, graded_at, time_spent_s
  shown_payload                   # exactly what the grader saw
  verdict, rubric_item_scores, free_text_notes
```

Human records are what make the Cohen's κ calibration auditable. Without `shown_payload` and `time_spent_s`, a disputed κ cannot be re-examined.

### 6.4 Exclusion policy

Exclusion is the one mechanism by which results get massaged, even honestly. Controls:

1. Exclusion criteria **written down before the run begins**
2. Every excluded run **keeps its full record** plus a reason code — nothing is deleted
3. **Exclusion rate reported per model.** Asymmetry is itself a finding; if one model draws 5× the exclusions, that is signal, not noise

| Class | Counts against model? | Handling | Examples |
|---|---|---|---|
| Model failure | Yes | scored | wrong patch, gave up, looped, false success |
| Infra failure | No | retry, log both attempts | Bedrock 5xx, throttle, container OOM, network |
| **Adapter failure** | **Reported both ways** | never collapsed into one number | LiteLLM tool-translation bug, malformed tool call |
| Task defect | No | drop task, note in writeup | flaky test, bad base_sha, underspecified |

Adapter failures are genuinely ambiguous: they are fixable going forward *and* they are the production path. Headline results are published **with and without** them, always both.

### 6.5 Storage

Gzipped trajectory JSONL, gzipped wire logs, incremental checkpoint diffs, container stdout/stderr, test output, full judge and human-grader records. Wire logs are the largest component — expect low tens of GB total for 2,400 runs rather than a few. Cheap insurance relative to re-running.

Private repo or bucket with a documented access list, per §3.6. Secret-scanned on write, since wire logs and trajectories both contain repository source.

### 6.6 Logger verification — gate before the real run

Fault-injection pass. Confirm each of these produces a complete, correctly-classified record with nothing lost:

- [ ] Container killed mid-run
- [ ] Bedrock throttle / 5xx during generation
- [ ] Deliberately malformed tool call
- [ ] Token budget exhausted mid-edit
- [ ] Turn budget exhausted
- [ ] Agent issues a destructive command
- [ ] Test harness itself crashes
- [ ] Disk full during checkpoint write
- [ ] Wire log captures a malformed completion in full, pre-parse
- [ ] Per-turn token and cost records reconstruct the run-level totals exactly
- [ ] Version block populated and correct for every component
- [ ] Interrupted run leaves a partial-but-valid record, not a corrupt one

The logger is trusted with 2,400 runs. Test it before, not after.

---

## 7. Phases

Reordered so data capture precedes scoring. Scoring reads the log; it never gates collection.

**Phase 0 — Access and privacy (~1–2 days)**
Bedrock model access for all three (allow ~15 min propagation); confirm region and `bedrock-mantle` endpoint for Gemma; Service Quota increases (~1 business day lead time); LiteLLM proxy standing up against Bedrock. Engineer consent and secret-scanning gate for team transcripts (§3.6). Re-check SWE-rebench and AA leaderboards for newly published independent scores.

**Phase 0b — Cost baseline correction (~0.5 day)**
Re-baseline Sonnet 5 against the **actual AWS bill**, not list-price math. See §8. Confirm per-candidate Bedrock prompt-cache support (§5.8). Pricing tier is settled: **standard**.

**Phase 0c — End-to-end smoke test (~0.5 day) — go/no-go**
One trivial task, one run, each of the four models, through Claude Code + LiteLLM end to end. Verify the agent completes a loop, tool calls translate, a diff lands, and the logger produces a valid record.

This is a **go/no-go gate**. If a candidate cannot drive Claude Code at all through LiteLLM, that must surface on day one, not in week three after the dataset is built. A failure here is not necessarily disqualifying — it may be an adapter issue (§6.4) — but it changes the plan immediately, either by scheduling adapter work or by dropping the candidate.

Also sizes the compute plan: measure wall-clock and token consumption for one run, then extrapolate to 2,400 and set parallelism against the Bedrock quotas obtained in Phase 0.

**Phase 1 — Instrumented harness (~4–5 days)**
Build the run harness, event log, wire-level capture, checkpointing, and exclusion classification. Complete the §6.6 fault-injection gate. No scoring work begins until capture is verified.

**Phase 2 — Dataset construction (~4–5 days)**
Transcript stats pass for distribution weighting. Harvest ~80 candidate tasks across the three classes. Build containers, pin SHAs, wire F2P/P2P sets. Seed rubric items from human correction turns.

**Phase 3 — Calibration pilot and freeze (~2–3 days)**
All four models at N=3 across all ~80 (~960 runs). Drop only tasks where all four hit the floor or all four hit the ceiling. Set budget caps from the slowest-converging model's p95. Freeze the set at ~60.

**Phase 4 — Full run (~3–4 days, mostly unattended)**
All four models × ~60 tasks × N=10.

**Phase 5 — Scoring and analysis (~3–4 days)**
Executable grading; judge run with human gold subset and κ calibration; failure taxonomy; destructive-behavior review; cost-per-completed-workflow; CI computation.

**Phase 6 — Report (~2 days)**
Publish the §10.1 scorecard for all four models, with and without adapter-failure numbers, exclusion rates per model, κ alongside every judge-derived figure, and the §9 limitations. Migration decision and canary planning are downstream of this eval, not part of it.

**Optional, gated on primary completing:** opencode harness re-run for harness-fit disambiguation.

Total ~3.5–4.5 weeks, longer than the plan doc's 2.5–3 weeks. The delta is the instrumentation phase and N=10 instead of N=3, both of which are what make the result defensible.

---

## 8. Open Item Carried From the Plan Doc — Cost Baseline

The plan's $5,000/month Sonnet 5 baseline is `665M × $3.00 + 200M × $15.00` — flat list price, zero caching assumed.

Real usage is heavily cached. A representative assistant record from the operator's own transcripts:

```
input_tokens: 2 | cache_read: 26,777 | cache_creation: 13,898 | output: 215
```

~99.99% of input was cache reads, billed at 0.1× list. Actual Sonnet 5 spend is therefore likely well below $5,000/month.

Meanwhile, **prompt caching support on Bedrock for Gemma 4, Nemotron 3 Super, and Kimi K2.5 is unconfirmed.** If candidates lack caching while the baseline has it, the true savings is materially smaller than the stated 80–96%, and possibly small enough to change the recommendation.

**Pricing tier: standard** ($3.00 / $15.00), confirmed. Not introductory. All cost figures in this spec use standard tier.

Action, Phase 0b: confirm per-candidate Bedrock cache support, and re-baseline against actual AWS billing data rather than list-price arithmetic. **This is a blocking input to the recommendation, not a footnote.**

---

## 9. Stated Limitations

To appear in the final writeup, not buried:

- **Autonomous single-instruction setting is harder than real usage**, where engineers nudge the agent back on track. Results are a lower bound on practical performance, and the gap may differ by model.
- **Task set is Pindrop-specific.** High external validity for Pindrop, none for anyone else. That is the intent.
- **Tier B judgment is low-resolution by nature.** GDPval's human experts agree with each other only 71% (κ ≈ 0.42) on subjective deliverable quality. No grading scheme resolves finer than its instrument.
- **Harness fit is confounded with model capability** in the primary run. The opencode secondary run exists to disambiguate; without it, a Claude Code result is a statement about model × Claude Code, not about the model.
- **Transcript-derived tasks skew toward work that was already agent-suitable** — the team chose to hand those to an agent. Work never given to an agent is underrepresented.

---

## 10. Reported Scores

**The deliverable of this eval is a benchmark scorecard, not a verdict.** Scores are produced for all four models, including Sonnet 5, on the same footing. Whether any score clears a bar is a separate decision made later with this data in hand — no result is treated as a failure of the eval, and no threshold gates the reporting.

**No composite score.** Collapsing these into one number hides the tradeoff being made and renders the result unfalsifiable. A scorecard forces the tradeoff into the open where it can be argued with.

### 10.1 Scorecard

Every row reported per model, with a confidence interval, for all four arms:

| Metric | Measured as |
|---|---|
| Tier A pass@1 | binary resolved, N=10, cluster bootstrap over tasks |
| Tier A pass^k | all N samples resolved, per task |
| Sub-goal rate | fraction of sub-goals satisfied, reported beside resolve rate, never blended (§4.2.2) |
| Pass rate at budget K | checkpoint curve (§5.5), reported across K |
| Tier B Elo + pairwise win rates | round-robin blind judge (§4.2.3), κ reported |
| Tier B rubric profile | five dimensions, 3-point scale, per model |
| Cost per completed workflow | total spend ÷ resolved, cache-normalized (§5.8) |
| Cost at budget K | per-turn token records (§6.1) |
| Wall-clock p50 / p95 | decomposed timing (§6.1) |
| Tool-call malformation rate | per-run tool-call log |
| Failure taxonomy | distribution over `failure_class` |
| Destructive events | count and severity, with narrative |
| Exclusion rate | per model, per exclusion class (§6.4) |

Paired per-task differences are reported for every model pair, not just against Sonnet 5.

### 10.2 Reference points, not gates

These values are recorded so results can be read against an expectation. They are **annotations on the scorecard, not pass/fail criteria**, and nothing is disqualified for missing one.

They are expressed relative to Sonnet 5 because the business question is "can we replace the incumbent," not "which model is best in the abstract." That is a **commercial reference frame, not a quality standard** — Sonnet 5's own scores are measured here on the same footing as everyone else's, and it may lose rows. Scoring is model-neutral throughout (§3.5, §5.4); only this table is incumbent-relative.

| Reference | Value | Origin |
|---|---|---|
| Tier A capability | within −10 pts of Sonnet 5 | default, unvalidated |
| Reliability | pass^k gap ≤ Sonnet 5's + 15 pts | default, unvalidated |
| Tier B quality | ≥40% pairwise win rate vs Sonnet 5 | default, unvalidated |
| Cost | ≤40% of Sonnet 5 per completed workflow | from plan doc |
| Latency | p95 ≤ 2× Sonnet 5 | default, unvalidated |
| Tool-call integrity | malformation ≤5% | default, unvalidated |

Four of six are defaults with nothing behind them. If they are ever promoted from reference points to decision criteria, that must happen **before** results exist (§11, OPEN-8).

### 10.3 Reporting requirements

- Full scorecard for all four models, Sonnet 5 included and scored on the same rubric
- Headline figures published **both with and without adapter failures** (§6.4)
- Exclusion rate per model — asymmetry between models is itself a finding
- Any metric reported without a confidence interval is not reported
- Tier B κ reported alongside every judge-derived number; if κ < 0.6, those numbers are labeled directional
- Destructive events reported with narrative and impact, never as a bare count

---

## 11. Open Decisions

Everything an implementer must have answered before or during the phase named. Ordered by when it blocks.

### Resolved

| ID | Decision | Resolution |
|---|---|---|
| **OPEN-1** | Judge model | **GPT-class**, using existing Pindrop credits. Outside all four compared families. Third-party data-egress tradeoff reviewed and accepted (§4.3). Qwen on Bedrock or self-hosted is the drop-in alternative if residency policy changes; because judge inputs are logged (§6.3), switching is a re-score, not a re-run. |
| **OPEN-4** | Sonnet 5 pricing tier | **Standard** ($3.00 / $15.00). Not introductory. |
| **OPEN-8** | Threshold values | **Demoted to reference points, not gates** (§10.2). This eval produces scores; whether they clear a bar is a later decision. Thresholds must be promoted to criteria only before results exist, never after. |
| — | Checkpoint feasibility | Grading decoupled to an offline batch job over stored diffs, F2P+P2P subset only, tunable interval (§5.5). |
| — | Smoke test | Added as Phase 0c, **mandatory go/no-go before the full eval starts** (§7). |
| — | Sonnet 5's role | **Not ground truth.** Scored on the same rubric as every candidate and included in the pairwise round-robin. Ground-truth anchor is the merged PR diff (§4.2.3). |

### Open

| ID | Decision | Blocks | Owner | Notes |
|---|---|---|---|---|
| **OPEN-2** | **Repo and language scope** | Phase 2 | Suhaas | Which Pindrop repos, mono or multi, which languages. Determines container build effort and whether type-check (check 4) and lint (check 7) apply at all. §4.2.1 assumes `mypy`/`tsc` as placeholders until answered. Deferred to post-handoff. |
| **OPEN-3** | **Compute environment and parallelism** | Phase 4 | Suhaas + infra | Where 2,400 containerized agent runs execute, at what concurrency, against which Bedrock quotas. Sized from the Phase 0c smoke test. **Must be resolved before the full run**, not after handoff. |
| **OPEN-5** | **Human grading capacity** | Phase 2 | Eng manager | ~20% of Tier B comparisons need blind human grading for κ calibration (§4.3). Who, how many, hours allocated. Without it κ is unmeasurable and every judge-derived number is labeled directional by default. |
| **OPEN-6** | **Test-suite runtime → `every_k_turns`** | Phase 2 | implementer | Measure per-task F2P+P2P subset runtime; set checkpoint interval from it (§5.5). Hard feasibility input, not tuning. |
| **OPEN-7** | **Executable Tier B: yes or no** | **before Phase 4** | Suhaas | ~1–3 hr/task to author acceptance tests; converts Tier B from κ≈0.32 to deterministic (§4.5). Investigate during Phase 2–3 and decide before real runs begin. |
| **OPEN-9** | **Data retention lifetime** | Phase 0 | Suhaas + security | §3.6 specifies "defined lifetime" with no value. Wire logs and trajectories contain repository source. |
| **OPEN-10** | **Destructive-event severity scale** | Phase 1 | implementer | §6.1 uses `severity` without defining levels. Proposed: high = unreverted data/history loss or secret exposure; medium = reverted or contained; low = risky but no effect. |
| **OPEN-11** | **opencode secondary run: scope and trigger** | after Phase 5 | Suhaas | Currently "nice-to-have" with no acceptance criteria. Suggested trigger: run it only when a candidate scores poorly on Tier A **and** shows tool-call malformation above ~5%, i.e. only when harness-fit is a live explanation for the score. |
| **OPEN-12** | **Multi-PR Tier B tasks** | Phase 2 | implementer | Some feature-scale work spans several PRs, so no single `reference_diff` exists. Decide: concatenate the PR series, pick the primary PR, or exclude the task. |
| **OPEN-13** | **Pairwise sample pairing** | Phase 5 | Suhaas | Sample *i* vs sample *i* is specified (§4.2.3). Confirm, or choose an alternative pairing, once real per-model sample variance is visible. |

## Sources

- [Artificial Analysis — Intelligence Benchmarking Methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking)
- [OpenAI — Introducing SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)
- [OpenAI — Why we no longer evaluate SWE-bench Verified](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)
- [Terminal-Bench 2.1 announcement](https://www.tbench.ai/news/terminal-bench-2-1)
- [GDPval: Evaluating AI Model Performance on Real-World Economically Valuable Tasks](https://arxiv.org/pdf/2510.04374)
- [Reliability without Validity: Large-Scale Evaluation of LLM-as-a-Judge Models](https://arxiv.org/pdf/2606.19544)
- [LiteLLM — Bedrock provider docs](https://docs.litellm.ai/docs/providers/bedrock)
