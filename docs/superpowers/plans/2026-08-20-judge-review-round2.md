# Judge review round 2 — 10 confirmed findings, 4 tasks

A 7-finder + adversarial-verify review of the judge implementation (2026-08-20,
branch `judge-review-fixes`) confirmed 10 defects. This plan fixes them in 4
tasks, one file-cluster each. Findings are numbered F1–F10 as reported.

## Global Constraints

- **TDD.** Every fix lands with a test that fails before the fix and passes
  after. Write the failing test first, watch it fail, then fix.
- **Docstrings carry the why and the failure mode**, cross-referenced to spec
  sections. Claims about external behavior are annotated with what they were
  verified against; do not assert new ones without checking.
- **`bakeoff.judge`, `bakeoff.judge_schema`, `bakeoff.codex_judge`,
  `bakeoff.similarity` must stay importable without pulling `litellm`,
  `docker`, `bakeoff.litellm_patches`, or `bakeoff.proxy_callback`.**
- **Resume keys must not change meaning.** Gate-decided units keep effort
  `None` in their resume key — pinned by
  `test_a_gate_decided_line_resumes_across_efforts_without_a_duplicate`. That
  test must still pass unmodified.
- **The judgments file stays append-only.** No stored-line shape changes that
  would need a `JUDGE_SCHEMA_VERSION` bump unless the task says so; if a bump
  becomes necessary, bump it and record why in the version comment.
- **Loud failure over plausible zero.** A refusal names the cause and the fix.
- Per task, run only the named test files plus any test file you touched
  (`cd bakeoff && .venv/bin/python -m pytest tests/<file> -v`). The controller
  runs the full suite at the end.
- Work from repo root `/Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval`.

## Task 1 — `bakeoff/src/bakeoff/judge.py`: neutrality guard + verdict extractor

**F1 (judge.py:316-331, 223-255).** `assert_neutral_judge`'s vendor rule is
`lowered.startswith(prefix)` over `("anthropic.", "google.", "nvidia.",
"moonshot.")`, so a region- or route-prefixed id (`us.anthropic.opus-6`,
`bedrock/anthropic.opus-6` — the shapes Bedrock actually uses) evades it, and
`NON_NEUTRAL_FAMILY_TOKENS = ("claude", "gemma", "gemini", "nemotron",
"kimi")` misses Anthropic's product names, so a re-host named `sonnet-5` /
`opus-6` / `haiku-4-5` passes.

Fix:
- Vendor rule becomes a substring test: refuse when any vendor namespace
  string (`"anthropic."` etc.) appears **anywhere** in the lowered id, not
  only at the start. The guard errs toward refusal by design — the
  `BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE` escape exists for a false positive.
- Add `"sonnet"`, `"opus"`, `"haiku"` to `NON_NEUTRAL_FAMILY_TOKENS`. Match
  with the same semantics the existing tokens use (read the code; keep it
  consistent). Update the docstring's account of what the rules catch.

Tests (in `bakeoff/tests/test_judge.py`): `us.anthropic.opus-6` refused;
`bedrock/anthropic.opus-6` refused; `some-host.sonnet-5` refused;
`openrouter.haiku-4.5` refused; a genuinely neutral id (e.g.
`deepseek.v3.2`, `openai.gpt-5.6-sol`) still accepted.

**F6 (judge.py:1010-1067, `_json_object` / `_first_balanced_object`).** The
extractor takes the first balanced brace group; if that group is
balanced-but-not-JSON prose (a set literal, a code snippet), `json.loads`
fails and `MalformedVerdict` is raised without ever scanning for the next
balanced group. At temperature 0 every retry reproduces the same reply, so a
valid verdict present in every paid reply is lost.

Fix: on `json.JSONDecodeError`, continue scanning from after the failed
group's opening brace for the next balanced object; raise `MalformedVerdict`
only when no balanced group in the reply parses. Single-object replies keep
their current behavior byte-for-byte. Update the docstring: the failure mode
it now defends against is "braced prose before the verdict burns the unit
deterministically at temperature 0".

Tests: a reply with `{not json}` prose before a valid verdict object parses
the verdict; a reply whose only brace groups are non-JSON still raises
`MalformedVerdict`; existing extractor tests unchanged.

Test files: `tests/test_judge.py`.

## Task 2 — `bakeoff/src/bakeoff/codex_judge.py`: usage totality + env scrub + Ctrl-C recovery

**F7 (codex_judge.py:643-665, `usage_from_events`).** The loop walks
`reversed(events)` and returns on the first `turn.completed` met — the last
in the stream — so earlier turns' usage is dropped, and an unreadable last
block returns `None` even when an earlier block was readable. Spend wrong in
the low direction, the direction the module's own comments forbid.

