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

- **A run always produces a record.** Nothing between run start and write may raise past `execute_run`. The tokens are already paid for; a lost record cannot be re-derived at any price. A crash yields a partial-but-valid record, and checkpoints captured so far survive (the recorder is held outside the `try`).
- **The event log is append-only.** No update, no delete API. Records open with mode `"x"`; the index is appended only after the record is durably fsynced and atomically renamed. Excluded runs keep their records.
- **The harness does not grade.** `tests_passed` and `Checkpoint.tests_pass` stay `None`; `outcome` never becomes `RESOLVED` at harness time. Grading is an offline batch over stored diffs (~96k suite executions inline otherwise). Passing `False` instead of `None` would stamp `FALSE_SUCCESS` — an accusation of dishonesty — onto every well-behaved run, permanently.
- **Configuration is never reported as observation.** `sampling`, prompt and tool hashes, and `bedrock_model_id` come from the wire log (what was sent / what answered), not from the config file (what was asked for).
- **Absence is recorded, never implied.** `isolated=False` when no network was given, `harness_commit` carries `-dirty`, `task_set_commit` stays `""` by design, `trajectory_parse_error` is non-empty when derived fields are zero because parsing failed. Zeros in a record do not mean a quiet run.
- **Silence is the enemy.** Prefer a loud failure over a plausible-looking zero. `container._checked_exec` exists because a failed `git diff` returns empty output byte-identical to a clean tree.

## Config gotchas that have already cost a debugging session

- **`model_name` in `litellm_config.yaml` doubles as the `PRICE_BOOK` key** in `costs.py`. A name the price book does not know raises `UnknownModelError` mid-parse; `assemble_record` catches it, and the record survives with turns, tokens and cost all **zero**.
- **Every `model_name` must be distinct.** LiteLLM load-balances across repeated names, which would randomize transport per call.
- **The mantle token is carried as `BAKEOFF_MANTLE_TOKEN`, never `AWS_BEARER_TOKEN_BEDROCK`.** One proxy serves both transports, and the env var name is what keeps them apart. `base_aws_llm.get_request_headers` uses a deployment's `api_key` when set, falls back to `AWS_BEARER_TOKEN_BEDROCK`, and signs SigV4 only when both are absent — so a proxy holding the AWS-named variable bearer-authenticates all three `bedrock/` arms and fails them with `bedrock:CallWithBearerToken`, while the mantle arms stay green and it reads as a bedrock-runtime problem.
- **Sonnet 5 runs on `bedrock/` (SigV4 Converse), the candidates on the mantle passthrough.** Not cosmetic: the passthrough derives `anthropic-beta` *headers* from `context_management`/`output_config` and mantle rejects them, while Converse carries beta values as an `additionalModelRequestFields.anthropic_beta` *body* field. `additional_drop_params` removes the parameters and never the header. The asymmetry is a §6.4 confound on the reference arm and is tracked in `TASKS.md`.
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

Current state: Tasks 1–11 complete, Task 12's offline half done. The live half now runs at N=3 per arm, with **3 of 4 arms passing 3/3** — Sonnet 5 (on bedrock-runtime), Nemotron and Kimi K2.5. Gemma alone still fails, 9/9 identically, and is the P0 item in `TASKS.md`. Two of the three original "model failures" turned out to be adapter defects with one-line causes; weigh that before reading Gemma's as capability.
