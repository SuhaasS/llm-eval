# Pending Tasks

Single list of open work for the LLM bakeoff eval. **This file is the backlog.**
`tasks/todo.md` is the opposite — a completed-work review log, one section per
finished task. Nothing here is done; move it there when it is.

Last updated 2026-08-07, after the second Phase 0c live smoke run.

Spec: [docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md](docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md)
Harness plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

---

## P0 — Blocking Phase 0c go/no-go

The 2026-08-07 N=3 live run, 12 runs across 4 arms: **2 of 4 arms passed, 3/3
each.** Sonnet 5 (bedrock-runtime, 8 turns, correct diff every time) and
Nemotron (15–21 turns, correct diff every time). Gemma 0/3 and Kimi 0/3, both
failing identically across their three runs.

Both remaining failures are adapter-class, not model capability, and both are
now deterministic rather than the rates they first appeared to be — which is
itself the N=3 criterion earning its keep.

- [ ] **Gemma 4 31B — `JSON-RPC error -32602: Job registration failed ...
  Generation failed`.** Bedrock-side, not a parameter rejection. **Observed
  6/6**, identically: 1 turn, 0 tool calls, 2 wire calls, no diff. Deterministic.
  The error has two surface forms — a plain `BadRequestError` when it lands
  before the stream and a `MidStreamFallbackError` when it lands during it.
  One fault, not two. At N=3 every proxy-side error in the run attributed to
  this arm and to no other.
  Next: this arm has produced no tool call in any run, so nothing is known
  about whether Gemma can drive Claude Code at all. Decide whether it is
  fixable at the adapter layer, or whether Gemma leaves the eval — that
  decision sets the arm count, which sizes the dataset, so make it before
  Phase 3.

- [ ] **Kimi K2.5 — announces the fix and never applies it. 5/6 runs.**
  Both calls return 200, the agent emits *"...instead of addition. Let me fix
  it:"* and exits `subtype: success, is_error: false` at 3 turns. No edit tool
  call, no diff. Reproduced 3/3 at N=3 with near-identical token counts
  (208/206/199 output, $0.0408 each), so this is deterministic rather than the
  rate it first looked like.
  The single `MidStreamFallbackError` death recorded earlier did **not**
  reproduce in 3 further attempts; treat it as unexplained, not as a second
  mode, until it recurs.
  **This is the most dangerous open failure in the eval.** The run is
  well-formed, terminates successfully, and produces a record that is
  indistinguishable from a model that simply chose not to do the work. Every
  other failure here announces itself with a non-200. This one does not, and
  at scale it would be scored as capability.
  Next: read the wire log for the final turn — whether Kimi emitted a
  tool_call block that the mantle `openai/` bridge dropped, or emitted none at
  all, is the whole §6.4 adapter-vs-model question in one observation.

- [ ] **Record the Sonnet transport asymmetry as a §6.4 confound.** Sonnet 5
  now signs SigV4 against bedrock-runtime while all three candidates go through
  the mantle passthrough. Different code path, different request shape: on
  runtime, beta features ride as an `additionalModelRequestFields.anthropic_beta`
  *body* field rather than an `anthropic-beta` header. The reference arm is
  therefore not transport-identical to the arms it is the reference for, and
  any Sonnet-vs-candidate delta carries that. It needs to be stated wherever
  the comparison is published, not just known here.

- [ ] **Re-run the four-arm smoke to a real GO.** The N=3 criterion is now
  implemented and enforced (`smoke_test.py --repeats`, default 3 live, GO
  requires every repeat of every arm; a run below N=3 prints an explicit
  weaker-than-criterion warning). The 2026-08-07 N=3 run was **NO-GO at
  2 of 4 arms**: Sonnet 5 3/3, Nemotron 3/3, Gemma 0/3, Kimi 0/3.
  Blocked on the two arms above, not on the gate.

---

## P1 — Landmines that only fire at scale

None of these can appear at N=1. All will appear at N=10.

- [ ] **Sonnet's cost is order-dependent: 2.9× spread on byte-identical work.**
  Measured at N=3, three runs of the same one-line fix, same 8 turns, same
  157-byte diff:

  | run | cache_write | cache_read | cost |
  |---|---|---|---|
  | 1 | 125,995 | 209,781 | **$0.548** |
  | 2 | 34,243 | 301,773 | $0.231 |
  | 3 | 23,118 | 312,652 | $0.192 |

  The Bedrock prompt cache **persists across runs**, so run 1 pays cache_write
  at 1.25× and later runs read at 0.10×. Nothing about the model or the task
  changed. §5.7 randomizes and interleaves execution order, which means at
  N=10 per task each sample's cost depends on where the scheduler happened to
  put it — cost-per-task becomes partly an artifact of scheduling.
  It lands on one arm only: Sonnet is the sole arm with caching, so
  Sonnet-vs-candidate cost is confounded twice over (order, and the presence
  of caching at all).
  Next: decide the policy before any cost figure is published — report
  steady-state cost from warm runs, report cache-write separately, or pin
  execution order per task. All three are defensible; silently averaging is
  not.

- [ ] **The cache-token cost guard would zero a record without failing.**
  `costs.py` raises for all three candidates on any `cache_read`/`cache_write`,
  since their Bedrock prompt-cache support is unconfirmed. `assemble_record`
  catches it, so the record survives — with turns, tokens and cost all **zero**
  and only `trajectory_parse_error` set.
  **The N=10 premise recorded here before was wrong.** The cache warms on
  **turn 2 of a single run**, not at N=10: Sonnet call 0 writes 41,723 and
  call 1 reads 41,723 back. The guard has simply never had the chance to fire,
  because the three candidates return no cache fields at all on the
  OpenAI-compatible mantle route — `cached_tokens`, `cache_creation` and
  `cache_read` all absent across 9 candidate runs at N=3. `smoke_test.py` now
  reports this per arm each run, so it stays measured rather than assumed.
  Still worth fixing, for a better reason than the original one: a zeroed
  record reads as `turns=0, cost=0` with an empty `trajectory_parse_error` —
  **byte-identical to Gemma legitimately failing on its first call**, which
  this very run produced three times. The signature is already occupied, so
  the guard firing would be invisible.
  Next: make it a loud failure rather than a silent zeroing. Pricing the
  candidates' cache tokens is not needed unless one starts returning them.

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
  (OPEN-3). Sizing input from the live runs: a *one-line* fix takes 15–21
  turns on Nemotron and 8 on Sonnet, ~19k input tokens per call growing to
  311k cumulative. Wall clock, not cost, is the binding constraint.
  **Wall-clock variance is the real problem, and N=3 exposed it.** Nemotron ran
  the identical task in 201.9s, 42.6s and 54.2s — a 4.7× spread with no
  corresponding change in turns (19/15/21). A single-sample estimate is
  therefore worthless for capacity planning: sizing off 311s or off 42s gives
  answers an order of magnitude apart, and 2,400 runs amplifies whichever is
  wrong. Size off the p95 of a repeated measurement, not a mean of one.

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
