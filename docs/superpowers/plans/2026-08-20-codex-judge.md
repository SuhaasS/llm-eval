# Codex-CLI Judge Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the judge's completion backend for the production bakeoff run: instead of the Bedrock mantle GPT endpoint through LiteLLM, the judge calls **Codex CLI** (`codex exec`) authenticated against a **Pindrop ChatGPT Business/Enterprise seat**. Judge backend swap only — `runner.py`, the four candidate arms, the vote protocol (two forced positions), the rubric, and full N (~9,600 calls per 60-task pass) are unchanged. The driver additionally gains a concurrent completion path (`--concurrency N`) so a full pass finishes inside the operator's lifetime.

**Architecture:** One new module, `bakeoff/src/bakeoff/codex_judge.py`, implementing `CompleteFn` over a hermetic `codex exec` subprocess — a peer of `live_completion`, never a second agentic harness. `scripts/judge.py` selects the backend by judge-model-id namespace (`codex:` prefix), threads honest `judge_sampling`/`judge_harness` facts into every record, and splits phase-2 unit execution into a concurrent **compute** half (paid model calls, worker threads) and a strictly worklist-ordered **commit** half (writes, progress, breaker — main thread only). `judge_schema.py` gains one additive field (`judge_harness`) under `JUDGE_SCHEMA_VERSION` 1.1.0. `bakeoff/src/bakeoff/judge.py` — the payload/prompt/parse layer — is touched only for a usage lock and two docstring sentences; prompts, parsers, `VOTE_POSITIONS`, `majority`, and `JUDGE_PROMPT_VERSION = 2` stay byte-identical.

**Tech Stack:** unchanged — Python >=3.11, frozen dataclasses, pytest; plus `subprocess` + `concurrent.futures.ThreadPoolExecutor` (stdlib only; the codex path imports no litellm). Codex CLI `codex-cli 0.145.0-alpha.18` at `/Applications/ChatGPT.app/Contents/Resources/codex` (verified on this machine; NOT on PATH).

## Why this is more than an endpoint swap

1. **The record stops being true.** `JUDGE_SAMPLING` claims `temperature: 0.0`. Codex exposes no temperature flag, and Codex injects its own agent system prompt that `judge_prompt_sha` does not cover. A backend that keeps writing the old sampling block writes a false record.
2. **The two-vote protocol's stated justification weakens.** `VOTE_POSITIONS` is two forced positions because "at temperature 0 a judge is fully described by its answer under each of the two orders." Off temp-0 that is an approximation, not an identity.
3. **Scale.** ~9,600 calls, one `codex exec` process each. Serial does not finish.

**Upside:** the spec's open question **"Residency re-confirmation"** (`docs/superpowers/specs/2026-08-18-judge-design.md:353`) closes. §4.3 accepted "a GPT-class judge sends candidate diffs to a third party"; mantle was AWS-side and the spec flagged that as *less* than what was accepted. Codex on a company seat is exactly the accepted tradeoff, made true.

## Global Constraints

- All Global Constraints from `docs/superpowers/plans/2026-08-18-judge.md` and `2026-08-19-judge-fix-wave.md` still bind: frozen dataclasses, hand-rolled serialization, append-only jsonl, nothing writes the event log or grades.jsonl, whitelist-not-redaction, docstrings carry why+failure-mode with spec § refs, full-sentence test names, offline suite green after every task, the paid tests never run in the offline suite.
- Old stored lines (v1 three-vote, absolute payload paths, schema 1.0.0 mantle lines) must load and aggregate correctly forever. `_build` already ignores unknown fields; the new field defaults to `None`.
- `bakeoff/src/bakeoff/judge.py` stays importable without litellm, and `test_only_payload_inputs_from_touches_a_run_record_or_a_grade_record` (AST scan of that file) stays green.
- The mantle path keeps working byte-for-byte: `JUDGE_SAMPLING`, `live_completion`, `is_auth_failure`'s narrowness, and `test_the_pinned_judge_identity_constants` are untouched.
- Mutation checks run with `PYTHONDONTWRITEBYTECODE=1` (stale-.pyc trap).
- `git commit` messages end with: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## Binding design decisions

