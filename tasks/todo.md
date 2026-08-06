# Bakeoff Harness — Task Progress

Source plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](../docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

- [x] **Task 1** — Schema and event log
- [x] **Task 2** — Cost calculation
- [x] **Task 3** — Trajectory parser
- [x] **Task 4** — Destructive-command and secret scanners
- [x] **Task 5** — Container lifecycle and git pinning
- [x] **Task 6** — Checkpoint capture
- [x] **Task 7** — Wire-level logging
- [ ] Task 8 — Failure and exclusion classification (`classify.py`)
- [ ] Task 9 — Claude Code runner (`claude_runner.py`)
- [ ] Task 10 — Run orchestrator (`runner.py`)
- [ ] Task 11 — Fault-injection gate
- [ ] Task 12 — End-to-end smoke test

---

## Review — Task 7 (2026-08-05)

**Delivered:** `wire.py` (`WireLogger`, `BakeoffCallback`), `config/litellm_config.yaml`, `tests/test_config.py`. 12 new tests — 6 plan-doc + 3 wire + 3 config. **59 unit + 9 integration passing.**

**Four corrections:**

1. **`start_time` / `end_time` were discarded** (carried since Task 3). Both callbacks received them and `_record` never saw them. Now `metadata.latency_ms` — measured generation time, where Task 3 can only infer it from transcript gaps. Latency p95 is a stated success criterion (§10).
2. **The callback's `turn` was not a turn.** LiteLLM fires per API call and the proxy sets `num_retries: 3`, so one turn can produce several calls. Joining wire entries to trajectory turns — which the scoring plan must do for `ToolCallStats.malformed` — would silently mis-join. Renamed `call_index`.
3. **`gzip.open(path, "wt")` truncated.** Global constraint says files open mode `x`; `EventLog` already honored it. A wire log can't be reconstructed, so collisions must fail loudly. Now `"xt"`.
4. **Every model ID was wrong**, verified against AWS model cards. All four carried a spurious `-v1:0`; two had transposed name segments.

| model | plan doc | AWS model card |
|---|---|---|
| claude-sonnet-5 | `anthropic.claude-sonnet-5-v1:0` | `anthropic.claude-sonnet-5` |
| gemma-4-31b | `google.gemma-4-31b-v1:0` | `google.gemma-4-31b` |
| nemotron | `nvidia.nemotron-3-super-120b-v1:0` | `nvidia.nemotron-super-3-120b` |
| kimi-k2-5 | `moonshotai.kimi-k2-5-v1:0` | `moonshotai.kimi-k2.5` |

**Gemma was structural, not cosmetic.** It does not support `bedrock-runtime` at all — mantle only. `bedrock/google.gemma-4-31b-v1:0` could never resolve, and per §6.4 that failure presents as adapter failure indistinguishable from model weakness. The requirement was known upstream ([Model_Bakeoff_Plan.md:59](../docs/Model_Bakeoff_Plan.md), spec §11 Phase 0a) and lost in the harness plan — `grep -i mantle` there returned zero hits.

**Both transports configured.** Mantle primary (AWS-recommended; Gemma's only option), `bedrock-runtime` alongside for the three that support it, so Phase 0 chooses per arm from evidence and can A/B the adapter question. Distinct `model_name` per route — LiteLLM round-robins across repeated names, which would randomize transport per call and confound latency and tool-translation results. `PRICE_BOOK` gains `-runtime` aliases; verified pricing identical to their mantle counterparts, and Gemma correctly has none.

Sonnet's runtime entry uses the `us.` geo profile — its card lists the In-Region runtime URL as **N/A**, so the bare ID can't be invoked there.

**What uniform transport does not fix:** paths and protocols still differ per family — Sonnet `/anthropic/v1` (Anthropic Messages), Gemma `/openai/v1`, Nemotron and Kimi `/v1` (OpenAI Chat Completions). Inherent to Bedrock; belongs in §9 limitations, not in a claim the confound is gone.

**Gemma asymmetries recorded for §9** (adapter class, not model weakness):
- No parallel tool calls — "request tool calls one at a time." Claude Code issues them routinely, so Gemma's turn efficiency and latency take a transport-driven hit.
- Reasoning content returned only by the Responses API, so `TokenUsage.reasoning` reads zero on the Chat Completions path while Sonnet reports it — understating Gemma's cost, since reasoning bills at the output rate.

Corroborating Task 2: no candidate model card carries a prompt-caching table; Sonnet 5's specifies 4,096 min tokens and 5m/1h TTL. Consistent with the `None` cache multipliers, not proof. Phase 0b stands.

### Follow-up — litellm installed, and it exposed a silent-failure bug

Installing litellm 1.95.0 turned the "unverifiable" callback item into a verified defect, closing a Task 12 item early.

`BakeoffCallback` was a plain class. LiteLLM's `success_handler` dispatches on `isinstance(callback, CustomLogger)` — the only other branch is plain callables — so a duck-typed object with the right method names is **skipped in silence**: no wire log, no error, against a §6.2 mandatory requirement.

The config made it unreachable twice over. `get_instance_fn` resolves a dotted path with `getattr` and returns it **as-is**, so `callbacks: bakeoff.wire.BakeoffCallback` puts the *class object* in the callback list, which fails the same isinstance check. And no config string could supply the per-run `WireLogger` and `run_id` it needs anyway.

Fixed: `BakeoffCallback(CustomLogger)` with `super().__init__()`, the `callbacks:` line removed from the YAML with the reasoning inline, and `test_callback_is_dispatchable_by_litellm` pinning the contract. Verified both directions — the string form resolves to a class and is **not** dispatchable; a programmatic instance **is**.

Cost: `wire.py` now imports litellm, adding ~1.1s to test-suite startup (0.09s → 1.28s). Worth it — the alternative was a mandatory artifact silently never being written.

**Explicitly unverified:** routing, auth, endpoint reachability. No AWS credentials — Task 12 is the gate for those. Also made `test_config.py` import `yaml` directly rather than via `importorskip`; a skipped config check reads green while verifying nothing, which is the exact failure mode Tasks 5 and 6 kept surfacing. `pyyaml` added to dev extras.

---

## Review — Task 6 (2026-08-05)

**Delivered:** `checkpoints.py` — `CheckpointRecorder`, `SupportsSnapshot`. 5 tests, implemented verbatim (traced by hand first, all pass as specified). Plus the deferred `snapshot_diff` root-cause fix and 1 integration test. **47 unit + 9 integration passing.**

`CheckpointRecorder` depends only on a Protocol, so its tests use a fake and need no Docker.

**Root cause closed.** `snapshot_diff` ignored exit codes and returned stdout only, so any stderr-routed git failure yielded `("", [])` — indistinguishable from a clean tree. In Task 5 that was test hygiene; in Task 6 it becomes data integrity, because `_capture` writes that empty result into a `Checkpoint` as *"the agent had changed nothing by turn K"* — a fabricated measurement feeding the cost-at-budget-K curve.

Every git call now routes through `_checked_exec`, raising `ContainerError` with stderr attached. Safe because `git diff` runs without `--exit-code`, so it returns 0 regardless of whether differences exist; a non-zero code is unambiguously a failure.

Verified end to end:

- New test observed failing with `DID NOT RAISE ContainerError` before the fix — the silent path, live
- Re-ran the unmounted-repo scenario afterward: `test_snapshot_diff_returns_empty_for_clean_tree`, which **passed vacuously last turn**, now fails with `git add -A failed (exit 128): fatal: not a git repository`
- Confirmed the fix does not turn "no changes" into an error — the clean-tree test still passes normally when the repo *is* mounted

**Three defects found in Task 10's call site while tracing the consumer:**

1. **Checkpoints are captured after the run ends, so every one snapshots the same final state.** The loop runs post-hoc over `parsed.turns` once `ClaudeCodeRunner.run` has returned, meaning the working tree is at its end state for all of them. The per-turn progression would be fabricated — identical diffs relabeled with different turn numbers — making the cost-at-budget-K curve meaningless. Capture must be interleaved with the agent's execution. **Highest-priority item on this list.**
2. `force_capture(turn=len(recorder.captured) + 1, ...)` numbers the final checkpoint by *count*, not turn — 20 turns at K=5 emits `turn=5`, colliding with the real checkpoint at turn 5; at K=1 it emits `turn=21`, a turn that never happened.
3. Every intermediate `maybe_capture` passes `elapsed_ms=0`, so `Checkpoint.elapsed_ms` is meaningless except on the final capture.

`CheckpointRecorder` is correct in isolation — it snapshots when called, and the caller owns interleaving. Noted in the module docstring, since the recorder cannot detect the misuse itself.

---

## Review — Task 5 (2026-08-05)

**Delivered:** `container.py` — `RunContainer`, `ExecResult`, `ContainerError`. 9 tests (7 from the plan doc + 2 guards), verified against a real daemon: **42 default + 8 integration**. TDD order held.

**Environment.** No container runtime existed on this machine. Installed Colima 0.10.3 + Docker CLI 29.7.1 via brew; VM at `--cpu 2 --memory 4 --disk 20` (4GB because `RunContainer` defaults to `mem_limit="4g"`). Installed the `docker` SDK into the venv — it was already a declared dependency, just skipped by Task 1's `--no-deps`.

One unrelated fix needed: `~/.docker/config.json` carried `"credsStore": "desktop"` from a since-removed Docker Desktop, which broke every image pull with `docker-credential-desktop: executable file not found`. Removed that key only; **backup at `~/.docker/config.json.bak-precolima`**.

**Four defects, all confirmed empirically rather than by reading:**

1. **The conftest broke the whole suite.** It imported `bakeoff.container` at module level; conftest loads for every test, and with no `addopts` filter integration tests ran by default — so 41 passing tests became a collection error anywhere the SDK was missing. Fixed with `addopts = "-m 'not integration'"` plus fixture-local imports.
2. **`install_git` cannot work.** `apk add` needs network; containers are `network_mode="none"` per §5.1. Fixture now uses digest-pinned `alpine/git` (git 2.54.0, Alpine-based so busybox `wget` still serves the network test). The flag stays in the interface but can only short-circuit.
3. **Image entrypoints defeat `sleep infinity`.** Verified `alpine/git` declares `ENTRYPOINT ["git"]` — the container would have run `git sleep infinity` and exited. `RunContainer` now passes `entrypoint=["sleep"], command=["infinity"]`, which also covers production task images with their own entrypoints.
4. **The digest test was needlessly gated** behind a file-level `pytestmark`. It validates `__init__`, needs no daemon, and is the §5.1 pinning guarantee — now runs everywhere.

**The vacuous-pass hazard, reproduced — and scoped correctly.** On macOS the Docker VM mounts `$HOME` but not `/var/folders`, so a repo under pytest's default `tmp_path` mounts as an **empty directory with no error raised**. Confirmed by running integration without `--basetemp`: `test_snapshot_diff_returns_empty_for_clean_tree` **still passed** while measuring nothing, and only the new `test_repo_is_actually_mounted` failed, with a message naming the fix.

The two failure paths are not symmetric, and an earlier version of this note wrongly claimed both were vacuous:

| path | git behavior | error stream | clean-tree test |
|---|---|---|---|
| repo not mounted | git runs, "not a git repository" | **stderr** | **passes vacuously** |
| git binary absent | OCI exec failure, never runs | **stdout** | fails loudly |

`snapshot_diff` returns stdout only, which is why stderr-routed failures disappear and stdout-routed ones don't. Missing git is still a real defect — `install_git` can't work under `network_mode="none"` and all three git tests break — it just breaks honestly. `test_git_is_available_in_container` remains worthwhile as a direct assertion rather than an inferred one.

**Root cause still open:** `snapshot_diff` discards exit codes and stderr, so it cannot distinguish "clean tree" from "git command failed." The two guard tests cover the known paths, but any *new* stderr-routed git failure would reproduce the same silent-empty result. Checking `ExecResult.exit_code` in `snapshot_diff` would close it at the source — **candidate fix for Task 6**, which builds directly on this method.

**Run integration tests with:**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_container.py -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
```

`DOCKER_HOST` turned out to be unnecessary — Colima 0.10.3 symlinks `/var/run/docker.sock`, and `docker.from_env()` resolves it. Verified both with and without.

**Known, unchanged:** `HostMetrics.cpu_pct_p95` stays unpopulated — `stats()` returns memory only. Wiring CPU sampling belongs to Task 10.

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
