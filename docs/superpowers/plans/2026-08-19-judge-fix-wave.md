# Judge Fix Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 4 Critical and all Important findings from the five-lens review of the judge module (branch `judge` @ 1fea234): name-order-dependent Elo, degenerate temp-0 three-vote protocol, breaker/resume deadlock, unpinned prompts, scan-after-wire, and the operability gaps.

**Architecture:** No new modules. `elo_from_outcomes` becomes Bradley–Terry MLE under the same name; the vote protocol becomes two forced positions (a_first, b_first) at temperature 0; the driver gains a worklist census, per-unit progress, storage-failure aborts, and a neutral-family guard; summarize gains task-clustered bootstrap CIs, voted-only decomposition, and position consistency. Old 3-vote v1 lines stay readable everywhere; generation partitioning (JUDGE_PROMPT_VERSION 1→2) keeps the two protocols separate in every report.

**Tech Stack:** unchanged — Python ≥3.11, frozen dataclasses, litellm==1.95.0, pytest.

## Global Constraints

- All build-wave Global Constraints from `docs/superpowers/plans/2026-08-18-judge.md` still bind: frozen dataclasses, hand-rolled serialization, append-only jsonl, nothing writes the event log or grades.jsonl, docstrings carry why+failure-mode with spec § refs, full-sentence test names, whitelist-not-redaction, offline suite green after every task, the paid test never runs.
- Mutation checks run with `PYTHONDONTWRITEBYTECODE=1` (stale-.pyc trap).
- Old stored lines (vote_index 0..2, absolute payload paths, v1 generation) must load and aggregate correctly forever. `JUDGE_SCHEMA_VERSION` stays `"1.0.0"` — this wave adds no record field; an implementer who needs one must bump it and justify in the commit.
- `git commit` messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## Binding design decisions