- **D1 — Backend lives in `bakeoff/src/bakeoff/codex_judge.py`.** Symmetric with `live_completion` but its own file: (a) the AST leak-test parses `judge.py` and any addition there must argue with it; (b) `judge.py`'s "importable without paying for litellm" property has a codex twin — `scripts/judge.py` must not require codex to exist for a mantle or fully-gate-decided batch, so the import is deferred the way `scripts.smoke_bedrock` is (D5); (c) the module handles only strings and subprocesses, so it structurally cannot touch a `RunRecord`/`GradeRecord`. Stdlib only.
- **D2 — Backend inferred from the pinned id, no `--judge-backend` flag.** Codex judge id is `codex:<model>`. `judge_event_log` picks `lazy_codex_completion` on the `codex:` prefix, `lazy_live_completion` otherwise. A flag plus an id can contradict each other; the id already IS the identity every resume key, generation partition, and record carries. Consequence that carries the whole record-honesty argument: a codex id can never equal a mantle id, so `_rubric_key`/`_pairwise_key`/`_judge_generation` (`scripts/judge.py:646-655`, `2120-2146`) partition the two backends automatically. Silent mixing is inexpressible.
- **D3 — Neutrality guard needs no change.** `assert_neutral_judge("codex:gpt-5.2-codex")` passes today (no compared vendor prefix, no family token — `judge.py:222-254`). Add a test pinning both directions.
- **D4 — `JUDGE_PROMPT_VERSION` stays 2.** Its rule covers the harness-rendered prompt and the vote protocol; both are byte-identical here (same `render_*`, same `VOTE_POSITIONS`, same `_ask_and_parse`). What changed lives in fields built for it: `judge_model_id` (which the generation key partitions on first), the new `judge_harness`, and honest `judge_sampling`. Bumping to 3 would claim the prompt moved when the sha proves it did not, and would force a needless re-buy of every mantle rubric line. **Add one sentence each** to the `JUDGE_PROMPT_VERSION` and `judge_prompt_sha` docstrings: on the codex backend the sha attests the *user turn only*; the injected Codex system prompt is identified by `judge_harness.codex_cli_version`, not hashed.
- **D5 — Signatures.**
  - `codex_completion(judge_model_id: str, usage_totals: dict[str, int] | None = None, *, codex_bin: str | None = None, codex_home: str | None = None, reasoning_effort: str | None = None, timeout_s: float = CODEX_CALL_TIMEOUT_S) -> CompleteFn`
  - `build_codex_argv(codex_bin, model, scratch_dir, output_file, reasoning_effort) -> list[str]` — pure, testable without a subprocess.
  - `_run_codex(argv, prompt, env, timeout_s) -> CodexRun` — the ONE function that spawns; monkeypatched in every test.
  - `codex_cli_version(codex_bin) -> str` — cached, recorded into `judge_harness`.
  - Exceptions: `CodexUnavailable`, `CodexAuthFailure` (carries `status_code = 401`), `CodexRateLimited`, `CodexCallFailed`.
  - `codex_bin`: arg -> `BAKEOFF_CODEX_BIN` -> `CODEX_BIN_DEFAULT`. `codex_home`: arg -> `BAKEOFF_CODEX_HOME` -> **refuse** (`CodexUnavailable`) when unset or `<home>/auth.json` is absent. Defaulting to `~/.codex` is forbidden: it would silently judge on a personal seat with a contaminated `config.toml`/`AGENTS.md`, and requiring an explicit home is what makes the auth source a recorded fact.
  - `lazy_codex_completion(judge_model_id, usage_totals)` in `scripts/judge.py` beside `lazy_live_completion` (:663): a fully gate-decided batch must not require the codex binary, the home, or auth to exist.
