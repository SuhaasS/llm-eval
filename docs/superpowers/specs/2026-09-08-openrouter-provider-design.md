# OpenRouter provider route — design

2026-09-08. Adds a second provider behind the LiteLLM proxy so the matrix can
run Kimi K2.6 and Kimi K3 through OpenRouter, pinned to a named US upstream
(CoreWeave, Fireworks), instead of through AWS Bedrock. OpenRouter becomes the
default route. Bedrock stays selectable and untouched.

Revision 1. One design defect was found during review and is folded in
(§3: the resolved-params capture lived inside the wrapper being switched off).

## What it is

The harness's three-process topology (spec §2, `CLAUDE.md` "Three processes,
not one") does not change:

```
Claude Code --/v1/messages--> litellm proxy --openai/ provider--> <api_base>
```

Today the three candidate arms already reach bedrock-mantle through litellm's
`openai/` provider with a per-deployment `api_base`. OpenRouter is the same
provider class with a different `api_base`, `api_key`, and an `extra_body`
carrying OpenRouter's provider-routing preferences. The callback that writes
the wire log (`proxy_callback.py`) is registered the same way and fires on the
same litellm hook with the same kwargs, so every wire-derived field is produced
by the code that produces it today.

What has to move is the code that *assumes* AWS and fails before the first
call:

1. `proxy.proxy_environment` mints a mantle token and freezes SigV4
   credentials, and `SystemExit`s without both.
2. `litellm_patches._apply_openai_param_pins` rewrites every `openai/`
   deployment's outgoing params for the gemma mantle route: renames
   `max_tokens` → `max_completion_tokens` and pins `reasoning_effort: "none"`.
   Under OpenRouter's `require_parameters: true` the rename names a parameter
   no endpoint lists, which yields zero eligible providers; and the pin fights
   the per-arm `reasoning: {enabled: true}` this design sets.
3. `costs.PRICE_BOOK` does not know `kimi-k2-6` or `kimi-k3`, so
   `UnknownModelError` would zero every record's cost mid-parse.

Everything else is additive: four record fields, two classify reason codes, a
probe script, tests.

## Decisions taken during brainstorming

| Question | Decision | Why |
|---|---|---|
| Coexistence | Provider seam, `--provider {openrouter,bedrock}`, default `openrouter` | Bedrock config, tests and the 25 stored records stay interpretable. Env-driven selection is configuration reported as observation. |
| Arms | Kimi K2.6 (CoreWeave fp4) and Kimi K3 (Fireworks). No Sonnet. | Focus. Reference arm deferred; adding it later is one config entry. |
| Prompt caching | Required. Providers chosen for a listed `input_cache_read` rate; a probe proves it fires. | Bedrock bills no cache line for candidates. A listed price is not proof (the Bedrock lesson), so §7 measures. |
| Thinking | Configurable per arm via `extra_body.reasoning`, **default on** | K2.6 and K3 ship thinking-on; off is the setting nobody would deploy. Comparability with the Bedrock corpus is already gone (new models, new provider). Adapter round-trip is unverified, so §7 gates on it. |

## Provider data, measured 2026-09-08

Queried `GET https://openrouter.ai/api/v1/models/moonshotai/<model>/endpoints`.
US providers listing `tools` and a cache-read rate:

**moonshotai/kimi-k2.6** (ctx 262,144)

| tag | quant | max out | $/M in | $/M out | $/M cache read |
|---|---|---|---|---|---|
| `coreweave/fp4` | fp4 | 235,929 | 0.65 | 3.41 | 0.15 |
| `fireworks` | unknown | 235,929 | 0.95 | 4.00 | 0.16 |
| `crusoe/bf16` | bf16 | 235,929 | 0.70 | 3.50 | 0.35 |

**moonshotai/kimi-k3** (ctx 1,048,576)

| tag | quant | max out | $/M in | $/M out | $/M cache read |
|---|---|---|---|---|---|
| `fireworks` | unknown | 943,718 | 3.00 | 15.00 | 0.30 |
| `fireworks/us` | unknown | 943,718 | 3.30 | 16.50 | 0.33 |
| `baseten/fp8` | fp8 | 262,144 | 3.00 | 15.00 | 0.30 |

Facts that shape the config:

- CoreWeave does not serve K3.
- `fireworks/fast` lists **no `tools`**. `allow_fallbacks: false` plus an
  explicit `order` is what keeps the router from ever picking it.
