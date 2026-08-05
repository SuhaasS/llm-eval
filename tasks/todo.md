# Bakeoff Harness — Task Progress

Source plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](../docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

- [x] **Task 1** — Schema and event log
- [x] **Task 2** — Cost calculation
- [x] **Task 3** — Trajectory parser
- [x] **Task 4** — Destructive-command and secret scanners
- [ ] Task 5 — Container lifecycle and git pinning (`container.py`)
- [ ] Task 6 — Checkpoint capture (`checkpoints.py`)
- [ ] Task 7 — Wire-level logging (`wire.py`)
- [ ] Task 8 — Failure and exclusion classification (`classify.py`)
- [ ] Task 9 — Claude Code runner (`claude_runner.py`)
- [ ] Task 10 — Run orchestrator (`runner.py`)
- [ ] Task 11 — Fault-injection gate
- [ ] Task 12 — End-to-end smoke test

---

## Review — Task 4 (2026-08-05)

**Delivered:** `scanners.py` — `scan_destructive()`, `scan_secrets()`. 11 tests (10 from the plan doc + 1 characterization), 41 passing suite-wide. TDD order held. Logic implemented verbatim; all 10 plan-doc tests were traced by hand first and pass as written.

**Checked and cleared — `scan_secrets` flags but does not redact.** Looked like a leak; it isn't. Spec §6.2 requires wire logs to persist "the full request and response payload" *and* be "secret-scanned on write" — without the full payload, `malformed: true` is a dead end and the adapter-failure-vs-drop-the-model call can't be made. Flag-alongside is the specified behavior. (§3.6's stricter privacy gate covers team transcript *collection*, which this plan puts out of scope.) Noted in the module docstring so the next reader doesn't re-litigate it.

**Real gap found — revert status is never resolved.** The code hardcodes `reverted_by_agent=False` / `affected_outcome=False` under a comment claiming Task 8 fills them in. Nothing does: `RunSignals` carries no destructive fields, and Task 10 passes `scan_destructive` output straight into `assemble_record`.

Consequence: severity is permanently HIGH and spec OPEN-10's MEDIUM tier ("reverted by the agent, or contained") is unreachable. A model that deletes a file and immediately restores it scores identically to one that leaves it deleted — inflating a safety metric with weight on the recommendation.

Not fixable here: revert detection needs file state (Task 6 checkpoint diffs), and this scanner sees only the bash command stream. Inferring it from bash alone would miss restores done via the Write tool and produce false MEDIUM downgrades — under-reporting a safety event is worse than over-reporting one, so conservative HIGH stays. Comment corrected to state the real situation; `test_revert_status_is_unresolved_at_scan_time` pins the behavior so it breaks visibly when a later stage closes the gap. Plan doc annotated, **owner is Task 10.**

**Coverage probed beyond the suite.** Confirmed no false positive on `git push --follow-tags` (the `-f\b` boundary holds) and that `node_modules` is correctly suppressed. Known misses, all inherent to regex scanning and acceptable under the conservative-detection design:

| Missed | Why |
|---|---|
| `rm --recursive src/` | flag regex requires short-form `-r`/`-R` |
| `npm install lodash@4.17.0` | `@version` syntax not matched by the `[<=]=?` constraint pattern |
| `find . -delete`, `truncate -s 0 <test>` | deletion paths that don't route through `rm` |

One known false positive: `pip install requests==2.31.0` flags as `DEP_DOWNGRADE` — an exact pin isn't a downgrade. Acceptable per the module's stated stance that a false positive costs a human glance.

---

## Review — Task 3 (2026-08-05)

**Delivered:** `trajectory.py` — `ParsedTrajectory`, `parse_trajectory()`, `EDIT_TOOLS`. 12 tests (10 from the plan doc + 2 new), 30 passing suite-wide. TDD order held.

**Timing correction — the substantive deviation.** As drafted, `inference_ms` was the gap between *consecutive assistant records*, folding tool-execution time into inference and leaving turn 1 at zero; `tool_exec_ms` was hardcoded 0, and Task 10 derived it as `wall_clock_ms - inference_ms` — a residual, not a measurement.

Spec §6.1 defines the two distinctly ("inference_ms — model generating, the real speed difference"; "tool_exec_ms — test runs, builds") and latency p95 ≤ 2× Sonnet 5 is a stated success criterion (§10). A split that can't distinguish a slow model from a slow test run can't support that criterion.

Tool-result records carry timestamps, so the real split was already in the transcript. Now: an assistant turn's inference is the gap since the record that unblocked it; a tool result's gap since its assistant record is that turn's tool execution.

| against the fixture | inference | tool_exec | sum vs 30000ms span |
|---|---|---|---|
| as drafted | `[0, 7000, 8000, 10000]` | `[0, 0, 0, 0]` | 25000 — 5s unaccounted |
| corrected | `[5000, 6000, 7000, 5000]` | `[1000, 1000, 5000, 0]` | 30000 exactly |

Verified by reproducing the original logic against the fixture and confirming both new tests fail on it. Plan doc's Task 3 code block and test block corrected in the same commit; both now diff clean against the implementation.

**Follow-ups found while tracing downstream — not fixed here:**

1. **Task 10 — a candidate returning cache tokens loses the whole run.** `assemble_record` calls `parse_trajectory` unguarded, and `cost_usd` raises on cache tokens from a model whose cache support is unconfirmed (Task 2's guard). The exception propagates and no run record is written, against the "complete event log of every run" primary deliverable. Fail-fast is defensible during Phase 0b — which exists precisely to resolve candidate cache support before the full run — but the real run needs a defensive call site. Belongs to Task 10.
2. **Task 7 — true inference latency is captured and discarded.** `BakeoffCallback.log_success_event` receives `start_time`/`end_time` from LiteLLM and uses neither. That's wire-level ground truth for generation time and would cross-check the trajectory-derived numbers. Belongs to Task 7.

**Known, by design:** `ToolCallStats.malformed` stays 0 — the plan's self-review notes it comes from the wire log, since Claude Code's transcript doesn't record parse failures.

---

## Review — Task 2 (2026-08-05)

**Delivered:** `costs.py` — `ModelPricing`, `PRICE_BOOK` (4 models), `cost_usd()`, `UnknownModelError`. 9 tests, 18 passing suite-wide. TDD order held; failing run observed before implementation.

**Price correction — the one substantive deviation.** All four models were re-verified against the AWS Bedrock pricing page before implementing. Three matched the plan doc. **Kimi K2.5 output was wrong: $3.00/1M, not $2.50** — the plan doc took the optimistic end of `Model_Bakeoff_Plan.md`'s "$2.50–3.00" range. Implemented at $3.00; test expectation moved $3.10 → $3.60; the plan doc's Task 2 code block and Global Constraints line both corrected, with a dated note explaining why.

Understating a candidate's price biases the headline metric toward that candidate, which is exactly the failure this eval exists to avoid.

| Model | Plan doc | AWS Bedrock, US regions | |
|---|---|---|---|
| claude-sonnet-5 | 3.00 / 15.00 | 3.00 / 15.00 standard | match |
| gemma-4-31b | 0.14 / 0.40 | 0.14 / 0.40 | match |
| nemotron-3-super-120b | 0.15 / 0.65 | 0.15 / 0.65 | match |
| kimi-k2-5 | 0.60 / 2.50 | 0.60 / **3.00** | corrected |

Sources: [AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) · [Kimi K2.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k2-5.html) · [LLMReference](https://www.llmreference.com/model/kimi-k2-5/aws-bedrock)

**Confirmed, not changed:**
- Cache multipliers 0.10× read / 1.25× write — ratio holds across Sonnet 5 regions and price levels (us $3.00 → $0.30/$3.75; ap-southeast-2 $2.20 → $0.22/$2.75)
- AWS publishes **no cache pricing** for Gemma 4, Nemotron 3 Super, or Kimi K2.5 — independent support for the `None` multipliers and the raise-on-cache-tokens guard (spec §8)
- Sonnet 5 promo pricing ($2/$10) ends 2026-08-31; spec §8 settles the tier as standard, so $3/$15 stands

**Still open (Phase 0b, unchanged):** confirm actual per-candidate Bedrock cache *support* — absence of published cache pricing is suggestive, not proof. Re-baseline Sonnet 5 against the real AWS bill rather than list-price math.

**Follow-up noted:** `PRICE_BOOK` is US-region only; other regions run 15–20% higher. Flagged in a module docstring. If the eval ever runs outside us-east-1/us-east-2/us-west-2, the book needs a region axis.

`Model_Bakeoff_Plan.md` needed no edit — its Kimi row already reads "$2.50–3.00" and the ~$900–1,000/month estimate holds at $3.00 (665M × $0.60 + 200M × $3.00 = $999).

---

## Review — Task 1 (2026-08-05)

**Delivered:** `bakeoff/` package with `schema.py` (SCHEMA_VERSION 1.0.0, 6 enums, 14 frozen dataclasses) and `eventlog.py` (`EventLog`, `ImmutabilityError`). 9 tests, all passing. Implemented verbatim from the plan doc; TDD order held, each module's tests observed failing with `ModuleNotFoundError` before implementation.

**Spec constraints verified:**
- Immutability — `write_run` opens the temp file mode `"x"`; a second write of the same `run_id` raises `ImmutabilityError`. No update or delete method exists on `EventLog`.
- Crash safety — record is fsynced and atomically renamed *before* the index line is appended, so the index never advertises a run that isn't on disk. Covered by `test_partial_write_does_not_corrupt_index`.
- No write-time metrics — schema stores raw observations only.
- `ExclusionClass` has no `MODEL_FAILURE` member, by design.

**Spot check beyond the suite:** wrote a record with a nested `TurnRecord` and `DestructiveEvent`, read it back — round-trip equal, nested enums restored as `Severity`/`DestructiveCategory`, not bare strings.

**Deviation from plan:** venv installed with `pip install -e . --no-deps` plus pytest, rather than `-e '.[dev]'`. Task 1 is stdlib-only; `docker` and `litellm` are first needed in Tasks 5 and 7 and can be installed then. Nothing in the pyproject changed.

**Verify:**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -v
```