Fix: fold usage across **all** `turn.completed` events. Readable blocks sum;
an unreadable block among readable ones must still surface to the caller so
`calls_without_usage` is bumped while the readable counts contribute — this
mirrors `_add_usage`'s documented partial-block rule. Pick the interface
(e.g. return `(counts | None, unreadable_block_count)` or a small dataclass)
and update the one caller (`_fold_usage`) coherently. Do **not** touch the
"reasoning is a breakdown of output" decision — it is a separately tracked
open verification.

Tests: multi-`turn.completed` stream sums; last-block-unreadable with earlier
readable folds the readable and bumps the marker; zero readable blocks
returns none-shape and bumps the marker; single-turn behavior unchanged.

**F9 (codex_judge.py:386-402, `codex_environment`).** Only `OPENAI_API_KEY`
is popped; `OPENAI_BASE_URL` and other codex-honored provider variables ride
through and silently reroute judge calls while the records attest the seat.

Fix: remove every variable whose name starts with `OPENAI_` or `CODEX_`,
then set `CODEX_HOME` explicitly. Docstring must say why this is a prefix
denylist rather than the repo-preferred full allowlist: codex needs
system-level variables (`PATH`, locale, tmpdir) that cannot be enumerated,
and the provider-authority surface *can* be: it is exactly the two prefixes.

Tests: `OPENAI_BASE_URL`, `OPENAI_ORG`, `CODEX_API_KEY` scrubbed;
`CODEX_HOME` set to the judge home; `PATH`/unrelated vars survive.

**F10 (codex_judge.py:459-492, `_run_codex`).** The `TimeoutExpired` branch
recovers stdout and attaches `exc.events` so the caller folds paid usage; the
`except BaseException` branch (Ctrl-C) kills the process group and re-raises
with no recovery — the same wrong-in-the-low-direction spend defect commit
`0e47517` fixed on the timeout path, on the adjacent escape.

Fix: after `_kill_group`, attempt a **bounded** recovery (a short
`communicate` timeout, a few seconds) inside a `try` that can never mask the
original exception; parse whatever stdout was recovered and make the usage
reach the caller's fold before the re-raise (attach to the exception or fold
in `_attempt_once`'s `BaseException` handling — pick the shape that keeps
`_fold_usage`'s two-key decision intact). Ctrl-C must still exit promptly and
the original exception must propagate unchanged.

Tests: a fake process whose stdout already carries a `turn.completed` block
when interrupted folds those tokens before the exception propagates; a
recovery that itself fails still propagates the original exception.

Test files: `tests/test_codex_judge.py`.

## Task 3 — `bakeoff/scripts/judge.py`: generation split + supersede direction + manifest digest + exit contract

The big one. Four findings, one file.

**F2 (scripts/judge.py:2732-2737 `_judge_generation`, :1194, :3488-3604).**
Gate-decided lines write `judge_sampling={}` (nothing was sent — correct),
but `_judge_generation` derives its effort field from stored sampling, so on
a codex pass with `--reasoning-effort high` the pass's vote lines land in
generation `(judge, prompt, rubric, "high")` while its gate-decided lines
land in `(judge, prompt, rubric, None)`. Reproduced: `summarize` prints two
blocks — the effort block excludes every gate-decided pair from
`win_rate_x`/Elo and reports `gate_decided: 0`, a phantom effort-`None` block
holds only ladder results, and rule-2 supersession can never fire across the
split.

Fix — **aggregation only; stored lines and resume keys do not change**:
gate-decided lines are effort-agnostic ladder facts. In `summarize`'s bucket
walk, a gate-decided line joins **every** generation block that shares its
`(judge_model_id, judge_prompt_version, rubric_version)` triple, whatever
that block's effort. Vote supersession then applies within each block. When a
collection holds two effort generations, the same gate line is counted in
both blocks — correct, because blocks are separate readings reported side by
side, never pooled. Document exactly this in the bucket-walk docstring.

Tests: one codex pass at effort `high` with 2 voted comparisons + 1
gate-decided → **one** block, `gate_decided: 1`, gate pair in the combined
win rates; same collection judged at two efforts → the gate line appears in
both blocks; a vote line superseding a gate line works inside an effort
block (rule 2 fires, `superseded_gate_decided` increments).

**F3 (scripts/judge.py:3544-3556).** The vote-over-gate-decided preference is
unconditional; the docstring's justification ("the votes are the later,
richer verdict") is asserted, not checked. After a re-grade flips a
previously-passing side to failed, a **newer** gate-decided line is discarded
in favor of stale votes bought against a grade that no longer stands.

Fix: make the direction claim real. Read what the lines actually carry
(`grade_version_seen`, `judged_at` — inspect the stored-line fields) and
prefer votes **only when they are not stale relative to the gate line**:
if the gate-decided line reflects a strictly newer grade generation than the
votes were bought under, the gate line wins and the votes are counted in a
named counter (not silently dropped — extend the summary the way
`superseded_gate_decided` does, e.g. `superseded_votes`). If grade
generations are equal or incomparable, votes win (current behavior). Tie
goes to votes. Update the docstring to argue both directions.