- No endpoint lists `input_cache_write`. A cache write bills as plain input.
- K2.6 endpoints list `reasoning` but **not** `reasoning_effort`; K3 lists
  both. litellm derives `reasoning_effort` from Claude Code's
  `thinking: {"type": "adaptive"}`, so it must be dropped on every OpenRouter
  arm or `require_parameters` finds no provider for K2.6.
- Fireworks publishes no quantization for either model. `quantizations` is
  pinned only where the endpoint declares one.
- K3 input is $3.00/M against Sonnet 5's $2.00/M list. The "cheaper than
  Sonnet" question already has a partial answer for K3; the eval measures
  whether capability per dollar closes it.

## 1. Provider seam

**`run_matrix.py`, `smoke_test.py`**: new `--provider {openrouter,bedrock}`,
default `openrouter`. Selects the config file:

| provider | live config | offline config |
|---|---|---|
| `openrouter` | `litellm_config_openrouter.yaml` | `litellm_smoke_offline.yaml` (unchanged) |
| `bedrock` | `litellm_config.yaml` | `litellm_smoke_offline.yaml` |

**`proxy.proxy_environment(mode, provider)`**. Two branches, no shared code:

- `openrouter`: `load_env_file(ENV_FILE)`, read `OPENROUTER_API_KEY`;
  `SystemExit` with a one-line hint if empty. Returns exactly
  `{"OPENROUTER_API_KEY": …, "BAKEOFF_PROVIDER": "openrouter"}`. Never
  imports botocore or `scripts.smoke_bedrock` (`load_env_file` moves to a
  shared helper or is re-implemented in three lines; the test asserts on
  `sys.modules`). `BAKEOFF_PROVIDER` is what §3 reads inside the proxy.
- `bedrock`: today's body, verbatim, plus `BAKEOFF_PROVIDER=bedrock`.