- **D1** Delete the `votes` param from `judge_event_log` and the `--votes` CLI flag (one legal value under forced positions; the even-votes-silent-tie minor dies with it).
- **D2** `vote_index` 0..1; the driver derives position (0→`a_first`, 1→`b_first`) but `position_assignment` is still STORED on every vote record.
- **D3** Remove the `rng` seam entirely from the vote path (`judge_pair_vote`, `judge_event_log`); drop the now-unused `import random` where applicable.
- **D4** `majority()` is kept byte-identical — strict majority over n=2 IS agreement-else-tie, and it still reads old 3-vote generations.
- **D5** `JUDGE_PROMPT_VERSION` 1→2; docstring widened to "moves on any change to the prompt text **or the vote protocol**". Required: v1 lines carry vote_index 0/1 drawn under random positions; an unbumped resume would skip forced-position votes against them.
- **D6** The pairwise order sentence becomes: shown "in an order chosen by the harness that carries no information about the submissions — do not prefer a submission for appearing first or second."
- **D7** `elo_from_outcomes` keeps name/signature/summary-key; internals = Bradley–Terry MLE (MM/Zermelo iteration); rating = `ELO_ANCHOR + ELO_SCALE * log10(strength)`, mean-anchored; `ELO_K`/`ELO_BASE` deleted, `ELO_SCALE = 400.0`, `ELO_ANCHOR = 1000.0` added.
- **D8** BT degeneracy: one virtual tie (0.5 each way) per unordered pair with ≥1 real comparison; real ties = half-win each way; converge at 1e-10 or 10,000 iterations; empty outcomes → `{}`.
- **D9** `scan_payload(payload) -> set[str]` extracted public in `judge_schema.py` (raw-walk + canonical backstop); `judge_rubric`/`judge_pair_vote` call it immediately after `build_*_payload`, raising `PayloadSecretsFound` BEFORE render/complete; `write_payload` keeps its scan as backstop. New import edge judge→judge_schema is acyclic.
- **D10** `StorageFailure(RuntimeError)` wraps `OSError` from `write_payload`/`append_judgment` ONLY (never seam/socket OSErrors) → batch-fatal: record the error, abort immediately with a disk-shaped message ("writes are failing; continuing would buy verdicts that cannot be recorded; free space and resume").
- **D11** `input_payload_path` stores `payloads/<judgment_id>.json.gz` (relative to the judgments dir); `resolve_payload_path(judgments_dir, stored)` joins relative values and passes absolute (old) values through. No schema bump — the value self-describes.
- **D12** `live_completion(judge_model_id=…, region=…, usage_totals: dict[str, int] | None = None)`: the closure accumulates `calls`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `calls_without_usage`, `auth_refreshes` into the caller's dict, counting BEFORE content extraction so an auth-retried call counts both wire calls. Driver creates the dict, exposes it as `result["judge_usage"]` (None when a custom seam is injected), prints token totals with a sentence explaining why not dollars (judge models deliberately absent from `costs.PRICE_BOOK` — mantle pricing unpublished).
- **D13** Breaker counts attempted-and-failed units only, per invocation; skips neither trip nor reset it. `--max-consecutive-errors` (validated ≥1, default `MAX_CONSECUTIVE_ERRORS`). Pairs/rubric units whose run record could not be read are pre-filtered: one clear error line each ("not judged: run X could not be read"), never entering the try/except or the breaker. The abort message is built from the failing run's `(label, is_auth_failure)` pairs — all-auth → credential paragraph, none-auth → data paragraph naming the escape (`--only-task` exclusion or a higher limit), mixed → both — and names every unit via `_unit_label`.
- **D14** `PayloadSecretsFound` counts as a normal failure (uniform attempted-and-failed rule).
- **D15** `assert_neutral_judge(judge_model_id)` in `bakeoff/judge.py`: refuse ids whose lowercase starts with `anthropic.`/`google.`/`nvidia.`/`moonshot.` or contains `claude`/`gemma`/`gemini`/`nemotron`/`kimi`, raising `NonNeutralJudge` naming the matched family and §4.3; env `BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE=1` (no CLI flag) converts the raise into loud-warning text the driver appends to `warnings` AND prints. Called at the top of `judge_event_log`.
- **D16** `KeyboardInterrupt` caught around the walk → warning + `result["interrupted"] = True`; `main` prints the summary and returns 130.
- **D17** `CollectionNotFound(RuntimeError)` when `(event_log_root / "runs")` is not a directory — checked as the first statement of `judge_event_log`, before `EventLog.__init__` can mkdir; `main` catches it beside `ResumeRefused`.
- **D18** Task-clustered percentile bootstrap: `BOOTSTRAP_RESAMPLES = 1000`, `BOOTSTRAP_SEED = 0`, `random.Random(BOOTSTRAP_SEED)` per summarize call; resample `task_id`s with replacement; recompute pooled win rates and refit BT per resample; report 2.5/97.5 percentiles. A model absent from a resample: take percentiles over resamples where it appears (documented).

## Explicitly NOT fixed (adjudicated)

Skips-reset breaker variant (blinds the breaker on the resume it exists for); removing the identical-prompt malformed retry (transport nondeterminism — truncation, empty content — still makes a fresh call occasionally succeed at temp 0, and it is unpaid when parsing succeeds); `_deterministic` (it is called); `--samples` nonsense validation, same-generation re-judge test-retest number, candidate-diff content blinding, mixed-grader check-list fingerprint (deferred; not on edited lines).

## Verification (whole wave)

- Offline suite green after every task: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`.
- After Task 1: the reviewer's block-structure scenario (names sorting against strength) recovers the true ranking.
- After Task 6: fake-seam end-to-end on `~/.cache/bakeoff/trucking-pilot-v2` — old 3-vote lines and new 2-vote lines report side by side, never pooled; CIs print; the κ caveat survives every new number.
- Final: whole-branch re-review; the paid test never runs.

---

### Task 1: Bradley–Terry ratings on the Elo scale

**Files:**
- Modify: `bakeoff/scripts/judge.py` (constants ~197-198, `elo_from_outcomes` ~1066-1107, Elo print text ~1562-1575)
- Test: `bakeoff/tests/test_judge_script.py` (Elo tests ~1773-1857)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ELO_SCALE = 400.0`, `ELO_ANCHOR = 1000.0` (replacing `ELO_K`/`ELO_BASE`, deleted); `elo_from_outcomes(outcomes: list[tuple[str, str, float]]) -> dict[str, float]` — same signature, same `{0.0, 0.5, 1.0}` score validation with the pair-naming error message.

