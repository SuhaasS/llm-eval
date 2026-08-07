# Pending Tasks

Single list of open work for the LLM bakeoff eval. **This file is the backlog.**
`tasks/todo.md` is the opposite — a completed-work review log, one section per
finished task. Nothing here is done; move it there when it is.

Last updated 2026-08-07, after the second Phase 0c live smoke run.

Spec: [docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md](docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md)
Harness plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

---

## P0 — Blocking Phase 0c go/no-go

The second 2026-08-07 live run: **2 of 4 arms passed.** Sonnet 5 is fixed and
is no longer the blocker — it now completes the task over bedrock-runtime
(7 turns, 4 tool calls, correct one-line diff, $0.388, 13.6s), alongside
Nemotron (19 turns, 9 tool calls, $0.057). See `tasks/todo.md` for how.

Gemma and Kimi remain. Both are adapter-class, not model capability.

- [ ] **Gemma 4 31B — `JSON-RPC error -32602: Job registration failed ...
  Generation failed`.** Bedrock-side, not a parameter rejection. **Observed
  3/3**, so deterministic is now the better bet than intermittent.
  The 2026-08-07 second run also showed the same error arriving *mid-stream*,
  wrapped as `MidStreamFallbackError` — one fault with two surface forms
  depending on whether it lands before or during the stream. Do not count
  those as two bugs.
  Next: this arm has produced no tool call in any run. Decide whether it is
  fixable at the adapter layer at all, or whether Gemma leaves the eval — that
  decision blocks the dataset sizing, so make it before Phase 3.

- [ ] **Kimi K2.5 — two distinct failure modes in two runs, zero diffs.**
  Run 1: died mid-stream (`MidStreamFallbackError`) after 4 turns and 2 real
  tool calls. Run 2: both calls returned 200 and the agent exited
  `subtype: success, is_error: false` after announcing *"Let me fix it:"* and
  never calling the edit tool.
  These are *rates*, not binaries, and now plausibly two independent ones.
  A rate is invisible at N=1. At scale it forks badly: scored as model failure
  it penalizes Kimi for adapter reasons; excluded, its effective N shrinks and
  the surviving sample skews toward short runs.
  Next: 3–5 runs to measure both rates separately. Run 2's mode is the more
  dangerous of the two — it is indistinguishable from a model that simply did
  not do the work, and nothing in the record flags it.

- [ ] **Record the Sonnet transport asymmetry as a §6.4 confound.** Sonnet 5
  now signs SigV4 against bedrock-runtime while all three candidates go through
  the mantle passthrough. Different code path, different request shape: on
  runtime, beta features ride as an `additionalModelRequestFields.anthropic_beta`
  *body* field rather than an `anthropic-beta` header. The reference arm is
  therefore not transport-identical to the arms it is the reference for, and
  any Sonnet-vs-candidate delta carries that. It needs to be stated wherever
  the comparison is published, not just known here.

- [ ] **Re-run the four-arm smoke to a real GO**, then raise the Phase 0c exit
  criterion to **N=3 per arm, not N=1.** One run per arm structurally cannot
  see a failure rate — and Kimi has now demonstrated that exact point by
  failing differently the second time.

---

## P1 — Landmines that only fire at scale

None of these can appear at N=1. All will appear at N=10.

- [ ] **The cache-token cost guard silently zeroes a record.** `costs.py` raises
  for all three candidates on any `cache_read`/`cache_write`, since their
  Bedrock prompt-cache support is unconfirmed. `assemble_record` catches it, so
  the record survives — with turns, tokens and cost all **zero** and only
  `trajectory_parse_error` set. Today's runs were cold so it never fired; at
  N=10 per task the cache warms (§5.8) and the first cached run becomes a row
  of zeroes that looks like a quiet run.
  **Highest severity open item: it destroys data without failing.**
  Next: measure whether candidates return cache tokens, then either price them
  or keep the guard and make it a loud failure rather than a silent zeroing.

- [ ] **Both credentials expire mid-run, and now every arm is exposed.** The
  mantle bearer token is minted in memory by
  `smoke_bedrock.derive_mantle_token`; the SigV4 keys are frozen out of the SSO
  session by `smoke_test.freeze_sigv4_credentials`. Both inherit the SSO
  session's expiry — hours, not days. Phase 4 is 3–4 days mostly unattended, so
  the mantle arms start returning 401 and the runtime arms start failing
  signature validation partway through. No refresh path exists for either.
  Widened 2026-08-07: before Sonnet moved to bedrock-runtime this touched the
  candidates only, and the reference arm would have survived it.

