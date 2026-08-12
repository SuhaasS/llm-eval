# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An eval harness that scores agentic-coding models (Nemotron 3 Super 120B, Gemma 4 31B, Kimi K2.5, Claude Sonnet 5) by running the real Claude Code binary against pinned task repos, routed through a self-hosted LiteLLM proxy to AWS Bedrock.

The primary deliverable is **an immutable event log**, not a score. Scores are derived views computed offline over that log. The harness measures and records; it never grades, and it never decides.

All code lives under `bakeoff/`. Everything is run from that directory with its venv interpreter.

## Commands

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -v
```

Single test:

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_runner.py::test_name -v
```

Integration tests are opt-in (`addopts = "-m 'not integration'"`) and need a Docker daemon. On macOS they also need `--basetemp` under `$HOME` — the Docker VM mounts `$HOME` but not `/var/folders`, and a repo bind-mounted from there appears inside the container as a **silently empty directory**, so snapshot tests compare nothing against nothing and pass:

```bash
cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
```

The §6.6 gate — run before collecting any data. Offline, no credentials, no spend. Runs the unit suite, the integration suite, the dry run, and the offline smoke test. Reports `GATE INCOMPLETE` and exits 1 without a Docker daemon rather than passing a weaker gate under the same name:

```bash
cd bakeoff && .venv/bin/python scripts/verify_logger.py
```

Mutation check — reverts each guarantee and confirms a test goes red. Fails loudly on a stale anchor:

```bash
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

Whole orchestrator against a stand-in agent, no model calls:

```bash
cd bakeoff && .venv/bin/python scripts/dry_run.py
```

Images (build both before any smoke or real run):

```bash
cd bakeoff && docker build -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent . && docker build -f docker/litellm-proxy.Dockerfile -t bakeoff-litellm .
```

Phase 0c smoke — offline half needs nothing; live half needs credentials and spends money:

```bash
cd bakeoff && .venv/bin/python scripts/smoke_test.py --mode offline
```

```bash
cd bakeoff && .venv/bin/python scripts/smoke_bedrock.py && .venv/bin/python scripts/smoke_test.py --mode live
```

## Architecture

`execute_run` in [runner.py](bakeoff/src/bakeoff/runner.py) orchestrates one run and writes exactly one record. Everything else is a component it composes.

**Three processes, not one.** The harness process spawns a container; the agent runs *inside* that container; the agent's model calls go to the LiteLLM proxy, which is a *third* process in its own container. This is the single most load-bearing fact in the codebase:

- The agent's container joins an `internal=True` Docker network, which by construction has no host route. `127.0.0.1:4000` is unreachable by design — the proxy must be a peer on that network, addressed as `http://litellm:4000`.
- Wire logging therefore lives in [proxy_callback.py](bakeoff/src/bakeoff/proxy_callback.py), registered by dotted path in the proxy config. The in-process [wire.py](bakeoff/src/bakeoff/wire.py) callback observes nothing on a real run. Without `proxy_wire_dir` passed to `execute_run`, `sampling`, `system_prompt_sha`, `tool_schema_sha` and the API error status come back empty — silently, with the record still well-formed.
- Calls are attributed by `X-Bakeoff-Run-Id`, stamped via `ANTHROPIC_CUSTOM_HEADERS`. An unattributed call goes to `unattributed.jsonl` rather than being guessed at; any line there is a gate failure.

**Data flow.** container ([container.py](bakeoff/src/bakeoff/container.py), digest-pinned, repo detached at `base_sha`) → agent subprocess ([claude_runner.py](bakeoff/src/bakeoff/claude_runner.py), stream-json on stdout) → per-turn diffs ([checkpoints.py](bakeoff/src/bakeoff/checkpoints.py)) → transcript parse ([trajectory.py](bakeoff/src/bakeoff/trajectory.py)) + pricing ([costs.py](bakeoff/src/bakeoff/costs.py)) + scanning ([scanners.py](bakeoff/src/bakeoff/scanners.py)) + classification ([classify.py](bakeoff/src/bakeoff/classify.py)) → `assemble_record` → [eventlog.py](bakeoff/src/bakeoff/eventlog.py).