**Implementation (D7/D8):** aggregate outcomes to per-unordered-pair win counts (a tie contributes 0.5 to each side); add one virtual tie per pair that has ≥1 real comparison; run the MM/Zermelo iteration to convergence (1e-10 max-abs strength delta, cap 10,000 iterations); ratings `ELO_ANCHOR + ELO_SCALE * math.log10(strength)`, then shift so the mean rating equals `ELO_ANCHOR`. Delete `sorted(outcomes)` and the sequential walk entirely. Docstring: why BT (single-pass K-update was name-order-dependent — measured 40/40 wrong rankings when names sorted against strength), why the virtual tie (finite MLE for undefeated arms; moves ratings <1 point at real collection sizes), the disconnected-graph caveat (round-robin prevents it; cross-component ratings relate only through the anchor). Print text drops "K=32, base 1000", says "Bradley–Terry maximum likelihood, reported on the Elo scale (400/log10 spacing, mean anchored at 1000)"; the path-dependence sentence is deleted (no longer true); the generation-partition sentence stays.

**Steps:**
- [ ] **Step 1: Write failing tests** — `test_elo_ratings_are_invariant_under_model_renaming` (relabel arms, ratings follow labels exactly); `test_elo_is_a_function_of_the_outcome_multiset_not_its_order` (shuffled copy → identical dict); `test_elo_recovers_the_true_order_on_a_block_structured_outcome_stream` (the C1 fixture: 4 arms, ~150 comparisons per pair emitted in monotone per-pair blocks, names lexicographically AGAINST true strength; assert rating order matches win-rate order — this fixture fails 40/40 under the old code, verify it fails before implementing); `test_a_sixty_forty_split_between_two_arms_reads_as_about_seventy_elo` (400·log10(1.5) ≈ 70, tolerance ±10 for the prior); `test_an_undefeated_arm_gets_a_finite_rating`; `test_ties_enter_the_likelihood_as_half_wins`; `test_ratings_are_mean_anchored`; `test_empty_outcomes_yield_an_empty_table`. Adapt `test_elo_follows_the_win_rates_rather_than_leading_them`; delete `test_elo_starts_at_the_base_and_moves_by_k_on_an_even_first_match` and `test_elo_is_deterministic_over_a_fixed_outcome_set` (subsumed by order-invariance).
- [ ] **Step 2: Run tests, verify the new ones fail** (block-structure fixture must fail against the OLD implementation before you replace it — that pins the bug).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full offline suite green.**
- [ ] **Step 5: Commit** — `fix: replace sequential Elo with Bradley-Terry, killing the name-order dependence`

### Task 2: two forced positions at temperature 0

**Files:**
- Modify: `bakeoff/src/bakeoff/judge.py` (module docstring narrative, `JUDGE_PROMPT_VERSION` ~112, pairwise instructions ~345-367, `judge_pair_vote` ~861-910, `majority` docstring)
- Modify: `bakeoff/scripts/judge.py` (module docstring, votes guard ~616-620, vote loop ~863-891, `_vote_line` ~466-519, `judge_event_log` signature, `--votes` flag, `main` wiring, cost-math comments)
- Test: `bakeoff/tests/test_judge.py`, `bakeoff/tests/test_judge_script.py`