Tests: re-grade-flip scenario — newer gate line beats older votes and the
counter names it; same-generation votes still beat a gate line
(existing behavior pinned).

**F4 (scripts/judge.py:1766, 2672-2691; grade_schema.py:302;
tasks.py:717).** The driver holds `graded_against_manifest_digest` on every
grade and `TaskManifest.manifest_digest` on every loaded task, and compares
neither — a task edited between grading and judging silently anchors
payloads on a reference the gate never gated.

Fix: when building a task's units, compare each gating grade's
`graded_against_manifest_digest` to the loaded task's `manifest_digest`.
Mismatch → refuse **every unit of that task** into the `errors` bucket
(exit 1), with a message naming both digests and both remedies (re-grade
under the current taskset, or check out the taskset state the grades were
taken against). Other tasks proceed. A grade line whose digest is empty or
absent is *unknown*, not a mismatch: warn (into `warnings`) and proceed,
documented — absence is recorded, never implied.

Tests: digest mismatch refuses the task's units, errors name both digests,
other tasks still judged; empty digest warns and proceeds; matching digest
is silent.

**F8 (scripts/judge.py:1790-1813, 2118-2122, 4672).** The run-read loop
reads every graded run before `--only-task`/`--samples` filtering, and a
read failure lands in `errors`, so one corrupt run record forces exit 1 on
every future pass — including passes that never select that run — with no
flag to route around it, violating the documented "exit 0 means every
selected unit produced its lines" contract.

Fix: apply the task filter (**GradeRecord carries `task_id`**) — and the
sample filter if the needed key is available pre-read — **before**
`read_run`. A run excluded by filters is never read. A read failure on a run
that *would* participate in selection stays an error (the contract's
"selected" is honest). This also removes the eager full-collection read on
`--only-task` passes (a flagged efficiency cost). Document the boundary in
the read-loop comment: unselected-unreadable is not this pass's problem;
selected-unreadable is.

Tests: corrupt run JSON in task A + `--only-task B` → exit 0, no error line,
task B judged; corrupt run in the selected task → exit 1 with the read
error; no-filter pass still errors on the corrupt run.

Test files: `tests/test_judge_script.py`.

## Task 4 — `bakeoff/src/bakeoff/similarity.py`: lexical paths must match git-exact drop paths

**F5 (similarity.py:61, 184, 188-218).** Two defects with one consequence —
`allow_extra_paths` files are not dropped from `_kept_chunks`, so they
inflate `file_overlap` and `diff_size_ratio` in every payload shown to the
judge (the lexical path feeds a *filter*, so it moves numbers, not just a
context signal, which is beyond what the module docstring's purity trade
concedes):

- `_header_path` keeps git's `\t` terminator (paths containing spaces get a
  trailing TAB in `---`/`+++` headers) and C-quoted paths (`"b/pa\th.py"`
  for non-ASCII/escape characters) verbatim, so they can never equal the
  git-exact paths `tasks.py` puts in the manifest.
- The pure-rename fallback `_DIFF_GIT = r"^diff --git a/(.+) b/(.+)$"`
  greedy-splits on the **last** `" b/"`, misparsing any path containing
  `" b/"`, and declines quoted forms entirely.

Fix:
- `_header_path`: strip the trailing TAB terminator; C-unquote a
  double-quoted path (strip quotes, decode `\t` `\n` `\"` `\\` and octal
  escapes — implement the small decoder, no new dependency). The result must
  equal the path git plumbing reports for the same file.
- `_DIFF_GIT` fallback: make the split deterministic where it can be —
  when some split of the line yields identical a/ and b/ paths (the
  mode-change / no-content-change case, which is the common no-`---`/`+++`
  shape), prefer it; otherwise keep the current first-match behavior but
  say so honestly in the docstring. C-unquote quoted `diff --git` forms with
  the same decoder.
- Update the module docstring: the purity trade now excludes the drop
  filter — dropped paths are exact, only unparseable exotica degrade to a
  context signal.

Tests (in `tests/test_similarity.py`): a chunk touching `CHANGES 3360.rst`
(TAB-terminated header) is dropped when the manifest names that path; a
C-quoted non-ASCII path is dropped; a `diff --git a/x b/x`-style
mode-change chunk resolves both endpoints to `x`; existing tests unchanged.

## Not in this plan (tracked, deliberate)

Confirmed-but-cut findings parked for a later wave: the bare `assert` in
`_stored_payload_path` (dies under `python -O`), the split key/value secret
shape, `_vote_line`'s missing pre-append gate re-assert, the mantle
401-refresh mint outside the build lock, six reuse duplications, the
print-only κ caveat. TASKS.md carries the judge channel's non-code blockers
(neutral-judge entitlement, V1–V9, OPEN-5 κ).