## Invariants

These are enforced in code and asserted by tests. Breaking one is usually silent, which is why they are listed.

- **A run always produces a record.** Nothing between run start and write may raise past `execute_run`. The tokens are already paid for; a lost record cannot be re-derived at any price. A crash yields a partial-but-valid record, and checkpoints captured so far survive (the recorder is held outside the `try`). The write is the one thing allowed to raise, and `_write_or_strand` makes it survivable: the record lands in `artifacts/record.unwritten.json` **before** the exception escapes, so the failure is loud and the data is not gone. A caller that quietly continued would compute means over a matrix with a hole in it.
- **The event log is append-only.** No update, no delete API. Records open with mode `"x"`; the index is appended only after the record is durably fsynced and atomically renamed. Excluded runs keep their records.
- **The harness does not grade.** `tests_passed` and `Checkpoint.tests_pass` stay `None`; `outcome` never becomes `RESOLVED` at harness time. Grading is an offline batch over stored diffs (~96k suite executions inline otherwise). Passing `False` instead of `None` would stamp `FALSE_SUCCESS` — an accusation of dishonesty — onto every well-behaved run, permanently.
- **Configuration is never reported as observation.** `sampling`, prompt and tool hashes, and `bedrock_model_id` come from the wire log (what was sent / what answered), not from the config file (what was asked for). `isolated` is the one exception and is known: it is `bool(network)`, so `True` asserts a property nothing verified.
- **Absence is recorded, never implied.** `isolated=False` when no network was given, `harness_commit` carries `-dirty`, `task_set_commit` stays `""` by design, `trajectory_parse_error` is non-empty when derived fields are zero because the transcript could not be read — *including when there was no transcript at all*, which used to leave the field empty and made total loss read as a quiet run. `_sha256` returns `""` for an absent field rather than the digest of the four bytes `"null"`, and `artifacts.wire_log_gz` is `null` rather than a path to a file that was never opened. Zeros in a record do not mean a quiet run.
- **The record says how much of the run it could see.** `wire_entries_seen` and `wire_unattributed` sit beside the fields derived from the wire, because `sampling={}` plus empty hashes is what a run looks like when *every* call lost its `X-Bakeoff-Run-Id` — and that gate lived only in `smoke_test.py`, which does not run during an eval. `wire_unattributed` is `int | None`: `None` means no wire directory was configured and nobody counted; `0` is a measurement, and it is what licenses trusting the wire-derived fields.
- **The agent must be able to check its own work.** Spec §3.3 measures a loop that ends in "runs tests, sees failures, self-corrects". A task image with no test runner truncates it after "edits" and scores every arm on one unverified guess — which is what happened: the image shipped without `pytest` through all of Phase 0c, `tests/test_calc.py` imports from the repo root, and the only available command raised `ModuleNotFoundError` whether or not the bug was fixed. `smoke_test.assert_agent_can_verify_its_work` is a precondition, not a run criterion, because by the time a record exists the tokens are spent.
- **Silence is the enemy.** Prefer a loud failure over a plausible-looking zero. `container._checked_exec` exists because a failed `git diff` returns empty output byte-identical to a clean tree.

## Config gotchas that have already cost a debugging session