**Interfaces:**
- Produces (D1-D6): `JUDGE_PROMPT_VERSION = 2`; `VOTE_POSITIONS: tuple[str, str] = ("a_first", "b_first")`; `judge_pair_vote(a: PayloadInputs, b: PayloadInputs, position_assignment: str, complete: CompleteFn, retries: int = 2) -> VoteOutcome` (raises `ValueError` on a position outside `VOTE_POSITIONS`; `VoteOutcome` unchanged — position still stored); `judge_event_log(event_log_root, tasks, *, complete=None, judge_model_id=…, only_tasks=None, sample_indices=None, rubric=True, re_judge=False, …) -> dict` — `rng` and `votes` GONE. Vote loop: `for vote_index, position in enumerate(VOTE_POSITIONS):`.

**Design points:** the canonical-verdict inversion table is untouched — both positions now occur on every comparison, so an inversion bug flips exactly one of the two votes and surfaces as a catastrophic position-consistency rate (say so in the docstring). Keep the identical-prompt malformed retry (transport nondeterminism). D6 sentence replaces the "RANDOM order" claim. Resume keys unchanged in shape; the v2 generation guarantees no cross-protocol skip (D5 — say why in the constant's docstring). Update ~10,800-call cost comments to 2-per-comparison arithmetic (~7,200 pairwise).

**Steps:**
- [ ] **Step 1: Write failing tests** — `test_a_comparison_is_exactly_two_votes_one_per_forced_position`; `test_vote_index_zero_is_shown_a_first_and_vote_index_one_is_shown_b_first` (stored `position_assignment` + payload shown-order agree with the index); `test_judge_pair_vote_refuses_a_position_outside_the_two_it_knows`; `test_two_agreeing_votes_aggregate_as_that_verdict_and_a_split_as_a_tie`; `test_the_protocol_change_bumped_the_prompt_version` (`== 2`); `test_a_v1_line_with_the_same_vote_index_does_not_satisfy_the_v2_resume_key`; `test_the_pairwise_prompt_no_longer_claims_the_order_is_random`. Replace the seeded-rng test with a direct both-positions map-back test through `judge_pair_vote`; rewrite three-votes-three-calls → two-calls-two-prompts; delete the votes<1 guard test and the `--votes` wiring assertion (flag gone).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full offline suite green.**
- [ ] **Step 5: Commit** — `fix: two forced positions per comparison, extracting the full deterministic-judge signal`

### Task 3: breaker semantics, flag, diagnosis

**Files:**
- Modify: `bakeoff/scripts/judge.py` (`MAX_CONSECUTIVE_ERRORS` block ~173-192 incl. the "re-mints once per call" comment drift → "once per expiry", `_BatchAborted` ~231-246, `_unit_failed`/`_unit_succeeded` ~748-775, loop error strings, argparse)
- Modify: `bakeoff/src/bakeoff/judge.py` (promote `_is_auth_failure` → public `is_auth_failure`, update internal callers)
- Test: `bakeoff/tests/test_judge_script.py` (breaker section), `bakeoff/tests/test_judge.py` (classifier test names)

**Interfaces:**
- Produces: `is_auth_failure(exc: BaseException) -> bool` (public, same body); `judge_event_log(..., max_consecutive_errors: int = MAX_CONSECUTIVE_ERRORS)`; CLI `--max-consecutive-errors` (int, default constant, validated ≥1 via `parser.error`); `_unit_label(kind, task_id, sample_index, models, run_ids) -> str` — e.g. `vote 1 task=trucking-8 sample=0 gemma-4-31b vs kimi-k2-5 (runs 3fa2…, 91cc…)`.

**Mechanics (D13/D14):** counting stays attempted-and-failed per invocation, skips and gate-decided ignored. `_unit_failed` records `(label, is_auth_failure(exc))` into the rolling failure run; the abort message is built from that run — all-auth → credential paragraph (mint/SSO/role, "resume with the same command"), none-auth → data paragraph ("these units fail deterministically and will fail again on resume; exclude the task with --only-task or raise --max-consecutive-errors"), mixed → both — and names every unit in the failing run. Pre-filter: units whose run is absent from `records` (read already failed and was logged) get one clear error line each without entering the try/except or the breaker. All loop error strings switch to `_unit_label`. Do not classify by message text — `is_auth_failure` is the one classifier, deliberately narrow (`MalformedVerdict` and `PayloadSecretsFound` are data-shaped by construction).

**Steps:**
- [ ] **Step 1: Write failing tests** — `test_the_breaker_trips_only_on_attempted_units_so_a_skip_heavy_resume_reports_the_real_run_length`; `test_max_consecutive_errors_is_operator_settable_from_the_command_line`; `test_an_all_auth_failure_run_aborts_with_a_credential_shaped_message`; `test_a_data_shaped_failure_run_is_not_blamed_on_credentials`; `test_the_abort_names_the_task_model_and_sample_of_every_unit_in_the_failing_run`; `test_one_unreadable_run_fails_its_pairs_with_one_line_each_and_never_trips_the_breaker`. Keep/adapt: gate-decided-neither-trips-nor-resets, success-resets, below-limit-runs-to-end, abort-exits-one.
- [ ] **Step 2: Run, verify fail.** The deadlock scenario (probe: one bad run, 4 passes, stuck at partial) must now complete under a raised `--max-consecutive-errors` and be diagnosable from the data-shaped message.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full offline suite green.**
- [ ] **Step 5: Commit** — `fix: breaker names its failures, classifies their shape, and takes an operator override`

### Task 4: scan-before-wire, tripwire hardening, tmp cleanup, relative payload paths

**Files:**
- Modify: `bakeoff/src/bakeoff/judge_schema.py` (`_payload_strings` ~111-132, `write_payload` ~312-381, new helpers)
- Modify: `bakeoff/src/bakeoff/judge.py` (`judge_rubric`, `judge_pair_vote` — post-Task-2 shape)
- Modify: `bakeoff/scripts/judge.py` (`_rubric_line`/`_vote_line` store the relative path)
- Test: `bakeoff/tests/test_judge.py` (tripwire ~780-857, new rendered-prompt sentinel sweep), `bakeoff/tests/test_judge_schema.py`, `bakeoff/tests/test_judge_script.py` (payload-path tests)

**Interfaces:**
- Produces (D9/D11): `scan_payload(payload: dict[str, Any]) -> set[str]` (extracted raw-walk + canonical backstop); `payload_relative_path(judgment_id: str) -> str`; `resolve_payload_path(judgments_dir: Path | str, stored: str | Path) -> Path` (relative → join, absolute → pass through); `write_payload` unchanged signature, body gains `try/BaseException → tmp.unlink(missing_ok=True); raise` plus a payloads-directory fsync after `os.replace`. `judge_rubric`/`judge_pair_vote` call `scan_payload` immediately after `build_*_payload` and raise `PayloadSecretsFound` before render/complete. Driver stores `payload_relative_path(judgment_id)` after one equality assert against what `write_payload` returned (the "stored what was written" argument survives relativization).
- Tripwire: extend the conventional-name set with `{"run", "rr", "rec", "grade_rec"}` and add matching shapes to the pinned violations. New sweep: sentinel-loaded records → `payload_inputs_from` → render BOTH prompts → assert no sentinel in the rendered text.

**Steps:**
- [ ] **Step 1: Write failing tests** — `test_a_secret_shaped_payload_never_reaches_the_complete_seam` (counting fake: zero calls, no file, no line — both kinds); `test_the_write_time_scan_survives_as_a_backstop_behind_the_build_time_one`; `test_a_failed_payload_write_leaves_no_tmp_file_behind` (monkeypatch gzip/`os.replace` to raise); `test_a_secret_sitting_in_a_dict_key_refuses_the_write` (canonical backstop demonstrably misses keys — the raw walk must catch); `test_stored_payload_paths_are_relative_to_the_judgments_directory`; `test_a_moved_collection_still_resolves_every_payload`; `test_an_old_absolute_payload_path_still_resolves`; `test_an_innocently_named_helper_that_reads_a_record_still_trips_the_source_scan` (`run`/`rr`/`rec` shapes, detector pinned against them first); `test_no_record_sentinel_survives_into_a_rendered_prompt`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Note the module docstring interaction: `judge.py` importing `judge_schema` for a scan writes nothing — update the sentence so it can't be read as "never imports judge_schema".
- [ ] **Step 4: Full offline suite green.**
- [ ] **Step 5: Commit** — `fix: scan payloads before the wire, harden the tripwire, relativize payload paths`

### Task 5: driver operability — census, progress, interrupt, disk, imports, usage

**Files:**
- Modify: `bakeoff/scripts/judge.py` (walk restructure ~777-899, `main`, `print_summary`)
- Modify: `bakeoff/src/bakeoff/judge.py` (`live_completion`, `lazy_live_completion`, `_completion`)
- Modify: `bakeoff/src/bakeoff/grade_schema.py` (gains `grades_path`/`artifacts_root`, verbatim bodies from `scripts/grade.py:155-160`) and `bakeoff/scripts/grade.py` (re-imports them, names stay reachable)
- Test: `bakeoff/tests/test_judge_script.py`, `bakeoff/tests/test_judge.py`

**Interfaces (D10/D12/D16/D17):**
- `class CollectionNotFound(RuntimeError)` — raised as the first statement of `judge_event_log` when `(root / "runs")` is not a directory; `main` catches beside `ResumeRefused`.
- `class StorageFailure(RuntimeError)` — the three `_*_line` builders wrap ONLY `write_payload` + `append_judgment` in `except OSError → raise StorageFailure(...)`; per-unit handler treats it as batch-fatal (record error, abort with the disk-shaped message).
- `live_completion(judge_model_id=…, region=…, usage_totals: dict[str, int] | None = None)` and `lazy_live_completion(judge_model_id, usage_totals=None)`; accumulation BEFORE content extraction (auth-retried call counts both wire calls).
- Two-phase walk: build the worklist (pure selection, preserving today's sorted order exactly — resume determinism tests pin it; the inputs cache becomes one dict keyed by run_id), print the census (`selected: R rubric, V votes over P pairs, G gate-decided; S already judged (skipped)`) BEFORE the first paid call, then execute with one line per ATTEMPTED unit: `[i/N] <_unit_label(...)> ok|ERROR <type> (unit 3.2s, elapsed 8m12s)`. Skipped units appear in the census count, not as lines.
- Result dict gains `"interrupted": bool`, `"judge_usage": dict | None`, `"selected": {census}`; `main` returns 130 on interrupt after printing the summary + resume hint; usage totals printed as tokens with the why-not-dollars sentence.

**Steps:**
- [ ] **Step 1: Write failing tests** — `test_a_typoed_event_log_is_refused_before_anything_is_created_on_disk` (no `runs/` mkdir'd, no `judgments/`, exit 1); `test_the_census_is_printed_before_the_first_paid_call`; `test_every_attempted_unit_prints_a_progress_line_naming_task_model_and_sample`; `test_skipped_units_print_one_census_line_not_five_thousand`; `test_a_failed_append_aborts_the_batch_instead_of_buying_more_verdicts` (ENOSPC-shaped OSError, ≤1 further call); `test_a_connection_shaped_oserror_from_the_seam_is_a_unit_error_not_a_batch_abort`; `test_a_keyboard_interrupt_still_prints_the_summary_and_the_resume_hint_and_exits_130`; `test_usage_totals_accumulate_across_calls_and_survive_the_auth_retry`; `test_the_summary_reports_tokens_not_dollars_and_says_why`; `test_importing_the_judge_driver_needs_neither_docker_nor_litellm` (subprocess: `import scripts.judge`; assert `"docker" not in sys.modules and "litellm" not in sys.modules`).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full offline suite green.**
- [ ] **Step 5: Commit** — `fix: make a forty-hour batch operable — census, progress, clean aborts, usage totals`

### Task 6: summarize upgrades — CIs, voted-only decomposition, position consistency

**Files:**
- Modify: `bakeoff/scripts/judge.py` (`summarize` ~1110-1303 — vote tuples gain `position_assignment`; comparisons block; `_print_reading`)
- Test: `bakeoff/tests/test_judge_script.py`

**Interfaces (D18; needs Tasks 1+2):** `BOOTSTRAP_RESAMPLES = 1000`, `BOOTSTRAP_SEED = 0`. Added printout-dict keys (nothing stored): per generation-pair row `"win_rate_x_voted": float | None` (None on zero voted comparisons — never 0/0) and `"win_rate_x_ci95": tuple[float, float]`; per generation `"elo_voted": dict[str, float]`, `"elo_ci95": dict[str, tuple[float, float]]`, `"gate_decided_share": dict[str, {"comparisons": int, "gate_decided": int}]`, `"position_consistency": {"measurable": int, "consistent": int, "rate": float | None}`.

**Design points:** position consistency, one rule for both generations — measurable when a comparison's votes cover both positions (every v2 comparison; a v1 only when the random draw split), consistent when all canonical verdicts are identical; this satisfies §4.2.3's position-swap probe. Bootstrap groups outcome triples by `task_id` carried in a parallel structure — do NOT widen `elo_from_outcomes`' input contract. Voted-only BT + win rates printed beside combined with the §10.1 framing (Tier B must not silently re-derive Tier A); gate-decided share per arm. Every new number lives under the κ caveat — no exceptions to the `finally`. Keep the resample loop allocation-light (1000 resamples run inside every summarize test).

**Steps:**
- [ ] **Step 1: Write failing tests** — `test_voted_only_win_rates_are_reported_beside_combined_and_never_divide_by_zero`; `test_gate_decided_share_is_reported_per_arm_per_generation`; `test_position_consistency_counts_agreement_across_the_two_forced_positions`; `test_an_old_three_vote_comparison_is_measurable_only_when_both_positions_appear`; `test_win_rate_intervals_are_task_clustered_so_one_task_collections_collapse_to_the_point`; `test_the_bootstrap_is_seeded_and_two_summaries_of_one_file_agree_exactly`; `test_elo_intervals_follow_task_resamples`; `test_a_three_vote_generation_and_a_two_vote_generation_report_side_by_side_never_pooled`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full offline suite green** plus the fake-seam end-to-end on `~/.cache/bakeoff/trucking-pilot-v2` (old 3-vote lines aggregate correctly beside any new lines; CIs print; caveat survives).
- [ ] **Step 5: Commit** — `feat: confidence intervals, voted-only decomposition, and position consistency in the summary`

### Task 7: neutral-family guard + mutation-survivor test pack

**Files:**
- Modify: `bakeoff/src/bakeoff/judge.py` (guard), `bakeoff/scripts/judge.py` (call at top of `judge_event_log`; `main` catches)
- Test: `bakeoff/tests/test_judge.py`, `bakeoff/tests/test_judge_schema.py`, `bakeoff/tests/test_judge_script.py`

**Interfaces (D15):** `NON_NEUTRAL_VENDOR_PREFIXES = ("anthropic.", "google.", "nvidia.", "moonshot.")`; `NON_NEUTRAL_FAMILY_TOKENS = ("claude", "gemma", "gemini", "nemotron", "kimi")`; `ALLOW_NON_NEUTRAL_JUDGE_ENV = "BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE"`; `class NonNeutralJudge(RuntimeError)`; `assert_neutral_judge(judge_model_id: str) -> str | None` (raises naming the matched family and §4.3 unless env == "1", then returns the loud warning text; driver appends to warnings AND prints).

**Test pack (each name is its own brief):**
- Guard: `test_a_judge_from_a_compared_family_is_refused_with_the_spec_section_named` (all four families, prefix and token forms; `openai.gpt-5.6-sol` passes); `test_the_env_override_admits_a_non_neutral_judge_with_a_loud_warning_and_nothing_else_does`.
- Schema branches: `test_a_json_line_that_is_not_an_object_is_counted_malformed_not_fatal`; `test_a_line_that_parses_but_cannot_construct_a_record_is_counted_not_fatal`.
- Evidence fields: `test_a_gate_decided_line_carries_an_empty_sha_and_an_empty_sampling_block`; `test_a_rubric_line_stores_the_sha_of_the_prompt_it_actually_sent_and_the_sampling_as_sent`; `test_a_vote_line_records_the_sampling_block_as_sent`; `test_a_rubric_line_names_exactly_the_one_grade_generation_it_was_gated_on`.
- Driver warnings: `test_orphan_grade_lines_are_warned_with_the_copied_file_diagnosis`; `test_the_excluded_warning_counts_and_names_the_dropped_runs`; `test_only_task_ids_absent_from_the_log_are_warned_not_errored`; `test_a_task_missing_from_the_task_set_is_warned_and_its_cells_skipped`.
- CLI wiring: `test_re_judge_from_the_command_line_appends_rather_than_skips`; `test_samples_from_the_command_line_select_exactly_the_named_indices`; `test_only_task_from_the_command_line_reaches_the_selection`.
- `--help`: extend the caveat-suppression test with positive assertions (`"--event-log" in text`, `"--max-consecutive-errors" in text`) so empty stdout fails.

**Steps:**
- [ ] **Step 1: Write failing tests** (the pack above; mutation-verify each new assertion catches the survivor it was written for — deleting the guarded line/branch goes red).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement the guard; wire into driver + main.**
- [ ] **Step 4: Full offline suite green.**
- [ ] **Step 5: Commit** — `fix: refuse a non-neutral judge, and pin the branches the mutation sweep exposed`

### Task 8: prompt pinning, golden sha, spec amendments (STRICTLY LAST)

**Files:**
- Modify: `bakeoff/tests/test_judge.py` (anchor test ~1163-1182 rewritten; new pins; golden-sha test)
- Modify: `/Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval/docs/superpowers/specs/2026-08-18-judge-design.md` (new `## Amendments (2026-08-19)` section)

**Content:**
1. Rewrite the rubric anchor test positional/single-source (the SOLUTION_DIFF-collision pattern): fixtures whose diffs contain none of the pinned tokens; pin one full literal anchor line per dimension; assert the three anchor lines appear WITHIN each numbered dimension block by index; assert each dimension name's occurrence COUNT so deleting either name-carrying block fails.
2. Pin the pairwise response format: literal `{"verdict": "<A, B or TIE>", …}` and "Reply with ONE JSON object" in the pairwise render (the deletion that passed all 906 tests).
3. Pin the load-bearing sentences: Task 2's order sentence, "TIE is a real answer…", "deliberately not a similarity score".
4. Golden sha: `test_the_prompt_version_is_coupled_to_the_rendered_template_text` — pinned deterministic payload fixture, recorded `GOLDEN_RUBRIC_PROMPT_SHA` / `GOLDEN_PAIRWISE_PROMPT_SHA` constants, assert `prompt_sha(render) == constant` AND `JUDGE_PROMPT_VERSION == 2`, failure message: "the prompt text changed: bump JUDGE_PROMPT_VERSION and re-record both constants".
5. Spec amendments: (a) two-forced-position protocol replaces majority-of-3 — temp-0 majority carried the statistical content of one vote; forced positions extract the full information a deterministic judge has, at 2 calls not 3, and yield §4.2.3's position probe for free; `JUDGE_PROMPT_VERSION` now also moves on protocol changes; (b) "win rates convert to Elo" is read as Bradley–Terry MLE on the Elo scale — the single-pass K-update was name-order-dependent (measured), mean anchor 1000, virtual-tie prior noted.

**Steps:**
- [ ] **Step 1: Write the rewritten/new tests; mutation-verify** (delete anchors → red; delete response-format block → red; delete either name-carrying block alone → red; label swap still red; PYTHONDONTWRITEBYTECODE=1).
- [ ] **Step 2: Record the golden shas from the current (post-Task-2) templates.**
- [ ] **Step 3: Write the spec Amendments section.**
- [ ] **Step 4: Full offline suite green.**
- [ ] **Step 5: Commit** — `test: pin the prompts, couple the version to the text, and amend the spec`
