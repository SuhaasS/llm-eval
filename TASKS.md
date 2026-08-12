# Pending Tasks

Single list of open work for the LLM bakeoff eval. **This file is the backlog.**
`tasks/todo.md` is the opposite — a completed-work review log, one section per
finished task. Nothing here is done; move it there when it is.

Last updated 2026-08-12. Gemma went 0/3 that morning when its `/openai/v1`
route began enforcing OpenAI's reasoning-model parameter contract — three
separate rejections stacked behind each other, the first of which hid the
other two. Fixed the same day (fourth and fifth `litellm_patches`
interventions plus a config change), and **the four-arm live N=3 came back
GO: 12/12, every run landing the 157-byte diff**, workdir `20260812T175513Z`.

**Read that GO the way 2026-08-11 taught us to.** Run A that day was also GO
and run B immediately after was NO-GO on Nemotron alone. One GO is one
observation, not a rate, and the item below on sizing N is unchanged by this
one. Nemotron's quit-mid-plan flake did not fire here, which moves the pooled
rate to 2/22 and does not narrow the interval enough to act on.

Gemma was 6/6 across two independent N=3 runs on 2026-08-11, before any of
this — and those runs are what prove the 0/3 was not a regression from
anything in this repo. They are **not** a baseline for the post-fix numbers:
gemma now runs without `top_p`, and the 08-11 set was measured in an image
with no test runner.

**New on 2026-08-12, from the GO run itself:** the wire log cannot see either
new intervention, gemma started returning cache tokens it has no price for,
and reasoning cannot be enabled uniformly across the three candidates on any
single route. All three are filed below.

The `kimi-k2-5-runtime` item closed on 2026-08-11 — the constraint moved into
the config rather than into the patch. See `tasks/todo.md`.

**An observability audit of a real record the same day found six unwritten
fields.** `cache_state` — the only one that stated something false rather than
nothing — is now populated; see `tasks/todo.md`. A review of the whole cache
path on 2026-08-11 found that populating it had fixed one arm and left three
still asserting `warm: false` with no cache to be cold; schema 2.2.0 closes
that, prices Sonnet at the $2/$10 list, and records the ephemeral tier.

**Reviewing that feature then turned up a P0 underneath it: `turns_used`,
`tokens` and `cost_usd` were roughly doubled in every record ever written.**
Claude Code emits one transcript record per content block with the whole
`usage` repeated in each, and `parse_trajectory` counted records. Schema 3.0.0
dedupes on `message.id`; the re-derived parse now matches the wire log on
16/16 live runs, where the stored records did not. **Every cost figure in this
file predates that fix.** The remaining order:

1. **`host` / `container.stats()`** — dead code; without it a slow arm cannot
   be told from a loaded host, which is the §5.7 parallel-execution question.
2. **Error bodies in the wire log** (P2) — the only *capture* gap of the six.
   Everything else can be back-derived from stored artifacts at any time; this
   one cannot, so runs collected before it lands are permanently harder to
   diagnose.

Everything else found in that audit is a derivation gap and can wait for the
scoring plan. The distinction is now marked on every P2 item.

Spec: [docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md](docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md)
Harness plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

---

## P0 — Blocking Phase 0c go/no-go

- [ ] **Gemma's mantle route now rejects `max_tokens`. Sixth §6.4 adapter
  defect, not a model failure.** Live N=3 on 2026-08-12, workdir
  `20260812T075218Z`: gemma **0/3**, every call 400 before the model saw
  anything, zero tool calls, zero diff.

  ```
  OpenAIException - Unsupported parameter: 'max_tokens' is not supported
  with this model.  Received Model Group=gemma-4-31b
  ```

  **Isolated outside Claude Code**, one trivial completion per arm — the other
  five arms answer `OK`, gemma alone 400s. Then narrowed to the parameter:

  | sent | result |
  |---|---|
  | `max_tokens=16` | **FAIL** — unsupported parameter |
  | `max_completion_tokens=16` | OK |
  | neither | OK |

  **Bracketed, and nothing on our side moved.** Last known good
  `20260811T174141Z` (17:41 UTC, n=11/11/9, zero failures, `resp_model
  google.gemma-4-31b`); first known bad `20260812T075218Z` (07:52 UTC). A ~14
  hour window in which both records report `litellm 1.95.0` (pinned in
  `litellm-proxy.Dockerfile` with a build-time assert, and read back off the
  running proxy as `litellm_proxy_version`), `claude_code 2.1.220`, and the
  same three `litellm_patches`. The working-tree config diff contains no
  gemma, `max_tokens`, `drop_params` or `openai/` line. Same client, same
  resolved param, different answer.

  **The discriminator is the endpoint path, not the model.** Gemma is the only
  arm on `/openai/v1`; nemotron and kimi are on `/v1`. Probed directly:

  | arm | path | `max_tokens` | `max_completion_tokens` |
  |---|---|---|---|
  | gemma-4-31b | `/openai/v1` | **FAIL** | OK |
  | nemotron-3-super-120b | `/v1` | OK | OK |
  | kimi-k2-5 | `/v1` | OK | OK |

  `/openai/v1` tracks OpenAI's own API contract, and OpenAI deprecated
  `max_tokens` in favour of `max_completion_tokens`. The path split is now
  confirmed from litellm's own source rather than inferred:
  `bedrock_mantle.common_utils.mantle_base_segment` puts any model whose price
  map carries `use_openai_responses_path` on `/openai/v1`, and
  `litellm.model_cost["bedrock_mantle/google.gemma-4-31b"]` carries it.

  **`max_tokens` was wall 1 of 3, and its 400 hid the other two.** Request
  ladder against the live route, 2026-08-12:

  | sent | result |
  |---|---|
  | `max_tokens: 16384` | **FAIL** — needs `max_completion_tokens` |
  | `top_p: 0.95` | **FAIL** — *"'top_p' is not supported with this model"* |
  | `temperature: 1.0` | OK — `0.6` fails, *"does not support 0.6"* |
  | `presence_penalty`, `frequency_penalty` | OK |
  | `tools` with no explicit `reasoning_effort` | **FAIL** |

  The third is the one that matters for an agentic eval:

  ```
  Function tools with reasoning_effort are not supported for
  google.gemma-4-31b in /v1/chat/completions. To use function tools, use
  /v1/responses or set reasoning_effort to 'none'.
  ```

  The route applies a non-`none` default when the parameter is **absent**, so
  absent is not good enough — and gemma's deployment carried
  `additional_drop_params: ["reasoning_effort"]`, which strips exactly the
  parameter that unblocks it. A workaround for one defect was blocking the fix
  for the next.

  **The model did not change.** `resp_model` is `google.gemma-4-31b` in both
  eras and gemma still returns `reasoning_tokens: 0` on a default call today,
  exactly as in the 08-11 wire logs. What changed is the route's validation
  contract, not the model.

  **The fix is uniform, and that is the point.** `max_completion_tokens` is
  accepted on **all three** candidate arms, so the rename applies across the
  board rather than scoped to gemma — no per-arm divergence, which is what
  §5.4 requires. `reasoning_effort` is pinned to `"none"` on all three for the
  same reason: every arm was already thinking-off, and pinning makes explicit
  what was implicit rather than turning thinking on for one arm.
  `additional_drop_params: ["max_tokens"]` is the tempting one-liner and is
  wrong: measured, it leaves the body with **no cap at all** while every other
  arm runs at 16384.

  **Config cannot express the pin.** Claude Code sends `thinking: {"type":
  "adaptive"}` on every arm and litellm derives an effort from it; measured, a
  deployment carrying `allowed_openai_params: ["reasoning_effort"]` **and**
  `reasoning_effort: "none"` still puts `"medium"` on the wire. The derived
  value wins. Both interventions therefore live in `litellm_patches.py`,
  wrapping `litellm.OpenAIConfig.map_openai_params`.

  `top_p` is the exception and is handled in config: gemma omits it, exactly as
  Sonnet does and for the same reason. §5.3's constant across arms is the
  *policy* — lab-recommended unless the route refuses it — not the number, so
  nemotron and kimi keep `top_p 0.95`. **This is a real §5.3 divergence and
  must be published with any gemma comparison.**

  Sonnet is untouched on both transports and is exempt structurally:
  `anthropic/` resolves to `AnthropicConfig` and `bedrock/` to
  `AmazonConverseConfig`, neither reachable from the openai provider branch.

  **Not taken: `/v1/responses`.** Measured working for gemma with tools, and it
  would preserve reasoning *and* return unique tool-call ids
  (`call_c95af05096b2…` rather than `call_0`, retiring the collision patch on
  this arm). Rejected for now because
  `_should_route_to_responses_api` reads a process-global flag with no
  per-deployment override, so it needs a further patch, and because it would
  diverge gemma's transport from the other two candidates.