- **D6 — Hermetic invocation, two layers.**
  1. **Purpose-built `CODEX_HOME`** (primary): e.g. `~/.codex-bakeoff-judge/` holding ONLY `auth.json` from a seat login. No `config.toml`, no `AGENTS.md`, no sessions. An absent file cannot be injected regardless of flag semantics — this structurally closes the `$CODEX_HOME/AGENTS.md` question.
  2. **Flags** (defense in depth):
     ```
     CODEX_HOME=<judge home> <codex_bin> exec \
       --ignore-user-config --ignore-rules --skip-git-repo-check \
       --ephemeral -s read-only -C <fresh empty scratch dir> \
       -m <model, "codex:" stripped> \
       [-c model_reasoning_effort="<effort>"] \
       --json -o <scratch>/last_message.txt --color never \
       -
     ```
     Prompt on **stdin** (`-`), never argv — payloads embed two full diffs. All flags verified present in `codex exec --help`. `-C` points at a per-call `mkdtemp()` scratch dir (also holding the `-o` file), removed in `finally:`.
  3. **Subprocess env:** copy of `os.environ` with `CODEX_HOME` set and `OPENAI_API_KEY` **popped** — an ambient key must not silently flip the auth mode away from the seat (same hygiene precedent as the `AWS_BEARER_TOKEN_BEDROCK` scrub, `judge.py:1671-1681`).
- **D7 — Record honesty.** `_rubric_line`/`_vote_line`/`_gate_decided_line` stop reading module constants for the sampling block and take `judge_sampling: dict` and `judge_harness: dict | None` threaded from the driver.

  | path | `judge_sampling` | `judge_harness` |
  |---|---|---|
  | mantle | `dict(JUDGE_SAMPLING)` — unchanged bytes | `{"backend": "litellm-mantle"}` |
  | codex | only what was sent: `{"model_reasoning_effort": ...}` or `{}`. **Never `temperature`.** | `{"backend": "codex-cli", "codex_cli_version", "codex_model", "auth_mode", "auth_seat", "sandbox": "read-only"}` |
  | gate-decided | `{}` | `None` — nothing was sent |

- **D8 — Schema.** Add `judge_harness: dict[str, Any] | None = None` to `JudgeRecord`; bump `JUDGE_SCHEMA_VERSION` `"1.0.0"` -> `"1.1.0"`. Old lines load; `None` reads as "mantle-era or gate-decided".
- **D9 — Failure classification for the codex path.**

  | condition | classification | behavior |
  |---|---|---|
  | exit 0, `-o` present, non-empty | success | return file text |
  | exit 0, `-o` missing/empty | empty completion | return `""` -> strict parser raises `MalformedVerdict` -> `_ask_and_parse` re-asks (mirrors `content or ""`, `judge.py:1450-1454`) |
  | auth marker | `CodexAuthFailure`, `status_code = 401` | **no retry inside the backend** — nothing is mintable; `codex login` is a human act. Propagates -> per-unit error -> breaker |
  | rate-limit marker | backoff INSIDE the backend (`CODEX_RATE_LIMIT_RETRIES = 5`, `30s * 2^n` + jitter, cap 8 min), then `CodexRateLimited` | its OWN class, never auth |
  | subprocess timeout (`CODEX_CALL_TIMEOUT_S = 1200`) | kill process group, retry ONCE, then `CodexCallFailed` | per-unit error |
  | other non-zero exit | retry once, then `CodexCallFailed` | per-unit error |
  | binary/home/auth.json missing | `CodexUnavailable` at first prompt | per-unit error -> breaker |

  `CodexAuthFailure.status_code = 401` makes `is_auth_failure` (`judge.py:1296`, the ONE classifier) answer True without touching it, so both callers keep agreeing. `_abort_message` (`scripts/judge.py:1072`) takes a backend token: the credential paragraph gets a codex variant naming `CODEX_HOME=<judge home> codex login`; the data paragraph's 429 sentence gains the seat-usage-window note. **Markers are the least verifiable part (V3)** — until observed, classify conservatively as generic `CodexCallFailed` rather than guessing auth.
