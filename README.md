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

Status: Tasks 1–7 complete (schema, event log, pricing, trajectory parser, scanners, container, checkpoints, wire logging). 59 unit + 9 integration tests passing.

Bedrock model IDs in [config/litellm_config.yaml](bakeoff/config/litellm_config.yaml) are verified against the AWS model cards; routing and auth are not — Phase 0c's smoke test is the gate.