- **`model_name` in `litellm_config.yaml` doubles as the `PRICE_BOOK` key** in `costs.py`. A name the price book does not know raises `UnknownModelError` mid-parse; `assemble_record` catches it, and the record survives with turns, tokens and cost all **zero**.
- **Every `model_name` must be distinct.** LiteLLM load-balances across repeated names, which would randomize transport per call.
- **The mantle token is carried as `BAKEOFF_MANTLE_TOKEN`, never `AWS_BEARER_TOKEN_BEDROCK`.** One proxy serves both transports, and the env var name is what keeps them apart. `base_aws_llm.get_request_headers` uses a deployment's `api_key` when set, falls back to `AWS_BEARER_TOKEN_BEDROCK`, and signs SigV4 only when both are absent — so a proxy holding the AWS-named variable bearer-authenticates all three `bedrock/` arms and fails them with `bedrock:CallWithBearerToken`, while the mantle arms stay green and it reads as a bedrock-runtime problem.
- **Sonnet 5 runs on `bedrock/` (SigV4), the candidates on the mantle passthrough.** Not cosmetic: the passthrough derives `anthropic-beta` *headers* from `context_management`/`output_config` and mantle rejects them, while the runtime route carries beta values as a top-level `anthropic_beta` *body* field. `additional_drop_params` removes the parameters and never the header. The asymmetry is a §6.4 confound on the reference arm and is tracked in `TASKS.md`.
- **Gemma's `/openai/v1` route enforces OpenAI's reasoning-model contract, and three of its rules bite.** `max_tokens` is rejected (send `max_completion_tokens`), any non-default `top_p` is rejected, and `tools` are refused unless `reasoning_effort` is **explicitly** `"none"` — absent is not `none`, because the route supplies its own default. The first 400 hides the other two, which is how one wall was mistaken for the whole problem. The cap rename and the `reasoning_effort` pin are the fourth and fifth `litellm_patches` interventions, both wrapping `litellm.OpenAIConfig.map_openai_params` and both applied to all three candidates so no arm diverges (§5.4). They cannot live in config: Claude Code sends `thinking: {"type": "adaptive"}` on every arm and litellm derives an effort from it that **beats** a deployment's own `reasoning_effort`. `top_p` is the exception and *is* config — gemma omits it exactly as Sonnet does, and that is a §5.3 divergence to publish, not to level away by stripping `top_p` from nemotron and kimi too.
- **`OpenAIConfig` is not an `OpenAIGPTConfig`.** `get_optional_params` sends `custom_llm_provider == "openai"` to `litellm.OpenAIConfig`, which branches on o-series/gpt-5/audio *before* delegating to the module-level `openAIGPTConfig` — so patching the inner class is bypassed the moment litellm reclassifies a model. Patch the outer entry and post-process its result; that also puts the change after `_check_valid_arg` and after `additional_drop_params`, which is what lets `reasoning_effort` through a guard that is **litellm's, not AWS's**. Guard with `in vars(cls)`, never `hasattr`: `BaseConfig` declares the method abstractly, so `hasattr` stays true after the override is deleted and the wrapper wraps a stub returning `None`. And forward by keyword with the exact parameter names — litellm calls it with keywords, and a renamed signature raises `TypeError` that litellm re-wraps as `APIConnectionError`.
- **The wire log records the params that get dropped, precisely because they get dropped.** `thinking`, `reasoning_effort`, `context_management`, `output_config`, `anthropic_beta` and `stream` are in the projection since schema 3.1.0; `max_completion_tokens` joined them in 3.2.0, because `pick()` falls back to Claude Code's raw body and would otherwise report a `max_tokens` the wire never carried. The drops are *per arm* — `reasoning_effort` is listed on each candidate deployment and on neither Sonnet one — so whether a drop reached a given route is a §6.4 question about the arms being compared, and for 856 stored calls the log could not answer it. Measured since: Claude Code sends `thinking: {"type": "adaptive"}` and `output_config: {"effort": "high"}` **identically on every arm**, and no arm returned a single reasoning token. `pick()` prefers `optional_params` over the raw body, so on a live `openai/` deployment this field shows the *post-drop* state — which is what settles it. Keep the two projections (`proxy_callback._request`, `wire.BakeoffCallback._record`) key-for-key identical; a field in one and not the other reads as "not sent on this arm".
- **`bedrock/` is two routes, chosen by model id, not by config.** Claude models resolve to `AmazonAnthropicClaudeMessagesConfig` and take **Invoke** with a native Anthropic body — no `toolUseId` is ever built, which is why the tool-id passthrough cannot reach `claude-sonnet-5-runtime`. Every other model falls through to the openai→**Converse** bridge, which copies the model's own tool-call ids into `toolUseId` under a `[a-zA-Z0-9_-]{1,64}` class Bedrock enforces server-side. `test_config.py` pins that a Converse-bound arm is allowlisted with a measured id shape; `kimi-k2-5-runtime` is commented out because `functions.Read:0` fails it.
- **One API call is one turn, and Claude Code does not write it that way.** It emits one transcript record per **content block**, each repeating the whole `usage`, so counting `type: "assistant"` records doubled `turns_used`, `tokens` and `cost_usd` in every record written before schema 3.0.0 — measured, a 5-call run recorded as 6 turns with `cache_write` 83,867 against a true 42,171. `parse_trajectory` dedupes on `message.id`; `assistant_records` and `turns_streamed` sit beside `turns_used` so the collapse stays readable, and the wire log is the independent authority (one line per POST). A stub that reuses a `message.id` across calls makes the parser merge distinct calls — that is a fixture bug, not a parser one.
- **The inter-run cache carryover is a harness artifact, not practical use.** Every run is a fresh container, a wiped `CLAUDE_CONFIG_DIR`, a fresh repo and a one-shot `claude -p`; nothing crosses runs but Bedrock's server-side cache, and repeats are scheduled 3–5 s apart inside a 300 s TTL. What carries over is 30,506 tokens of **task-independent tool schemas** (the `tools` block is byte-identical across repeats). So `first_task` and `warm_followup` are two deployment situations, not a number and its correction — do not call either normalized. `--interleave` redistributes the write cost; a round takes 1.4–3.9 min, inside the TTL, so it cannot produce a cold run. `cache_state.seconds_since_prior_run` is what tells a reader which situation a run was in.
- **`cache_state.warm` is turn-1 `cache_read`, never the run total.** Bedrock's cache warms on turn 2 of a *single* run, so a run-total `cache_read > 0` is true of nearly every Sonnet run and separates nothing; turn 1 cannot read what this run wrote. On both 2026-08-11 N=3 sets, turn-1 `cache_read` alone separates the $0.547 run from the $0.176 ones. `None` is undetermined, not cold — and there are three ways to get there, because `false` is a *claim*: no turn parsed; a turn-1 record with no `usage` block (all-zero tokens are byte-identical to a genuine zero); or a run that reported no cache accounting anywhere, which is every Gemma and Nemotron run and is why schema 2.1.0's fix still left three arms lying. Never inferred from elapsed time, since Sonnet 5's TTL is undocumented and cross-region inference can force a cache write.
- **Sonnet 5 is priced at Anthropic's $2/$10 list; the candidates at the Bedrock page.** The eval asks whether a candidate beats buying Sonnet, not whether one AWS SKU beats another. `Versions.pricing_basis` says which book produced a `cost_usd` — the log is append-only, so pre-2026-08-11 records keep figures from the old $3/$15 book and a reader summing across the change gets a number that is not a price of anything. Cache multipliers are 0.10× read / 1.25× write at 5m / **2.00× at 1h**; the 1h tier is a *part* of `cache_write`, never an addition, and an untiered write takes the 5m rate.
- **`input` excludes cache tokens, on every route, and `cost_usd` adds all three.** Bedrock: *"total input tokens = inputTokens + cacheReadInputTokens + cacheWriteInputTokens"*. The candidate arms only preserve that because LiteLLM's Converse transform folds cache into `prompt_tokens` (OpenAI usage is inclusive) and the anthropic adapter subtracts it back out. Drop the subtraction and a warm Sonnet turn double-counts ~30k of a ~34k prompt — silently. Pinned in `tests/test_usage_accounting.py`.
- **Temperature is per-model on purpose** (Sonnet 5 400s on non-default sampling; Kimi stalls at 0). `config_digest` deliberately excludes it — the thing held identical across arms is the policy, not the number.
- **`CLAUDE_CONFIG_DIR`, not `--settings`, is what detaches the operator's config.** `--settings` merges *on top of* `~/.claude/settings.json`. Eval runs point `CLAUDE_CONFIG_DIR` at an empty per-run directory, so they never load your settings, hooks, skills or plugins — your own sessions are unaffected.
- **Env is an allowlist, not a denylist.** `CLAUDE_CODE_USE_BEDROCK`/`_USE_VERTEX` would make the CLI ignore `ANTHROPIC_BASE_URL`, bypass the proxy, and leave the mandatory wire log empty while the run looks normal.
- **The config dir must not live under `/repo`** — `git add -A` would sweep the whole config tree and every transcript into each checkpoint diff.
- **The agent image runs as non-root.** Claude Code refuses `bypassPermissions` under root and exits before emitting a single event.
- **LiteLLM callbacks must be registered as an instance of `CustomLogger`.** LiteLLM dispatches on `isinstance`; a dotted path resolving to a class, or a duck-typed object, is skipped in silence.
- **`litellm_patches` must never be imported from `bakeoff/`.** Importing it applies its patches — `instance` is built at module scope because `get_instance_fn` resolves the configured dotted path with `getattr`. The proxy imports it via `litellm_settings.callbacks`; the harness must not, or every harness process and every pytest run gets a patched litellm. `runner.py` imports `proxy_callback` at module level, which is why the manifest read/write lives there and not in the patch module.
- **A pricing failure must not reach `parse_trajectory`'s caller.** `cost_usd` raises by design, but the call site catches per turn and records `pricing_error`. Letting it escape aborts the parse, and `assemble_record` then discards the whole trajectory — turns, tokens, tool calls and destructive events all zero for a run that worked. `cost_usd` is `float | None`: `None` is "price unknown", `0.0` is a genuine zero, and the two are not interchangeable.
- **`mock_response` deployments short-circuit before the streaming wrapper**, so the success callback never fires and capture silently misses the call. Every real call is streaming — hence `fixtures/anthropic_stub.py`, a real streaming endpoint, for the offline smoke test.