- [ ] **The `reasoning_effort` drop was attributed to Bedrock and the
  attribution was wrong.** `litellm_config.yaml` recorded the 2026-08-07
  rejection as *"Bedrock's OpenAI-compatible route rejects it for all three
  candidate models"*. Probed 2026-08-12 with raw `httpx`, bypassing litellm
  entirely: **Bedrock accepts it on all three**, and it measurably changes
  output.

  | arm | omitted | `'none'` | `'high'` |
  |---|---|---|---|
  | gemma-4-31b | 262 tok | 240 | **580** |
  | nemotron-3-super-120b | 330 | 392 | 150 |
  | kimi-k2-5 | 170 | 171 | **717** |

  All 200s. The rejection is **litellm's own `_check_valid_arg`**, because
  `OpenAIGPTConfig.get_supported_openai_params` omits `reasoning_effort` for a
  non-o-series model. A client-side guard was recorded as a provider
  constraint, and the drop rested on that for five days — which is what made
  gemma's third wall unreachable.

  The comment is corrected and the drop entries are kept deliberately, as a
  loud-failure backstop rather than as the mechanism. What remains open is the
  eval question this reopens: **thinking is now demonstrably available on all
  three candidates**, and the eval currently runs every arm thinking-off. See
  the §P3 item, which today's measurements close the measurement half of.

- [ ] **Re-measure every arm. No Phase 0c figure was taken in an environment
  where the agent could run tests.** Found 2026-08-12 in a review of the
  logging change; the environment defect itself is fixed, the re-measurement
  is **partly** done — the 2026-08-12 live N=3 is the first live data with a
  working test runner. Sonnet 3/3, Kimi 3/3, Nemotron 2/3, gemma blocked by
  the item above. Every arm that ran now makes 4–5 turns with 4 tool calls and
  submits a 157-byte diff, against the 1-turn no-diff shape that preceded it.
  Re-run once gemma is unblocked.

  `docker/eval-agent.Dockerfile` installed `ca-certificates coreutils curl git
  ripgrep` and no test runner, and `apt`/`pip` cannot reach a mirror during a
  run by design. Separately, `fixtures/smoke_task/tests/test_calc.py` does
  `from calc import add` while `calc.py` sits at the repo root, so the only
  verification command available inside the container —
  `python3 tests/test_calc.py` — put `tests/` on `sys.path[0]` and raised
  `ModuleNotFoundError` **before and after a correct fix**.

  So spec §3.3's loop — *"reads, edits, runs tests, sees failures, and
  self-corrects"* — terminated after "edits" on every arm, and each was scored
  on one unverified guess. The symptom was already in the log and read as
  model behaviour: Gemma spent 30 of 30 turns re-running that command
  (`tasks/todo.md`). Any arm that tried to verify was penalised; any arm that
  did not try looked equally good.

  Fixed 2026-08-12 — pinned `pytest==9.1.1` in the image, `pytest.ini` with
  `pythonpath = .` in the fixture, `smoke_test.assert_agent_can_verify_its_work`
  as a **precondition** (by the time a record exists the tokens are spent), and
  red-before/green-after assertions offline and in-container. What remains is
  the measurement: the N=3 sets, the Nemotron 1/16 flake and both GO/NO-GO
  calls below all predate it, and the flake in particular is the kind of thing
  an unverifiable task can manufacture.

Two independent N=3 runs on 2026-08-11, 24 runs across 4 arms:

| arm | run A | run B |
|---|---|---|
| claude-sonnet-5-runtime | 3/3 | 3/3 |
| gemma-4-31b | 3/3 | 3/3 |
| nemotron-3-super-120b | 3/3 | **2/3** |
| kimi-k2-5 | 3/3 | 3/3 |

Run A was GO. Run B was NO-GO on Nemotron alone. **The GO does not reproduce**,
and one observation of it was not a rate — the same mistake §5.7 and the N=3
criterion exist to prevent, made here in the space of one afternoon.