- **D10 — Usage accounting.** `calls` incremented BEFORE spawn (an attempt that reached the wire may have been billed — the direction rule at `judge.py:1436-1437`). Token counts parsed from `--json` JSONL via `_usage_from_events(events) -> dict | None`. **Do not write field names before V4 verifies them.** Unreadable counts -> `calls_without_usage += 1` with readable fields still contributing (mirrors `_add_usage`'s partial-block rule). If codex emits no token events: totals stay 0 and `print_summary` gains "the codex seat is not token-metered from here; `calls` is the reconcilable number". `auth_refreshes` stays 0. **All `usage_totals` mutation — `codex_judge.py` AND `judge.py`'s `_completion`/`_add_usage` — goes under a module-level `threading.Lock`**, because D12 moves calls onto worker threads and `d[k] += v` is not atomic.
- **D11 — No `--output-schema` this wave.** The strict parsers already tolerate prose and the retry already answers truncation. Enforcing a response schema changes the decoding contract — a real protocol change that WOULD demand a `JUDGE_PROMPT_VERSION` bump, plus two schema files to keep in sync with `_RUBRIC_RESPONSE_FORMAT`/`_PAIRWISE_RESPONSE_FORMAT`. Adopt only if V7 shows a malformed rate that burns real money, and then as its own versioned change.
- **D12 — Concurrency: concurrent compute, ordered commit.** `judge_event_log` gains `concurrency: int = 1`; CLI gains `--concurrency N` (validated >=1 via `parser.error`).
  - **Phase 1 selection untouched** — worklist order stays byte-identical; resume determinism is pinned on it.
  - **Phase 2 splits each paid unit.** *Compute* (worker thread): `judge_rubric(...)` / `judge_pair_vote(...)` — the paid call and its malformed retries, nothing else. *Commit* (main thread, strictly worklist order): build `JudgeRecord`, `_write_payload_or_fail`, `_assert_rubric_gate` (:588), `_append_or_fail` (:721), `_finish`, breaker. `_rubric_line`/`_vote_line` split into their call half plus new `_commit_rubric`/`_commit_vote`; at `concurrency == 1` they compose exactly as today.
  - **Scheduler:** sliding window over the worklist — submit up to `concurrency` compute futures ahead of a commit cursor that walks in order, waits on the head unit, commits, tops up. Gate-decided and not-judged units have no future and are handled inline at the head. `_judgeable_inputs` runs at submission on the main thread (it mutates shared caches; single-threaded avoids locking them).
  - **Why this shape wins every constraint at once:** writes stay single-threaded (no lock on the jsonl or payload store, `fsync` semantics unchanged); line order stays deterministic; exactly one `_finish` per attempted unit is preserved verbatim; `StorageFailure` can only raise on the main thread and stays batch-fatal before any further commit; `KeyboardInterrupt` lands on the main thread, which stops committing and calls `executor.shutdown(wait=False, cancel_futures=True)` — in-flight results are discarded and **no line is written**, today's KI contract exactly. Up to `concurrency - 1` paid calls wasted; bounded and acceptable.
  - **Breaker redefinition:** "N consecutive failures **in commit order**" — which is worklist order, i.e. the same sentence evaluated at the same place. Completion order is nondeterministic; commit order is not, so "consecutive" keeps a deterministic, resume-stable meaning. Rejected: counting in completion order (the same batch aborts at different units on different runs) and a failure-rate window (an uncalibrated new statistic).
  - **Both positions of one comparison may run concurrently** — the contract is already "two independent calls with no shared context"; `CompleteFn` is stateless and each codex call is its own process with `--ephemeral` and a fresh scratch dir.
  - **Progress:** same call site (`_finish`, main thread). Per-unit duration becomes the *compute* duration measured in the worker — wall-clock would misreport a fast unit stuck behind a slow head.
  - **Default `--concurrency 1`**, deliberately: byte-identical sequential semantics for the mantle path, and the seat's rate window is unmeasured. Pick the production number (recommend 8) after V7. A default that guesses at parallelism against an unmeasured limit is how the first full run opens with a rate-limit abort.
- **D13 — `JUDGE_MODEL_ID_DEFAULT` stays `"openai.gpt-5.6-sol"`.** Production passes `--judge-model codex:<pinned>` explicitly. Flipping the default is a one-line follow-up gated on V8.

## Explicitly NOT done (adjudicated)

No second agentic harness (codex judges; it never runs tasks). No `--output-schema` (D11). No change to `VOTE_POSITIONS`, `majority`, parsers, prompts, or `JUDGE_PROMPT_VERSION` (D4) — but the spec MUST record that the temp-0 justification for two votes is now an approximation (Task 6). No `codex mcp-server`/`exec-server` integration (a persistent server would share a process across votes; the seam exists to make that impossible). No mantle-path concurrency changes beyond the usage lock (it inherits `--concurrency` through the same scheduler). No attempt to read seat/org identity out of `auth.json` tokens — operator-attested instead (Task 5).

---

### Task 1: `codex_judge.py` — the hermetic backend (TDD)

**Files:** create `bakeoff/src/bakeoff/codex_judge.py`, `bakeoff/tests/test_codex_judge.py`.

**Implementation:** D5, D6, D9, D10. Constants: `CODEX_BIN_DEFAULT`, `CODEX_BIN_ENV = "BAKEOFF_CODEX_BIN"`, `CODEX_HOME_ENV = "BAKEOFF_CODEX_HOME"`, `CODEX_CALL_TIMEOUT_S`, `CODEX_RATE_LIMIT_RETRIES`. The closure per call: mkdtemp scratch -> `build_codex_argv` -> bump `calls` under the lock -> `_run_codex` with prompt on stdin -> classify -> fold usage -> read `-o` file -> return text (or `""`) -> `finally:` remove scratch.

- [ ] **Step 1: failing tests** — `test_the_argv_carries_every_hermetic_flag_and_reads_the_prompt_from_stdin`; `test_the_model_id_prefix_is_stripped_for_dash_m_and_kept_for_the_record`; `test_a_missing_codex_home_refuses_with_the_login_command_named`; `test_an_ambient_openai_api_key_never_reaches_the_subprocess`; `test_an_empty_last_message_returns_empty_string_for_the_parser_to_refuse`; `test_an_auth_failure_is_never_retried_and_answers_is_auth_failure`; `test_a_rate_limit_backs_off_and_is_never_classified_as_auth`; `test_a_timeout_kills_the_process_and_retries_exactly_once`; `test_calls_count_before_the_wire_and_missing_usage_is_counted_not_dropped`; `test_usage_mutation_is_locked`; `test_the_scratch_dir_is_removed_even_when_the_call_raises`.
- [ ] **Step 2:** implement to green.
- [ ] **Step 3:** offline suite green.
- [ ] **Step 4:** commit `feat: codex-cli judge completion backend (hermetic, seat-authenticated)`.

### Task 2: schema — `judge_harness`, `JUDGE_SCHEMA_VERSION` 1.1.0

**Files:** modify `bakeoff/src/bakeoff/judge_schema.py`; test in `bakeoff/tests/test_judge_schema.py`.

- [ ] Tests: `test_a_schema_1_0_0_line_loads_with_judge_harness_none`; `test_judge_harness_round_trips_and_the_schema_version_moved`.
- [ ] Implement D8; offline suite green; commit.

### Task 3: driver — backend selection, honest records, breaker wording

**Files:** modify `bakeoff/scripts/judge.py` (`lazy_codex_completion` beside :663; selection ~:1567; `_rubric_line`/`_vote_line`/`_gate_decided_line` ~:838/904/972; `_abort_message` :1072; header print ~:3912); modify `bakeoff/src/bakeoff/judge.py` (usage lock + the two D4 docstring sentences only); tests in `bakeoff/tests/test_judge_script.py`, `bakeoff/tests/test_judge.py`.

- [ ] Tests: `test_a_codex_prefixed_judge_model_selects_the_codex_backend_lazily`; `test_codex_lines_never_claim_temperature_zero_and_carry_the_harness`; `test_mantle_lines_are_byte_identical_to_before`; `test_a_codex_judge_id_passes_the_neutrality_guard_and_a_codex_claude_does_not`; `test_the_abort_credential_paragraph_names_codex_login_on_the_codex_backend`; `test_codex_and_mantle_lines_in_one_file_are_never_pooled`.
- [ ] `test_the_pinned_judge_identity_constants` must NOT change.
- [ ] Commit `feat: judge backend selected by model-id namespace; records say what was actually sent`.

### Task 4: concurrency — compute/commit split, `--concurrency`

**Files:** modify `bakeoff/scripts/judge.py` (phase 2, `judge_event_log` signature, `main`); tests in `bakeoff/tests/test_judge_script.py`.

- [ ] Tests (injected fake seam with per-prompt latency): `test_concurrency_commits_lines_in_worklist_order_regardless_of_completion_order`; `test_the_breaker_counts_consecutive_failures_in_commit_order_under_concurrency`; `test_a_storage_failure_aborts_before_any_further_commit_and_in_flight_results_are_discarded`; `test_every_attempted_unit_still_prints_exactly_one_progress_line_under_concurrency`; `test_both_positions_of_one_comparison_are_two_independent_calls`; `test_concurrency_one_is_byte_identical_to_the_sequential_walk`; `test_the_concurrency_flag_is_validated_at_one_or_more`.
- [ ] Commit `feat: concurrent completion path with ordered commit (--concurrency)`.

### Task 5: operator setup + paid smoke

- [ ] Register a `codex_live` pytest marker beside `judge_live` (`pyproject.toml:37-54`), excluded from the offline selector (see `test_verify_logger_selector_excludes_judge_live`), with one paid smoke: build `codex_completion("codex:<pinned>")`, send a trivial pairwise-shaped prompt, assert it parses.
- [ ] Record the `auth_seat` attestation: who logged the judge home in, against which Pindrop seat, on what date.

### Task 6: docs — spec amendment, residency closed

**Files:** modify `docs/superpowers/specs/2026-08-18-judge-design.md` (new "Amendments (2026-08-20)" subsection; superseded text left standing per the file's convention).

- [ ] Record: (a) the Codex/seat backend, and that this **resolves "Residency re-confirmation" in the direction §4.3 originally accepted** — candidate diffs go to OpenAI directly, which is exactly the tradeoff reviewed and accepted, now made true rather than approximated by the AWS-side mantle posture; the auth source is recorded per-line in `judge_harness`. (b) Temperature is no longer controllable, so "at temperature 0 a judge is fully described by its answer under each of the two orders" is now an approximation — the two-forced-position protocol is retained because position consistency is still directly measured and reproducibility is now statistical rather than exact; a same-generation `--re-judge` A/A is the honest way to measure the judge's self-agreement and is newly meaningful. (c) The malformed-verdict retry now also answers sampling nondeterminism, which strengthens rather than weakens it. (d) Concurrency and the commit-order breaker. (e) The weaker model pinning (V6).
- [ ] Commit `docs: codex judge amendment; residency open question resolved as originally accepted`.

### Task 7: rollout (V7-V9)

- [ ] **V7 tiny pass**, **V8 A/B agreement**, **V9 production** — see Verification.

---

## Verification

Operator-run, one command each. Every one is currently an unverified assumption.

- **V1 seat auth:** `mkdir -p ~/.codex-bakeoff-judge && CODEX_HOME=~/.codex-bakeoff-judge /Applications/ChatGPT.app/Contents/Resources/codex login`, then `... codex login status`; confirm `auth_mode` is `"chatgpt"` in `~/.codex-bakeoff-judge/auth.json`.
- **V2 AGENTS.md suppression (the flagged open question):** put an `AGENTS.md` carrying a marker word in a scratch `CODEX_HOME`, then `CODEX_HOME=<scratch> codex exec --ignore-user-config --ephemeral --skip-git-repo-check -C <empty dir> --json 'If any system or developer instructions mention a code word, say it; otherwise say NONE.'` If the marker leaks, `--ignore-user-config` does NOT cover `AGENTS.md` and the purpose-built home is the load-bearing layer — this decides what the docstring may claim.
- **V3 failure markers:** one `codex exec` under a logged-OUT scratch home; capture exact stderr and exit code for the auth classifier. Rate-limit markers gathered opportunistically from V7 logs before the 429 branch is enabled.
- **V4 usage events:** one real `codex exec --json ... 'Reply with the word ok.'` under the judge home; grep the JSONL for token-count events and record the exact event/field names into `_usage_from_events`. **Blocks Task 1's usage code.**
- **V5 large stdin:** pipe a ~500KB prompt through stdin; confirm the reply and the `-o` file behave.
- **V6 model pinning:** enumerate what the seat exposes; pin the most specific model string available. Codex model strings may still float server-side — `judge_harness.codex_cli_version` + date is the compensating record, and the spec amendment must say this is weaker pinning than mantle's.
- **V7 tiny pass:** `cd bakeoff && .venv/bin/python scripts/judge.py --event-log <collection> --only-task <one> --samples 0 --judge-model codex:<pinned> --concurrency 2`. Confirms census, progress, record honesty (spot-read a line), per-call latency, malformed rate, 429 markers.
- **V8 A/B agreement:** run the codex judge over the already-mantle-judged collection. **No `--re-judge` needed** — the codex generation's resume keys differ in `judge_model_id`, so every unit is fresh and both generations land in one append-only file that `summarize` reports side by side, never pooled. Compare voted-comparison majorities and position-consistency across generations (read-only script keyed by `_comparison_key` minus generation). Gate the production flip on no systematic direction flip.
- **V9 production:** full pass at `--concurrency 8` (or what V7 supports), resume-on-abort with the same command.

Offline suite green after every task: `cd bakeoff && .venv/bin/pytest -m "not judge_live and not codex_live"`.

## Genuinely risky, named honestly

1. **Rate limits on a seat are unmeasured** — the backoff constants are guesses until V7; a 9,600-call pass may need pacing this plan cannot pre-compute.
2. **Codex model pinning is weaker than mantle's** (server-side floating possible); `judge_harness` + dates is mitigation, not a fix.
3. **Token accounting may be unavailable** (V4); `calls_without_usage` is the honest fallback, but judge spend then has no token figure anywhere.
4. **The codex agent layer is un-hashed input** — `judge_prompt_sha` covers the user turn only; nothing can attest the injected system prompt beyond the CLI version.
5. **Failure-marker classification (D9) is the least verifiable part** — built against strings observed in V3, and a codex update can move them; the conservative default bounds the damage.

## Critical Files

- `bakeoff/src/bakeoff/codex_judge.py` (new — the backend)
- `bakeoff/scripts/judge.py` (backend selection, record threading, concurrency, breaker message)
- `bakeoff/src/bakeoff/judge.py` (usage lock + docstring notes only; the seam and constants it defines constrain everything)
- `bakeoff/src/bakeoff/judge_schema.py` (`judge_harness` field, schema bump)
- `docs/superpowers/specs/2026-08-18-judge-design.md` (amendment; residency open question closed)