## Conventions

- **Module and function docstrings carry the *why* and the failure mode**, cross-referenced to spec sections (`spec section 5.1`, `OPEN-10`). This is the dominant style — a change that alters an invariant should update the prose that explains it, and a comment that says what a line does rather than what breaks without it does not fit here.
- Claims about external behavior are annotated with what they were verified against (`Verified against claude 2.1.220`, `litellm 1.95.0`). Do not assert new ones without checking.
- `SCHEMA_VERSION` in [schema.py](bakeoff/src/bakeoff/schema.py) moves even for additive fields — a reader that cannot tell versions apart reads an absent field as a positive negative claim.

## Docs

| File | Role |
|---|---|
| [TASKS.md](TASKS.md) | **The backlog.** Open work only, P0–P3. |
| [tasks/todo.md](tasks/todo.md) | Completed-work review log, one section per finished task. Not a backlog. |
| [specs/2026-08-03-llm-bakeoff-eval-design.md](docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md) | The spec every `section N.N` reference in the code points at. |
| [plans/2026-08-04-bakeoff-harness-logging.md](docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md) | Implementation plan, Tasks 1–12. |

Current state: Tasks 1–11 complete, Task 12's offline half done. See `TASKS.md` for the live half — it is the current record, and it moves faster than this line.

**Read every Phase 0c capability figure with this caveat: none of them were taken in an environment where the agent could run tests.** The eval image shipped no `pytest` and the fixture was not importable, so the only verification command available raised `ModuleNotFoundError` with the bug fixed and unfixed alike. Fixed 2026-08-12; every arm should be re-measured before a number is published. Four of the "model failures" found so far turned out to be adapter or environment defects with one-line causes — weigh that before reading the next one as capability.
