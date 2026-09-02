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
- [~] **Task 12** — End-to-end smoke test — offline DONE; live Gemma 6/6, but the GO does not reproduce (Nemotron flake)

---

## Reviewing the turns fix found the environment, not the code — 2026-08-12

Same two questions as the previous review — does this promote observability,
does it simulate practical use — asked of the whole logging change (~2,700
insertions, schema 3.0.0). The observability answer was mostly yes with
unevenly applied discipline. The practical-use answer was no, for a reason
that is not in any of the changed files.

### The agent could not run tests, and had not been able to for all of Phase 0c

`docker/eval-agent.Dockerfile` installed `ca-certificates coreutils curl git
ripgrep`. No pytest, and `apt`/`pip` cannot reach a mirror during a run by
design. Separately, `fixtures/smoke_task/tests/test_calc.py` does `from calc
import add` while `calc.py` sits at the repo root, so the one verification
command available — `python3 tests/test_calc.py` — put `tests/` on
`sys.path[0]` and raised `ModuleNotFoundError`. **Before the fix and after
it.** There was no command in that image that turned the fixture green.

Spec §3.3 measures a loop: *reads, edits, runs tests, sees failures,
self-corrects*. It terminated after "edits" on every arm. And the symptom was
already sitting in this file, read as model behaviour — Gemma's 30 calls were
"mostly `python3 tests/test_calc.py` repeated". That is a model doing the
correct thing against an environment that could not answer it. An arm that
tried to verify was penalised; an arm that did not try looked equally good.

Four of the "model failures" found so far have now turned out to be adapter or
environment defects. That is the base rate to weigh the next one against.

Fixed: pinned `pytest==9.1.1` in the image (with a build-time version assert,
same argument as `CLAUDE_CODE_VERSION`), `pytest.ini` with `pythonpath = .` in
the fixture, and `smoke_test.assert_agent_can_verify_its_work` as a
**precondition** rather than a run criterion — by the time a record exists the
tokens are spent, and no field can distinguish "the model never verified" from
"the model could not". Red-before/green-after is asserted twice: offline in
`test_smoke_fixture.py`, and inside the built image in
`test_runner_integration.py`, because only the second can show the runner and
the fixture's ini actually meeting.

**The measurement is not redone.** Every N=3 set, the Nemotron 1/16 flake and
both GO/NO-GO calls predate this. P0 in `TASKS.md`.

### Three ways a total loss wrote a clean-looking record

Each is the same shape: something the log exists to make impossible, made
possible by a field that stayed empty.

**The final write was unguarded.** `event_log.write_run(record)` was the last
statement of `execute_run`, outside every `try`, against a module docstring
promising nothing may raise past it. A stale `<run_id>.json.partial` raises
`ImmutabilityError` forever — and `run_id` is deterministic, so the retry
raises too — while a full disk raises `OSError`. Either way the record was
gone after the tokens were spent. `_write_or_strand` now writes
`artifacts/record.unwritten.json` **before** re-raising: loud failure, surviving
data. The re-raise is deliberate; a caller that believed the log was complete
would compute means over a matrix with a hole in it.

**A missing transcript read as a quiet run.** `assemble_record` only parsed
when the path existed, so `trajectory_path=None` sent turns, every token count,
tool calls, destructive events and cost to zero together with
`trajectory_parse_error` still `""` — the single ambiguity the schema's whole
vocabulary was built to eliminate, sitting in the middle of it. The field now
covers "could not be read" rather than "failed to parse", and says `absent`.

**Attribution loss was invisible.** `unattributed_count` had no caller in
`execute_run` and no record field — the gate for it lived only in
`smoke_test.py`, which does not run during an eval. A run whose header stamping
broke wrote a well-formed record with `sampling={}` and empty hashes, verbatim
the failure `proxy_callback`'s docstring was written against. `wire_entries_seen`
and `wire_unattributed` now sit beside the fields derived from the wire.
`wire_unattributed` is `int | None`, and it is a **delta**: `unattributed.jsonl`
is shared across runs, so an absolute count would charge each run with every
earlier run's losses.

Two smaller fabrications went with it. `_sha256(None)` returned the sha256 of
the four bytes `"null"` — an ordinary-looking 64-hex digest, so a record that
observed no system prompt could not be told from one that observed a real
prompt, and two arms that both sent nothing agreed on a hash. It returns `""`
now. And `artifacts.wire_log_gz` was asserted unconditionally, publishing a
path to a file that was never opened.

That last one had a consequence worth recording: the integration test asserting
`len(system_prompt_sha) == 64` had been **passing on the fabricated digest**.
The fake agent sent no `system` block. Both were fixed — the fake agent now
sends what Claude Code sends, and the assertion pins the actual hash rather
than its length.

### The wire log dropped exactly the fields the open §6.4 question turns on

The request projection was six keys: `model, messages, tools, system,
temperature, max_tokens`. `thinking`, `reasoning_effort`, `context_management`,
`output_config`, `anthropic_beta` and `stream` were discarded before write — by
the log designated as the authority on *what was actually sent*, and those are
precisely the params `additional_drop_params` removes **per arm**
(`reasoning_effort` on each candidate deployment, on neither Sonnet one).

So the question "is thinking live on the reference arm and off on the
candidates" had no answer in 856 stored calls. The only evidence available was
an outcome proxy: zero reasoning tokens and zero thinking content on all five
arms, Sonnet included — which says no arm was thinking in the Phase 0c data,
but says nothing about what any arm was asked to do.

Widened, in both projections, as an explicit allowlist rather than `**body`
(the secret scan depends on a known shape). The offline smoke immediately
showed Claude Code sending `thinking: {"type": "adaptive"}` and
`output_config: {"effort": "high"}` **identically on every arm**. What settles
the drop is a live run: `pick()` prefers `optional_params` over the raw body,
so on an `openai/` deployment the field shows post-drop state. Filed P3.

### The setup document contradicted the invariant it was written for

Found while trying to run the live half. `.env.example` — the file an operator
copies to `bakeoff/.env` — told them to paste the mantle key into
`AWS_BEARER_TOKEN_BEDROCK`. That is the one variable `CLAUDE.md` says never to
use: `base_aws_llm.get_request_headers` falls back to it when a deployment has
no `api_key`, so a proxy holding it bearer-authenticates all three `bedrock/`
arms and fails them with `bedrock:CallWithBearerToken`, while the mantle arms
stay green and it reads as a bedrock-runtime problem. Following the setup
document verbatim produced exactly that, on arms other than the one being
configured.

`test_no_mantle_arm_reads_the_bearer_variable_litellm_falls_back_to` pinned the
config and nothing pinned the instructions. Template now names
`BAKEOFF_MANTLE_TOKEN`, keeps the wrong variable as a commented warning so a
reader cannot conclude it was merely forgotten, and
`test_the_env_template_names_the_variable_the_config_actually_reads` plus a
mutation anchor hold it there.

### Verification

288 unit tests, 26 integration tests, `verify_logger.py` **GATE PASSED**, and
`mutation_check.py` **44/44** — seven new anchors, one per guarantee above,
each confirmed to turn a test red when reverted. The fixture anchor deletes
`pythonpath` from `pytest.ini`, which reproduces the Phase 0c environment
exactly.

**The live half did not run: no credentials in this environment.**
`BAKEOFF_MANTLE_TOKEN` unset (all three candidate arms) and the SSO session
expired with `TokenRetrievalError` (the `claude-sonnet-5-runtime` reference
arm). `smoke_bedrock.py` names both correctly and exits 0 with
`preflight 1 problem(s)` rather than pretending. This is the credential
expiry `TASKS.md` already flags as having "a refresh path that does not exist
yet", hit in practice.

Schema 3.1.0. Minor, not major: no stored value changes meaning and a 3.0.0
reader ignores the new fields safely — but the version is load-bearing anyway,
because in 3.0.0 the *absence* of these signals was not evidence of anything.

### Not fixed, filed instead

Roughly twenty findings across P2 and P3 in `TASKS.md`. The ones most likely to
bite: a crashed run's cause exists nowhere in the log (no exception message,
stderr captured and then discarded); `ToolCallStats.errored` holds failed *API*
calls under a tool-shaped name and is the only place retries surface; nine
fields are permanently zero and read as measurements; `TurnRecord` has no
absolute timestamp, so per-turn, checkpoint and wire time bases never join.

---

## Reviewing the cache feature found a bigger one under it — 2026-08-11

A review of the cache work against two questions: does it promote
observability, and does it simulate practical use. It did neither, for one
reason each, and neither reason was in the cache code.

### Every token and every dollar in the log was roughly doubled

`parse_trajectory` counted `type: "assistant"` records as turns. **Claude Code
writes one record per CONTENT BLOCK, and repeats the whole `usage` block in
each.** A reply with text and two tool calls is three records carrying the same
tokens three times.

Measured on stored records rather than argued:

```
run 7be2933b  turns_used=6  assistant records=6  distinct message.id=5
  recorded  cache_write 83,867   cost $0.3726
  deduped   cache_write 42,171   cost $0.2148
run 0a39ee8e  turns_used=7  distinct message.id=5
  recorded  read 270,549  write 22,937   deduped  read 198,063  write 11,652
```

A Kimi transcript shows the mechanism bare: 10 assistant records, 5 ids, shaped
`['text'], ['tool_use'], ['tool_use']` for one reply.

**One API call is one turn**, keyed on `message.id` globally so a tool result
landing between two blocks cannot split a reply. A record with no id gets a key
nothing can match, because merging on absence is the same error in the
expensive direction. `SCHEMA_VERSION` 3.0.0, MAJOR: `turns_used`, `tokens` and
`cost_usd` all change value for the same underlying run.

Re-derived across the archive: **16/16 live runs now match the wire log** on
both call count and `cache_write`, where the stored records matched on neither.
185 of 219 runs had records collapse. `cache_state.warm` moved on **0** records,
which is the check that the dedupe did not reorder turns.

The stub was reusing one `message.id` across distinct calls, so the offline gate
undercounted against its own wire log while every live run stayed correct. Fixed
in the fixture, not worked around in the parser: a stub that reuses ids is not a
stand-in for an API that does not.

Three counts are now stored where one was: `turns_used` (API calls),
`assistant_records` (transcript records), `turns_streamed` (what the CLI itself
counted). Their differences are the collapse. A single number is what let this
sit unnoticed.

### The warm-run cost was the artifact, not the correction

`print_cost` labelled the warm number `cache_normalized`, which asserts it is
the corrected one. The evidence says otherwise.

Every run is a fresh container, a wiped `CLAUDE_CONFIG_DIR`, a fresh repo and a
one-shot `claude -p`. **Nothing crosses runs but Bedrock's server-side cache**,
and the repeats are scheduled 3–5 seconds apart inside a 300-second TTL. 6/6
matrices: run 1 cold at `(0, ~41.7k)`. 10/10 later repeats: warm at exactly
30,506.

And 30,506 is not the work. Hashing the request components across repeats: the
`tools` block is byte-identical (84,627 chars), only the `system` tail differs
by the git SHA, and `30506 + 11187 = 41693 = 41695 − 2`. **The carried prefix is
the tool schemas — task-independent boilerplate.** That closes the open
`TASKS.md` question in the direction it suspected.

So the two numbers are two deployment situations, not a number and its
correction: `first_task` (a developer opening a fresh task, who pays that write)
and `warm_followup` (another task inside the TTL). Both reported, neither
headlined. `--interleave` stays, with the measured fact recorded that a round
takes 1.4–3.9 minutes and therefore **cannot produce a cold run**.

`cache_state.seconds_since_prior_run` puts the deciding fact in the record, so
a reader sees "warm, and the previous run started 3.4 s ago" without the wire
log. The smoke output now says it outright: *ALL INSIDE the 300s prompt-cache
TTL. Every warm run below was warmed by this harness.*

### Six correctness defects, two of them mine

- `_usage_from` could raise `AttributeError` on a `cache_creation` that arrives
  as a list, and store `None` on a null tier that then raised `TypeError` in
  `TokenUsage.__add__` a turn later. Both escape the parse and zero the whole
  record — the 2026-08-07 defect class, reintroduced by yesterday's fix. Every
  field is coerced now, and the function may not raise.
- `last_run_id_for` claimed to be total by construction and was not: a `null`
  index line is valid JSON and raises on `.get`, and a truncation splitting a
  multi-byte character raises `UnicodeDecodeError` from the iterator. It runs
  **outside** `execute_run`'s try, so either one cost the record — and the bad
  line stays in the index, so it cost every later run too.
- The CRASHED filter added yesterday dropped real warmers. `container_crashed`
  comes from an `except Exception` around the whole body, so a run that made
  twenty calls and failed in checkpoint capture is CRASHED. Filters on
  `turns_used` now, which is what "did it warm the cache" actually means.
- `cost_usd` charged nothing for tier tokens with a zero total, and overcharged
  3× when the 1h tier exceeded the total, behind a `max(..., 0)` clamp. Both
  raise now; this module reports gaps and does not estimate them.
- `pricing_basis` was blanked whenever the run total was `None`, while surviving
  per-turn dollars stayed in the record with no book attached. Keyed on any
  priced turn now.