**`proxy.credential_window(region, provider)`**: `openrouter` returns
`CredentialWindow(None, "openrouter-static")`. `credential_stop` already
returns `""` for a window with no expiry. `run_matrix.py`'s existing
"expiry unreadable … the abort streak is the only backstop" branch prints for
this source too; the wording is adjusted so a static key reads as "no expiry"
rather than "unreadable". `StreakTracker` is the backstop for a revoked or
exhausted key, as it already is for static AWS keys (`CLAUDE.md`, "That
refusal cannot fire on static keys").

**`.env.example`**: new section 0, `OPENROUTER_API_KEY=<paste-openrouter-key>`,
placed first because it is now the default route. Sections 1–2 unchanged.

## 2. `config/litellm_config_openrouter.yaml`

```yaml
model_list:
  - model_name: kimi-k2-6
    litellm_params:
      model: openai/moonshotai/kimi-k2.6
      api_base: https://openrouter.ai/api/v1
      api_key: os.environ/OPENROUTER_API_KEY
      # litellm derives reasoning_effort from Claude Code's thinking:adaptive.
      # K2.6 endpoints do not list it; under require_parameters that is zero
      # eligible providers. extra_body.reasoning is the knob instead.
      additional_drop_params: ["reasoning_effort"]
      temperature: 1.0
      top_p: 0.95
      extra_body:
        provider:
          order: ["coreweave"]
          allow_fallbacks: false
          require_parameters: true
          quantizations: ["fp4"]
        usage: {include: true}
        reasoning: {enabled: true}
  - model_name: kimi-k3
    litellm_params:
      model: openai/moonshotai/kimi-k3
      api_base: https://openrouter.ai/api/v1
      api_key: os.environ/OPENROUTER_API_KEY
      additional_drop_params: ["reasoning_effort"]
      temperature: 1.0
      top_p: 0.95
      extra_body:
        provider:
          order: ["fireworks"]        # or ["fireworks/us"]; §7 check 1 decides
          allow_fallbacks: false
          require_parameters: true
        usage: {include: true}
        reasoning: {enabled: true}

litellm_settings:      # identical to litellm_config.yaml
router_settings:       # identical: disable_cooldowns: true
general_settings:      # identical: num_retries: 3, request_timeout: 900
```

Why `openai/` and not litellm's `openrouter/` provider: `_apply_openai_param_pins`,
the resolved-params capture (§3), `REQUEST_KEYS`'s `max_completion_tokens`
comment, and `test_every_candidate_arm_uses_the_openai_provider_prefix` all
key on the `openai/` provider class. Switching class would move the seam the
whole proxy-side observability hangs from, for no gain: OpenRouter's API is
OpenAI Chat Completions.

`model_name` doubles as the `PRICE_BOOK` key (§5). Every `model_name` distinct,
as today.

Thinking off later is `reasoning: {enabled: false}` on the arm. No code.

## 3. Patches: split the wrapper

Found in review: `record_resolved_params` — the hand-off that fills `resolved`
and `sampling_source` in the wire log — is called from inside the same
`map_openai_params` wrapper that performs the two mantle rewrites
(`litellm_patches.py:610`). Switching that wrapper off under OpenRouter would
null `resolved` on every call and set `resolved_state` to `not_recorded`: the
exact observability loss the seam exists to avoid.

So the wrapper splits into two, applied in order to
`litellm.OpenAIConfig.map_openai_params`:

| id | applied when | does |
|---|---|---|
| `openai_max_completion_tokens_rename` | `BAKEOFF_PROVIDER != "openrouter"` | rename, as today |
| `openai_reasoning_effort_pinned_none` | `BAKEOFF_PROVIDER != "openrouter"` | pin, as today |
| `openai_resolved_params_capture` (new) | always | `record_resolved_params({"model": model, **mapped})`, rewrites nothing |

Capture wraps *outside* the rewrites so it records what goes out, which is the
whole point of `resolved`. The `in vars(cls)` guard, keyword forwarding, and
the behavioural probe in `apply()` stay; the probe asserts the rename/pin only
when they are live, and asserts capture always.

`apply()` returns the ids actually applied, so `Versions.litellm_patches`
lists six on a bedrock record and four on an OpenRouter record. §4's
`provider_route` says why.

The three Anthropic-adapter patches (`anthropic_tool_use_id_passthrough`,
`anthropic_tool_schema_property_names_strip`,
`anthropic_tool_use_id_collision_uniquify`) are provider-neutral and stay on.

`BAKEOFF_PROVIDER` is read once at `apply()` time from the proxy's own
environment. Absent means bedrock, so an old invocation path that never sets
it behaves exactly as before.

## 4. Observability additions

Every field below is measured from what the proxy saw, never from the flag.

- **`Versions.provider_route: str`** — `"openrouter"` | `"bedrock"` | `""`.
  Written into the adapter-patch manifest by the proxy (`write_manifest`) from
  `BAKEOFF_PROVIDER`, read back by `read_manifest` in the harness. Empty on
  records written before this schema and on runs with no manifest, which is
  already the "proxy never reported" state.
- **Wire `metadata.upstream_provider`, `metadata.native_finish_reason`** —
  OpenRouter returns top-level `provider` and `native_finish_reason` in every
  response body. litellm's `ModelResponse` is not guaranteed to preserve
  unknown top-level keys, so the callback reads them from
  `kwargs["original_response"]` (the raw provider JSON litellm passes every
  success callback) and falls back to `None`. Never falls back to the config's
  `order` value. Both projections (`proxy_callback._request` and
  `wire.BakeoffCallback._record`) grow the same keys.
- **`RunRecord.upstream_providers: list[str]`** — distinct values in order
  seen across the run's wire entries. A list, not a scalar: a second provider
  appearing mid-run is the fallback `allow_fallbacks: false` forbids, and a
  scalar would hide it. Same reasoning as `finish_reasons`.
- **`RunRecord.terminal_native_finish_reason: str | None`** — the last wire
  entry's `native_finish_reason`. Beside `terminal_finish_reason`, not
  replacing it: one is the route's word, the other the upstream's.
- **`RunRecord.cost_usd_provider: float | None`** — sum of `usage.cost` over
  the run's wire entries, present because `usage: {include: true}` asks for it.
  `None` when any entry lacks it. Sits beside the book's `cost_usd`; the gap
  is reported, never reconciled ("report the gap, never estimate it").
- **Reasoning tokens.** `TokenUsage.reasoning` exists and `costs.cost_usd`
  bills `output + reasoning`. On the OpenAI shape `completion_tokens` is
  *inclusive* of `completion_tokens_details.reasoning_tokens`. Whether
  litellm's Anthropic adapter emits `output_tokens` exclusive of reasoning
  decides whether that line double-bills. §7 check 5 measures it. If
  inclusive, `trajectory.py` subtracts at parse (the same shape as the cache
  subtraction `test_usage_accounting.py` already pins) and the new invariant
  is pinned there.

`SCHEMA_VERSION` 3.8.0 → 3.9.0.

## 5. Price book

Two entries keyed by `model_name`. The docstring names the endpoint, because
the rate is the endpoint's, not the model's, and a reader repricing must know
which upstream produced it.

| key | endpoint | in / out per 1M | cache_read mult | cache_write mult | 1h mult |
|---|---|---|---|---|---|
| `kimi-k2-6` | coreweave/fp4 | 0.65 / 3.41 | 0.15 / 0.65 = 0.2308 | 1.0 | 1.0 |
| `kimi-k3` | fireworks | 3.00 / 15.00 | 0.30 / 3.00 = 0.10 | 1.0 | 1.0 |

`cache_write` 1.0 is a **price**, not a placeholder: OpenRouter lists no write
surcharge for these endpoints, so a written prefix bills as plain input. The
1h tier does not exist on this route; 1.0 keeps `cost_usd` total at
`prompt_tokens × input_per_1m` when no cache read occurred.

`PRICING_BASIS` gains a segment: `+openrouter-2026-09-08`. The existing
`kimi-k2-5`, `gemma-4-31b`, `nemotron-3-super-120b`, `claude-sonnet-5*`
entries stay for the records that carry them.

If §7 check 1 selects `fireworks/us` for K3, the row becomes 3.30 / 16.50 /
0.10 before any matrix runs. The book is edited once, before spend, never
after.

## 6. Classify

- `AUTH_ERROR_SIGNATURES += ("no auth credentials found",)`. `"invalid api key"`
  is already present.
- Status ladder, after the auth check and the router-refusal check, before
  the 429/408/5xx branches:
  - `402` → `reason_code="api_credits"`, `INFRA_FAILURE`. OpenRouter's
    exhausted-balance status. Operator failure, not model.
  - `404` with `"no endpoints found"` in the last terminal message →
    `reason_code="router_no_endpoint"`, `INFRA_FAILURE`. What
    `require_parameters: true` or a wrong `order` slug produces. Today a 404
    falls through to `code = None` and the run scores as a model that did
    nothing.

`terminal_error_messages` is the trailing block of failed calls, as today.

## 7. Probe gate: `scripts/probe_openrouter.py`

Runs before the first matrix, spends cents, and its verdicts settle the values
this design leaves open (`fireworks` vs `fireworks/us`; whether
`output_tokens` is reasoning-inclusive). Two legs, because two different things
are under test.

**Leg A — straight at `https://openrouter.ai/api/v1/chat/completions`**
(litellm not in the loop; the provider is the thing under test):

1. **Provider pin.** For each arm, send with its configured `order` and
   assert response `provider` equals the expected name. Additionally send K3
   with `order: ["fireworks/us"]` and record whether the router honours the
   variant slug or 404s. Decides §2's K3 `order` value.
2. **`require_parameters` viability.** Send the exact parameter set Claude
   Code's request resolves to (`tools`, `tool_choice`, `temperature`, `top_p`,
   `max_tokens`, `stop`, `reasoning`) and assert 200, not 404.