- [ ] **Nemotron 3 Super quits mid-plan, 1 run in 16.** Investigated
  2026-08-11 and confirmed to be the model, not the bridge. The failing
  response, read raw off the wire:

  ```
  finish_reason: 'stop'   tool_calls: None   function_call: None
  content: 'Now let me check the test file to see what the expected
            behavior should be:\n'         completion_tokens: 46
  ```

  It narrated its next action and ended the turn without emitting the call.
  Claude Code takes `end_turn` at face value and stops, so the run records
  `terminated_by: agent_finish` with 1 tool call and no diff.

  **Adapter ruled out, on evidence rather than absence.** `finish_reason` is
  literally `stop`, not an unrecognised value coerced to `end_turn` by the
  translation (the §P2 trap below); the content holds no tool call emitted as
  text, which is the Nemotron-family failure worth suspecting; `stop` not
  `length`, and 46 completion tokens, so nothing was truncated; ids unique and
  zero `(no content)` turns, so the collision uniquifier provably never fired
  on this arm. A successful run says almost the same sentence — *"Now let me
  check the test file to see what's expected:"* — with `finish_reason:
  tool_calls` beside it. Same intent, one sampled with the call and one
  without.

  **Rate, measured rather than assumed.** N=10 on this arm alone came back
  **10/10**. Pooled over every post-fix run: **1/16, 6.2%, 95% CI 1.1–28.3%.**
  The interval is the finding — 16 runs cannot separate a 2% flake from a 25%
  one, and it would take ~47 runs to be 95% sure of seeing a 6% event at all.
  Do not quote 6% as the rate.

  Sampling is a plausible contributor and is not free to change: this arm runs
  `temperature 1.0, top_p 0.95` on NVIDIA's own guidance, and §5.3 keeps the
  policy identical across arms rather than the number, so lowering it for
  Nemotron alone would trade a model property for a config confound.

  Next: this is the eval's **first failure that survives an adapter
  explanation** — everything before it was harness. It needs a ruling in the
  scoring plan, not here: a quit-without-tool-call is either a legitimate
  failure or an exclusion, and §5.7 sizing has to carry whichever it is. The
  offline grader can identify the class cheaply — final turn `stop`, no diff,
  and prose promising an action it never took.

Gemma took two fixes to get there, both adapter defects, neither about the
model: its Bedrock engine rejects `propertyNames` in a tool schema, and it
returns the tool-call id `call_0` on every response so Claude Code could not
pair anything after the first call. See `tasks/todo.md`.

**Four for four.** Every "model failure" this eval has produced — Sonnet's beta
header, Kimi's tool-id mangling, Gemma's `propertyNames`, Gemma's colliding ids
— has been a harness-layer defect. Each looked deterministic and total
beforehand. Nothing in the record yet supports a capability claim about any arm,
and that base rate is the thing to weigh the next total failure against.

- [ ] **The request carries `system` twice, and the first copy lands after the
  user message.** Seen in every Gemma wire log from 2026-08-11: the request has
  a top-level `system` field *and* 6 inline `system`-role messages, with
  `messages[1]` being the first of them — so the role sequence opens
  `['user', 'system', ...]`. Not implicated in any measured failure, and
  deliberately not bundled into the tool-call id fix so that run stays
  attributable. Worth a look before Phase 3: it is not the shape any provider
  documents, and an arm that handles it badly would look like a weak model.

- [ ] **Record the Sonnet transport asymmetry as a §6.4 confound.** Sonnet 5
  now signs SigV4 against bedrock-runtime while all three candidates go through
  the mantle passthrough. Different code path, different request shape: on
  runtime, beta features ride as an `additionalModelRequestFields.anthropic_beta`
  *body* field rather than an `anthropic-beta` header. The reference arm is
  therefore not transport-identical to the arms it is the reference for, and
  any Sonnet-vs-candidate delta carries that. It needs to be stated wherever
  the comparison is published, not just known here.

- [ ] **Re-run the four-arm smoke to a real GO.** Run A on 2026-08-11 was GO,
  all four arms 3/3; run B immediately after was NO-GO at Nemotron 2/3. Still
  open, and now blocked on the Nemotron flake rather than on Gemma.
  What the two runs *do* establish, independent of the verdict: the loop, the
  transport, the capture and the §5.2 config dump work on every arm, and Gemma
  is 6/6. What they do not: any statement about relative model quality — one
  fixture task, and the harness does not grade.
  **The N=3 criterion is itself now suspect.** It was chosen so a single run
  could not be read as a rate, and two consecutive N=3 runs just disagreed on
  the verdict. Whatever N Phase 3 uses has to be sized off the measured flake
  rate above, not picked in advance.

---

## P1 — Landmines that only fire at scale

None of these can appear at N=1. All will appear at N=10.

- [ ] **`host` metrics are dead code: `container.stats()` has zero callers.**
  `HostMetrics` collects `mem_peak_mb` at [container.py:254](bakeoff/src/bakeoff/container.py:254)
  and nothing ever calls it, so every record carries
  `{"cpu_pct_p95": null, "mem_peak_mb": null, "contention_flag": false}`.
  `contention_flag` is the field that tells a slow model apart from a loaded
  host, and §5.7 runs arms interleaved and parallel by design. The 22× wall-clock
  spread on identical work in the quota item below is currently unattributable
  for exactly this reason — there is no way to ask whether A1's 201.9s was
  Nemotron or the machine.
  `cpu_pct_p95` and `contention_flag` are not collected at all; decide whether
  they come from `docker stats` sampling or are dropped from the schema, but do
  not leave a field that reads as measured-and-zero.

- [ ] **Sonnet's cost is order-dependent: 2.9× spread on byte-identical work.**
  Measured at N=3, three runs of the same one-line fix, same 8 turns, same
  157-byte diff:

  | run | cache_write | cache_read | cost |
  |---|---|---|---|
  | 1 | 125,853 | 210,149 | **$0.548** |
  | 2 | 118,499 | 217,519 | $0.522 |
  | 3 | 34,166 | 259,646 | $0.217 |

  > **Every absolute number in this item is inflated ~2× and every dollar
  > figure also predates the $2/$10 Sonnet book.** Schema 3.0.0 found that
  > `parse_trajectory` counted transcript records rather than API calls, and
  > Claude Code writes one record per content block with the full `usage`
  > repeated in each. Run 1's true `cache_write` is 42,305, not 125,853, and
  > it made 5 calls, not 8 — verified against the wire log, which agrees with
  > the re-derived parse on 16/16 live runs. The **ratio** survives, because
  > cold and warm runs inflated alike; nothing else here does. Re-derive from
  > the stored transcripts before quoting any of it.

  Reproduced across two separate N=3 runs, with the *shape* stable and the
  *breakpoint* not: the first run measured 2.9× and the second 2.5×, and which
  run gets the cheap one moved. So this is not a fixed warm-up cost that could
  be subtracted — it depends on cache state at dispatch.

  The Bedrock prompt cache **persists across runs**, so a run that finds it
  cold pays cache_write at 1.25× and later runs read at 0.10×. Nothing about
  the model or the task changed. §5.7 randomizes and interleaves execution
  order, which means at N=10 per task each sample's cost depends on where the
  scheduler happened to put it — cost-per-task becomes partly an artifact of
  scheduling.
  It lands on one arm only: Sonnet is the sole arm with caching, so
  Sonnet-vs-candidate cost is confounded twice over (order, and the presence
  of caching at all).
  Next: decide the policy before any cost figure is published — report
  steady-state cost from warm runs, report cache-write separately, or pin
  execution order per task. All three are defensible; silently averaging is
  not.
  **The decision is now cheap to make from the log.** `cache_state.warm` is
  populated as of schema 2.1.0 and separates the runs directly: on both N=3
  sets the single `warm: false` run is the expensive one ($0.547 and $0.373
  against ~$0.18–0.22). "Report warm runs only" is a one-line filter rather
  than a reconstruction.
  **Both halves are now built** (2026-08-11): `smoke_test --interleave` gives
  the §5.8 ordering and `print_run_order` states whether the ordering actually
  used separated the repeats — the spec requires that be verified, not assumed.
  `print_cost` reports the two scenarios by name, `first_task` and
  `warm_followup`, and calls neither one normalized.

  **The reframing matters more than the flag.** The inter-run cache hits are a
  harness artifact: each run is a fresh container, a wiped `CLAUDE_CONFIG_DIR`,
  a fresh repo and a one-shot `claude -p`, so nothing crosses runs but
  Bedrock's server-side cache — and the repeats are scheduled 3–5 s apart
  inside a 300 s TTL (6/6 matrices cold on run 1, 10/10 warm after). Measured:
  an interleaved round takes 1.4–3.9 min, still inside the TTL, so
  `--interleave` redistributes the write cost and **cannot produce cold runs**.

  A real developer starting a task pays the tool-schema write. So `first_task`
  is the faithful number for "opening a fresh task" and `warm_followup` for
  "another task within five minutes" — two deployment situations, not a number
  and its correction. `cache_state.seconds_since_prior_run` is now in every
  record so a reader can see which one a run was.

  Still open: which the recommendation quotes.

- [x] **`costs.py` matches Bedrock's cached-token accounting.** Verified
  2026-08-11 against the AWS prompt-caching page: *"the `inputTokens` field
  represents only the non-cached input tokens… total input tokens = inputTokens
  + cacheReadInputTokens + cacheWriteInputTokens."* `cost_usd` charges `input`
  at full rate and adds cache read/write on top, which is correct.
  The candidate arms survive a round trip that could easily have broken it:
  LiteLLM's Converse transform folds the cache tokens *into* `prompt_tokens`
  (OpenAI usage is inclusive where Anthropic's is not) and the anthropic adapter
  subtracts them back out. Nothing pinned that, so a litellm upgrade dropping
  the subtraction would have double-counted the cached prefix silently — on a
  warm Sonnet turn, ~30k of a ~34k prompt. Now pinned in
  `tests/test_usage_accounting.py` against the installed version.

- [ ] **Gemma now returns cache tokens too, so the unpriced problem is two arms
  wide.** Measured on the 2026-08-12 GO run: gemma `cache_read=18016` across
  the set (16512 / 0 / 1504), where the 2026-08-08 measurement recorded gemma
  returning no cache fields in 9 runs. `costs.py` has no gemma cache
  multiplier, so **2 of 3 gemma runs carry `cost_usd: null`** — same failure as
  Kimi, on a second arm, and it appeared without anything on this side
  changing.

  At N=10 per task the cache is warm far more often than not, so most gemma
  rows would carry no cost at all. Combined with the Kimi item below, that is
  **two of three candidate arms unpriceable on their warm runs** — and the
  warm runs are the majority. Any cost headline computed today silently
  compares Sonnet's full distribution against whatever subset of the
  candidates happened to run cold.

  Same resolution path as Kimi: the pricing-permissions item, or a stated
  fallback. Decide it once, for both arms.

- [ ] **Kimi's cost is unknown on any cache-warm run, and no rate exists.**
  The record-destroying half of this is fixed (see `tasks/todo.md`): a pricing
  failure now costs the price only, and the tokens survive so the run stays
  repriceable. What remains is that **there is no rate to reprice it with**.
  Measured 2026-08-08: Kimi returned `cache_read` on 2 of 3 runs (33,152 each),
  so those two records carry `cost_usd: null`. At N=10 per task the cache is
  warm far more often than not, so most Kimi rows will carry no cost at all.
  Nemotron and Gemma returned no cache fields in 9 runs; Sonnet is priced.
  **Blocks any published cost figure involving Kimi**, and it cannot be
  resolved inside the harness — see the pricing-permissions item below.
  Decide the fallback now in case AWS publishes nothing: report Kimi cost as a
  lower bound from cold runs, or exclude Kimi from cost claims and say so.

- [x] **The warm prefix is task-independent — settled from the archive,
  2026-08-11.** A warm turn 1 reads 30,506 and still *writes* 11,187. Hashing
  the request components across the three repeats of a matrix: the `tools`
  block is **byte-identical** (sha `45b32a6181f6`, 84,627 chars) and only the
  `system` tail differs, by the git SHA the fresh per-repeat commit produces.
  `30506 + 11187 = 41693 = 41695 − 2`, the whole prefix split once at that
  boundary. The constant moved 30,538 → 30,506 only when the CLI's tool
  schemas changed, never when the repo did.

  So a run of a **different** task on the same model is a valid warmer, and
  `prior_same_task_run_id` is keyed too narrowly to name it. Left as-is
  deliberately: the field is corroborating evidence and it is honest about
  what it covers, while `seconds_since_prior_run` — added in 3.0.0 — is what
  actually explains a warm run. Widening the key would trade a narrow answer
  for a confident wrong one.

  The practical consequence is the important half: **what the harness carries
  across runs is task-independent boilerplate, not work.** See the run-order
  item above.

- [ ] **Reprice and recount the archive under 3.0.0.** 219 stored records carry
  inflated `turns_used`/`tokens`/`cost_usd`, and 44 of them also carry Sonnet at
  the old $3/$15 book. The log is append-only, so they keep those figures; the
  transcripts and wire logs are on disk, so a derived view can recompute all of
  them. Needed before any cost or turn-count figure is published, and it is the
  reason `Versions.pricing_basis` and `RunRecord.assistant_records` exist —
  without them a re-derivation cannot tell which records need correcting.

- [ ] **The prior-run lookup is unsafe under §5.7 parallelism.** `index.jsonl`
  is appended unlocked and `last_run_id_for` runs before the container starts,
  so two concurrent runs of the same (task, model) read the same prior id and
  neither sees the other. Sequential today, so this is a precondition on
  parallelising the runner, not a live defect.

- [ ] **The ephemeral tier is unobservable on the candidate arms.** Schema 2.2.0
  records `cache_write_5m` / `cache_write_1h` (spec §3.1), but only Sonnet's
  native-Anthropic route carries the nested `cache_creation` object — LiteLLM's
  Converse bridge emits the flat total and drops Bedrock's `cacheDetails`. Those
  writes are recorded as untiered and charged at the 5m rate. Harmless while
  Claude Code's Bedrock TTL stays hardcoded to 5m (claude-code#32671), and it
  becomes a mispricing the moment that changes or a candidate gets a cache rate.
  Fixing it means a `litellm_patches` intervention to carry `cacheDetails`
  through, which is not worth doing before a candidate has a rate at all.

- [ ] **Request `pricing:GetProducts` and `pricing:DescribeServices`** on the
  `BedrockModelTester` SSO role. Read-only, free, no data access. It is the
  only way to settle whether AWS publishes cache rates for the candidates,
  which retires the guard properly rather than working around it.
  Checked 2026-08-07: **Bedrock returns no dollar figure on any call** — the
  API and its response headers carry token counts only, so the price book is
  the sole pricing authority and a gap in it must be reported, never
  estimated. Cost Explorer and CloudWatch are also denied to this role, and
  neither can attribute spend to a `run_id` anyway. Application inference
  profiles with cost-allocation tags could attribute per arm but not per run,
  and would change the model id in every deployment.

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
  (OPEN-3). Wall clock, not cost, is the binding constraint.
  **Wall-clock and turn-count variance is the real problem, and N=3 exposed
  it.** On the *same one-line task*, Nemotron measured across two N=3 runs:

  | run | turns | wall |
  |---|---|---|
  | A1–A3 | 19 / 15 / 21 | 201.9s / 42.6s / 54.2s |
  | B1–B3 | 24 / 21 / 7 | 52.5s / 98.2s / 9.0s |

  A 22× spread in wall clock (9.0s to 201.9s) and 3.4× in turns, on identical
  work. The two do not track each other — B2 took 98.2s for 21 turns while A1
  took 201.9s for 19 — so per-turn latency is varying independently of how much
  the model chose to do.

  A single-sample estimate is therefore worthless for capacity planning: the
  original OPEN-3 sizing used one 311s sample, and 2,400 runs amplifies
  whichever end you picked. Size off the p95 of a repeated measurement, and
  measure it on a real task rather than this fixture.

- [ ] **`RunRecord.sampling` reflects the bridge, not just the config.** On the
  Responses path all three candidates reported `temperature=1.0`; switching to
  chat-completions made it read `absent`; it returned on nemotron's successful
  run. Pin which bridge each arm uses before the full run, or sampling
  provenance is unreliable across arms.

---

## P2 — Capture gaps found in passing

Two different animals live in this section, and the distinction decides
urgency. A **capture** gap is unrecoverable — the observation was never
written and no offline pass can invent it. A **derivation** gap means the
observation is sitting in `wire.jsonl.gz` and simply is not surfaced in the
record; it can be closed at any time, including after Phase 4. Each item below
says which it is.

### Found by the 2026-08-12 GO run

- [ ] **CAPTURE. The wire log records the request Claude Code sent, not the one
  the provider answered — so neither openai param intervention is visible in
  it.** Read off `20260812T175513Z`, gemma run 0, a run that demonstrably
  worked:

  ```
  max_tokens            = 16384
  max_completion_tokens = None
  reasoning_effort      = None
  thinking              = {'type': 'adaptive'}
  ```

  The wire carried `max_completion_tokens: 16384` and `reasoning_effort:
  "none"` — the route 400s otherwise, and this run returned `finish_reason:
  tool_calls` on 5 of 6 calls. `pick()` prefers `optional_params` and falls
  back to the raw body, but the callback fires on the **outer**
  `anthropic_messages` call while both patches operate inside the nested
  `acompletion`, so the resolved params are never in scope. Widening the
  projection (schema 3.2.0) was necessary and is not sufficient: the data is
  not reachable from where the callback runs.

  Consequences, in order of how much they cost:

  - `sampling.max_output_tokens` is right by coincidence — correct value,
    wrong key, wrong provenance. §6.2 makes the wire log the record of what
    went over the wire, and for these two fields it is not.
  - **It falsifies the premise of the `reasoning_effort` P3 item below**,
    which says a live `openai/` run settles the question because `pick()`
    shows the post-drop state. Measured: it shows raw-body absence. No live
    run can settle it until the callback can see resolved params.
  - `thinking: {"type": "adaptive"}` in the log is what Claude Code **asked
    for**, never what was served — it was dropped at the proxy on every
    candidate arm, on every run ever recorded.

  Fixing it means capturing on the inner call, or having the patches record
  what they changed into a place the callback can read. Until then, treat every
  stored `max_tokens` and `reasoning_effort` on a candidate arm as a statement
  about Claude Code, not about the wire.

### Found by the 2026-08-12 live run itself

- [ ] **The capture reconciliation compares two different quantities and
  reports the wrong direction.** `smoke_test` fails the gate on
  `served != captured`, where `served` counts `POST /v1/messages` in the
  proxy access log (client requests) and `captured` sums wire entries
  (provider attempts). `num_retries: 3` means one client request can produce
  several callback invocations — the wire log's own docstring says so: *"Each
  retry is its own callback invocation, which is why the wire log counts calls
  rather than turns."*

  Measured: `served=39`, `captured=42`, and gemma's three runs each hold
  exactly two entries for one failing request (`call_index` 1 and 2, both
  `failed=True status=400`). The delta is exactly the three retries. The
  message printed is *"some calls were captured nowhere"*, which describes
  `captured < served` — the opposite of what happened. A gate that cries loss
  on every retry is a gate that gets ignored, and it would then miss a real
  loss.

  Second, independent hazard in the same function: `request_count` reads
  `self.logs(tail=10000)`, so a long run silently truncates the numerator.

- [ ] **`smoke_bedrock.py --live` skips the four mantle arms that
  `smoke_test.py` runs fine.** The preflight reads `BAKEOFF_MANTLE_TOKEN` from
  the environment; the run calls `derive_mantle_token` and mints one from the
  SSO session. So the gate reports `SKIP ... needs BAKEOFF_MANTLE_TOKEN` for
  arms that are fully credentialed, and a preflight is weaker than the thing
  it gates. Same root cause makes it print `MISSING BAKEOFF_MANTLE_TOKEN` on
  four rows and `preflight PASS` on the next line.

- [ ] **Nemotron's quit-mid-plan reproduced, verbatim.** Run 1 of 3 on
  2026-08-12: `finish_reason: stop`, `tool_calls: None`, 108 completion
  tokens, content narrating the plan it then did not execute — the same
  signature recorded at the top of this file. Pooled rate is now **2/19**; the
  interval is still far too wide to act on. New detail worth keeping: that
  call took **179.6 s**, against 0.5–15 s for every other call in the matrix.

### From the 2026-08-12 review of the logging change

Tier A of that review landed (see `tasks/todo.md`). These are what it left.

- [ ] **CAPTURE. A crashed run's cause exists nowhere in the log.**
  `runner.py` catches the whole run body with `except Exception: crashed = True`
  and keeps no type, no message, no traceback; `Artifacts.container_stderr` is
  never set even though stderr *is* captured (`claude_runner.py`,
  `container.py`) and the `finally` writes stdout only. The record shows
  `CRASHED` + `container_crashed` and nothing else, so a harness defect and a
  genuine infra failure are indistinguishable — and exclusion is, by the
  module's own comment, "the one mechanism by which results can be massaged".
  Wants `crash_error: str` plus persisting stderr.

- [ ] **CAPTURE. `ParsedTrajectory.malformed_lines` is counted and has no
  consumer.** No `assemble_record` read, no `RunRecord` field. A transcript
  with 40 unreadable lines is byte-identical in the record to a clean one.
  `claude_runner` drops unparseable stdout lines with no counter at all, which
  silently undercounts `turns_streamed` — one of the three cross-check counts.

- [ ] **CAPTURE. The agent's exit code is never recorded.**
  `ClaudeRunResult.exit_code` has no `RunRecord` field, so a CLI that exited
  non-zero but wrote a transcript looks identical to a clean finish.

- [ ] **CAPTURE. Wire log: no success status, no retry identity, no
  timestamps that survive.** `status_code` is `None` on success, conflating
  200 with unknown. `num_retries: 3` is configured but nothing carries an
  attempt number or a retry-of correlation id, so retries are inferable only
  as adjacent `failed: true` lines and `retry_backoff_ms` stays 0 forever.
  `bedrock_request_id` is captured by the *in-process* callback, which never
  runs in production, and not by the proxy one. And `WireLogger.log_call`
  re-stamps `logged_at` while replaying proxy entries, so every timestamp in
  the canonical artifact is the harness's post-run replay time, not the call
  time.

- [ ] **DERIVATION. `ToolCallStats.errored` holds failed *API* calls, not tool
  errors.** `errored=failed_calls` from wire metadata. It is also the only
  place proxy retries surface in a record. Anyone reading it as "tools the
  agent invoked that failed" gets the retry count. Wants a separate
  `api_calls_failed`, and `errored` either populated correctly or removed.

- [ ] **DERIVATION. Fields that are permanently zero and read as
  measurements.** `retry_backoff_ms`, `ToolCallStats.malformed` (so
  `malformation_rate` is always 0.0 and `TOOL_MALFORMATION`/`ADAPTER_FAILURE`
  can never fire), `truncation_events`, `p2p_regressions`, `diff_stats`,
  `Checkpoint.per_test`, `DestructiveEvent.affected_outcome`,
  `Exclusion.pre_registered` (always `True`), `HostMetrics.contention_flag`.
  Populate or delete — deleting is honest, leaving them is a claim. The `host`
  item at the top of this file is the same defect.

- [ ] **DERIVATION. `TurnRecord` has no absolute timestamp.** Per-turn data,
  checkpoints (`elapsed_ms`) and wire entries (`logged_at`) use three time
  bases that never join, so no per-call latency can be attached to the turn
  that incurred it. Also: for a deduped later content block `inference_ms` is
  computed and then dropped, so `sum(per_turn.inference_ms)` systematically
  undercounts the span it purports to cover.

- [ ] **`isolated` is configuration reported as observation.** Set from
  `bool(network)`. `False` is honest; `True` asserts a property nothing
  verified — the one place `runner.py`'s own stated rule does not hold. The
  integration suite proves the network has no host route; the record could
  carry that result instead of the argument.

- [ ] **Durability: five narrow windows in an append-only claim.**
  (a) No `os.fsync` of the runs directory after the rename, so a power loss can
  leave the index line durable and the record's directory entry lost — the
  exact inversion the comment says is impossible.
  (b) A stale `<run_id>.json.partial` from a process killed mid-write poisons
  that run_id permanently; `run_id` is deterministic and nothing increments
  `attempt_number`, so there is no operational retry path. Tier A stopped this
  losing the record (`record.unwritten.json`); it does not clean up the
  `.partial`. Wants a sweep at `EventLog` open, loudly.
  (c) A crash between rename and index append leaves a record `list_runs` sees
  and `last_run_for` does not, so `prior_same_task_run_id` silently names the
  wrong run.
  (d) `read_run` is intolerant where `last_run_for` is deliberately total — a
  torn record file raises out of the reader.
  (e) Wire logs are `flush()`ed but never `fsync`ed, and `proxy_callback._write`
  has no `try`, so an unwritable wire dir raises inside LiteLLM's logging path.

- [ ] **`scan_destructive` failing produces a positive safety claim.** The
  `try` in `execute_run` spans both `parse_trajectory` and `scan_destructive`.
  If the *scanner* raises, `assemble_record`'s independent re-parse succeeds,
  so `trajectory_parse_error` stays `""` and the record carries
  `destructive_events: []` — precisely what the comment above it says it is
  guarding against. Split the two `try` blocks.

- [ ] **`git checkout --detach` and `git clean` use `exec`, not
  `_checked_exec`.** At the one place the codebase elsewhere argues checking
  is mandatory: an unchecked failure yields empty output byte-identical to a
  clean tree. `build_smoke_repo`'s docstring names this exact hazard without
  fixing the call site.

- [ ] **A wire-log name collision is recorded as a container crash.**
  `WireLogger` opens `gzip.open(path, "xt")` inside the run `try`, so
  `FileExistsError` becomes `crashed=True` → `CRASHED` + `container_crashed`,
  on a run whose container never started. The comment says the collision "must
  cost the wire log, not the run record". Move the construction out of the try.

- [ ] **`eventlog.last_run_id_for` carries a ~70-line design rationale and has
  zero production callers.** It is a two-line delegate; `last_run_for`, the
  function actually on the hot path, documents only `started_at`. Move the
  docstring to the implementation or delete the delegate.

- [ ] **`schema.Exclusion` is the only nested class not routed through
  `_build`.** A record written by a later schema still raises `TypeError`
  there, which is the failure `_build` exists to prevent.

- [ ] **CAPTURE. The wire log records a failure's status code but not its error body.**
  `BakeoffCallback._record` stores `response_obj`, which is `None` on failure,
  so a 400 lands as `{"raw_completion": "None"}`. Every diagnosis on
  2026-08-07 had to come from the proxy's own logs instead — and in the full
  run the proxy log is not an artifact of the record. §6.2 exists so a failure
  can be diagnosed after the fact; right now it cannot be.
  Confirmed again on the second run: the failing mantle Sonnet arm recorded
  exactly `{"status_code": 400, "raw_completion": "None"}`, and `invalid beta
  flag` was recoverable only by grepping `proxy.log`. Two runs, two diagnoses
  that the event log could not support on its own.

- [ ] **DERIVATION. Four more record fields are never assigned and take their
  dataclass defaults.** Verified on a real record from the 2026-08-11 live
  smoke — `assemble_record` sets none of them:

  | field | what every record says | recoverable from |
  |---|---|---|
  | `diff_stats` | `{}` | `checkpoints[].diff_vs_base` |
  | `truncation_events` | `[]` | wire `finish_reason` / `stop_reason` |
  | `p2p_regressions` | `[]` | hardcoded `[]` at `classify.py`; needs the scoring plan |
  | `time.retry_backoff_ms` | `0` | proxy retries (`num_retries: 3`) — **not currently observed anywhere** |

  Lower priority than the two P1 items above because three of the four are
  honest empties rather than false claims, and all three are re-derivable from
  stored artifacts. `retry_backoff_ms` is the exception and is a real capture
  gap wearing a derivation gap's clothes: the proxy retries up to three times
  per call and nothing records that it did, so retry latency lands inside
  `inference_ms` with no way to separate it. That matters at scale, where
  throttling is routine — see the exclusion-rate item in P1.

- [ ] **CAPTURE. Every dataset task repo needs a `.gitignore`.** §5.6 stages everything
  (`git add -A`), so the first live run's diff led with a binary
  `__pycache__/calc.cpython-312.pyc`. Without this, diff size and file counts
  measure the interpreter rather than the agent. Done for the smoke fixture
  only.

- [ ] **DERIVATION. `ToolCallStats.malformed` is never populated at harness time**, so
  `TOOL_MALFORMATION` and `ADAPTER_FAILURE` are both unreachable — and §6.4
  calls the adapter-vs-model distinction the eval's most consequential call.
  Keep `malformed` at 0 at harness time (deciding a call was malformed means
  reading raw completions, which §6.4 puts offline). The Kimi tool-id defect
  made the cost of this concrete: it was an adapter failure the harness could
  never have flagged, and it took a live run to find.
  Next, for the scoring plan: a pass over `wire.jsonl.gz` where the set of
  `tool_use` ids in a response must equal the set of `tool_result` ids in the
  next request. That single check would have caught the id mangling directly,
  offline and for free.

- [ ] **DERIVATION. §5.2's config dump is an artifact, not a record field.** Written to
  `artifacts_root/<arm>/effective_config.json` and diffed across arms by the
  smoke script. The spec asks for it stored *in the run record*, which needs an
  `Artifacts.effective_config_json` field and a schema bump to **2.1.0** —
  1.2.0 was reserved here, but the pricing change took the schema to 2.0.0.

- [ ] **DERIVATION. The adapter coerces `finish_reason` and the harness reads
  the result as intent.** LiteLLM's openai→anthropic translation maps `stop`→`end_turn`,
  `length`→`max_tokens`, `tool_calls`→`tool_use`, and **anything else to
  `end_turn`**. `assemble_record` treats `end_turn` as the agent deciding it
  was finished (`TerminationReason.AGENT_FINISH`), so an unrecognised provider
  finish_reason is silently recorded as a deliberate stop. The raw value is
  still in the wire log (`choices[0].finish_reason`); record it beside the
  translated `stop_reason` so the offline grader can tell a real end_turn from
  a coerced one.
  Worth stating plainly because it reads like a blocker and is not: the
  Nemotron P0 above **is** diagnosable today, offline, from `wire.jsonl.gz` —
  that is where the `finish_reason: 'stop'` in its evidence block came from.
  This item makes the class cheap to query in bulk; it does not gate the
  ruling.

- [ ] **DERIVATION. Truncated tool-call JSON is silently repaired.** On the non-streaming
  adapter path LiteLLM closes unmatched brackets in tool arguments and returns
  the result as though the model emitted it — a repaired `Edit` is a different
  edit than the model asked for, and the only trace is a log line. Streaming
  forwards raw `input_json_delta` and does no repair, so this bites the
  occasional non-streaming call rather than every turn. Detect offline by
  comparing the wire log's raw arguments against the transcript.

- [ ] **CAPTURE. `TokenUsage.reasoning` is structurally 0 on all three candidates.** The
  adapter's usage translation emits input/output/cache fields and has no
  reasoning field to emit, so a zero there is "not representable", not "none
  used". Measure whether reasoning tokens are excluded from `completion_tokens`
  before publishing any cost-per-token comparison — if they are, the candidates'
  output is undercounted against Sonnet's.

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

### From the 2026-08-12 review

- [ ] **`reasoning_effort` is dropped on the three candidates and on neither
  Sonnet arm — now measurable, not yet measured.** Each candidate deployment
  lists `additional_drop_params: ["reasoning_effort"]` because the global list
  does not reach an `openai/` deployment; `claude-sonnet-5-runtime` lists
  nothing. Kimi runs at its documented *thinking-mode* temperature while the
  parameter that would enable thinking is dropped.

  **Measured so far, and it is weaker evidence than it looks.** 856 stored
  calls across five arms carry **zero** reasoning tokens and no thinking
  content, Sonnet included — so no arm was doing extended thinking in the
  Phase 0c data and the drop cannot have disadvantaged the candidates there.
  But that is an *outcome* proxy: the wire log's request projection was six
  keys and carried none of this. Schema 3.1.0 widens it, and the offline smoke
  now shows Claude Code sending `thinking: {"type": "adaptive"}` and
  `output_config: {"effort": "high"}` **identically on every arm**.

  What settles it is a live run: `pick()` prefers `optional_params` over the
  raw body, so on an `openai/` deployment the field shows the post-drop state.
  Do that before publishing any reasoning-related comparison.

  **Measured 2026-08-12, and the framing above was wrong in one load-bearing
  way.** The drop was never a Bedrock constraint — see the P0 item. Bedrock
  accepts `reasoning_effort` on all three candidates and `'high'` measurably
  changes output (gemma 240 → 580 completion tokens, kimi 171 → 717). So
  thinking **is** available and the eval is choosing not to use it.

  The decision taken for now is thinking-**off**, pinned explicitly to `"none"`
  on all three candidates, because that is what the whole Phase 0c corpus
  already is (Sonnet included) and it keeps the existing data comparable. That
  is a defensible eval and it is not the only one. What is still open:

  - **Whether the published eval should run thinking-on.** It is the
    deployment-realistic configuration, and it is what Claude Code sends by
    default. Turning it on invalidates rather than re-measures the corpus, and
    it needs gemma on `/v1/responses` — on `chat/completions` that arm can have
    tools **or** reasoning, not both.
  - **Reasoning tokens are unmeasurable in the record either way.** gemma
    reports `reasoning_tokens: 0`, nemotron and kimi omit the field entirely,
    and litellm's `_translate_openai_usage_to_anthropic_usage_delta` emits only
    input/output/cache — so the datum reaches `wire.jsonl.gz` and is dropped on
    the way to `TokenUsage.reasoning`. Deliberately deferred: it only bites if
    thinking is turned on, but it must be closed **before** that, or the extra
    tokens land inside `output` priced correctly and attributable to nothing.

    **Worse than "not plumbed through" — the provider does not report it
    either.** Measured 2026-08-12: gemma returns `reasoning_tokens: 0` at
    `effort=high` while producing 2.4× the output (240 → 580 tokens), and on
    `/v1/responses` it returns `0` with an explicit `reasoning` block sitting
    in the same response. There is no route and no arm on which reasoning is
    countable today, so this cannot be closed by plumbing alone.

- [ ] **Reasoning cannot be enabled uniformly across the three candidates, on
  any single route.** Measured 2026-08-12, tools present, which is the only
  configuration an agentic eval cares about:

  | arm | `none` | `default` | `medium` | `high` |
  |---|---|---|---|---|
  | gemma-4-31b | OK | **400** | **400** | **400** |
  | nemotron-3-super-120b | OK | **400** | OK | OK (46 tok) |
  | kimi-k2-5 | OK | **400** | OK (30) | OK (69 tok) |

  Three findings, and each closes a door:

  - **Gemma is tools XOR reasoning on chat/completions.** Any non-`none`
    effort with tools is a 400.
  - **There is no "adaptive" value to set.** Anthropic's `adaptive` is an
    Anthropic concept; litellm maps it to a fixed `"medium"`, overridden by
    `output_config.effort` (`"high"` from Claude Code). OpenAI's nearest
    equivalent, `reasoning_effort: "default"`, is rejected on **all three**
    arms. Only a static level can be chosen, so the eval cannot reproduce what
    Claude Code actually requests.
  - **`/v1/responses` gives gemma both** — measured, a `reasoning` block and a
    `function_call` in one response, and the tool ids come back **unique**
    (`call_83c9aab1…` rather than `call_0`), which would retire
    `anthropic_tool_use_id_collision_uniquify` on that arm. But nemotron and
    kimi **cannot use that route at all**: Bedrock answers *"The model
    'nvidia.nemotron-super-3-120b' does not support the '/v1/responses' API"*,
    measured 2026-08-07, which is why `use_chat_completions_url_for_anthropic_messages`
    is set in the first place.

  So the best achievable thinking-on configuration is gemma on `/v1/responses`
  and the other two on chat-completions at `high` — reasoning everywhere, at
  the cost of gemma running a different transport from the arms it is compared
  against. That is a §6.4 confound to publish rather than remove, and it needs
  a further `litellm_patches` intervention because
  `_should_route_to_responses_api` reads a process-global flag with no
  per-deployment override.

  **Decision as it stands: thinking off, uniformly**, because that is the only
  configuration all three candidates can share. It measures a deployment
  nobody ships. Sonnet is thinking-off too, so the comparison is internally
  fair — and that has to be stated on the scorecard, not left implicit.

- [ ] **Arm-major ordering is the default, inside a 300 s cache TTL.** Spec
  §5.7/§5.8 require the opposite. Measured: the first Sonnet run cost 2.9× the
  others, purely from scheduling. `--interleave` redistributes the write cost
  and cannot produce a cold run (a round takes 1.4–3.9 min). Latency is not
  normalised for cache state at all, so repeats 2–3 of each arm get a
  systematic TTFT advantage. Decide whether `--interleave` becomes the default
  and whether cost publishes `first_task` / `warm_followup` separately rather
  than a mean.

- [ ] **The cost headline compares non-comparable subsets.** Sonnet is on
  Anthropic list pricing, the candidates on the Bedrock page — defensible, and
  documented. Less defensible: candidate cache multipliers are `None`, so any
  candidate run returning cache tokens gets `cost_usd = None` and drops out of
  the means entirely. Sonnet's warm runs price at 0.1×; the candidates' warm
  runs are unpriced. Either measure the multipliers or state the subset on the
  figure.

- [ ] **Per-turn snapshotting runs inside the synchronous stdout loop.**
  `every_k_turns=1` by default and never overridden; each turn boundary fires
  three synchronous `docker exec`s while the harness stops draining the
  agent's stdout. That time lands inside `wall_clock_total_ms` — a headline
  metric — and if the pipe buffer fills, the agent itself blocks. The
  approximate-boundary tradeoff is argued in `claude_runner.py`; the wall-clock
  contamination is not mentioned anywhere.

- [ ] **No run-level retry exists, despite the schema being built for it.**
  `attempt_number` and `parent_run_id` are defined and hashed into `run_id`,
  and no caller ever passes a non-default. A throttled run is recorded,
  excluded, and the matrix moves on. Combined with the stale-`.partial` item
  in P2, re-running a sample needs a new event-log root or a hand-passed
  `attempt_number` — which a 2,400-run unattended matrix will need.

- [ ] **`WebFetch`/`WebSearch` are in the tool schema and dead.** The container
  has no route off the host. A model that reaches for docs is penalised in a
  way a real session would not be, and nothing records that a run failed on a
  fetch attempt. Decide whether that is the intended measurement.

- [ ] **Subagents (`Task`) are never discussed anywhere.** The default tool set
  ships them and the config dir is empty, so runs get built-in general-purpose
  subagents. Not excluded, not measured, not mentioned in the spec.

---

## Housekeeping

- [ ] **27 commits unpushed.** Pushed manually.

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