- `from_dict` filtered unknown top-level keys but built nested classes with a
  bare `klass(**data)`, so a 2.2.0 record raised `TypeError` in 2.1.0 code. The
  reader still sees `schema_version` and can decide; raising decided for it, in
  the direction of losing the record.

### Gate

272 unit tests, 25 integration, **37/37 mutations caught** including six new
ones on this path, dry run and the §6.6 gate green.

---

## Cache-handling review — 2026-08-11

A review of the whole cache path, one day after `cache_state` was populated.
The headline: **that fix was right and it covered one arm of four.**

### The same lie, three arms over

`warm` was `parsed.turns[0].tokens.cache_read > 0`, and `_usage_from` defaults
every cache field to `0` when the provider sends none. Gemma and Nemotron
return no cache fields at all — 9 runs each — so every one of their records
claimed it had started against a **cold cache**, an assertion about a cache
whose existence is unconfirmed. `costs.PRICE_BOOK` carries `None` multipliers
for those models for exactly that reason, and the record contradicted it.

Kimi is the worse case: it returns cache tokens on some runs and not others, so
its `false` could not be told from a genuine cold start.

`runner._cache_warm` now returns `None` on three paths instead of one. The
added condition is observational, not a lookup against the price book:

    total.cache_read == 0 and total.cache_write == 0  ->  nothing was accounted

Checked against the measured Sonnet runs before committing to it: the cold run's
turn 1 is `(0, 41695)`, so the run total sees writes and it stays `false`. **No
value that was already a measurement moves.** A test pins that specific shape,
because a guard keyed on `cache_read` alone would have called the most expensive
run in the log undetermined and dropped it from every cost comparison.

Third path: a turn-1 record carrying no `usage` block at all projects to
all-zero tokens, byte-identical to a genuine zero. API-error and replayed
assistant records land there, and turn 1 is where the measurement is taken.

### The tier the log was throwing away

Spec §3.1 asks for cache-creation tokens split by 5m/1h ephemeral tier. Nothing
read it. Verified against the AWS Bedrock prompt-caching page: a **1h write
bills at 2.00×** against the 5m tier's 1.25×, so `cache_write_multiplier=1.25`
was a 5m rate applied with no tier check — and with the tier discarded, a run
priced wrong could not be repriced from the log afterwards.

Exposure today is zero: Claude Code's Bedrock TTL is hardcoded to 5m
(claude-code#32671, the 1h path behind an undocumented
`ENABLE_PROMPT_CACHING_1H_BEDROCK` the env allowlist does not pass). The defect
was that **nothing would notice if that changed.** `TokenUsage` now carries
`cache_write_5m` / `cache_write_1h` as parts of `cache_write`, never additions
to it, so an untiered write is charged once at the 5m rate rather than falling
through to free.

### Sonnet moves to list pricing

$2.00 / $10.00 per 1M, not the Bedrock page's $3.00 / $15.00 — the eval asks
whether a candidate beats buying Sonnet, not whether one AWS SKU beats another.
The log is append-only, so records written before today keep figures from the
old book. `Versions.pricing_basis` now says which book produced a cost, because
a reader summing across the change gets a number that is not a price of
anything and, until now, could not tell.

### One open question closed, in the harness's favour

