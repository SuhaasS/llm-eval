# Bakeoff Harness — Task Progress

Source plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](../docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

Open work lives in [TASKS.md](../TASKS.md). This file is the completed-work
review log — one section per finished task, kept for what each one turned up.

- [x] **Task 1** — Schema and event log
- [x] **Task 2** — Cost calculation
- [x] **Task 3** — Trajectory parser
- [x] **Task 4** — Destructive-command and secret scanners
- [x] **Task 5** — Container lifecycle and git pinning
- [x] **Task 6** — Checkpoint capture
- [x] **Task 7** — Wire-level logging
- [x] **Task 8** — Failure and exclusion classification
- [x] **Task 9** — Claude Code runner
- [x] **Task 10** — Run orchestrator
- [x] **Task 11** — Fault-injection gate
- [x] **Task 12** — End-to-end smoke test — offline DONE; live **GO, 4 arms 3/3**

---

## Gemma's second adapter defect, and Phase 0c goes GO — 2026-08-11

With `propertyNames` fixed Gemma finally emitted tool calls — 30 of them, every
run — and still did nothing: all `Bash`, 4 distinct commands, no `Edit`, no
diff, `terminated_by: turns`. That read like a model looping. It was not.

**Gemma returns the same tool-call id on every response.**

```
tool-call ids Gemma returned across all 30 responses: {'call_0': 30}
```

Its ids are indexed *within* a response and it emits one call per response
(`supports_parallel_function_calling: false`), so the index is always 0. Every
other arm is unique — Sonnet `toolu_bdrk_…` 5/5, Nemotron `call_4196d6e0…` 8/8,
Kimi `functions.Read:0` 5/5.

Claude Code executed all 30 locally (`tool_use {'call_0': 30}`,
`tool_result {'call_0': 30}`) but could not pair duplicates when re-serializing,
so the conversation Gemma actually saw carried **1 tool_use, 1 tool_result and
28 `(no content)` turns**. It never observed anything after its first `ls`, so
it re-issued the same plan to the turn cap. Every symptom followed from that.

**The fix** rewrites a response-path id only when it collides with one already
in the conversation — a strict no-op for the three arms whose ids never repeat,
which is what keeps it from becoming a per-arm difference and what stops it
rewriting Kimi's `functions.Read:0`, the exact shape the previous patch exists
to preserve.

**Two mistakes, both caught by measuring rather than reasoning.** They are worth
recording because the second is the failure mode this file keeps describing.

1. **The ContextVar carrier did not work.** The plan was to set the
   conversation's id set in the pre-request hook and read it on the per-chunk
   translation. Probing the running proxy:

   ```
   [probe] HOOK          set={'functions.Read:0'}
   [probe] RESPONSE      seen=None      <- every chunk, every time
   [probe] WRAPPER_INIT  seen={'functions.Read:0'}
   ```

   The SSE response is iterated by the server's own task, whose context was
   copied before the hook ran. `AnthropicStreamWrapper.__init__` still sees it —
   it runs in the handler coroutine — so the set is snapshotted there, onto the
   stream, and read back in `_should_start_new_content_block`.

2. **A mutable ContextVar default hid that entirely.** `default=set()` is one
   object shared by every context that never called `set()`, so the broken
   version still produced unique-looking ids by accumulating them process-wide
   across every run and arm, and the gate passed. The default is now `None`,
   which the code refuses to guess from.

**The gate needed a third check, and finding that out took reverting the fix.**
The stub now issues the same Kimi-shaped id twice, reproducing Gemma's failure
offline byte-for-byte — with the patch off, `{'functions.Read:0': 29}` and 28
`(no content)` turns. But the obvious signals stayed green: the arm ran **60
turns and 30 tool calls and still landed the 157-byte diff**, so `diff_b` and
the verdict passed, and `validate_tool_call_ids` never fired because Claude Code
drops the unpairable call rather than echoing a bad one. `validate_loop_progress`
closes it — the openai script is exactly 3 calls, so a 4th means the loop is not
advancing. Carrier working: kimi 5 turns, GO. Carrier broken: crashed, NO-GO.

**Result: GO. Four arms, 3/3 each**, correct 157-byte diff on all 12 runs.
Gemma 17/19/23 turns and 8/9/11 tool calls; the mechanism verified directly
rather than inferred from the outcome:

```
gemma-4-31b-0: model returned 8 calls / 1 distinct raw id {'call_0': 8}
   after uniquify: ['call_0', 'call_0_1', ... 'call_0_7']
   tool_results=8   '(no content)' turns=0
```

**Four for four.** Every "model failure" this eval has produced has been a
harness-layer defect: Sonnet's beta header, Kimi's tool-id mangling, Gemma's
`propertyNames`, Gemma's colliding ids. Each was deterministic, each was total,
each had a small cause. No capability claim about any arm is supported by
anything in the log yet, and that is the base rate the next total failure should
be read against.

**Left open, deliberately:** the request carries `system` both as a top-level
field and as 6 inline `system`-role messages, the first landing *after* the user
message. Filed in `TASKS.md` rather than bundled here, so this run stays
attributable to the id fix alone.

---

## The third "model failure" was also an adapter defect — 2026-08-11

Gemma 4 31B had failed **9/9** with `JSON-RPC error -32602: Job registration
failed: Engine bad request: Task submission failed with status 400 Bad Request:
Generation failed`, 1 turn and 0 tool calls every time. `TASKS.md` recorded it
as "Bedrock-side, not a parameter rejection". **`-32602` is JSON-RPC's `Invalid
params`**, and the invalid param was in the tool schema the whole time.

**The cause is one JSON-Schema keyword: `propertyNames`.** Found with a 15-rung
request ladder against live mantle, each rung adding one thing to the one above,
with Nemotron re-run as a control on every failing rung:

| probe | gemma |
|---|---|
| bare / stream / plain system / system blocks with `cache_control` / temperature | PASS |
| one tool, `$schema`, `additionalProperties: false` | PASS |
| `exclusiveMinimum`, `maxItems`, `anyOf`, `const`, `format: uri`, `pattern` | PASS |
| **`propertyNames`**, object-level and nested alike | **FAIL** |
| two tools, `max_tokens` 16384, first 12 real tools | PASS |
| all 24 real tools | **FAIL** |

Exactly two of the 24 tools Claude Code 2.1.220 declares carry it — `TaskCreate`
and `TaskUpdate`, both on `properties.metadata`. Each fails alone against Gemma
and passes against Nemotron. Confirmed against the eval's own pinned image
rather than a laptop's claude: `bakeoff-eval-agent` emits the same 24 tools with
the keyword on the same two. **74 bytes**, and it cost an arm.

**What was ruled out first, offline.** Capturing what LiteLLM actually POSTs for
each mantle arm — the real Claude Code body replayed through the real config,
against a local capture server — showed all three candidate arms emitting the
same top-level keys, the same translation and the same `stream_options`,
differing only in `model` and in the `/openai/v1` vs `/v1` base. So the bridge
was not treating Gemma differently; Gemma's engine was rejecting a shared
payload. Routing through litellm's first-class `bedrock_mantle/` provider
instead of the config's generic `openai/` produced a byte-identical body, so
that was not it either.

**The fix is a pre-request hook, not a monkeypatch.** `anthropic_messages` calls
`_execute_pre_request_hooks` before it branches on provider and keeps the
`tools` the hook returns, so one hook on `BakeoffAdapterPatches` — already a
registered `CustomLogger` — covers the `anthropic/` Sonnet arm and the three
`openai/` candidates identically. Patching the openai→anthropic adapter would
have reached the candidates only and reintroduced exactly the §6.4 asymmetry the
harness works to avoid.

**Why stripping it is not a thumb on the scale.** `propertyNames: {"type":
"string"}` is vacuous — JSON object keys are strings by definition — so removing
it constrains nothing that was constrained before, and no arm's tool contract
changes meaning. Only the measured keyword is stripped: the other six were each
probed individually and each passed, and widening the strip to a denylist would
be working around faults that do not exist.

**Two guards, because they fail differently.** `apply()` proves the strip
function works and the hook exists, and the proxy refuses to start otherwise —
verified by reverting the strip, which killed startup rather than degrading.
But nothing in-process can prove litellm *calls* the hook, so the offline stub
now 400s on a surviving `propertyNames`, on **both** routes since the hook runs
before provider branching. Verified by neutering the hook while leaving the
strip intact: the gate went red with
`propertyNames survived in 'tools[14].function.parameters.properties.metadata'`.
A gate that cannot fail is not evidence, and these two mutations fail it in the
two different ways it can actually break.

**Result: Gemma goes from 0 tool calls in any run to 30 in every run.** 3/3.
It is still 0/3 on the task, and the reason is now a model observation rather
than a transport one: all 30 calls are `Bash`, only 4 distinct commands among
them, mostly `python3 tests/test_calc.py` repeated; no `Read`, no `Edit`, no
diff, `terminated_by: turns`. `errored: 0`, `malformed: 0` — every call is
well-formed and answers 200. Carried to `TASKS.md` with the caveat that 30/30
calls on a single tool is itself a suspicious distribution and deserves a wire-
log read before it is called capability.

**The base rate is now three for three.** Sonnet's beta header, Kimi's tool-id
mangling, and Gemma's `propertyNames` — every "model failure" this eval has
produced has been a harness-layer defect with a one-line cause, and each looked
deterministic and total beforehand.

---

## Review — Task 11 (2026-08-06)

**Delivered:** `tests/test_fault_injection.py` (all twelve spec §6.6 cases), `scripts/verify_logger.py`, `src/bakeoff/proxy_callback.py`, `config/litellm_fault_injection.yaml`, `docker/litellm-proxy.Dockerfile`. **136 unit + 25 integration passing.** Gate exits 0.

**Four defects found, all closed.** None would have been caught by the seven tests the plan drafted for this task — every one of them passes against the code as it stood.

1. **A Bedrock throttle was scored against the model.** `execute_run` never populated `api_error_status`, so `classify_exclusion`'s 429/408/5xx branch was unreachable in production. The drafted test called the classifier directly and passed green over the dead path. Now derived from the wire log, with the rule that **only the final call counts, and only if it failed** — `num_retries: 3` means a recovered throttle produced a complete run, and excluding it would discard good data.

2. **Four of six version fields were empty on every record** — `litellm`, `bedrock_model_id`, `harness_commit`, `task_set_commit`. §6.6 asks for "correct for every component". `harness_commit` carries a `-dirty` suffix on an uncommitted tree, so a record can never claim a provenance it does not have. `task_set_commit` stays empty and is asserted empty until the dataset plan exists.

3. **Checkpoints were discarded whenever a run failed part way through.** `checkpoints = recorder.captured` sat inside the `try` after the agent call, so a snapshot hitting a full disk — or the container being killed — threw away every checkpoint already captured. The record still looked well-formed with an empty list, which is indistinguishable from a quiet run.

4. **Wire capture was dead in the real topology.** `litellm.callbacks` registers in the harness process, which makes no model calls; the agent calls the proxy, a third process. Every real run would have produced an empty wire log — empty `sampling`, empty prompt and tool hashes, zero errored calls — silently, while §6.2 makes wire logging mandatory. Capture now runs inside the proxy (`proxy_callback.py`), attributed per run by an `X-Bakeoff-Run-Id` header, replayed through the existing `WireLogger` so there is still exactly one canonical artifact.

**Verified rather than assumed:**
- Pointed claude 2.1.220 at a local listener: `ANTHROPIC_CUSTOM_HEADERS="X-Bakeoff-Run-Id: <id>"` lands verbatim on every `POST /v1/messages?beta=true`. It probes `HEAD /api/hello` first.
- `mock_response: "litellm.RateLimitError"` raises a **real** `RateLimitError` inside the proxy, so §6.6's throttle case is injected end to end with no credentials and no spend.
- On `/v1/messages`, sampling fields are **not** top-level callback kwargs and `optional_params` is `{}`; the payload lives in `litellm_params.proxy_server_request.body`. Reading the obvious place would have recorded `None` for every sampling field — indistinguishable from a caller that sent none.
- `litellm[proxy]==1.95.0` does not cap fastapi, and 0.141.0 removed `get_flat_dependant`, which the proxy imports at startup. An unpinned build dies before serving a request, on whatever day that release shipped, with nothing in this repo changed. Pinned to 0.140.0.

**Deliberately not closed:** `tool_calls.malformed` stays 0 on every harness-written record — deciding a call was malformed means inspecting raw completions, which is the wire-log analysis in the scoring plan. A test pins that 0 so it is never misread as "the harness looked and found none".

**Carried to Task 12:** a temperature configured on a deployment is invisible to the proxy-side callback on the Anthropic Messages route. Observed against a `mock_response` deployment, which short-circuits before provider param transformation, so it is not proof the value is dropped on a real call — but §5.3 puts sampling entirely in proxy config, so if it is dropped, three arms run at unspecified sampling. Must be read back from the wire log after the first real call.

**One behaviour worth stating:** with no Docker daemon the gate reports `GATE INCOMPLETE` and exits 1 rather than passing. The mid-run kill, the proxy-side wire log, and live checkpoint capture are only observable against a real daemon; certifying the capture path on the strength of checks that cannot see it is the failure the gate exists to prevent.

**Verify:**

```bash
cd bakeoff && .venv/bin/python scripts/verify_logger.py
```

### Verification pass (same day)

"All tests pass" is not evidence a suite would catch a regression — a test can assert something that was always true. So every guarantee was removed one at a time to see whether a test went red. `scripts/mutation_check.py`, **16/16 caught**. Test count 136 → **166 unit + 25 integration**; coverage 84% → **89%**, with `proxy_callback.py` 34% → 93%.

Three real gaps surfaced, and two false alarms in the harness itself:

- **The `-dirty` suffix had no test at all.** The mutation harness first reported it CAUGHT because pytest exits non-zero on "no tests collected" — an empty selector scored as a catch. Fixed the harness to treat exit 5 as a miss, then wrote the test.
- **`proxy_callback.py` was only ever tested end to end**, and cannot be otherwise covered from the harness process — it executes inside the proxy container, so coverage numbers are blind to it by construction. Now exercised directly against kwargs shaped like the real ones (dumped from a live proxy), which turned up a **dead fallback**: `_headers` looked for `proxy_server_request` at the top level, but it lives under `litellm_params`, so the last-resort attribution path was unreachable.
- **`unattributed_count == 0` was a vacuous assertion** — the count is 0 when nothing was written at all. Added the positive case: a header-less call is kept and flagged.
- **The concurrency test did not test the lock.** It passed with the lock deleted: the GIL and `O_APPEND` make the race not materialise by luck. Rewritten to force a thread switch between the counter's read and its write, which does catch removal. The volume test stays, relabelled for what it actually shows.
- **`verify_logger.py` had no tests**, including for its most important behaviour — refusing to report success when it could not run the checks that see the capture path.

One earlier "MISSED" was a false negative: the phantom-index mutation is caught by `test_writing_same_run_id_twice_raises` and by the dry run (`FAIL: re-write was accepted`); my `-k` filter simply did not select them.

---

## Review — Task 10 (2026-08-06)

**Delivered:** `runner.py` — `TaskSpec`, `make_run_id`, `resolve_reverts`, `assemble_record`, `execute_run`. Plus `docker/eval-agent.Dockerfile`, and changes to `container.py`, `claude_runner.py`, `schema.py`. **115 unit + 19 integration passing.**

All 11 carried-forward defects closed. Two of them changed the architecture rather than the orchestrator.

### The agent now runs inside the container

This is what defect 1 actually cost. `network_mode="none"` and "agent in the container" are mutually exclusive — an isolated container cannot reach the LiteLLM proxy. §5.1's second branch resolves it: "network off during the run, **or through a recording proxy**", and the proxy *is* the recording proxy. `RunContainer` gained a `network` parameter for an `internal=True` network carrying only the proxy.

**This changes how you run the proxy: it must be a container on that network.** An internal network has no host route, so `127.0.0.1:4000` is unreachable by design; the agent reaches `http://litellm:4000` through Docker's embedded DNS. `scripts/smoke_bedrock.py` is unaffected — it drives the Router in-process and tests routing, not isolation.

`execute_run` defaults `network=None` and records `isolated=False` when it is absent. Such a run still executes, but §5.1 did not hold for the process under test, and the record says so rather than letting `container_image_digest` imply otherwise. New field, so `SCHEMA_VERSION` moved to 1.1.0 — a reader that could not tell the versions apart would read an absent `isolated` as a positive claim that the run was *not* isolated.

### Checkpoints are captured live

Demonstrated against the plan's own capture order with a fake stream-json agent:

```
post-hoc (as planned)          live capture (now)
  turn=1 elapsed=0               turn=1 elapsed=2043
    files=[first, second]          files=[first]
  turn=2 elapsed=0               turn=2 elapsed=4102
    files=[first, second]          files=[first, second]
  distinct diffs: 1              distinct diffs: 2
```

Because capture is now concurrent with the agent, `snapshot_diff` can no longer touch `.git/index` — staging under a live agent means the agent's next `git commit` sweeps in files it never staged, and its `git status` disagrees with reality. Staging moved to a throwaway `GIT_INDEX_FILE`. The guard failed against the old code with `['loose.txt', 'staged.txt'] == ['staged.txt']`.

**Off-by-one found while reviewing my own fix.** An assistant message announces the tool calls a turn is *about* to make, so at message k the tree holds k−1 turns of work. Firing `on_turn(k)` there files turn k−1's work under turn k — understating every checkpoint by one turn, on the exact axis the cost-at-budget-K curve is plotted against. The callback now reports turns *completed*; `force_capture` supplies the final turn, whose tools run after the stream's last message. That also removed a duplicate — the old `force_capture(turn=len(captured) + 1)` collided with the last `maybe_capture`.

**Limitation I did not paper over:** the boundary is approximate by however long a snapshot takes. A tool writing faster than `git add -A` completes can put part of turn k+1 into turn k's checkpoint. Closing that window means pausing the agent, which no external observer can do. The alternative isn't more precise, it's wrong — post-hoc capture gives every checkpoint the same end state.

### Two defects found during implementation, neither on the list

- `build_env` forwarded the host `PATH` and `HOME` into the container, where Docker merges them over the image's own environment. `claude` would have stopped resolving, and any host path that happened to exist in the image would have resolved to something never installed. Split into `build_env` (host) and `container_env` (eval keys only).
- `DISABLE_UPDATES=1` set *before* the install step makes the Claude Code installer print "Updates are disabled by your administrator" and leave no binary — while exiting 0, so the failure surfaces several steps later as `claude: not found`. Caught by building the image.

### Reference image

`docker/eval-agent.Dockerfile` pins claude 2.1.220 and **fails the build** if the installed version differs, so drift cannot ship silently into `versions.claude_code`. Carries `git`, `ripgrep` (a Claude Code runtime dependency), and `coreutils` for `timeout` — which enforces the wall-clock budget from inside the container, because Docker offers no way to kill a running exec from outside.

```bash
cd bakeoff && docker build -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent .
```

### Still open, deliberately

`tool_calls_malformed` stays 0: deciding a call was malformed means inspecting raw completions, which is the scoring plan's wire-log analysis. `TOOL_MALFORMATION` and `ADAPTER_FAILURE` remain unreachable at harness time — now by design rather than oversight. `errored` is populated from wire failures. `affected_outcome` needs the test oracle.

---

## Review — Task 9 (2026-08-06)

**Delivered:** `claude_runner.py` — `ClaudeCodeConfig`, `RunnerResult`, `build_command`, `build_env`, `config_digest`, `ClaudeCodeRunner`; `config/eval_settings.json`; per-arm sampling in `config/litellm_config.yaml`. 19 new tests (9 plan-doc + 10), **96 unit + 9 integration passing.**

Everything checked against the installed CLI, **claude 2.1.220** — the `versions.claude_code` floor.

**Verified sound before changing anything.** `--max-turns` is absent from `--help`, which looked like a missing flag and would have meant the §5.4 turn budget silently doing nothing. It is present in the binary (`--max-turns <turns>`), just hidden. No change made — the flag was fine, and removing it on the strength of help output would have deleted the cap the eval is measured against.

**1. `--settings` adds settings, it does not replace them.** Help text: "load **additional** settings from." So `eval_settings.json` merges on top of `~/.claude/settings.json` — hooks, skills, output styles, plugins, user `CLAUDE.md` all still load. `--strict-mcp-config` covers only MCP servers. §5.2 requires "no user-level settings" and names this machine as the risk. The plan also **stripped** `CLAUDE_CONFIG_DIR`, which forces the fallback to `~/.claude` — the lever pulled backwards. Now **set**, to an empty per-run directory; one lever moves user settings, `CLAUDE.md`, skills, plugins and `projects/` at once. Harness subprocesses only — `~/.claude` is never read or modified.

**2. The environment was a denylist over `dict(os.environ)`.** Three keys popped; 19 `CLAUDE*`/`ANTHROPIC*` variables set on this machine, **none of them among the three**. The one that matters:

```
USE_BEDROCK survives: True
USE_VERTEX  survives: True
CLAUDE_CONFIG_DIR set: False
```

`CLAUDE_CODE_USE_BEDROCK` / `USE_VERTEX` (both in the 2.1.220 binary) make the CLI ignore `ANTHROPIC_BASE_URL` and call the provider directly — proxy bypassed, §6.2-mandatory wire log empty, run looks normal. Replaced with an allowlist, which fails closed.

**3. `_find_transcript` could return another run's transcript.** Two problems, both demonstrated against the plan's own code:

```
=== stale transcript ===
this run wrote nothing; returned: prev-run.jsonl

=== dot in path ===
real dir on disk : repo-v1
plan looks for   : repo.v1
transcript found : None
```

Newest-by-mtime in a shared project dir hands back the *previous* model's trajectory, tokens and cost when this run crashes early — and Task 10 reuses one repo path across the N=10 and across arms. Separately the munging rule is wrong: Claude Code hyphenates dots as well as separators (`/Users/x/.claude` → `-Users-x--claude`, confirmed on disk), so a dotted repo path yields `None` and Task 10 records zeros without complaint. Both close by globbing the per-run config dir.

**4. `temperature` was a no-op, and the intuitive fix would have broken two arms.** The field reached neither command nor env, and the proxy config set nothing — so §5.3 was unimplemented while `RunRecord.sampling` stood ready to record a value never applied. Same shape as the Task 8 `FALSE_SUCCESS` bug: the record asserting something untrue.

Routed to the proxy, the only layer that can apply it. Values verified per lab, and they are **not** interchangeable:

| Arm | Setting | Why |
|---|---|---|
| Claude Sonnet 5 | **omit entirely** | non-default `temperature`/`top_p`/`top_k` returns **400** |
| Kimi K2.5 | 1.0, top_p 0.95 | Moonshot documents **multi-minute stalls at 0** |
| Nemotron 3 Super 120B | 1.0, top_p 0.95 | NVIDIA, all tasks |
| Gemma 4 31B | 1.0, top_p 0.95 | Google |

Temperature 0 uniformly — the obvious "fair" choice — would 400 every call on the reference arm and turn Kimi's config error into a latency measurement. That is the §5.4 failure mode exactly: a config choice scored as a model difference. §5.3's "(or lab-recommended)" is load-bearing; what is identical across arms is the policy, not the number. `config_digest` therefore excludes temperature, since a per-model value inside a cross-arm identity digest would make the digest assert something false. `test_sonnet_5_arms_send_no_sampling_parameters` pins it, because "make sampling uniform" is the natural later edit and is wrong.

**Recorded, not fixed** — outside this task, added to the plan's self-review:

- §5.2's other half is unimplemented: it requires **dumping and diffing the effective config at session start** and storing the dump. Task 9 digests *intent*. If `--settings` silently fails to load, the digest is unchanged and identical across arms while the runs are contaminated. Needs a live run → Task 12.
- Sampling is configured but not confirmed applied — LiteLLM merges `litellm_params` as defaults and Claude Code's own request body may win. Only the wire log shows what was sent → Task 12 reads it back from `wire.jsonl`.
- Sonnet 5's new tokenizer emits **~30% more tokens for the same text**, so a uniform token cap is not a uniform text budget, and cost-per-task is not comparable at equal output. §5.4 caps come from the calibration pilot; flagged there.
- `PRICE_BOOK` has Sonnet 5 at standard $3/$15; Anthropic lists introductory $2/$10 through 2026-08-31. **Whether Bedrock mirrors it is unverified** — worth checking before any cost figure is published.

Task 10's carried-forward defect list is now 11 items and lives in the plan doc at the head of Task 10, not just in session notes. The top item is new and structural: **the agent runs as a host subprocess, so §5.1's container isolation does not apply to the process under test** while the run record asserts a pinned image digest.

---

## Review — Task 8 (2026-08-05)

**Delivered:** `classify.py` — `RunSignals`, `classify_failure`, `classify_exclusion`, `PRE_REGISTERED_REASONS`. 15 tests (13 plan-doc + 2), **75 unit + 9 integration passing.** Pure logic, stdlib only.

**The correction — every clean run would have been labelled a false success.**

Task 10 builds `RunSignals` with `agent_claimed_success = (terminated_by == AGENT_FINISH)` and `tests_passed=False` hardcoded, and assigns `outcome = FAILED` to any self-terminating agent. So on the normal path — agent finishes, no malformation, no truncation, no loop — `classify_failure` reached `agent_claimed_success and not tests_passed` and returned `FALSE_SUCCESS`.

Demonstrated against Task 10's exact signal construction:

```
plan doc  (tests_passed=False): FailureClass.FALSE_SUCCESS
corrected (tests_passed=None) : None
```

`FALSE_SUCCESS` asserts the model claimed a success it did not achieve. Applying that to essentially every well-behaved run is an accusation of dishonesty written into a log with no update API — permanent. `Outcome.RESOLVED` is never assigned at harness time either, so the `RESOLVED` guard never fires to prevent it.

The plan's self-review did flag the hardcoded `False` as a known deferral with the offline grader re-deriving later. But a derived view cannot un-write a bad value from an immutable record, and the global constraints say the logger records raw observations while every score is derived later.

Root cause: treating "not graded yet" as "the tests failed" — absence of evidence recorded as evidence of failure. `tests_passed` is now tri-state (`bool | None`, default `None`), splitting the classes by what each needs:

| class | needs | harness time |
|---|---|---|
| `P2P_REGRESSION`, `TOOL_MALFORMATION`, `TRUNCATION`, `LOOP_REPETITION`, `GAVE_UP` | transcript only | classifies as before |
| `FALSE_SUCCESS` | test oracle | requires explicit `False` |
| `WRONG_BUT_CONFIDENT` (catch-all) | test oracle | requires a result at all |

Verified structural failures still classify without the oracle — malformation, truncation, loop, and gave-up all fire with `tests_passed=None`. All 13 original tests unaffected (the fixture supplies `False` explicitly). `test_harness_time_signals_leave_the_failure_class_undetermined` reproduces Task 10's construction, so the regression can't return silently.

**Found while tracing:** `tool_calls_malformed` is always 0, because `ToolCallStats.malformed` comes from the wire log and isn't wired through `assemble_record`. So `TOOL_MALFORMATION` and `ADAPTER_FAILURE` are **both unreachable at harness time** — the adapter-vs-model distinction §6.4 calls the eval's most consequential call cannot currently fire. Recorded in the plan doc's self-review alongside the existing deferral.

**Correct as specified:** `task_defect_flaky_test` and `task_defect_bad_base_sha` are pre-registered but never emitted by `classify_exclusion`. That's the point — they're applied by a human during analysis, and pre-registering them before any run is what stops exclusion criteria being invented after seeing results.

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

---

## Review — Task 12, offline half (2026-08-06)

`scripts/smoke_test.py --mode offline` runs the real Claude Code binary in the
pinned container against a real LiteLLM proxy on an internal Docker network.
It is now part of `verify_logger.py`. Result: 3 turns, 2 tool calls, 3 wire
entries, a 157-byte staged diff, every call attributed, effective config
identical across arms.

**Four defects found by building it.** Each would have made the paid run
produce nothing usable, and none was visible to any existing test.

1. **The production proxy config registered no wire callback.** Every real run
   would have read an empty wire directory — empty sampling, empty prompt and
   tool hashes, no resolved model id, no API error status — on a record that
   otherwise looked complete. Task 11's defect 15, in a second place. The
   config's own comment argued correctly about a dotted path to a *class*; the
   fix is a dotted path to a module-level *instance*.

2. **`config/eval_settings.json` was mounted nowhere.** Every call site passed
   `--settings /eval/eval_settings.json`, a path no image had. Claude Code does
   not fail on a missing settings file — it starts with none of the pinned
   settings loaded.

3. **The image ran as root, and Claude Code refuses `bypassPermissions` under
   root.** Only visible once defect 2 was fixed and the settings actually
   loaded: the agent then exited before emitting a single event.
   `--allow-dangerously-skip-permissions` does not lift the guard; a non-root
   uid does. Together, 2 and 3 would have landed no diff on any arm — a harness
   bug that reads as four models failing the task.

4. **Mock deployments never exercise the streaming path.** `mock_response`
   short-circuits inside `anthropic_messages` before litellm's streaming
   wrapper, so the success callback never fires for a streaming request — and
   every real Claude Code call is streaming. Measured: two calls served, one
   captured. `fixtures/anthropic_stub.py` speaks real SSE so the gate now
   exercises the path a live run uses, and the gate cross-checks its own
   capture against the proxy's access log rather than only counting what it
   managed to record.

**Task 11's carried §5.3 question is answered.** A deployment-level
`temperature: 1.0` does reach the wire and `RunRecord.sampling`. Its absence
before was a mock artifact. The candidate arms route through `openai/` rather
than `anthropic/`, so that variant is still live-only.

**Also fixed:** the agent's stdout is kept as an artifact. The §5.2 init event
is emitted there and nowhere else — a completed session's transcript carries
queue-operation, user, attachment, assistant and last-prompt records and no
init — so the effective config could not otherwise be read back at all.

**Honest limits of the offline gate.** The stub replies instantly, so all
three checkpoints hold the same diff: the checkpoint boundary race documented
in `tests/conftest.py` is degenerate at zero turn latency. `scripts/dry_run.py`
covers checkpoint progression, with a stand-in agent that sleeps between turns.
And nothing here says anything about a model.

**Blocked on credentials, not on code.** `bakeoff/.env`'s
`AWS_BEARER_TOKEN_BEDROCK` is still an unfilled `<placeholder>`, and the SSO
session expired 2026-08-06T03:21Z. The live half needs:

```bash
aws sso login --profile pindrop-bakeoff
cd bakeoff && .venv/bin/python scripts/smoke_bedrock.py --derive-mantle-token
```

then `scripts/smoke_bedrock.py --live` as the cheap routing check, then
`scripts/smoke_test.py --mode live`.

---

## Sonnet 5 moved to bedrock-runtime — 2026-08-07

**The P0 blocker is gone.** The reference arm now completes the smoke task:
7 turns, 4 tool calls, 5 wire calls, the correct one-line fix, $0.388, 13.6s.
Nemotron still passes alongside it (19 turns, 9 tool calls, $0.057), so the
change did not cost the mantle arms anything.

**What was actually wrong.** Not what the error said. `"invalid beta flag"`
comes from LiteLLM's `anthropic/` passthrough deriving `anthropic-beta` HTTP
headers from Claude Code's `context_management` and `output_config`, which
bedrock-mantle rejects. `additional_drop_params` removes the *parameters* and
never touches the *header*, which is why per-deployment drops did nothing.
`bedrock/us.anthropic.claude-sonnet-5` leaves that passthrough entirely: on the
Converse path beta values ride as an `additionalModelRequestFields.anthropic_beta`
*body* field (`converse_transformation.py:1474`, litellm 1.95.0), and
`_filter_context_management_for_bedrock_converse` reduces `context_management`
to compact-edits-only or drops it. Different mechanism, not just a different
endpoint.

**The real blocker was credentials, and it was invisible.** The runtime arm
could never have worked through the proxy no matter what the config said:

1. `smoke_test.proxy_environment` passed the container **only** the mantle
   bearer token. `bedrock/` deployments sign SigV4 and need real AWS keys,
   which nothing supplied. The container cannot resolve an SSO profile itself —
   no `~/.aws`, no SSO cache, no browser — so `freeze_sigv4_credentials` now
   resolves the session on the host and passes the frozen triple in.

2. Worse, the two transports **could not coexist in one proxy**. LiteLLM's
   `base_aws_llm.get_request_headers` (verified 1.95.0) uses a deployment's
   `api_key` when set and otherwise falls back to `AWS_BEARER_TOKEN_BEDROCK`,
   signing SigV4 only when both are absent. The runtime deployments carry no
   `api_key` on purpose, so a proxy holding that variable bearer-authenticates
   all three of them. `smoke_bedrock.py` had been working around this in-process
   by popping the variable after Router construction; a container cannot.
   The fix is a rename — the harness now carries the token as
   `BAKEOFF_MANTLE_TOKEN`, a name LiteLLM never looks up, so mantle deployments
   take the explicit-`api_key` branch and runtime deployments fall through
   to SigV4.

**Proven before spending anything.** Two proxy containers, dummy credentials,
same request to `claude-sonnet-5-runtime`:

| proxy env | error | branch |
|---|---|---|
| `BAKEOFF_MANTLE_TOKEN` only | `The security token included in the request is invalid` | SigV4 |
| `+ AWS_BEARER_TOKEN_BEDROCK` | `Invalid API Key format: Must start with pre-defined prefix` | bearer |

That second message is the one `smoke_bedrock.check_sigv4` already warns about:
a bearer-token error on a route that does not use bearer tokens, which reads as
a config fault and is not one. Pinned by two tests in `tests/test_config.py` —
no arm may reference `AWS_BEARER_TOKEN_BEDROCK`, and no `bedrock/` arm may
carry an `api_key`. Both failures are silent and neither has a local symptom.

**The live run carried its own control.** `claude-sonnet-5` on mantle ran in
the same proxy, same run, and still 400d with `invalid beta flag` — so the
difference is attributable to transport and to nothing else that changed.

**What this cost.** The reference arm is no longer transport-identical to the
arms it is the reference for. Recorded as an open §6.4 confound in `TASKS.md`
rather than treated as free. The alternative — pointing Claude Code straight at
Bedrock with `CLAUDE_CODE_USE_BEDROCK` — was rejected: it empties the wire log
on the reference arm, needs the agent container to have real network egress on
a pinned task repo, and puts AWS credentials inside a `bypassPermissions`
sandbox.

**Still NO-GO, for unrelated reasons.** Gemma failed 3/3 with the same
Bedrock-side `Generation failed`; Kimi failed a second time and in a *different*
way than the first. Both carried forward in `TASKS.md`.

---

## Phase 0c raised to N=3 per arm — 2026-08-07

`smoke_test.py --repeats` (default 3 live, 1 offline). GO now requires every
repeat of every arm; a run below N=3 prints an explicit weaker-than-criterion
warning, because a GO at N=1 otherwise reads identically to a GO at N=3.

Two things had to reach through `run_arm`, and missing either is fatal rather
than degrading: a per-repeat **workdir** (repeat 2 would otherwise start from
repeat 1's dirty tree and report a diff its own agent never made) and a
per-repeat **`sample_index`** (`run_id` hashes `(task, model, sample, attempt)`
and `write_run` opens `"x"`, so a second repeat at index 0 raises
`ImmutabilityError` out of `execute_run`).

Reporting is per-run rows plus per-arm pass rates, with failure modes **grouped
by `run_problems`' returned reasons rather than summed** — the earlier two Kimi
failures were two different faults and a count of 2 would have hidden that.
Arms run arm-major so repeats of one arm stay close in time and a rate is not
confounded by drift in Bedrock-side load across the matrix.

**Result: NO-GO, 2 of 4 arms, 3/3 each.** Sonnet 5 (8 turns, correct diff every
run) and Nemotron (15–21 turns, correct diff every run). Gemma 0/3, Kimi 0/3.
Verified: 12 records, 12 distinct `run_id`s, `unattributed.jsonl` empty, all
six proxy-side errors attributing to `gemma-4-31b` and to no other arm.

**Three findings that only N>1 could produce.** None of them is about a model,
and all three would have been invisible at N=1:

1. **Sonnet's cost varies 2.9× on byte-identical work** — $0.548 / $0.231 /
   $0.192 for the same 8 turns and the same 157-byte diff. The Bedrock prompt
   cache persists *across runs*, so run 1 pays `cache_write` at 1.25×
   (125,995 tokens) and later runs read at 0.10× (312,652 by run 3). §5.7
   interleaves execution order, so at N=10 per task each sample's cost depends
   on where the scheduler put it. Sonnet is the only arm with caching, so
   Sonnet-vs-candidate cost is confounded twice: by order, and by the presence
   of caching at all.

2. **Nemotron's wall clock varies 4.7×** — 201.9s / 42.6s / 54.2s at 19/15/21
   turns. OPEN-3 sizing was drawn from a single 311s sample; sizing off 311s
   versus off 42s differs by an order of magnitude across 2,400 runs.

3. **Kimi's failure is deterministic, not a rate.** 3/3 identical: both calls
   return 200, the agent says *"Let me fix it:"* and exits
   `subtype: success, is_error: false` at 3 turns with no edit tool call. The
   earlier `MidStreamFallbackError` did not reproduce. This is the most
   dangerous open failure in the eval — the run is well-formed, terminates
   successfully, and its record is indistinguishable from a model that chose
   not to do the work. Every other failure announces itself with a non-200.

**A recorded premise was wrong and is now corrected.** `TASKS.md` held that the
cache-token cost guard would first fire "at N=10, when the cache warms". The
cache warms on **turn 2 of a single run** (Sonnet call 0 writes 41,723, call 1
reads it back). The guard has never fired for a different reason entirely: the
three candidates return no cache fields at all on the OpenAI-compatible mantle
route, across 9 candidate runs. `smoke_test.py` now reports cache tokens per
arm so this stays measured rather than inferred. The guard is still worth
making loud, on a better argument than the original: a zeroed record reads as
`turns=0, cost=0` with an empty `trajectory_parse_error`, which is exactly what
Gemma legitimately produced three times in this same run.

---

## Two adapter defects that were being measured as model failure — 2026-08-08

Kimi K2.5 went **0/3 to 3/3** with no model change. Both fixes were needed, and
in this order: the second alone would have converted a visible failure into an
invisible one.

### 1. A pricing failure destroyed the whole trajectory

`cost_usd` raises for the three candidates on any cache token and for a model
absent from the price book. It was called inside `parse_trajectory`'s per-turn
loop, so the raise aborted the parse — and `assemble_record` then replaced the
whole `ParsedTrajectory` with an empty one. Turns, tokens, tool calls **and
destructive events** all zeroed for a transcript that had parsed perfectly.

Caught live: a Kimi run that produced the correct 157-byte diff was recorded as
`turns=0 tools=0 tokens=0 cost=0` — byte-identical to Gemma legitimately dying
on its first call, which the same run produced three times. The signature was
already occupied, so the corruption was unnoticeable.

`TASKS.md` had this as a latent N=10 risk. Both halves were wrong. The cache
warms on **turn 2 of a single run** (Sonnet call 0 writes 41,723; call 1 reads
it back), and Kimi **does** return cache tokens once it runs a real multi-turn
loop. The earlier reading that the candidates returned none came from runs that
died at turn 2 — contaminated by defect 2 below.

`cost_usd` still raises: it is the right primitive, and the guard is what stops
a guessed multiplier corrupting the headline metric. The call site now catches
per turn. `SCHEMA_VERSION` 1.1.0 → **2.0.0**, the first non-additive change —
`cost_usd` becomes `float | None`, where `None` is "price unknown" and `0.0`
keeps meaning a genuine zero.

### 2. LiteLLM mangled Kimi's tool-call ids

Kimi emits `functions.Read:0`. `normalize_anthropic_tool_use_id` rewrote every
character outside `[a-zA-Z0-9_-]` to `_`, so Claude Code received
`functions_Read_0`, stored it, and echoed it back — and Kimi stopped driving
the tool loop, announcing the fix in prose and exiting `subtype: success` with
no edit.

The function's own docstring names this pairing. It was added for the *reverse*
case — a Kimi-format id reaching a native Anthropic deployment — and is
destructive applied on the Kimi path itself.

**Diagnosis was by controlled experiment, not inspection.** The wire log could
not answer it: `proxy_callback._request` prefers `optional_params` over the
inbound body, so it records Anthropic-shaped messages and OpenAI-shaped tools,
and never the outbound messages. Reconstructing the bridge's actual output
(verified against the real transformation, no network) and replaying it:

| tool_call_id | tool calls emitted |
|---|---|
| `functions.Read:0` (native) | 42/42 |
| `functions_Read:0`, `zz:0` (colon kept) | included above |
| `functions_Read_0` (what the bridge sent) | **0/22** |
| `call_abc0` (foreign, no colon) | 4/6 |

Function name in the id is irrelevant (`functions.Edit:0` works 4/4). A simple
"colon required" reading is wrong — `call_abc0` mostly works without one — so
the honest statement is narrower: **the exact string the bridge produced is in
the always-fails class, and preserving the colon is deterministically safe.**

End-to-end confirmation came before any fix was written: disabling the
sanitizer made Kimi run `Read ×2 → Edit → Bash → Bash` and produce the correct
diff, 3/3. That same run is what fired the cache guard for the first time — and
produced exactly the predicted catastrophe, a **successful** run recorded as a
row of zeroes.

**Not a thumb on the scale.** Verified post-patch: Sonnet still emits
`toolu_bdrk_…` and Nemotron `call_3a20…`, both already inside the allowed class,
so the sanitizer was a no-op for them and remains one. The patch is
unconditional and process-wide precisely so it cannot become a per-arm
difference.

**Design points that came out of adversarial review, each verified before
adoption:**

- The patch must **not** hang off `proxy_callback.py`: `runner.py` imports that
  module at harness level, so it would patch litellm in every harness process
  and every pytest run. It lives in its own module, registered separately in
  all three proxy configs — patching only the production config would let the
  §6.6 gate certify an unpatched topology under the same name.
- Patching `common_utils` alone is a **no-op on the response path**:
  `adapters.transformation` bound the symbol with `from … import` and holds its
  own reference. A mutation anchor now reproduces exactly that half-fix.
- Provenance is **observed, not configured**. The proxy writes a manifest with
  its own patch set and its own litellm version; the harness reads it back.
  `Versions.litellm` is the harness's version and says nothing about the
  container, which pins its own — recording "patch active" from our config
  would be configuration reported as observation.

**Result: 3 of 4 arms at 3/3.** Gemma alone still fails, now 9/9 identically.
Two of the three original "model failures" have turned out to be adapter
defects with one-line causes.

**Known exposure, recorded in `TASKS.md`:** removing the sanitizer process-wide
makes `kimi-k2-5-runtime` unsafe, since Bedrock's Converse `toolUseId` enforces
its own character class. No arm in `EVAL_ARMS` is affected.

**Gap left open:** the §6.6 offline gate cannot reach either defect — every
offline deployment uses the `anthropic/` provider, which bypasses the
openai→anthropic adapter entirely. The fix has unit coverage and a mutation
anchor, but the gate would stay green if the patch died.

---

## The offline gate can now catch a dead adapter patch — 2026-08-08

Closes the gap left by the tool-id fix. Every offline deployment used the
`anthropic/` provider, which bypasses the openai→anthropic adapter entirely, so
the §6.6 gate could not reach the tool-id defect **or** its fix — the patch had
unit coverage and a mutation anchor, but the gate would have stayed green if it
died. That is what made the original defect cost a live run to find.

`fixtures/anthropic_stub.py` now serves `/v1/chat/completions` alongside
`/v1/messages` from the same process, so no container plumbing changed. The
offline config gains a third arm on `openai/`, and the stub does two things
that make the check real:

- it issues **Kimi-shaped** tool-call ids (`functions.Read:0`,
  `functions.Write:1`). An id already inside Anthropic's character class would
  make the check vacuous.
- it **rejects a `tool_call_id` it never issued**, with a 400. Without that the
  round trip is unchecked: a mangled id comes back, the stub answers anyway,
  and the gate passes while the adapter corrupts every tool call.

Verified by disabling the patch and re-running: the arm goes NO-GO with
`tool_call_id 'functions_Read_0' was never issued by this stub -- the adapter
rewrote it`. That is the 2026-08-07 defect, caught offline, for free.

**Three divergences between the offline and production configs surfaced while
wiring it up**, each of which would have made the gate certify different
behaviour than it was meant to prove:

1. `use_chat_completions_url_for_anthropic_messages` was missing offline, so
   LiteLLM routed the `openai/` arm to `/v1/responses` — the exact production
   defect measured on 2026-08-07, live in the gate itself.
2. `additional_drop_params: [reasoning_effort]` has to be **per deployment**.
   The global `litellm_settings` entry does not reach an `openai/`
   deployment's param validation; measured here, the arm still 400s with only
   the global entry.
3. The stub answered unknown paths by falling through to its Anthropic
   handler, so a wrong-endpoint bug surfaced as an unhelpful parse error two
   layers away. Unknown paths now 404 with the path in the message.

A config setting that differs between these two files is a §5.2 divergence in
the gate itself, and all three were invisible until an arm actually exercised
the adapter.
