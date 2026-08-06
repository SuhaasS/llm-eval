# Pindrop LLM Bakeoff Eval

Internal eval harness for scoring agentic-coding models (Nemotron 3 Super 120B, Gemma 4 31B, Kimi K2.5, Claude Sonnet 5) on Pindrop's own repos, routed through a self-hosted LiteLLM proxy against AWS Bedrock.

The eval measures; it does not decide. Sonnet 5 is an arm, not the answer key.

## Docs

| Doc | What it is |
|---|---|
| [Model_Bakeoff_Plan.md](docs/Model_Bakeoff_Plan.md) | Candidate comparison, cost analysis, LiteLLM vs. OpenRouter, timeline |
| [specs/2026-08-03-llm-bakeoff-eval-design.md](docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md) | Eval design spec — data pipeline, scoring, success criteria |
| [plans/2026-08-04-bakeoff-harness-logging.md](docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md) | Implementation plan, Tasks 1–12 |

## Primary deliverable

A complete, immutable event log of every run. Scores are derived views over that log — re-running 2,000 agentic sessions is expensive, re-scoring a preserved log is free.

## Planned layout

```
bakeoff/
├── pyproject.toml
├── src/bakeoff/
│   ├── schema.py         # run record dataclasses; SCHEMA_VERSION
│   ├── eventlog.py       # append-only writer/reader; immutability enforcement
│   ├── costs.py          # PriceBook: TokenUsage -> USD
│   ├── trajectory.py     # Claude Code JSONL -> turns, tool calls, usage
│   ├── container.py      # Docker lifecycle, git pinning, diff extraction
│   ├── checkpoints.py    # per-turn diff capture
│   ├── scanners.py       # destructive-command and secret scanning
│   ├── wire.py           # LiteLLM callback -> wire log
│   ├── claude_runner.py  # Claude Code subprocess with controlled config
│   ├── classify.py       # failure_class and exclusion classification
│   └── runner.py         # orchestrates one run end-to-end
└── tests/
```

## Running the tests

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -v
```

Integration tests are opt-in — they need a Docker daemon, and on macOS they need `--basetemp` under `$HOME`, because the Docker VM mounts `$HOME` but not `/var/folders` and a repo mounted from there appears inside the container as a silently empty directory:

```bash
cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
```

Status: Tasks 1–11 complete (schema, event log, pricing, trajectory parser, scanners, container, checkpoints, wire logging, classification, Claude Code runner, run orchestrator, fault-injection gate). 166 unit + 25 integration tests passing, 89% coverage.

### The gate

Spec §6.6 requires the logging layer to be fault-injected before it is trusted with 2,400 runs. Run this before collecting any data:

```bash
cd bakeoff && .venv/bin/python scripts/verify_logger.py
```

All twelve §6.6 cases are injected offline — no credentials, no spend. Throttles come from a real LiteLLM proxy whose `mock_response` raises a genuine `RateLimitError`. Without a Docker daemon the gate reports `GATE INCOMPLETE` and exits 1 rather than passing: the mid-run kill, the proxy-side wire log, and live checkpoint capture are only observable against a real daemon.

A passing suite is not by itself evidence the suite would notice a regression, so each guarantee is checked by removing it and confirming a test goes red:

```bash
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

Sixteen mutations, all caught. It fails loudly on a stale anchor — a mutation harness that quietly stops mutating reports a clean sweep while testing nothing.

## Running a run

The agent executes **inside** the pinned container, on an internal Docker network whose only reachable endpoint is the LiteLLM proxy — spec §5.1's "network off, or through a recording proxy". That has one operational consequence: **the proxy must run as a container on that network**, not as a host process. An internal network has no host route, so `127.0.0.1:4000` is unreachable by design; the agent reaches `http://litellm:4000` through Docker's embedded DNS.

Build the reference agent image first (it pins `claude` by version and fails the build on drift):

```bash
cd bakeoff && docker build -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent .
```

`execute_run` without a `network` still runs, but records `isolated=False` — §5.1 did not hold for the process under test, and the record says so rather than letting the pinned image digest imply otherwise.

**Wire logging happens inside the proxy, not in the harness.** The harness process makes no model calls — the agent does, through the proxy — so a callback registered on `litellm.callbacks` here observes nothing. Build the proxy image, mount `src/` and a wire directory into it, and pass that directory to `execute_run` as `proxy_wire_dir`; without it `sampling`, `system_prompt_sha`, `tool_schema_sha` and the API error status come back empty:

```bash
cd bakeoff && docker build -f docker/litellm-proxy.Dockerfile -t bakeoff-litellm .
```

Each request carries `X-Bakeoff-Run-Id` via `ANTHROPIC_CUSTOM_HEADERS`, which is how the proxy attributes a call to a run. A call that arrives without it is written to `unattributed.jsonl` rather than guessed at, and the gate fails on any such line.

Bedrock model IDs and per-arm sampling in [config/litellm_config.yaml](bakeoff/config/litellm_config.yaml) are verified against the AWS model cards and each lab's published guidance. Routing, auth, and whether the proxy actually applies that sampling are not — Phase 0c's smoke test is the gate, and it reads the applied values back from the wire log rather than from the config file.

Eval runs are pinned to `claude 2.1.220` and launched with `CLAUDE_CONFIG_DIR` pointed at an empty per-run directory, so they never load your `~/.claude` settings, hooks, skills, or plugins. Your own Claude Code sessions are unaffected.