`TASKS.md` asked whether `cost_usd`'s additive charging matches Bedrock's
accounting. It does. What the check turned up is that the candidate arms reach
that through a round trip that nearly loses it: LiteLLM's Converse transform
folds cache tokens **into** `prompt_tokens` (OpenAI usage is inclusive,
Anthropic's is not) and the anthropic adapter subtracts them back out. Nothing
pinned it, so an upgrade dropping the subtraction would have double-counted the
cached prefix — on a warm Sonnet turn, ~30k of a ~34k prompt — with no
exception and no zero, just a wrong number. `tests/test_usage_accounting.py`
now pins it against the installed litellm.

### §5.8's other controls

Of the spec's three cache-confound controls, only "log warm and the prior run
id" existed. `smoke_test` is arm-major and ran repeats 15 seconds apart, which
is precisely the confound — and every cost figure in `TASKS.md` came from it.
`--interleave` is the control; `print_run_order` reports whether the ordering
actually used separated the repeats, because §5.8 says that must be verified
rather than assumed, and it reports honestly under the default too.
`print_cost` gives raw and warm-only side by side. No normalization formula was
invented: on two independent N=3 sets a different repeat was the expensive one,
so there is no fixed warm-up to subtract — only a run that paid the write.

### Left open, deliberately

`prior_same_task_run_id` skips CRASHED runs now (a run that died before its
first call warmed nothing, and the index already carried `outcome`). Two limits
are recorded rather than fixed: the cached prefix is plausibly
task-*independent*, which a measurement settles and a wider key would only
paper over; and the lookup is unsafe under §5.7 parallelism. Both are in
`TASKS.md`.

### Gate

251 unit tests green, **32/32 mutations caught** including six on this path, dry
run and the §6.6 gate both pass. The offline smoke now prints `at_start=?` for
all three arms where it used to print `cold` — the stub returns no cache tokens,
so undetermined is the true answer.

---

## `cache_state` stops lying — 2026-08-11

`execute_run` never passed `cache_state`, so `assemble_record` fell back to
`CacheState()` and **every record ever written asserted `{"warm": false,
"prior_same_task_run_id": null}`**. Not an empty field a reader could see — a
false positive claim, on the axis Sonnet's 2.9× cost spread turns on.

Same shape as the defect `test_fault_injection.py` already warns about in its
opening paragraph: *"a 429 asserted directly at classify_exclusion passes today
while the path that would carry it in a real run does not exist."* A parameter
nobody passes is invisible to any unit test of the function that takes it.

**What `warm` had to mean, decided from the provider docs and then from the
data.** Verified against the AWS Bedrock prompt-caching docs, 2026-08-11:
`cacheReadInputTokens` reports "how many tokens were read from the cache…
because of your previous request"; the TTL "resets with each successful cache
hit"; **Sonnet 5 is absent from the doc's TTL table**, so the exact window is
unverified. Cross-region inference — which `claude-sonnet-5-runtime` uses via
the `us.` geo profile — "may lead to increased cache writes" under load. Two
consequences: warmth must be *read*, never inferred from elapsed time, and a
cold turn 1 does not imply the TTL expired.

Then the archived records settled the definition. Per-turn `(cache_read,
cache_write)` for both 2026-08-11 Sonnet N=3 runs:

| started | turn 1 | cost |
|---|---|---|
| 08:40:40 | **(0, 41695)** | **$0.547** |
| 08:40:56 | (30506, 11187) | $0.216 |
| 08:41:10 | (30506, 11187) | $0.176 |
| 17:41:51 | **(0, 41696)** | **$0.373** |
| 17:42:07 | (30506, 11187) | $0.176 |
| 17:42:22 | (30506, 11188) | $0.216 |

Turn-1 `cache_read > 0` separates the expensive run from the cheap ones **2/2**,
and it is the first-started run each time. That is the entire 2.9×/2.5× spread.

Two readings this kills:

- **The run total measures nothing.** The cache warms on turn 2 *within* a
  single run — the cold run above is reading 41695 by turn 4 — so summed over
  the run, every Sonnet run looks warm. `smoke_test.print_cache_tokens` had
  already recorded this on 2026-08-07; it just had not been connected to the
  field.
- **Warm ≠ no writes.** A warm turn 1 still writes 11187 tokens, a partial
  prefix match. Anything keyed on `cache_write == 0` would misclassify all of
  them.

**Implemented.** `warm` is `per_turn[0].tokens.cache_read > 0`, derived inside
`assemble_record` from the parsed trajectory so no caller can forget it — the
old `cache_state` parameter is gone, replaced by `prior_same_task_run_id`, which
is the only half a caller supplies. `EventLog.last_run_id_for(task_id, model)`
reads `index.jsonl`, which already carries both keys.

Three decisions worth their reasoning:

- **`warm: bool | None`, schema 2.1.0.** `None` is undetermined, not cold: with
  no turn parsed nothing was observed, and a reader filtering for cold runs must
  not silently collect parse failures. The bump follows the 1.1.0 `isolated`
  precedent rather than the 2.0.0 `cost_usd` one — `None` is falsy so nothing
  raises, but a reader still has to tell a 2.0.0 `false` (a default, meaningless)
  from a 2.1.0 `false` (a measurement).
- **Keyed on model as well as task**, despite the field's name. Bedrock's cache
  is per-model; a Sonnet run cannot warm Nemotron's. Keyed on task alone, the
  stored id would routinely fail to explain the `warm` beside it.
- **Last index line, not max `started_at`.** The index is appended after the
  record is durable, so line order is *completion* order — and the run that
  finished most recently is the one that most recently touched the cache. The
  two coincide under today's sequential runner and diverge under §5.7's
  interleaving, where completion order stays the causally right one.
  `last_run_id_for` is total by construction (missing file, unreadable file,
  torn trailing line all yield `None`), which is what lets `execute_run` call it
  before the container starts without endangering "a run always produces a
  record".

**Tested at the level the defect lived at.** Every unit test here would have
passed while the field was unreachable, so the load-bearing one goes through
`execute_run`: `test_execute_run_records_cache_state_and_the_run_that_warmed_it`
runs two fake runs against one event log and asserts the first is cold with no
prior run, the second warm and pointing at the first.

Both undetermined paths are pinned, and the second matters more than it looks:
a run with no transcript is obviously unmeasured, but a run whose transcript
*failed to parse* still produces a record that looks complete, and `warm: false`
there would refile a parse failure as a measurement. That test asserts
`trajectory_parse_error != ""` and `warm is None` together so neither can drift.
`warm=None` is also pinned across the JSON boundary, and a stored 2.0.0 record
is pinned to keep its own version string — that string is how a reader knows its
`warm: false` was a default rather than an observation.

Three mutation anchors, all CAUGHT: run total instead of turn 1, `False` instead
of `None` for an unmeasured run, and dropping the lookup argument. 28/28 overall,
241 unit tests, gate PASSED.

Applied to the archived records, the new rule labels exactly one run per N=3 set
cold, and it is the expensive one both times. Note what that check is and is
not: it re-applies the *rule* to `per_turn[0].tokens.cache_read` on records the
old code assembled. It confirms the definition against real data; it does not
exercise the new code path, which only a live run would.

**Found in passing, not fixed:** the docs state `inputTokens` excludes cached
tokens on Bedrock. Whether `costs.py` accounts for that is now a P1 item.

---

## The tool-id patch's exposure, moved into the config — 2026-08-11

`TASKS.md` offered two fixes for `kimi-k2-5-runtime`: scope the patch by
resolved provider, or drop the deployment. The first turns out to be the wrong
one, and for a reason worth writing down.

**The mechanism is real, and it is not Kimi's.** Traced offline against litellm
1.95.0. Below `/v1/messages` the `bedrock/` route splits **by model id, not by
config**, and the two halves behave differently:

```
bedrock/us.anthropic.claude-sonnet-5  -> AmazonAnthropicClaudeMessagesConfig  (Invoke)
bedrock/nvidia.nemotron-super-3-120b  -> None                                 (Converse bridge)
bedrock/moonshotai.kimi-k2.5          -> None                                 (Converse bridge)
```

Claude models take Invoke with a native Anthropic body and never construct a
`toolUseId`. Everything else goes openai → Converse, where
`prompt_templates/factory.py:3717,3886` copies the tool id verbatim into
`BedrockToolUseBlock(toolUseId=)` and `BedrockToolResultBlock(toolUseId=)`.
Reproduced end to end through litellm's own functions:

```
PATCHED   -> toolUseId ['functions.Read:0',  'functions.Read:0']
UNPATCHED -> toolUseId ['functions_Read_0',  'functions_Read_0']
```

**Two corrections to what was written down.** `claude-sonnet-5-runtime` is
exempt *structurally* — it never builds a `toolUseId` at all — not because its
ids happen to be inside the character class, which is what the old note
implied and is a claim that does not generalize.  And the exposure was never
Kimi-specific: `nemotron-3-super-120b-runtime` takes the same bridge and is
safe only because `call_4196d6e081be491194548c6d` already satisfies the class.
The invariant is about the route, not the model.

**Why provider-scoping was rejected**, in the order that mattered:

1. It reinstates the defect it would be fixing. Sanitize for bedrock and
   `functions.Read:0` becomes `functions_Read_0`, which Converse *accepts* and
   Kimi cannot pair — verbatim the 2026-08-07 failure: 0/22 tool calls, prose
   answer, `subtype: success`, no diff. It trades a loud 400 for a
   plausible-looking zero.
2. There is no frame to scope from. `sanitize_tool_use_ids_in_anthropic_messages`
   runs at `handler.py:234`; `_execute_pre_request_hooks` runs at `:250`. A
   ContextVar set in `async_pre_request_hook` is one request too late.
3. Asymmetry hazard, the same class this eval keeps rediscovering:
   `_SEEN_TOOL_USE_IDS` would fill from already-sanitized messages while the
   stream wrapper mints raw ids, so `_uniquify_tool_use_id` would silently stop
   matching and the Gemma collision fix would die on that route.
4. `Versions.litellm_patches` is per-proxy-process. A conditional patch makes
   every record claim a patch that was off for some calls — §5.2 inverted, the
   record asserting a false positive. That would have forced a `SCHEMA_VERSION`
   move; this change does not, because it removes a possible *value* of
   `RunRecord.model` and no field's meaning changes.

**So the patch did not change; the configuration did.** Removing the sanitizer
process-wide moved the character-class constraint out of the library, and the
config is where it now lives. `kimi-k2-5-runtime` is **commented out, not
deleted** — it is the fallback transport if mantle breaks for Kimi the way it
broke for Sonnet, and the restore procedure is kept beside it: uncomment, add
the model to `CONVERSE_SAFE_TOOL_ID_MODELS` with the id shape it actually
returns as the evidence, smoke-test.

`test_no_bedrock_arm_reaches_converse_with_ids_it_would_reject` enforces it
across every `config/litellm*.yaml`, resolving each arm's route through
litellm's own routing rather than a hardcoded list — so a litellm upgrade that
moves Sonnet onto the Converse bridge fails here too. Against the unmodified
config it failed exactly as intended:

```
AssertionError: litellm_config.yaml: kimi-k2-5-runtime routes through the
Converse bridge, where its tool-call ids become toolUseId unsanitized.
assert 'bedrock/moonshotai.kimi-k2.5' in {'bedrock/nvidia.nemotron-super-3-120b'}
```

Mutation anchor `config: put the kimi runtime deployment back on the converse
route` reports CAUGHT. The companion characterization test in
`test_litellm_patches.py` deliberately gets **no** anchor: reverting the
passthrough would make it fail, and an anchor there would reward removing a fix.

**One claim left open on purpose.** That the id reaches `toolUseId` unsanitized
is confirmed offline. That Bedrock *rejects* it is inferred from AWS's
published pattern and is not verified — settling it costs money on an arm
outside `EVAL_ARMS`, and the fix is sound whichever way it falls. Kept as two
separate sentences everywhere it is written down.

`PRICE_BOOK` keeps the `kimi-k2-5-runtime` alias. A price for a route nobody
serves costs nothing; a missing one turns the day the deployment returns into
an `UnknownModelError` raised mid-parse, after the tokens are spent.

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

**Result: Gemma 6/6 across two independent N=3 runs**, 17–23 turns and 8–11
tool calls, correct 157-byte diff every time. The mechanism verified directly
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

**And the first GO did not reproduce.** Run A was GO at four arms 3/3; run B,
immediately after, was NO-GO at Nemotron 2/3 — `agent_finish` at 3 turns after a
single `Read`, having written *"Now let me check the test file"* and then not
emitted the call. Unique ids, zero `(no content)`, `errored: 0`, no API error;
the proxy-side interventions provably never fired on that arm, so unlike the
four above this one survives an adapter explanation.

That is worth more than the GO was. Two consecutive N=3 runs disagreed on the
verdict, which is the same "one observation is not a rate" error the N=3
criterion was introduced to prevent — committed here in the docs an hour before
the repeat run falsified them. The GO claim has been withdrawn from `TASKS.md`
and the Nemotron flake rate is the new P0.

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

---

## Gate 0 — the unrecoverable capture gaps (2026-08-12)

Schema **3.3.0**. Eight observations that were made and then discarded, so each
one read in a stored record as a well-formed zero. All eight are *capture* gaps:
unlike a derivation gap, no offline pass over stored artifacts can invent them
later, so the cost of leaving them grows with every run collected.

Gate: unit 341 green, integration 31 green, `verify_logger.py` PASSED,
`mutation_check.py` **61/61**.

### What the probe found before any code moved

The largest item turned on one unmeasured fact, and three of the four answers
were not what the backlog assumed.

| question | answer |
|---|---|
| which call fires the wire callback | the **outer** `anthropic_messages` only; the nested `acompletion` fires nothing |
| `optional_params` / `standard_logging_object.model_parameters` | **populated, and pre-patch** — `max_tokens: 16384`, no `max_completion_tokens`, no `reasoning_effort` |
| retry identity | `litellm_call_id` and `litellm_trace_id` are both on the kwargs |
| what a failed call does to the callback | fires it **twice**, same `litellm_call_id` |

The second is worse than the "the resolved params are never in scope" recorded
in TASKS.md. `optional_params` is not empty — it belongs to the outer call — so
the old projection preferred it and reported the *pre-rename* `max_tokens` with
the provenance of a resolved param. Ignoring it entirely would have been safer
than reading it.

The fourth revises a claim in TASKS.md: the `served=39 / captured=42` surplus
was recorded as "exactly the three retries of gemma's failing call". It was
three failures each logged twice. Same arithmetic, different cause, and nothing
in the log could separate them.

### The two-hop channel, and why the obvious version is dead

`resolved` needed carrying up from `map_openai_params` to the callback.

Hop 1 (hook → mapping) is a ContextVar and works — the route
`_SEEN_TOOL_USE_IDS` already takes. Hop 2 (mapping → callback) is a **keyed dict
in one process**, because the ContextVar version *passed every unit test and
captured nothing in the real proxy*: `resolved_state: no_capture`, 9 for 9. The
callback runs from a context copied before the hook. A library-level
reproduction shows the opposite, since there everything is one coroutine — so
the offline gate, not a unit test, is what settles this class of question.

`resolved_state` is what made that visible in one run rather than by reasoning.
It was added as a diagnostic and kept: without it a broken hand-off and an
`anthropic/` arm behaving correctly are the same null.

Measured on the offline gate afterwards, on the `openai/` arm:

```
sampling_source = resolved
resolved: max_completion_tokens=16384  reasoning_effort=none  max_tokens=None
sampling:  {'max_output_tokens': 16384, 'temperature': 1.0}
```

That `temperature: 1.0` closes a second thing. `proxy_callback._request`
carried a standing *"KNOWN GAP, to verify at Phase 0c"*: a deployment-configured
temperature appeared nowhere the callback could see, and §5.3 puts sampling
entirely in proxy config because Claude Code has no temperature flag — so
nothing could confirm it was ever applied. It was reaching the provider all
along and was simply not observable.

### The rest

- **`crash_error` + `artifacts.harness_traceback`.** `except Exception: crashed
  = True` kept nothing, so a harness defect and an infra failure were the same
  record — and exclusion is the one mechanism by which results can be massaged.
- **`artifacts.container_stderr`.** The field existed and no code set it, while
  stderr was captured and dropped in the `finally` beside stdout.
- **`agent_exit_code`.** A non-zero exit that still wrote a transcript was
  byte-identical to a clean finish.
- **`transcript_malformed_lines` / `stdout_malformed_lines`.** The first was
  counted with no consumer. The second was not counted at all: the stdout
  reader treated "not an assistant event" and "not JSON" alike, which
  undercounts `turns_streamed` — the count that exists to cross-check the other
  two, and so the miscount the others cannot catch.
- **`scanner_error`.** `scan_destructive` shared a `try` with the trajectory
  parse, so a *scanner* failure left `destructive_events: []` with an empty
  `trajectory_parse_error` — a positive safety claim (§7) manufactured by a
  failure, which is exactly what the comment above it said it prevented.
- **`wire_log_error`.** A wire-log name collision raised inside the run body and
  became `CRASHED` + `container_crashed`, on a run whose container never
  started. Now the run keeps its wire-derived fields and loses only the gzipped
  copy. `artifacts.wire_log_gz` had to gain an **ownership** check to match:
  existence alone publishes the *earlier attempt's* file under this record,
  which resolves and is therefore worse than the null it replaced.
- **`checked_exec` for `git checkout --detach` and `git clean`.** Bare `exec` at
  the one place the codebase argues loudest that checking is mandatory.
- **`ToolCallStats.api_calls_failed`.** `errored` sits inside `ToolCallStats`
  and held the count of failed *API* calls, so it read as a statement about the
  agent while describing the transport.
- **`wire_entries_distinct`.** Callback invocations and logical calls are
  different numbers; see the fourth probe finding.

### `isolated` stopped being an argument

Measured from the networks the container actually joined, via Docker rather
than by probing from inside — `Internal: true` *is* §5.1's property, so
inspecting it costs nothing and needs no timeouts.

One thing had to be measured twice. Docker reports `network_mode=none` as
membership of a network literally **named** `none`, whose `Internal` flag is
**false** — so reading that flag alone labels the one configuration with no
connectivity at all `ROUTABLE`. Right verdict, wrong evidence, and the evidence
is the entire point of the field. The driver (`null`) is what distinguishes it.

`isolated` is now `bool | None`; `None` means the inspection failed, which is
not the finding `False`.

### Not in scope, deliberately

Gate 1 (task manifest, per-task images, a real issue) and Gate 2 (credential
refresh, run-level retry, parallel-safe prior-run lookup, `container.stats()`).
`ToolCallStats.malformed` stays 0 at harness time — §6.4 puts the
adapter-vs-model call offline.

---

## Gate 1 — the harness half of the real-task path — 2026-08-12

Plan: [docs/superpowers/plans/2026-08-12-gate-1-real-task-path.md](../docs/superpowers/plans/2026-08-12-gate-1-real-task-path.md)

`TASKS.md` framed Gate 1 as five missing pieces. That list was right and it was
a list of symptoms; two root causes produce all five.

**Root cause A — nothing had ever validated the harness's INPUT.** Every check
that existed was about the harness (capture, attribution, isolation) or about a
run's output (turns, tool calls, a diff). Nothing asserted that the thing handed
to the agent was a task at all. That is the defect that invalidated every Phase
0c capability figure, and `assert_agent_can_verify_its_work` — added as the fix
— proves a binary is on `PATH`, which is a weaker claim than the one that
matters.

**Root cause B — the eval's operational knowledge lived in a script.**
`smoke_test.py` was the only place that knew the proxy topology, both credential
paths, the image build and the §5.2 dump. A driver written beside it would
either duplicate that knowledge, at which point the Phase 0c gate certifies one
environment and the paid run executes another, or reach into a script from
library code.

### What the precondition actually has to prove

Not "pytest exists" but "**this task, in this image, at this start state, is red
before the reference fix and green after it**". The distinction the whole gate
rests on is one exit code: pytest's `1` means tests ran and failed, while
`2`/`4`/`5` mean the environment is broken. Both are non-zero, so
`returncode != 0` — the obvious implementation — accepts the exact Phase 0c
failure as evidence the bug is present. `tests/test_preflight.py` reproduces
that case end to end and the mutation check confirms the operator is
load-bearing.

It paid for itself on the first real task. `pallets/click`'s suite needs `less`
on `PATH`; without it the pager test closes the borrowed stdout and 189
unrelated tests error in setup and teardown. Invisible on macOS, invisible to
any host-side check, and an agent handed that suite is debugging the image
rather than the bug.

### The start state is not `base_sha`

A real bug-fix PR carries the test that proves the fix, so at `base_sha` the
oracle does not exist and §3.3's loop has nothing to run — the Phase 0c
truncation arriving through the dataset instead of the image. The test half is
therefore applied and **committed**, and that commit is what the container
detaches to: the submission diff does not carry the test patch, and
`restore_paths` restores the patched tests rather than deleting them.

Fixed author, committer, date and message make the commit a pure function of
(base_sha, test half, gitignore_extra), so `start_sha` can be pinned in the
manifest and a mismatch is a load error. That is what catches a re-cut patch.

One reference diff, split at load rather than two hand-maintained patch files.
The split is a partition and is checked as one, so nothing can be dropped from
the thing the offline grader will compare against.

### Three things the design got wrong before it was written down

Recorded because the plan document was iterated twice before any code:

- **Per-test results by report parsing.** Rejected: pytest's JUnit `classname`
  cannot be mapped back to a node id unambiguously, so the parser becomes a
  second thing that can be wrong about what happened, inside the check that
  exists to be right about it. Explicit selection and `--deselect` reduce the
  whole verdict to exit codes and `FAILED` lines.
- **Resume against an edited task.** `task_version` is not part of `run_id`, so
  editing a task leaves every existing cell looking complete and the matrix
  silently mixes two tasks under one `task_id`. This was the most damaging hole
  in the first draft and it is the exact class Gate 1 exists to close. Now a
  hard refusal, per cell.
- **`.gitignore` as a file to check for.** The property that matters is that
  running the suite leaves the tree clean; a `.gitignore` that does not cover
  what *this* suite drops passes the filename check and fails the real one.

### The defect found while verifying, which is the reason to verify

`verify_logger.py` failed once, then passed. The tempting reading is "flake".
The record said otherwise:

```
ContainerError: git add -A failed (exit 128): fatal: unable to stat 'calc.py.tmp...'
```

Checkpoints are captured **while** the agent edits, and Claude Code's Write is
atomic — it creates `<name>.tmpXXXX` and renames it. A rename landing between
git's readdir and its stat makes `git add -A` exit 128. And because
`maybe_capture` is called from inside the agent's stdout loop, the exception did
not lose a checkpoint: it unwound out of `ClaudeCodeRunner.run`, past the
container context manager, into `execute_run`'s catch-all, and the run was
recorded as **CRASHED with zero turns, zero tokens, no diff and no cost** — for
a run that was working. One offline arm in seven.

Three changes, because any one alone is a worse fix:

- **Retry** the lost race, bounded, and only for that message. `--ignore-errors`
  would drop the file from the snapshot and report success, which is a wrong
  diff rather than a missing one.
- **Contain** the residual failure. Checkpoints are supplementary evidence
  (§5.5's curve); the trajectory and the final diff are the run's product, and
  the tokens are spent by the time the recorder is called.
- **Name the gap** (`checkpoint_error`, schema 3.5.0). Containment alone swaps a
  loud wrong record for a quiet one: a short `checkpoints` list is
  byte-identical to an agent that changed nothing for two turns, and the §5.5
  curve is computed post-hoc over exactly that list.

`force_capture` is deliberately still allowed to raise — it runs after the agent
exits, has no race left to lose, and its diff *is* the submission.

### Schema moved twice, both times for a field's meaning rather than its shape

3.4.0: `task_set_commit` is populated. An empty string used to say "no dataset
exists" and now says "this task did not come from a task set" — same bytes,
different claim.

3.5.0: `checkpoint_error`. Before it, an empty `checkpoints` list meant either
an idle agent or a crash somewhere else entirely.

### Not in scope, deliberately

The dataset itself (~80 tasks, §3.5) and everything in Gate 2. A hand-written
task Dockerfile escape hatch is deferred: every escape hatch is a way back to
the two structural failures the generator makes impossible.

### The result

Four arms, N=1, `pallets/click` #3360, 2026-08-12 (`eventlog-gate1`). Four
records, exit 0: no stranded cells, no uninterpretable rows, every arm isolated
with full wire attribution and `sampling_source: resolved` on all three
candidates.

| arm | turns | tools | wall | cost | diff | terminated_by |
|---|---|---|---|---|---|---|
| claude-sonnet-5-runtime | 16 | 15 | 98.9 s | $0.349 | 618 B | agent_finish |
| kimi-k2-5 | 23 | 22 | 62.3 s | unpriced | 1,485 B | agent_finish |
| nemotron-3-super-120b | 40 | 40 | 101.3 s | $0.180 | 2,493 B | **turns** |
| gemma-4-31b | 40 | 38 | 369.5 s | unpriced | 1,215 B | agent_finish |

A hand-run of the oracle over the four stored diffs — not the harness, which
still never grades — says all four **resolved** it: every submission applied
cleanly to the start state and both f2p and p2p pass after restoring the test
files (§4.2.1 check 2). Nemotron's submission rewrites `write_usage` more
broadly than the reference does and still passes, which is the property the
task was chosen for: its tests assert rendered output rather than an internal
name.

Three things that matter more than the pass rate:

- **This task would be dropped by §3.5's own rule.** All four solving it makes
  it a ceiling task with no discriminating signal. Right task to prove Gate 1,
  wrong task to keep in a frozen set.
- **The 40-turn placeholder cap is already binding.** Nemotron terminated on
  `turns` with a working submission in hand. §5.4 sets caps from the
  calibration pilot's slowest-converging model; until that runs, no
  turn-limited result means what it looks like.
- **Two of three candidate rows were unpriced**, and the two priced rows were
  Sonnet and Nemotron. On a 20k-line repository the cache is warm nearly every
  turn, so the pricing hole is the normal state of a candidate row rather than
  an edge case — much worse than it looked on a 157-byte fixture.

Also confirmed on real data: the submission diff contains only `src/`, never
the test half, so committing the oracle into the start state does what it was
designed to do.

---

## The two cheap pre-collection items, and the identity defect found under them

**2026-08-13, schema 3.7.0.** 474 unit tests, 37 integration, 91/91 mutations,
`verify_logger.py` PASSED.
Planned across six adversarial review rounds (47 findings, all fixed) before any
code was written; the rounds are why the last three items below exist at all.

### 1. The provider's own finish reason

The record described the end of a run only in Claude Code's translated
vocabulary — `per_turn[].stop_reason` from the transcript, `terminated_by`
derived from the last of those. litellm's mapping from the route's own word was
captured in the wire log and reachable only by decompressing an artifact, which
is what answering "did nemotron finish or get cut off?" cost by hand.

Both capture paths now stamp `metadata.finish_reason`; the record carries
`finish_reasons` (a histogram) and `terminal_finish_reason` beside
`terminated_by`. Measured: all four arms log an OpenAI-shaped response, so
`choices[0].finish_reason` is uniform — including `claude-sonnet-5-runtime` on
the bedrock Invoke route.

**Record-level, not per-turn, and that was checked rather than assumed.** No
join key exists: the transcript keys on `message.id` (`msg_bdrk_…`), the wire
log's `response.id` is a litellm-generated UUID, `TurnRecord.bedrock_request_id`
was `None`, and `proxy_callback` never writes one at all. Joining by position is
wrong on any arm that retried — nemotron's stored run is 44 entries against 40
successes.

### 2. The host block

`RunContainer.stats()` had zero callers. All 24 stored records carry
`{"contention_flag": false, "cpu_pct_p95": null, "mem_peak_mb": null}` — two
honest nulls and one positive claim ("this run had the host to itself") produced
by a dataclass default.

`HostSampler` holds a `stats(stream=True)` generator for the life of the agent.
Verified against a real container: `cpu_pct_p95` 99.4 on a one-core busy loop,
`vm_cpus` 2, `samples` 3, `error` empty.

Three things the review rounds changed about this design:

- **`contention_flag` as first drafted could never fire.** `docker info` NCPU is
  **2**; `os.cpu_count()` is **18**. A `loadavg/cpu_count` threshold needs host
  load above 18 while the agent's budget is a 2-vCPU VM, and pairing it with a
  container percentage scaled by `online_cpus=2` puts two machines' denominators
  behind one boolean. The flag now claims one narrow thing — another
  `bakeoff.eval_agent` container shared the host — and `load_p95`, `vm_cpus` and
  `cpu_pct_p95` are recorded for an offline view to interpret. The harness
  measures; it does not decide.
- **It then fabricated `False` a second way.** `any([]) is False`, so gating on
  frames rather than on peer observations turned "no docker client / every poll
  failed" into "nobody else was here". Gated on `_peers`, with the poll failure
  named in `error`.
- **The first stream frame is a trap.** Measured: it carries `precpu_stats` with
  `cpu_usage` and **no** `system_cpu_usage`, so `pre.get("system_cpu_usage", 0)`
  deltas against the absolute system total — enormous, positive, and passing any
  `sys_delta <= 0` check. A fabricated ~0.000003% reading on every run. Guard on
  the key; keep the `.get` beside it or the guard is dead code no mutation can
  catch.

And the containment nearly cost what it was protecting: `stop()` joins a thread
`start()` may never have started, that raises, and it is called from
`execute_run`'s **outer `finally`**, which is not inside a `try` — so the
exception escapes `execute_run` and the run produces **no record at all**, not
even `record.unwritten.json`. Both `start()` and `stop()` are now total.

### 3. Collection identity — found while measuring item 1

`eventlog-gate1`'s gemma record claims `wire_entries_seen: 40`; the file its
`artifacts.wire_log_gz` points at holds **23 lines**, written seven hours later.

Root cause is not the path. `run_id = sha256(task|model|sample|attempt)` **names
no collection episode**, so identity is unique within a matrix and not across
one. The artifacts path repeated the mistake in path form —
`CACHE/artifacts/<cell>`, unstamped while `wire_dir` on the next line was
stamped — and `run_cell` rmtree'd it before every attempt, so the delete landed
before `execute_run`'s `mkdir` and `WireLogger`'s `"x"` never fired.

Census across all 10 stored event logs: 24 records, **9 distinct `run_id`s, 6 in
more than one log** (`11ab9cf6527a188f` in seven), and **12 records whose
`wire_entries_seen` disagrees with the file they point at**. Every matrix
summary reports `stranded: []`.

Fixed by stamping the root, deleting the rmtree — a delete keyed on a path with
no `attempt_number` re-arms this the moment run-level retry lands — and adding
`collection_id` to the record and to the event-log index, which is the surface a
merging reader scans. `smoke_test` and `dry_run` pass one too.

**Not repaired, and cannot be:** the log is append-only and those artifacts are
gone. `TASKS.md` carries the caveats a reader of pre-3.7.0 records needs.

### What the §6.6 gate caught that six review rounds did not

`collection_id` reached `assemble_record` and never `execute_run`. Every real
caller — `run_matrix`, `smoke_test`, `dry_run` — raised `TypeError`, and **all
472 unit tests passed**, because every one of them drives `assemble_record`
directly. The gate failed on the offline smoke, one layer from a paid run.

Same shape as the `cache_state` defect that gave `test_fault_injection.py` its
first rule: *a parameter nobody passes is invisible to a unit test on the
function that receives it.* Fixed in three parts — the parameter, an end-to-end
test through `execute_run`, and a mutation anchored on that call site, since the
record-level mutation cannot see this half.

### Verification, measured

| check | result |
|---|---|
| unit suite | 474 passed |
| integration | 37, including two new ones against a real container |
| `mutation_check.py` | **91/91** |
| `verify_logger.py` (§6.6) | **GATE PASSED** |
| offline matrix ×2, two event logs | 8 records, all `OK` |

The finish reason, on the offline matrix: `terminated_by: agent_finish` beside
`terminal_finish_reason: "stop"` — the translated and the raw vocabulary
agreeing, recorded separately for the first time. The two arms with no stub
produce `finish_reasons: {}` and `terminal_finish_reason: null` against
`wire_entries_seen: 2`, which is "nothing returned" and not "nobody counted".

The host block, same run: `samples: 1`, `mem_peak_mb: 136`, `vm_cpus: 2`, a real
`load_p95`, `contention_flag: false` **measured**, and `cpu_pct_p95: null` —
correct, not broken. A 0.6 s stub cell yields one frame and the first frame
yields no percentage by construction. The arm that ended in 0.3 s with zero
frames reports every field `null` including `contention_flag`, which is the
tri-state doing its job.

The collision, before and after:

| | records pointing at a file that is not theirs |
|---|---|
| pre-3.7.0 | **12 of 24** |
| post-3.7.0 | **0 of 8** |

The second matrix ran the same four cells under a second event log. All four
`run_id`s appear in both collections — unchanged and intended — and every record
keeps its own artifacts, separated by `collection_id`. Before this change the
second run would have deleted the first's.

### Live confirmation on real Bedrock traffic

**2026-08-13 18:49Z**, four arms, click #3360, N=1, schema 3.7.0. 4/4 records,
no stranded cells, no exclusions, no infra problems, `wire_unattributed: 0`.
Credential window read and printed before the proxy started: `usable until
2026-08-13T19:49:30Z (sts)` — one hour, the third independent measurement.

Every 3.7.0 field populated on live traffic:

| arm | turns | `terminal_finish_reason` | `host.samples` | `cpu_pct_p95` | graded |
|---|---|---|---|---|---|
| claude-sonnet-5-runtime | 18 | `stop` | 96 | 23.9 | **resolved**, 1623/1623 |
| kimi-k2-5 | 16 | `stop` | 62 | 27.8 | **resolved**, 1623/1623 |
| gemma-4-31b | 40 (cap) | `tool_calls` | 460 | 8.1 | failed, no regressions |
| nemotron-3-super-120b | 2 | `stop` | 5 | 29.4 | failed, 0-byte diff |

`vm_cpus: 2` on every arm, `contention_flag: false` **measured**, `error: ""` —
no sampler failure on any live run, including the 463-second one.

**The finish reason earned its place on the first live run.** Nemotron emitted
the text *"Let me check the formatting.py"* and then ended the turn with no tool
call — `end_turn`, 108 output tokens, nothing edited. The record now says in one
read that the **provider itself sent `stop`**: not `length`, not an error, no
failed calls. That rules out truncation and adapter failure without
decompressing anything, which is exactly the §6.4 call the field was added for.

Also measured: `terminal_finish_reason` agrees with the transcript's translated
`stop_reason` on all four arms (`stop`↔`end_turn`, `tool_calls`↔`tool_use`). The
mapping is faithful here. That is a claim the record could not previously make
in either direction.

Two known items re-triggered, both already tracked: gemma and kimi priced `null`
(`cache support unconfirmed, saw cache_read` — and they *did* return cache
accounting this time), and gemma hit the placeholder 40-turn cap.

## The prune cache was a claim, not evidence — 2026-08-14

Started as coverage work: five lines of `1d6bafd`'s pruned-mirror code were
uncovered (`_strip_derived`'s directory limb, both `_verify_pruned` raises, the
marker `except ValueError`, the fingerprint-mismatch limb). Writing tests for
them found **two defects**, both in the cache-hit branch tree, and both
invisible for the same reason: every existing test drove `materialize` against a
*fresh* cache, so that whole branch tree had never executed.

### Defect 1 — the fast path could hand over the answer

`_pack_fingerprint` digests `*.idx` names and sizes, chosen so a run tree's
`git add -A` cannot invalidate it. Measured against git 2.50.1: `git fetch
<upstream> +refs/heads/*:refs/future/*` into the **cached pruned mirror** lands
its objects **loose** — under `transfer.unpackLimit` no pack is written — so no
`.idx` moves, the digest is byte-identical, the fast path returns unchecked, and
`git cat-file -p <the merged fix>` works in the next run tree. Same `start_sha`,
no error, nothing recorded.

That is the leak `1d6bafd` closed, reintroduced one layer up. Fixed by folding a
loose-object count into the digest. The safety property is not "the count is
zero" but that it cannot return to its recorded value without also moving an
`.idx` — packing those objects rewrites an index. Verified it does not
reintroduce the freshening problem the `.idx`-only rule exists for: a run tree
has its own `.git/objects` and no `objects/info/alternates`, and the mirror's
digest is unchanged across a run tree's `git add -A`, `commit`, `gc --prune=now`
and `repack -adq`.

### Defect 2 — the slow path wedged the task forever

A fingerprint mismatch re-verified the cached mirror and **raised** when that
failed — before the rebuild block, so the damaged entry stayed on disk.
Measured: same damage, three consecutive `materialize` calls, three identical
`TaskError`s, marker still present. Every cell of every task on that
`(repo, base_sha)` would die on every re-invocation, with a message that reads
like a prune bug and no instruction to delete anything — the same failure class
the stale-`prune-*.tmp` sweep three lines below refuses explicitly.

Fixed by **deleting** the re-verify limb rather than wrapping it: the whole
cache branch collapsed to one condition, and everything else falls through to
the rebuild. Net −7 lines. The limb existed to skip a rebuild when a mismatch is
benign, but the only benign trigger is an operator repack — and a repack leaves
the digest identical (measured), so it never reached the limb at all. Safety is
unchanged: the rebuild still ends in `_verify_pruned` on the `tmp` it just
built, which still raises and still gates the atomic rename.

### What the tests pin

56 in `test_tasks.py` (was 48), 529 in the suite, **116/116 mutations**, five of
them new. Each new test was checked against the defect it names: the fetch test
**fails** with the loose term removed (the fix is readable in the run tree), and
both heal params **fail** under the old raising branch.

`tasks.py` coverage 91 % → 93 %; all five target lines closed, one of them by
deleting the code.

Two of the five branches are covered by calling `_verify_pruned` directly rather
than through `materialize`, and the docstrings say why: under the collapsed
branch it has one call site, where `base_sha` is present by construction. They
are defensive asserts, and the honest way to cover a defensive assert is to call
it. Without the `base_sha` guard, `_commits_outside` over an empty store returns
`[]` — byte-identical to a correct prune.

### Three comments that were measurably wrong

- `_GC_CONFIG` claimed each of its seven overrides "was measured to defeat the
  prune". Leave-one-out against a hostile global config on a packed repository:
  **two** are load-bearing (`gc.bigPackThreshold`, `gc.writeCommitGraph`). The
  other five are shadowed by an explicit argument elsewhere — `gc.pruneExpire`
  and `gc.cruftPacks` by `--prune=now` on the same command line,
  `gc.reflogExpire*` by the explicit `reflog expire --expire=now --all`, and
  `repack.packKeptObjects` by `_strip_derived`'s unlink. Kept, but the comment
  now says which is which.
- A `.keep` "makes gc refuse the pack entirely" — only if it matches an existing
  pack. A `.keep` naming no pack is ignored by git. The old `stale.keep` fixture
  was therefore vacuous; it is now a real `pack-<hash>.keep`, which **does**
  survive `repack.packKeptObjects=true`, so the unlink is what saves that case
  and it now has a mutation anchor.
- `_pack_fingerprint`'s "nothing writes there, because `origin` is removed at
  build time" — see defect 1.

### The one guarantee still not falsifiable

`_DERIVED_PATHS`' `objects/pack/multi-pack-index` is defensive and cannot be
anchored through `materialize`: `gc --prune=now` deletes an inherited midx
whenever the refs were retargeted first, which `ensure_pruned_mirror` always
does. Isolated: `delrefs=False` → midx survives, `delrefs=True` → midx gone. A
fixture for it would pass with or without the strip. Recorded rather than
papered over with an assertion that proves nothing.

### Also new

`test_the_prune_holds_under_a_hostile_gitconfig` — `GIT_CONFIG_GLOBAL` pointed
at a written file with every measured bypass set. Not for a silent leak
(`_verify_pruned` catches those) but for the other failure: an operator's own
gitconfig making every task refuse to materialize. It asserts the hostile file
is actually being read, because otherwise it would pass vacuously and take two
mutation entries down with it.

## The pruned-mirror handoff closeout — 2026-08-14

Everything HANDOFF.md listed as mine-and-cheap or deferred-on-purpose, in
seven commits, plan iterated to convergence by two independent reviewers
before any code moved (docs/superpowers/plans/2026-08-14-pruned-mirror-handoff.md).

- [x] The six prose defects — including the "vacuous pass" story that was
      false in three places at once, corrected everywhere it appeared.
- [x] The two uncovered IO paths — and the publish message they exposed,
      which named a path the finally-block had already reclaimed.
- [x] `_git` decodes pinned utf-8 with replacement. The old "mangled ref
      gets deleted" claim measured false; the object sweep is the backstop,
      pinned end-to-end with a packed-refs latin-1 fixture (APFS refuses the
      loose-ref spelling).
- [x] `_commits_outside` streams, stops one past `_OUTSIDE_SAMPLE`, and the
      message says "more than 3" instead of a count nobody bounded.
- [x] One flock per repo slug over three readers — the two HANDOFF named
      plus materialize's run-tree clone. Body extracted verbatim to
      `_build_pruned_mirror` so six mutation anchors kept their indentation.
- [x] The pruned mirror publishes at mkdtemp's 0700; the fix was deleting
      the rmtree whose justifying comment was measurably false.
- [x] Found on the way: the mutation harness intermittently read its own
      stale pyc — two entries shrink tasks.py by the same 25 bytes inside
      one second, colliding CPython's (mtime, size) key. Fresh
      PYTHONPYCACHEPREFIX per entry; batch stable 5/5 after.

Review: gates all green on a clean tree — 543 tests, 125/125 mutations
(26 under tasks:), tasks.py 93%, verify_logger GATE PASSED, preflight PASS
with start_sha still 33575cc0. One operational lesson recorded in
HANDOFF.md: never run mutation_check concurrently with another gate — it
mutates tasks.py in place, and the neighbouring suite fails against source
it never shipped. Remainder (unanchored materialize assertion, full-mirror
umask mode, images.py's unlocked git archive) recorded in HANDOFF.md and
TASKS.md, not silently closed.

## Live run of the closeout — 2026-08-17

- [x] `run_matrix --mode live --repeats 1` into a fresh event log
      (`eventlog-closeout-20260817`), credentials from env keys in
      bakeoff/.env (AWS_PROFILE commented out so botocore does not prefer
      the SSO profile over the static pair; mantle token derived from the
      SigV4 session, len 1984). 4/4 records, exit 0, ~$0.53 priced spend.
- [x] Every record: no exclusion, wire_unattributed 0, isolated true,
      terminal_finish_reason captured, collection_id stamped.
- [x] Object sweep on the served mirror: rev-list --all == rev-list base
      == commit count (3130) — the fix unreachable.
- [x] Found live: the cached pre-fix mirror was served at 755 — mode is
      not a fast-path check, so the 0700 fix never reached existing
      caches. _PRUNE_VERSION bumped to 3; rebuild verified 0700, marker 3,
      still pruned. 543 tests, 26/26 tasks: mutations after the bump.

## Offline grader — Task 7: mutation anchors + docs — 2026-08-17

- [x] Seventeen anchors added to `scripts/mutation_check.py`, one per
      guarantee, each restoring a branch that turns something which is NOT
      the model's doing — a flake, a broken image, a missing tool, a mid-run
      snapshot, the harness's own start state — into a stored verdict about
      the model. None pre-existed; nothing was deduplicated.
- [x] Two of them were earned in the review rounds rather than planned:
      oracle's `_existing_prefixes` filter (a quarantine derived outside the
      graded scope deselects nothing, silently) and grader's restore
      checkout (`start_sha`, not `base_sha` — the single most load-bearing
      correction in the design now has its own anchor).
- [x] Two anchors the brief placed elsewhere moved with the code:
      `preflight_cache_key` is public in `preflight.py` now, so the
      "serve a verdict from an older preflight forever" mutation lives there
      while its witness stays `tests/test_run_matrix.py`; the p2p seam is
      `preflight.pass_to_pass`.
- [x] Step 0 run per new anchor before the batch — all 17 CAUGHT
      individually. `mutation_check` isolates its own pyc cache, which is
      what makes a by-hand run reproducible here: a byte-count-preserving
      edit plus a same-second write feeds pytest the stale bytecode on the
      HOST, not just in the eval image.
- [x] The check-9 disjunction still has no anchor, on purpose:
      `trajectory_parse_error` non-empty implies `turns_used == 0` through
      every real path, so the only witness would be an internally
      inconsistent hand-built record.
- [x] Docs: §4.2.1's four stale statements (check-1's diff basis, check-2's
      `start_sha`, checks 3/4/7 as manifest-declared argv, the `resolved`
      sentence's `not_configured` caveat) plus §5.5's "against `base_sha`",
      plus a paragraph naming `grade_failure` as a different claim from
      `failure_class` rather than a rename of it.
- [x] `CLAUDE.md`: the grader command, the "harness does not grade" bullet
      naming `grader.py`/`grades.jsonl` and that the grader never writes into
      the log, the two grader docs in the table, and the §6.6 paragraph
      noting the `task_image` exclusion.
- [x] `task_image` registered in `pyproject.toml` and excluded from
      `verify_logger.py`'s integration leg — registered BEFORE Task 8 writes
      the tests that carry it, so the gate never briefly means something
      other than what CLAUDE.md says it means.
- [x] `checkpoints.py`'s "filled by the offline grader" was wrong about the
      direction, not merely stale: a record is immutable once written and the
      grader appends beside it. Same correction at `runner.py:18`, the
      `tests_passed=None` site, and `classify.py`'s header.
- [x] `HARVESTING.md` gained the grading layer (declare `grading:` or record
      why each key is waived; a declared command is pinned in the image) and
      the leaf-node-id requirement on an explicit `tests.p2p` — the oracle's
      swallow-refusal fails OPEN on a non-leaf id, and the set is authored
      there.
- [x] click's `task.yaml` carries the waiver the same commit requires, so
      the shipped task is not in violation of the document.

Review: 742 unit tests, **142/142 mutations caught, 0 stale, 0 missed** on a
solo run. `verify_logger.py` GATE PASSED with the narrowed integration
selector. `run_matrix --preflight-only` re-earned PASS after the task.yaml
comment moved `manifest_digest` — expected, since the digest hashes raw
bytes — and `start_sha` stayed `33575cc0`, which is the useful half: a
comment does not move the start state. Not verified: nothing here exercises
the grader against a real image; that is Task 8's `task_image` suite, whose
marker this task registered.

## Offline grader — blocker 5 closed, the log has verdicts — 2026-08-17

What was built, across Tasks 1–9 (17 commits, +7918 lines under `bakeoff/`):

- [x] `grade_schema.py` — `GradeRecord`, the grader's own append-only derived
      view, written to `<event-log>/grades/grades.jsonl` and never into the
      log. Nulls that say which null they are: `not_graded_reason` /
      `not_graded_detail`, `assembly_error`, `environment_error` with its
      `environment_error_check`, and `crash_error` — four absences, four
      fields, because `resolved: False` is an accusation against the model and
      every way the GRADER can fail has to land somewhere that is not it.
- [x] `oracle.py` — the manifest IS the oracle. The grader consumes the
      declared `f2p`/`p2p` and derives only the flake quarantine, scoped by
      `_existing_prefixes` so a quarantine derived outside the graded scope
      cannot silently deselect nothing.
- [x] `grader.py` — the nine-check ladder: `patch_non_empty`, `test_restore`,
      `build`, `typecheck`, `f2p`, `p2p`, `lint`, `secret_scan`,
      `destructive_scan`. `run_ladder` is pure over an `env` protocol, so the
      whole ladder is unit-tested against a fake that never starts a
      container; `grade_run` builds the production env.
- [x] `tasks.py` — optional `grading:` section in `task.yaml`; an undeclared
      command is `not_configured`, never a silent pass. Unknown keys and a
      non-mapping section are refused at load.
- [x] `preflight.py` — validates what the grader will actually run (scoped
      p2p, the graded argvs) rather than something adjacent to it, behind a
      versioned cache key: `PREFLIGHT_VERSION` is a key component precisely
      so a verdict written by an older gate misses instead of being served
      forever.
- [x] `scripts/grade.py` — the offline batch, resumable, beside the log it
      never touches. Restarts skip on `run_id` already present.
- [x] Gates: 143 mutation anchors, `task_image` registered in
      `pyproject.toml` and excluded from `verify_logger.py`'s integration leg
      (`-m "integration and not task_image"`), so the §6.6 gate stays offline
      and image-free.

Three defects the project caught by running rather than by reasoning — each
one would have produced a plausible, permanent, wrong verdict:

- [x] **virtiofs stat cache.** `materialize` writes the index on the HOST;
      `git apply --index` does not compare content, it compares cached stat
      data through `ce_match_stat`, and virtiofs reports `st_dev`/`st_ino`/
      `st_uid`/`st_gid` differently on the two sides. Every graded submission
      failed `does not match index` on a clean tree with an appliable patch,
      while plain `git apply` succeeded in the same container on the same
      tree. `APPLY_FAILED` is a `GradeFailure`, so this stamped
      `resolved: False` — the model's patch did not work — on every
      submission of every arm, in an append-only store, over an environment
      difference the model never saw. `_refresh_index` re-stats inside the
      container before the ladder applies anything, and it RAISES on a
      non-zero refresh: unreachability is the argument for refusing, since a
      condition that cannot occur costs nothing to refuse and has no honest
      verdict waiting.
- [x] **gitleaks blind on `/var/folders`.** `scan_secrets` allocated its scan
      and report dirs with a bare `tempfile.TemporaryDirectory()`. Reproduced
      before fixing, same live-shaped AKIA key and the same pinned digest:
      from `/var/folders/…` Docker Desktop mounts a silently EMPTY directory
      and gitleaks reports `scanned ~0 bytes (0)` / `no leaks found` / exit 0
      with no report reaching the host; from `$HOME/.cache` the same input
      gives exit 42 and a report. Byte-identical to a genuine clean scan, so
      it would have stamped `secret_scan: pass` on every record, permanently
      — the same trap `conftest.py` already documents for repo bind mounts.
      Fixed by mounting where Docker can see it AND by `_scan_saw_input`,
      which refuses a clean exit without positive evidence the scanner read
      anything: a finding needs no evidence, it IS evidence.
- [x] **pytest 9.1.1, not the 8.3.5 the brief assumed.** The `-q` summary
      discriminator was pinned against the eval image's real pytest. Six
      shapes checked plus the `(0:01:01)` suffix pytest appends past 60
      seconds, produced by running a 61-second test: anchoring at `s$` would
      read every p2p run over a minute as "no summary line" — as NOT
      MEASURED — on most real suites. gitleaks v8.30.1 was pinned the same
      way: `detect --no-git --source` is GONE at that digest, `dir` is the
      subcommand, and `--exit-code 42` collides with gitleaks' own default
      leak code of 1, which is why a `1` is refused rather than read as a
      finding.

The live proof — `grade.py --event-log ~/.cache/bakeoff/eventlog-closeout-20260817`,
four stored records from the 2026-08-17 live run, verbatim:

```
event log /Users/suhaassurapaneni/.cache/bakeoff/eventlog-closeout-20260817
grades    /Users/suhaassurapaneni/.cache/bakeoff/eventlog-closeout-20260817/grades/grades.jsonl
task set  /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval/bakeoff/taskset  (1 task(s))
grader    version 2, commit a44f5f471c465eb515b52111807792fbe422be9c-dirty

4 graded, 0 not graded, 0 errored, 0 already graded

claude-sonnet-5-runtime
  graded          1
  resolved        1
  not_configured  1
  image_mismatch  0

gemma-4-31b
  graded          1
  resolved        0
  not_configured  1
  image_mismatch  0
  failed          f2p_failed: 1

kimi-k2-5
  graded          1
  resolved        1
  not_configured  1
  image_mismatch  0

nemotron-3-super-120b
  graded          1
  resolved        0
  not_configured  1
  image_mismatch  0
  failed          f2p_failed: 1
```

Per record, `click-3360-write-usage-empty-args`, all four graded in image
`sha256:8942bd4824b8…` with `image_matches_run: true`, oracle fingerprint
`2d3031db243d…`, 7 f2p declared, `exclusion_class: None`, no
`assembly_error`, no `crash_error`, no `environment_error`:

- [x] `claude-sonnet-5-runtime` (7abb8b2a25abc2df) — **resolved true**,
      `grade_failure: None`. 0/7 f2p failed, 0 p2p failed, 0 quarantined,
      `agent_modified_tests: false`. The 547-byte diff was expected to fail
      f2p and did not: it is a correct minimal fix.
- [x] `kimi-k2-5` (960aa0e97cc82488) — **resolved true**,
      `grade_failure: None`. 0/7 f2p failed, 0 p2p failed, 0 quarantined,
      `agent_modified_tests: false`.
- [x] `gemma-4-31b` (11ab9cf6527a188f) — **resolved false**,
      `grade_failure: f2p_failed`, 1/7 f2p failed
      (`test_help_formatter_write_usage[empty-args-long-prog]`) — and
      `agent_modified_tests: true`. Check 2 restored the test half and the
      f2p check then failed on the restored oracle, which is the whole point
      of ordering `test_restore` above `f2p`.
- [x] `nemotron-3-super-120b` (3fbe890adfa040cb) — **resolved false**,
      `grade_failure: f2p_failed`, 7/7 f2p failed. The run hit max turns; the
      submission does not fix the bug.
- [x] Both resolved-true records: `p2p_deselect_requested: 7` against
      `p2p_deselected: 30007`. Not drift — click's own
      `addopts = "-m 'not stress'"` deselects its stress matrix, the exact
      surplus `_check_p2p`'s comment records as measured. The floor claim
      (`p2p_deselected < p2p_deselect_requested` is staleness) stays honest;
      on this task it is vacuous, and the offline view is where a constant
      surplus reads as configuration.
- [x] The log is untouched. `index.jsonl` and all four `runs/*.json` are
      byte-identical to their pre-grade sha256s, mtimes still Aug 16 23:xx;
      only `grades/` is new. Re-invocation: `0 graded, 0 not graded, 0
      errored, 4 already graded`, zero new lines, exit 0.

Review: `run_matrix --preflight-only` PASS (forced past the cache: *f2p red at
start, green after the reference; p2p green both ways; tree clean*), **744
unit tests**, **43 integration** (the 37-selected set plus Task 8's six
`task_image` cases), **143/143 mutations caught, 0 stale, 0 missed** on a solo
run, `verify_logger.py` **GATE PASSED**. A leftover `wire.py` mutation from an
interrupted earlier `mutation_check` was found dirty in the tree and reverted
before any gate ran — the anchor list is only as good as the restore, and a
run that dies between edit and restore leaves a source that still compiles.

Not verified, and the reason to say so: **a clean four-line run is not
evidence the gate set is right.** None of the refusal gates — crashed,
no-turns, excluded, image mismatch, scope-collected-nothing — can fire on this
log, because all four records are well-formed. Their witnesses are the unit
tests and the mutation anchors, not this run. What this run proves is
narrower and is the thing that was missing: the store now holds derived
verdicts about models, produced offline, from stored diffs, in the pinned
image, beside a log that was not modified to hold them.

## 2026-08-20 — Codex-judge review fixes (branch codex-judge, 05c6812..cd611b1)

A 10-finder + adversarial-verify review of the codex-judge branch surfaced 15
findings; 13 were fixed across 8 plan tasks plus one final wave
(`docs/superpowers/plans/2026-08-20-codex-judge-review-fixes.md`), each task
TDD'd, per-task reviewed, and re-reviewed after fixes.

- [x] The reasoning effort is part of a verdict's identity: `_resume_key`,
      `_rubric_key`/`_pairwise_key`, `_judge_generation` (now 4 fields) all
      carry it; gate-decided units key on `None` because nothing is sent.
      Stored and batch sides derive identically for every input including
      `""` (normalized `or None`) and `judge_sampling: null` (hand-edited
      line). Seven generation-keyed `sorted` calls moved onto
      `_deterministic` — `None` beside `"high"` raised `TypeError` out of
      `summarize`, at the end of a paid batch.
- [x] `_run_codex`: every escape from `communicate` kills the process group
      (`start_new_session=True` means Ctrl-C never reached the child), and
      both pipes decode `utf-8/replace` so one bad diagnostic byte cannot
      discard a paid verdict. The timeout path now folds usage the recovered
      stdout already reported (exit-124 synthetic run; never bumps
      `calls_without_usage`).
- [x] The stop signal reaches both backends (mantle's `live_completion`
      refuses at entry and refuses the pre-mint retry) and the codex backoff
      waits on the event (`stop.wait`), not `time.sleep`.
- [x] The CLI refuses what it silently dropped: `--reasoning-effort` with a
      mantle id, a mixed-case `codex:` prefix, and non-`[a-z]+` effort values
      (choices= at the CLI, ValueError in `judge_event_log` and
      `build_codex_argv`); `codex_harness` records `effort or None`.
- [x] Resolution re-checks its documented invariants: the judge home refuses
      `config.toml`/`AGENTS.md` beside `auth.json`; the binary must be an
      executable file, not a path that exists.
- [x] `_walk_concurrently`: the worker budget counts running futures and a
      `FIRST_COMPLETED` wake loop tops up behind a slow head (the plan's
      counter-only fix was measured insufficient — the main thread had no
      wake point); buffer capped at `2 × width`, discard bound now
      `2 × concurrency − 1` in code, help, and spec. Cap and wake both
      mutation-pinned.
- [x] The paid smoke's attestation assert can fail (`!= "unattested"`).

Skipped with reasons (review outcomes recorded): `turn.failed` outranking an
exit-0 written verdict (contradicts a documented design choice on a
speculated codex behavior — revisit on live evidence), and the unserialized
concurrent mantle mint (bounded waste; single-flight refresh is its own task;
the expensive post-abort half is closed by the stop event).

Review: full offline suite **1091 passed** (was 1068), every task's diff
reviewed against its brief plus a whole-wave final review (verdict: ready to
merge, zero Critical/Important). Not verified, and the reason to say so: the
codex-side changes are pinned offline against a faked `_run_codex`; the two
real-process tests cover the kill and decode paths, but the seat, the real
`codex exec` flag set, and whether `codex login` writes a `config.toml` into
a fresh home (which would now refuse, loudly) are still live-run questions —
V1–V9 of the codex-judge plan remain the gate before a paid pass.

---

## 2026-08-20 — Judge review round 2 (branch judge-review-fixes, 2698b08..HEAD)

A 7-finder + adversarial-verify review of the judge implementation confirmed 10
defects; all 10 were fixed across 4 plan tasks
(`docs/superpowers/plans/2026-08-20-judge-review-round2.md`), each TDD'd and
per-task reviewed, plus one whole-branch final review and its fix wave.

- [x] **F1** `assert_neutral_judge`'s vendor rule was anchored
      (`startswith("anthropic.")`), so `us.anthropic.opus-6` and
      `bedrock/anthropic.opus-6` — the shapes Bedrock actually hands out — walked
      past it. Now a substring test, and `sonnet`/`opus`/`haiku` joined the family
      tokens so a re-host under a product name is refused too. The guard errs
      toward refusal; `BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE` is the escape.
- [x] **F6** The verdict extractor took the FIRST balanced brace group and raised
      `MalformedVerdict` if it was not JSON. At temperature 0 every retry
      reproduces the reply, so braced prose in front of a valid verdict burned the
      unit deterministically. It now rescans for the next balanced object on a
      decode failure and raises only when none parses.
- [x] **F7** `usage_from_events` returned on the LAST `turn.completed` only, so
      earlier turns' tokens were dropped and an unreadable last block reported
      `None` over readable earlier ones — spend wrong in the low direction, which
      the module's own comments forbid. It now folds every readable block.
- [x] **F9** `codex_environment` popped only `OPENAI_API_KEY`, so an inherited
      `OPENAI_BASE_URL` rerouted judge calls while the record went on attesting
      the seat. Every codex-honored provider variable is scrubbed.
- [x] **F10** `_run_codex`'s Ctrl-C branch killed the process group and re-raised
      with no recovery, discarding usage the stream had already reported — the
      same low-direction defect the timeout branch had already been fixed for.
      The interrupt path now folds recovered spend.
- [x] **F5** `similarity`: `allow_extra_paths` files were not dropped from
      `_kept_chunks`, inflating `file_overlap` and `diff_size_ratio` in every
      payload the judge sees — a lexical path is never the path the manifest
      drops, and the path feeds a filter, so it moved numbers rather than only
      prose.
- [x] **F8** The run-read loop read every graded run before `--only-task` /
      `--samples` filtering, so one corrupt record forced exit 1 on passes that
      never selected it. Reads are filtered first; a run no filter selected does
      not decide the pass's exit code.
- [x] **F4** `graded_against_manifest_digest` and `TaskManifest.manifest_digest`
      were both held and never compared, so a task edited between grading and
      judging silently anchored the payload to a different manifest than the gate.
      Compared now — and the fix round closed the second half: a digest check with
      nothing on one side used to report a clean pass.
- [x] **F2** Gate-decided lines write `judge_sampling={}` (correct — nothing was
      sent), so `_judge_generation` read their effort as `None` and a codex pass at
      `--reasoning-effort high` split itself into two blocks. A gate-decided pair
      now belongs to every block its oracle triple gates, and the fix round
      deduplicated the supersession count across those blocks — one line printed as
      two the moment a second effort existed.
- [x] **F3** The vote-over-gate-decided preference was unconditional and its
      docstring asserted the direction rather than checking it, so a NEWER
      gate-decided line was discarded after a re-grade flipped a passing side to
      failed. The direction is now read off `grade_version_seen`, both
      supersessions are tallied, and the votes keep the tie.

Final-review fix wave (this section's own commits): `_gate_bucket` /`_oracle_of`
hand-counted the slice `_GENERATION_FIELDS` exists to stop — `_EFFORT_FIELDS`
now holds both ends, with a pin that the effort is the LAST generation field
(mutation-checked against both a widened slice and a reordered generation).
`_gate_bucket`'s docstring cited a test that is not that pin, and now says so.
`_supersedes` and rule 2 name the blind spot they have — a `--re-grade` at the
same `GRADER_VERSION` is invisible to the direction check — and the
over-claiming "MEASURED" phrasing is gone. `_print_reading`'s two supersession
sentences both said "left out of the numbers above"; a gate line superseded in
one block can still settle a pair in another, and superseded votes stay in
`vote_verdicts` and in the position-consistency probe, so both now say what
happens.

One off-list defect the verification runs found and this wave fixed:
`test_an_interrupt_recovers_the_stdout_the_child_already_wrote` (from F10's
commit) waits 5.0 s for its stub child's marker and then interrupts whatever
happened. Under parallel load the shell misses that deadline about one run in
32, the interrupt lands on an empty pipe, and the usage assertion fails as
`None == {...}` — a scheduling delay reading as "the interrupt path dropped
the spend". The bound stays and now asserts, named, at 30 s.

Parked with reasons, filed in `TASKS.md` (P2 subsection plus one P3 decision):
the bare `assert` in `_stored_payload_path` under `python -O`; the split
key/value secret shape that passes both payload scans; the pre-append gate
re-assert `_rubric_line` has and the two pairwise writers do not; the mantle
401-refresh minting outside `build_lock`; six reuse duplications; the κ caveat
not travelling with the returned summary; a JSON-shaped-but-wrong preamble
still burning a unit at temperature 0; `--only-task` filtering on
`grade.task_id` while cells key `record.task_id`; and `graded_at` on the
judgment line, which needs a `JUDGE_SCHEMA_VERSION` bump and so is a decision.

Review: full offline suite **1129 passed, 46 deselected** — 1128 at the end of
the four plan tasks, plus the one layout pin the fix wave added; 1091 on the
codex-judge branch this one started from. Every task diff reviewed against its
brief, plus a whole-branch final review. Not verified, and the reason to say so: **no live
judge pass ran on this branch.** Every fix is pinned offline — the neutrality
guard against synthetic ids, the extractor against synthetic replies, the codex
paths against a faked `_run_codex`, and the supersession rules against
hand-built judgment lines. Whether a real pass reproduces the two-block split
F2 fixed, and whether a real codex reply exercises the extractor's rescan, are
still live-run questions.

## Broadening 1 — `strip_paths` — 2026-09-01

The corpus's binding constraint was not the loader's strictness but a *date*:
most public repositories added `CLAUDE.md` or `AGENTS.md` at some point, and
every candidate PR after that commit was out. sqlglot — the richest source by
an order of magnitude — lost 116 candidates to a file no bug-fix PR touches.
`strip_paths` removes the named paths in the same fixed-identity setup commit
that already applies the test half and `gitignore_extra`, so the modification
lands in `manifest_digest` and in `start_sha` instead of in a hand-rewritten
history nobody can find upstream.

Five decisions worth keeping:

- **Strip does not imply exclusion from the reference halves.** The tempting
  shape is for a stripped path to be dropped from `solution_diff`
  automatically. That makes the reference stop being the merged PR verbatim,
  and preflight only notices when the missing hunk happens to be one the f2p
  tests need — so the case that survives every gate is a reference that is no
  longer a reference. `allow_extra_paths` already means "neither half" and
  already leaves the file named in `extra_files`, so requiring it keeps the
  combination visible.
- **A path matching nothing raises, in `materialize`.** Not at load — the
  loader never sees the tree. Not in preflight — verdicts there are cached, and
  the code that would silently do nothing is `materialize`. `--ignore-unmatch`
  is what a typo needs to become permanent: preflight's `_CONTEXT_FILES` check
  knows four names, so a mistyped vendored tree would pass every gate into an
  append-only log. The cost is a constraint task authors have to know: a strip
  only applies to a path tracked at `base_sha`, so a file the PR *creates*
  cannot be stripped even when `allow_extra_paths` legitimately names it.
- **The existence check reads `git ls-files`'s output, not its exit code.**
  Measured: `git ls-files -z -- nope` exits **0** with an empty stdout. Same
  silent zero `container._checked_exec` exists to refuse.
- **The strip probe is `-e` OR `-L`; the context-file probe stays `-e`.**
  Measured: for a symlink whose target is gone, `[ -e x ]` exits 1 and
  `[ -L x ]` exits 0. sqlglot's `CLAUDE.md` is a symlink to `AGENTS.md`, so
  stripping the target alone leaves a path the agent's `ls` shows and an
  `-e`-only assertion calls removed. The two callers want opposite answers on
  that input, which is why the predicate takes a flag rather than picking one.
- **The build context is stripped too.** For agent files it does not matter —
  the bind mount replaces `/repo`. For a committed venv it does: the tree is on
  the import path when `image.build` runs `pip install -e .`, so the image
  pins an environment resolved against a directory the run tree does not have.

Two things the validation had to add beyond `_validate_prefixes`, both because
this key's effect is a delete rather than a classification: `.` passes every
existing check and names the whole tree (`PurePosixPath(".").parts` is `()`,
and `is_relative_to(".")` is True for everything), and `git rm` reads
pathspecs, so an unrefused `*` would make what is removed a property of the
tree rather than of the manifest.

Both one-line **call sites** are pinned by default-suite tests rather than by
mutation anchors, and that was round 1 of the plan review's finding: helper
tests leave `build_task_image`'s call and `preflight`'s assertion deletable
with the suite green, and an integration-only pin is deselected by
`addopts = "-m 'not integration'"` on the run anyone actually makes.

`PREFLIGHT_VERSION` 2 → 3, and the grader's prose reference to version 2 became
"2 or later". `SCHEMA_VERSION` did not move: nothing new is written into a
record, and the strip is already visible there as the start sha `to_task_spec`
carries in `base_sha`.

Confound recorded in HARVESTING.md rather than in code: the humans who wrote
the PR had the stripped file. A repository whose `CLAUDE.md` shaped how its
contributors worked is not quite the repository the models are handed once it
is gone, and that belongs in each manifest's comments as a §6.4 caveat.

## Broadening 2 — f2p that cannot be COLLECTED at the start state — 2026-09-01

A task whose fix ADDS a symbol has always had a real shape: the test half
raises `ImportError` at the start state instead of an assertion failing, and
preflight refused it outright as a broken environment. This broadening accepts
it, under a measured, narrow condition — and the plan's own first draft got
the mechanics wrong twice before landing.

- **The brief said exit 2; the gate sees exit 4.** `_Runner.select` passes node
  ids positionally, and pytest answers a node id whose module raises on import
  with a usage error. Exit 2 is a directory or module-path run — how the
  `trucking-doc-extraction` #3 measurement was taken. An acceptance written for
  2 alone would have been dead code on every task preflight actually
  runs. Measured against pytest 9.1.1 and 8.3.5; they agree on every row.
- **`--continue-on-collection-errors` was measured and rejected.** Exit 4 on
  the f2p selection with and without it, byte-identical. It does rescue the
  p2p run, but only to exit 1, and applying it to the *graded* p2p would turn a
  broken import anywhere in the tree from an environment error into a
  `p2p_regression` — an accusation manufactured out of the environment.
  `--ignore=<module>` on preflight's p2p-before, the one p2p argv the grader
  never makes, gives exit 0 instead.
- **The confinement parse alone is `returncode != 0` wearing a regex.** The
  f2p selection imports only the f2p modules, so a missing interpreter
  dependency produces exactly the confined error set the task shape produces.
  The p2p baseline is the second conjunct, and it is why the f2p verdict is
  deferred until after the p2p run rather than the runs being reordered.
- **Equality in preflight, containment in the grader.** Preflight must account
  for every declared id and a partial collection error hides the rest of the
  selection (measured), so equality is the id-level rule at module
  granularity. The grader must never accuse for anything outside the task, so
  a partially fixed submission errors on a subset and is still `f2p_failed`,
  while a stranger module stays an environment error.
- **The mis-bucketing the grader change fixes was directional.** A do-nothing
  arm graded NOT GRADED and a half-fixing arm graded `False`, so the arm that
  did nothing was invisible in every view counting `False`.
- **The acceptance is a THREE-way conjunction, and the first draft of this
  plan claimed two.** A dependency imported *only* by the f2p module is
  confined (the f2p selection imports nothing else) and leaves p2p green (p2p
  never imports it), so neither of the first two conjuncts can see it.
  **Green-after is the environment discriminator** — and the existing Phase 0c
  integration fixture is that shape exactly.
- **The Phase 0c end-to-end pin had to be rewritten, and its mutation anchor
  repointed.** That fixture's broken import lives in the declared f2p module,
  so it became *confined* and was intercepted by the new branch — the
  assertion no longer matched and, worse, `mutation_check`'s revert of
  `elif red.exit_code != EXIT_TESTS_FAILED:` became **inert**, i.e. green on a
  reverted guarantee. The replacement puts the missing module in
  `tests/conftest.py`: measured, that exits 4 with no `short test summary
  info` section at all, so the reported set is empty and the run is
  unconfined.
- **The new `MUTATIONS` entry anchors on the equality, not on the
  `and p2p_green` conjunct.** `if not p2p_green:` is unconditional, so
  reverting that conjunct changes the refusal *message* and not the verdict;
  dropping the equality flips a NO-GO into a GO.
- **What was NOT relaxed:** green-after (pinned by its own test), check 6,
  `oracle._classify`, the graded p2p argv, and `tests.runner` must contain
  `pytest`.
- **Operator note carried into `HANDOFF`-style prose rather than left
  implicit:** `GRADER_VERSION` 2 → 3 makes `scripts/grade.py`'s resume gate
  re-grade **every stored run**, into a fresh `v3` artifacts directory beside
  the existing one. That is intended (a verdict derived under a different
  ladder is a new line whose disagreement with the old one is the finding),
  and it costs a full grading pass per event log — budget for it rather than
  discovering it mid-run.
- `PREFLIGHT_VERSION` 3 → 4, `GRADER_VERSION` 2 → 3, `SCHEMA_VERSION` unmoved,
  no manifest key, `click-3360`'s `start_sha` unmoved.

Two small fixes folded in with the docs, from this task's own review: the
red-before refusal message used to print `sorted(collected) if collected else
'empty'`, but `collected` is also `None` when the exit code fell outside
`EXIT_COLLECTION_FAILURES` and the parse never ran — a case that used to read
as "pytest reported nothing" when the branch never looked. The message now
says which. And `test_an_f2p_module_that_will_not_import_is_accepted_when_p2p_is_green`
is parametrized over exit codes `[4, 2]` — the exit-2 arm, reachable via a
bare-module f2p entry, was unpinned.

## Broadening 3 — hypothesis suites — 2026-09-01

Layer 2 categorically excluded a property-based suite: "a hypothesis-driven
suite can pass a wrong fix on a lucky draw and fail a right one on an unlucky
seed." Five commits (`c41d20a`, `e34aeb7`, `009ac45`, `5f4caab`, `579579e`)
replace that exclusion with a manifest `image.env` key — an allowlist of exactly `CI` and
`HYPOTHESIS_STORAGE_DIRECTORY` — baked into the task Dockerfile as `ENV` lines
after every build step, so Hypothesis's determinism reaches every process in
the container: the gate's runner, the oracle's, the grader's, the agent's own
`claude`, and the commands the agent invents. `pinned_env_keys()` proves the
allowlist disjoint from the keys the harness itself sets, so nothing an image
declares can be silently shadowed on one exec and not another.

Measurements that decided it:

- **§1a's ten-run row.** One property test over a rare input, no seed,
  default profile: `0 0 0 0 1 1 1 1 0 0` across ten fresh runs of unchanged
  code on an unchanged tree. Under `CI=1`: `1 1 1 1 1 1`.
- **The `ci` profile is three settings, not one, registered at import.**
  `derandomize=True`, `database=None`, `deadline=None`, auto-loaded on any of
  twelve CI variables being present — `"CI"` counts on presence alone, any
  value. `derandomize=True` *implies* `database=None` (passing a non-`None`
  database alongside it raises `InvalidArgument`), so seed and database are
  one lever, not two.
- **`HYPOTHESIS_PROFILE` is not an environment variable.** Grepping hypothesis
  6.167.1's site-packages for it returns no matches; the string in
  `pytest --help` is argparse's metavar for `--hypothesis-profile`, and that
  flag with an unregistered profile name is `INTERNALERROR`, exit 3, zero
  tests run.
- **`.hypothesis/.gitignore` self-ignores.** Hypothesis writes it containing
  `*` the first time it creates the directory, so `git status --porcelain` is
  already clean and `git add -A` does not sweep it into a submission diff.
- **`--hypothesis-seed` is exit 4 without the plugin**, identically via the
  CLI and via `PYTEST_ADDOPTS`, in an image without hypothesis — where `CI=1`
  is inert there (exit 0, nothing written). That asymmetry is why `CI` is the
  key and a seed flag is not: a seed flag would break every other task's
  image the moment hypothesis was absent.
- **The Docker merge was measured, not taken from the docstring.** Against an
  image declaring three `ENV` keys, three exec shapes agree on one rule:
  merge, with the exec's own keys winning. No `env=` argument sees all three
  image keys; an `env={...}` argument sees all three plus its own; an
  overriding `env=` sees the override and the other two unchanged. That is
  what makes `container_env` naming none of `_IMAGE_ENV_ALLOWED` load-bearing,
  pinned by `pinned_env_keys()`'s disjointness assertion offline and by
  `5f4caab`'s integration test against a real image and both exec shapes.
- **§1g's constant-mining table, the finding that reshaped Layer 2.** Same
  property, same `CI=1`, varying only a literal in an imported `magic.py`:
  `MAGIC = 137` finds a one-in-a-billion bug `1 1 1 1 1 1`, `MAGIC = 1370`
  misses it `0 0 0 0 0 0`, `MAGIC = 137` again finds it, `MAGIC = 999` misses
  it — reversible, six of six each way. The same literal in a file the suite
  does not import changes nothing. Determinism makes the oracle reproducible;
  it does not make it correct, and the example pool a submission is judged by
  is a function of the source under test — coupled to exactly the modules the
  agent is asked to edit.
- **The `claude` 2.1.220 grep, settled by a live probe rather than left as a
  reading of a 272 MB bundle.** Every `CI` hit in the bundled CLI is colour
  selection or an environment-name function; seven of eight `isCI` matches are
  false positives from bundled zod's `isCIDR`. The offline smoke gate then ran
  the real CLI over the real transport twice — once against the unmodified
  base image, once against a throwaway image carrying `ENV CI=1` appended at
  the end of `docker/eval-agent.Dockerfile`, reverted immediately after. Both
  runs agreed on all three arms: `turns_streamed=3`, `stdout_malformed_lines=0`,
  `turns_used=3`, verdict `go`. `TASKS.md` keeps only what this cannot answer —
  whether a *live* run differs.

Four things the plan's own first draft got wrong, corrected before code was
written:

1. It assumed a `HYPOTHESIS_PROFILE` environment variable existed; §1b found
   none.
2. It assumed `gitignore_extra` was needed for `.hypothesis/`; the tree is in
   fact already clean without it (decision 10), and the entry would have
   moved `start_sha` for no observable change.
3. It assumed a seed and a database were separate levers to pull; measured,
   `derandomize=True` implies `database=None`, so they are one.
4. **Its mechanism for tree-dependence was wrong.** The first draft blamed
   "the agent edits the tree" — adding an unrelated file at the repo root
   holding the falsifying literal changed nothing (`0 0 0`, §1g). The real
   mechanism is Hypothesis mining constants out of the modules the suite
   **imports**, which is narrower than the first draft's claim, worse (it is
   coupled to exactly the files the agent is asked to change), and reversible
   in both directions on the same literal.

Deliberately not built (decision 12): no `tests.env` or per-exec env plumbing
(decision 3's rejected mechanism, because the agent is never told
`tests.runner`); no new `RunRecord`/`GradeRecord` field and no
`SCHEMA_VERSION`/`GRADER_VERSION` bump (decision 8 —
`Versions.container_image_digest` already pins the `ENV` layer as an
observation of the built image); no hypothesis in the base image (a task that
needs it declares `image.pip`, like any other dependency); no unknown-key
rejection for the `image:` block generally; no new task cut from
`attrs`/`cattrs` — their other blocker, an editable install colliding with a
site-packages `attr`, is unmeasured, and `HARVESTING.md` records that rather
than implying the repositories are now usable; no `--hypothesis-seed`,
`--hypothesis-profile`, `HYPOTHESIS_DATABASE_FILE` or
`PYTEST_DISABLE_PLUGIN_AUTOLOAD` anywhere (decisions 1e, 2, 11).

`PREFLIGHT_VERSION` 4 → 5 (the env read-back and the two hypothesis probes are
new preflight assertions). `SCHEMA_VERSION` and `GRADER_VERSION` unmoved —
nothing new reaches a record and no ladder check changes what it means.
`click-3360`'s `start_sha` unmoved: the new key is under `image:`, which is
not an input to `materialize`. Unit suite: 1225 passed, 50 deselected.

Final-review fix wave (this section's own commit): a loader-table row for
`image.env` and two preflight bullets for the hypothesis-without-CI NO-GO and
the rg-could-not-answer refusal, both of which were code refusals listed only
under Layer 2; the `PREFLIGHT_VERSION` 5 comment corrected from "two
environment assertions" to the three that shipped; the ambiguous "hypothesis-
import probe" wording (confusable with the `import hypothesis` probe) renamed
to "hypothesis-import scan (rg over tests.paths)", with the never-ran test's
substring assertion updated to match; a missing `"--"` before the scanned
paths in the rg argv, so a `tests.paths` entry starting with `-` cannot parse
as a flag; two new params on the non-empty-string `_env_map` refusal (`CI: 1`
as a YAML int, `CI: ""` as an empty string), which had no coverage; and the
click `task.yaml` refused-chars comment, which named a newline, a quote, a
backslash and `$` but not `\r`. Verified: full unit suite; the integration leg
including `task_image` (the env-merge test at
`tests/test_integration_grader.py` had never run); a fresh `--preflight-only`
GO on `click-3360-write-usage-empty-args`; and `mutation_check.py` run solo.

## Broadening 4 — a per-task suite timeout — 2026-09-01

The 600 s bound wrapping every in-container command preflight, the oracle and
the offline grader run was a constant three consumers each held their own
copy of. `budget.suite_timeout_s` (default 600, same number) moves it into the
manifest, so a task whose suite legitimately needs more than 600 s can be
gated and graded under a bound the author actually declared, instead of being
refused by the gate or stamped `timed_out` by the grader on a number nobody
chose. One key, not two: it bounds the suite invocations **and** every
declared `grading.*` argv (build / typecheck / lint), because a second key
for the grading commands would be a second number that can diverge between
the gate and the grader — the exact defect being closed, for a distinction
nobody asked for.

- **The parameters are deleted, not defaulted.** `preflight()` and
  `ensure_oracle()` both already take `task`, so a `timeout_s: int = 600`
  parameter beside it would be a second source for one number whose
  divergence is invisible: a suite that fits one bound and is killed under
  the other stamps `timed_out` — a `GradeFailure`, i.e. `resolved: False` — on
  a number the model never saw. Deleting the parameter makes "a consumer left
  on the constant" unrepresentable rather than merely tested for. **Neither
  driver needed a line changed as a result** — `run_matrix.py` and
  `grade.py` call both functions with `task` and never passed a bound today —
  which is the evidence the seam (`task.budget.suite_timeout_s`, read at each
  call site) was the right one rather than a parameter threaded through two
  more layers.

- **`suite_timeout_s > wall_clock_timeout_s` is a `load_task` refusal, not a
  preflight problem and not author advice.** The agent re-runs this suite
  *inside* its wall clock with no per-command bound (`claude_runner`'s
  container backend wraps the whole `claude -p` in one `timeout`, fed
  `wall_clock_timeout_s`), so a longer `suite_timeout_s` describes a task no
  arm could verify even once — spec §3.3 measures a loop that ends in "runs
  tests, sees failures, self-corrects", and a run SIGTERMed mid-suite is that
  loop truncated with an unchecked diff, indistinguishable on the record from
  an honest `BUDGET_EXHAUSTED`. It is a *load* error rather than a preflight
  one because it is settled by arithmetic over two manifest numbers — no
  container, no daemon, no measurement needed to see the contradiction — and
  pushing it to preflight would pay an image build to discover what is
  already visible in the YAML.

- **Versions.** `PREFLIGHT_VERSION` "5" → "6" (broadening 3 had already moved
  it off "4"), `ORACLE_VERSION` "1" → "2", `GRADER_VERSION` "3" → "4",
  `GRADE_SCHEMA_VERSION` "1.0.0" → "1.1.0". `SCHEMA_VERSION` **unmoved** — the
  bound never touches the agent's container, so no `RunRecord` field changes.
  `click-3360`'s `start_sha` **unmoved** — `suite_timeout_s` is a `budget:`
  key, not an input to `materialize`. `manifest_digest` **does** move on any
  manifest that adds the YAML comment or an actual value, because it is
  `sha256(manifest bytes + reference bytes)`.

- **Operator cost, and why it is intended rather than swallowed.** The
  `GRADER_VERSION` bump makes `scripts/grade.py`'s `(run_id, grader_version)`
  resume key treat every stored run as ungraded, so the next grading pass
  re-grades everything into a fresh `v4` artifacts directory beside the
  existing `v3` one — on today's corpus this changes **no verdict** (no
  stored manifest declares the key, so every ladder still runs at 600), and
  it still cannot wait: `grade.py`'s resume key never consults
  `manifest_digest`, so the first manifest edit that raises the bound would
  otherwise find every affected run already marked graded under the old
  behaviour, with nothing on either line saying they were measured against
  different bounds. The bump has to land with the code that makes the
  divergence possible, not with the manifest that first exercises it — so
  it's schedulable rather than urgent, but it is on purpose, the same shape
  as broadening 2 and 3's version bumps. `PREFLIGHT_VERSION` and
  `ORACLE_VERSION` moving invalidates every cached verdict and quarantine the
  same way, deliberately: a warm cache would otherwise serve a verdict
  computed under a bound the manifest no longer asks for.

- **`GRADE_TIMEOUT_S` → `SCAN_TIMEOUT_S`.** The gitleaks secret scan is a
  fixed-size scan of one diff on the *host*, not the task's suite —
  `_ContainerEnv.scan_secrets` has no `task` in scope — so it keeps a
  constant rather than reading the manifest. Renamed because leaving it
  called "the grade timeout" was the trap: a constant with that name invites
  the next consumer to reach for it instead of `task.budget.suite_timeout_s`.

- **The doc correction found on the way.** `HARVESTING.md` said preflight
  runs the suite "four times." It is **five** on the branch every task
  actually takes — f2p-before, p2p-before, f2p-after, p2p-after, and the
  scoped p2p-after the grader will also make — and only four when
  `tests.p2p` is declared explicitly, which skips the scoped run
  (`if not tests.p2p`). On top of that it runs one command per declared
  `grading.*` argv, so the worst case is 8 × `suite_timeout_s` per task at
  the gate, all of it before the proxy starts and inside the one-hour SSO
  session the matrix itself needs. No new gate is added on that worst case —
  it is a bound, not a duration, and nothing yet measures preflight's actual
  elapsed time per task to check it against. That measurement is named as a
  follow-up in `TASKS.md`, alongside the still-bare `int(...)` parse on
  `max_turns` and `wall_clock_timeout_s`.

Unit suite: 1256 passed, 50 deselected (docs-only task; the count reflects
Tasks 1-5's plumbing and test pins, not this task, which touched no `.py`
file).

## Broadening 5 — a per-task Python version — 2026-09-01

- **The `ARG` name is measured, not stylistic.** The `python:` images set their
  own `ENV PYTHON_VERSION` (3.11.16 / 3.12.13 / 3.13.15) and `ENV` beats `ARG`,
  so `${PYTHON_VERSION}` after the `FROM` reads the patch level even when
  redeclared. Latent today; a plausible wrong value the moment anything
  expands it.
- **The default is byte-identical.** The parameterised file with no
  `--build-arg` builds to `sha256:dfd2cc06…`, the same id as the
  unparameterised one, so no stored `container_image_digest` and no cached
  verdict moved.
- **`assert_one_agent` is the check the broadening created.** Several bases
  make preflight's `expected_claude_version` refusal tautological; the
  driver-level comparison is the only place a split agent is visible.
- **No record field.** `execute_run` makes no `claude --version` exec —
  `Versions.claude_code` comes from the transcript — so there was no free
  run-time read to make an observation out of. Digest + manifest is the join,
  as it is for `image.env`, and `SCHEMA_VERSION` did not move.
- **What was not done:** no repository was re-screened at 3.11 or 3.13, so no
  currently-excluded repo has been shown to be reopened by this key (see
  `TASKS.md` follow-up below).

Also fixed on review of Tasks 3-4: `preflight.py`'s `python_observed` no longer
collapses two different absences into the same `None`. A container that starts
but whose `python --version` exits non-zero now records `""` — an observed
empty answer — while `None` stays reserved for the path that never starts a
container at all (`tests.runner` without `pytest`). One line in `preflight.py`
plus one test assertion in `test_preflight.py` changed; the mutation anchor
line was left byte-identical.

Unit suite: 1290 passed, 51 deselected.