3. **Cache fires.** A ~4,000-token shared prefix, sent twice 3 s apart, five
   replicates per arm; assert the second call's
   `usage.prompt_tokens_details.cached_tokens > 0` and that `usage.cost` is
   lower than the first call's. Report hit rate per arm, not one success.
   Also run once with the alternate provider (`crusoe` for K2.6, `baseten`
   for K3) so a zero on the chosen provider is a provider finding, not a
   probe finding.

**Leg B — through the proxy container** (litellm's Anthropic adapter is the
thing under test), Anthropic Messages shape as Claude Code sends it, with
`X-Bakeoff-Run-Id` set:

4. **Thinking round trip.** Turn 1 with one tool defined; expect `thinking`
   and `tool_use` blocks. Turn 2 re-sends both plus a `tool_result`; assert
   200 and a non-empty text block. A 400 here means litellm 1.95.0 drops
   `reasoning_content` on the echo and the arm must run thinking-off.
5. **Usage exclusivity.** For one Leg B call, compare the Anthropic-shaped
   `usage.output_tokens` and `reasoning_tokens` against OpenRouter's
   `completion_tokens` and `completion_tokens_details.reasoning_tokens` for
   the same call (read from the wire entry's `original_response`). Decides
   §4's subtraction.
6. **Callback capture.** The wire entry carries `upstream_provider`,
   `native_finish_reason`, `usage.cost`, `resolved` non-null with
   `resolved_state == "captured"`, and `unattributed.jsonl` is absent.

Output: `<scratch>/probe_openrouter/<check>.json` per check plus a printed
pass/fail table. Any failure prints `GATE INCOMPLETE` and exits 1, the same
wording `verify_logger.py` uses, so a weaker gate does not pass under the same
name.

The probe is not a unit test and is not in the default pytest run. It is the
OpenRouter counterpart of `scripts/probe_cache.py` and lives beside it.

## 8. Tests

- **`tests/test_config.py`**: parametrized over both config files. Bedrock-only
  pins (mantle bearer variable, runtime arms carry no `api_key`, gemma has no
  runtime entry, Converse tool-id allowlist) are guarded to
  `litellm_config.yaml`. New pins on `litellm_config_openrouter.yaml`: every
  arm has `api_base` under `https://openrouter.ai/`, `allow_fallbacks: false`,
  `require_parameters: true`, `usage.include: true`, `reasoning_effort` in
  `additional_drop_params`, a `reasoning` block, and a `PRICE_BOOK` entry.
  `test_no_deployment_targets_the_real_anthropic_api` runs on both.
- **`tests/test_proxy.py`**: `proxy_environment("live", "openrouter")` returns
  exactly two keys, never imports `botocore` or `scripts.smoke_bedrock`
  (asserted on `sys.modules` in a fresh subprocess), and `SystemExit`s with
  the hint when the key is absent; `credential_window(…, "openrouter")` has no
  expiry and `credential_stop` returns `""`; the bedrock branch is
  byte-for-byte today's behaviour (existing tests).
- **`tests/test_litellm_patches.py`**: with `BAKEOFF_PROVIDER=openrouter`,
  `apply()` returns the three adapter ids plus `openai_resolved_params_capture`
  and `map_openai_params` leaves `max_tokens` and `reasoning_effort` alone
  while `resolved_params(call_id)` is populated; without it, all six ids and
  today's behaviour. Run in a subprocess, since importing the module patches
  litellm (`CLAUDE.md`, "`litellm_patches` must never be imported from
  `bakeoff/`").
- **`tests/test_costs.py`**: both new keys price; `cache_read` on `kimi-k2-6`
  bills at 0.2308×; `input + cache_read + cache_write` with all multipliers 1.0
  equals `prompt_tokens × input_per_1m` (existing invariant, new keys).
- **`tests/test_classify.py`**: 402 → `api_credits`; 404 + "no endpoints
  found" → `router_no_endpoint`; a bare 404 still yields no exclusion.
- **`tests/test_proxy_callback.py`**, **`tests/test_runner.py`**: the new
  metadata keys are read from `original_response`, absent → `None`;
  `upstream_providers` collects distinct values in order; `cost_usd_provider`
  is `None` when any entry lacks `usage.cost`; `provider_route` round-trips
  through the manifest.
- **`scripts/mutation_check.py`**: one anchor per new guarantee (the
  `BAKEOFF_PROVIDER` gate, the capture-outside-rewrites order, the 402 and 404
  branches, the `None`-not-config fallback for `upstream_provider`).

## 9. Docs

- `CLAUDE.md`: every `run_matrix.py` / `smoke_test.py` command shows
  `--provider`; a new "OpenRouter gotchas" block records what §7 measured
  (whichever way each check went), in the same "measured YYYY-MM-DD" style as
  the Bedrock entries.
- `.env.example`: section 0.
- `TASKS.md`: the Bedrock corpus is non-comparable with OpenRouter records;
  thinking-on is the policy and its cost estimate; Sonnet reference arm on
  OpenRouter is deferred; the `fireworks` quantization is unpublished.

## Out of scope, on purpose

- Sonnet 5 via OpenRouter (one config entry later; needs the
  `test_no_deployment_targets_the_real_anthropic_api` pin reconsidered).
- Removing any Bedrock code, config or test.
- The grader and the judge: both read stored diffs and records, neither knows
  the provider.
- Region selection: OpenRouter exposes no region knob beyond provider variant
  slugs; `fireworks/us` is the only one on offer here.

## Testing this design

Unit suite green on both provider values. `verify_logger.py` (offline gate)
unchanged and green — it uses the offline stub config, which this design does
not touch. `probe_openrouter.py` all six checks passing, with its JSON outputs
attached to the `TASKS.md` entry, before `run_matrix.py --mode live` is
invoked on either arm.