- [ ] **Throttling makes the exclusion rate load-dependent.** No 429s at N=1. At
  scale they are routine, and the exclusion rate then correlates with when and
  how parallel the run was rather than with the model. §5.7's randomize-and-
  interleave is the mitigation and the spec says to verify it, not assume it.

- [ ] **Confirm Bedrock service quotas** for the concurrency the plan needs
  (OPEN-3). Sizing input from the live run: 311s and 16 turns for a *one-line*
  fix, ~19k input tokens per call growing to 311k cumulative. Wall clock, not
  cost, is the binding constraint — 2,400 × 311s ≈ 207 hours serial, ~21h at
  10-way parallelism, and real tasks will be well above that.

- [ ] **`RunRecord.sampling` reflects the bridge, not just the config.** On the
  Responses path all three candidates reported `temperature=1.0`; switching to
  chat-completions made it read `absent`; it returned on nemotron's successful
  run. Pin which bridge each arm uses before the full run, or sampling
  provenance is unreliable across arms.

---

## P2 — Capture gaps found in passing

- [ ] **The wire log records a failure's status code but not its error body.**
  `BakeoffCallback._record` stores `response_obj`, which is `None` on failure,
  so a 400 lands as `{"raw_completion": "None"}`. Every diagnosis on
  2026-08-07 had to come from the proxy's own logs instead — and in the full
  run the proxy log is not an artifact of the record. §6.2 exists so a failure
  can be diagnosed after the fact; right now it cannot be.
  Confirmed again on the second run: the failing mantle Sonnet arm recorded
  exactly `{"status_code": 400, "raw_completion": "None"}`, and `invalid beta
  flag` was recoverable only by grepping `proxy.log`. Two runs, two diagnoses
  that the event log could not support on its own.

- [ ] **Every dataset task repo needs a `.gitignore`.** §5.6 stages everything
  (`git add -A`), so the first live run's diff led with a binary
  `__pycache__/calc.cpython-312.pyc`. Without this, diff size and file counts
  measure the interpreter rather than the agent. Done for the smoke fixture
  only.

- [ ] **`ToolCallStats.malformed` is never populated at harness time**, so
  `TOOL_MALFORMATION` and `ADAPTER_FAILURE` are both unreachable — and §6.4
  calls the adapter-vs-model distinction the eval's most consequential call.
  Deferred to the wire-log analysis in the scoring plan; nothing can fire it
  until that lands.

- [ ] **§5.2's config dump is an artifact, not a record field.** Written to
  `artifacts_root/<arm>/effective_config.json` and diffed across arms by the
  smoke script. The spec asks for it stored *in the run record*, which needs an
  `Artifacts.effective_config_json` field and a schema bump to 1.2.0.

---

## P3 — Decisions to settle before numbers are published

- [ ] **Sonnet 5 pricing.** `PRICE_BOOK` carries standard $3/$15 while Anthropic
  lists introductory $2/$10 through 2026-08-31. Whether Bedrock mirrors the
  introductory rate is unverified. Check the AWS pricing page before any cost
  figure leaves the team.

- [ ] **Sonnet's tokenizer is ~30% denser.** A uniform token cap gives Sonnet
  ~30% less *text* budget than the other arms — a config choice that would be
  scored as a capability difference (§5.4) — and cost-per-task is not directly
  comparable at equal text. Caps derive from the Phase 3 calibration pilot;
  decide the policy there.

- [ ] **`task_set_commit` stays `""`** by design until the dataset plan exists.
  Asserted empty on purpose so nobody reads it as populated.

- [ ] **`Checkpoint.tests_pass` stays `None`** by design — §5.5 requires offline
  grading. Lands with the scoring plan.

---

## Housekeeping

- [ ] **20 commits unpushed.** Pushed manually.

- [ ] **`bakeoff/.env` holds three commented-out static AWS credential lines
  with plaintext values**, annotated in-file as an expired STS session
  superseded by the SSO profile. Inert and gitignored, but a real secret-key /
  session-token pair sitting on disk with no expiry tracking. Delete the lines.

---

## Out of scope here — each needs its own plan

1. **Dataset construction** — transcript/PR/Jira join, task harvesting,
   container builds, stratification (§3)
2. **Scoring** — deterministic checks, offline checkpoint grading, judge
   protocol, κ calibration (§4)
3. **Analysis and reporting** — paired cluster bootstrap, pass@1/pass^k, Elo,
   scorecard (§10)
