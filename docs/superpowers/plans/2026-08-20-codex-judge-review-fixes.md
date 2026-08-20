# Codex Judge Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 13 top findings from the codex-judge branch review: identity (reasoning effort missing from resume/generation keys), process hygiene (Ctrl-C kill path, strict decode), stop-signal completeness (mantle backend, sleeping backoff), CLI validation (mantle+effort, effort values, mixed-case prefix), resolution hardening (judge home contents, binary executability), the concurrent walk's head-of-line stall, the timeout path's dropped usage, and the vacuous attestation assert.

**Architecture:** Every fix is a contained edit to one of four files (`bakeoff/scripts/judge.py`, `bakeoff/src/bakeoff/codex_judge.py`, `bakeoff/src/bakeoff/judge.py`, `bakeoff/tests/test_integration_codex.py`) plus its tests. No schema change, no new module, no change to prompts, parsers, `VOTE_POSITIONS`, or `JUDGE_PROMPT_VERSION`. The one identity-bearing change (Task 1) adds a fourth element to the resume/generation tuples in a way that leaves every existing mantle line's key semantics intact (the new element is `None` for every line that exists today, derived identically on the stored and batch sides).

**Tech Stack:** Python >=3.11, pytest, stdlib only. All commands run from `bakeoff/` with `.venv/bin/python`.

## Global Constraints

- Offline suite green after every task: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` (unit selection; `addopts` already excludes `integration`).
- The event log and `judgments.jsonl` stay append-only; nothing writes into an existing line.
- Docstrings carry the *why* and the failure mode (house style). A change that alters an invariant updates the prose that explains it. Do not add comments that merely narrate code.
- Test names are full sentences describing the invariant (house style: `test_the_x_does_y_because_z`).
- Claims about external behavior must carry what they were verified against; do not add new unverified external claims.
- `bakeoff/src/bakeoff/judge.py` must stay importable without litellm; `bakeoff/src/bakeoff/codex_judge.py` stays stdlib-only.
- `test_only_payload_inputs_from_touches_a_run_record_or_a_grade_record` (AST scan of `judge.py`) must stay green.
- Do NOT run `scripts/mutation_check.py` concurrently with anything else; it is not needed for these tasks.
- Git commits end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Line numbers below were verified on branch `codex-judge` at commit 954ab43. Re-locate by symbol name if drifted.

---

### Task 1: reasoning effort joins the resume keys and the generation partition

**Files:**
- Modify: `bakeoff/scripts/judge.py` — `_resume_key` (:615), `_rubric_key` (:656), `_pairwise_key` (:661), selection call sites (:2045, :2083, :2105), `_judge_generation` (:2535), `_GENERATION_FIELDS` (:2563), `_generation_label` (:3874), `judge_event_log` (batch effort, near :1615)
- Test: `bakeoff/tests/test_judge_script.py` — `_judge_gen` helper (:3124), `test_mantle_lines_still_carry_the_pinned_sampling_block_unchanged` (:5394-5412), the generation assertions at :4299-4300, plus three new tests

**Interfaces:**
- Produces: `_rubric_key(run_id: str, judge_model_id: str, effort: str | None) -> tuple` and `_pairwise_key(task_id, sample_index, run_id_a, run_id_b, vote_index, judge_model_id, effort: str | None) -> tuple` — the `effort` parameter is REQUIRED (no default), so a call site that forgets it fails at collection time rather than silently keying without it.
- Produces: `_judge_generation(judgment) -> tuple` now returns a 4-tuple `(judge_model_id, judge_prompt_version, rubric_version, effort)`; `_GENERATION_FIELDS = 4`.
- The stored-side effort is always `judgment.judge_sampling.get("model_reasoning_effort")` — `None` for every mantle line, every gate-decided line, every schema-1.0.0 line, and every codex line judged without the flag. The batch-side effort is `reasoning_effort if backend == "codex" else None`, and `None` for gate-decided units (nothing is sent, so effort is not part of that unit's identity — this is what lets a gate-decided line bought under one effort satisfy a resume at another without a duplicate append).

Why this shape: a verdict's reproducibility identity is "the model that gave it, the prompt text it saw, the rubric it was scored under" — and now, on the codex backend, the effort it was sampled at. Leaving effort out lets a resume mix efforts inside one comparison and lets every aggregate pool high- and default-effort verdicts under one generation, silently — the exact pooling the `codex:` namespace exists to make inexpressible.

- [ ] **Step 1: Write the failing tests** (append to `bakeoff/tests/test_judge_script.py`, near the other codex-backend tests at :5280+):

```python
def test_two_efforts_over_one_collection_are_two_generations_never_pooled(
    tmp_path, monkeypatch
):
    """The effort is the codex backend's one sampling knob, so two passes at
    two efforts are two readings of the collection -- resume must re-buy, and
    aggregation must partition. Without this, a resume that omitted the flag
    would skip vote 0 (bought at high) and buy vote 1 at default, and
    `majority` would combine two different judges as one.
    """
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path)

    _run(root, judge_model_id=CODEX_JUDGE, rubric=False,
         reasoning_effort="high")
    first = len(_lines(root))
    result = _run(root, judge_model_id=CODEX_JUDGE, rubric=False)

    # Nothing skipped: the default-effort pass is new work, not a resume.
    assert not result["skipped"]
    assert len(_lines(root)) > first
    generations = {
        judge_script._judge_generation(line) for line in _lines(root)
    }
    assert len(generations) == 2


def test_a_resume_at_the_same_effort_skips_every_unit_already_bought(
    tmp_path, monkeypatch
):
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path)

    _run(root, judge_model_id=CODEX_JUDGE, rubric=False,
         reasoning_effort="high")
    before = len(_lines(root))
    result = _run(root, judge_model_id=CODEX_JUDGE, rubric=False,
                  reasoning_effort="high")

    assert result["skipped"]
    assert len(_lines(root)) == before


def test_a_gate_decided_line_resumes_across_efforts_without_a_duplicate(
    tmp_path, monkeypatch
):
    """A gate-decided pair sends nothing, so effort is not part of its
    identity. Keying it on the batch's effort would append a duplicate
    gate line on every resume that changed the flag -- permanent lines in an
    append-only file, saying nothing new.
    """
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path, resolved_b=False)

    _run(root, judge_model_id=CODEX_JUDGE, rubric=False,
         reasoning_effort="high")
    gate_lines = [ln for ln in _lines(root) if ln.verdict == "gate_decided"]
    assert len(gate_lines) == 1

    result = _run(root, judge_model_id=CODEX_JUDGE, rubric=False)
    gate_lines = [ln for ln in _lines(root) if ln.verdict == "gate_decided"]
    assert len(gate_lines) == 1
    assert result["skipped"]
```

Note: `_run` forwards `**kw` to `judge_event_log`, so `reasoning_effort="high"` passes through. `_fake_harness`, `CODEX_JUDGE`, `_two_arms`, `_lines` already exist in this file (:5289-5320).

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_judge_script.py -k "effort or gate_decided_line_resumes" -q`
Expected: the two new generation/resume tests FAIL (one generation where two are expected; `skipped` non-empty where new work is expected). The gate-decided test may pass before the change — it pins the invariant against the change.

- [ ] **Step 3: Implement**

In `bakeoff/scripts/judge.py`:

(a) `_resume_key` — append the sampling identity to `versions` and extend the docstring:

```python
    versions = (
        judgment.judge_model_id,
        judgment.judge_prompt_version,
        judgment.rubric_version,
        # The one operator-variable sampling knob. `None` on every mantle
        # line (JUDGE_SAMPLING carries no such key), every gate-decided line
        # (judge_sampling is {}), and every codex line judged without the
        # flag -- so every line that exists today keys exactly as it did.
        # Absent from the key, a resume that changed the flag would skip
        # vote 0 bought at one effort and buy vote 1 at another, and
        # `majority` would combine two different judges as one.
        judgment.judge_sampling.get("model_reasoning_effort"),
    )
```

Add one sentence to the docstring paragraph about the three version fields: they are now four — the effort a codex line was sampled at is part of what makes a verdict reproducible, and `None` (mantle, gate-decided, pre-flag codex) is itself a value the two sides derive identically.

(b) `_rubric_key` / `_pairwise_key` — required `effort` parameter, appended in the same position:

```python
def _rubric_key(run_id: str, judge_model_id: str,
                effort: str | None) -> tuple:
    return ("rubric", run_id, judge_model_id, JUDGE_PROMPT_VERSION,
            RUBRIC_VERSION, effort)


def _pairwise_key(task_id: str, sample_index: int, run_id_a: str,
                  run_id_b: str, vote_index: int | None,
                  judge_model_id: str, effort: str | None) -> tuple:
    return ("pairwise", task_id, sample_index, run_id_a, run_id_b, vote_index,
            judge_model_id, JUDGE_PROMPT_VERSION, RUBRIC_VERSION, effort)
```

(c) In `judge_event_log`, right after `backend = _judge_backend(judge_model_id)` (:1615), compute the batch-side identity once:

```python
    # The batch side of the sampling identity in every resume key. `None` on
    # the mantle backend whatever the argument says, because the mantle path
    # sends no effort -- the key must describe what goes out, not what was
    # typed.
    batch_effort = reasoning_effort if backend == "codex" else None
```

(d) The three selection call sites:
- :2045 → `_rubric_key(record.run_id, judge_model_id, batch_effort)`
- :2083 (gate-decided) → `_pairwise_key(task_id, sample_index, run_id_a, run_id_b, None, judge_model_id, None)` — with the comment `# effort None: nothing is sent for a gate-decided pair, so effort is not part of its identity and a flag change must not re-append it.`
- :2105 (votes) → `_pairwise_key(task_id, sample_index, run_id_a, run_id_b, vote_index, judge_model_id, batch_effort)`

(e) `_judge_generation` — append the same stored-side element and extend the docstring one sentence:

```python
    return (
        judgment.judge_model_id,
        judgment.judge_prompt_version,
        judgment.rubric_version,
        judgment.judge_sampling.get("model_reasoning_effort"),
    )
```

(f) `_GENERATION_FIELDS = 4` (update the comment's "four-field generation" arithmetic if it references three).

(g) `_generation_label` — unpack four, print the effort only when set so every existing mantle printout is byte-identical:

```python
    judge_model_id, prompt_version, rubric_version, effort = generation
    label = (
        f"judge {judge_model_id}, prompt v{prompt_version}, "
        f"rubric {rubric_version}"
    )
    return label if effort is None else f"{label}, effort {effort}"
```

(h) Grep for every other caller of `_rubric_key`/`_pairwise_key`/`_judge_generation` in `scripts/judge.py` and the tests (`grep -n "_rubric_key\|_pairwise_key\|_judge_gen" scripts/judge.py tests/test_judge_script.py`) and update signatures/expected tuples: the `_judge_gen` test helper (:3124) gains an `effort=None` parameter appended to its tuple; the assertion at :5409 becomes the 4-tuple ending in `None`; :4300's expectation likewise.

- [ ] **Step 4: Run the new tests, then the full offline suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_judge_script.py -q` then `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, no other test broken.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/scripts/judge.py bakeoff/tests/test_judge_script.py
git commit -m "fix: the reasoning effort is part of a verdict's identity

Absent from the resume keys and the generation partition, two efforts
pooled into one number and a resume could mix efforts inside one
comparison. The element is None on every line that exists today, so no
existing collection re-keys.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `_run_codex` kills the group on every escape, and decodes leniently

**Files:**
- Modify: `bakeoff/src/bakeoff/codex_judge.py` — `_run_codex` (:376-430), `codex_cli_version` (:705-720)
- Test: `bakeoff/tests/test_codex_judge.py`

**Interfaces:**
- Consumes/produces nothing new — `_run_codex(argv, prompt, env, timeout_s, output_file) -> CodexRun` is unchanged in signature.

Why: only `TimeoutExpired` triggers the process-group kill today. `start_new_session=True` means the terminal's Ctrl-C never reaches the child, and CPython's `Popen.__exit__` assumes it did — so a KeyboardInterrupt out of `communicate` leaves a detached `codex exec` running and billing the seat with nothing enforcing the timeout, and any other exception blocks in `__exit__`'s untimed `wait()`. Separately, `text=True` with no `encoding`/`errors` decodes strictly with the locale codec: the SIGKILL-truncated recovery read can raise `UnicodeDecodeError` *instead of* the intended timeout error, and one undecodable byte in the diagnostic stream can discard a paid exit-0 verdict.

- [ ] **Step 1: Write the failing tests** (append to `bakeoff/tests/test_codex_judge.py`; these drive the real `_run_codex` with a scripted shell binary, not the `_FakeCodex` seam):

```python
def test_any_escape_from_communicate_kills_the_process_group(
    tmp_path: Path,
):
    """Only TimeoutExpired had a kill path. A KeyboardInterrupt (or any
    other exception) out of `communicate` must not leave the detached child
    running -- `start_new_session=True` means the terminal's Ctrl-C never
    reached it, so the driver is the only thing that can stop the spend.
    """
    import subprocess as _subprocess

    marker = tmp_path / "child-alive"
    binary = tmp_path / "codex"
    # A child that records its pid, ignores stdin, and lingers.
    binary.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {marker}\n"
        "sleep 300\n"
    )
    binary.chmod(0o755)

    real_communicate = _subprocess.Popen.communicate

    def interrupted(self, *args, **kwargs):
        # Let the child start and write its pid, then interrupt.
        deadline = time.monotonic() + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise KeyboardInterrupt

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_subprocess.Popen, "communicate", interrupted)
        with pytest.raises(KeyboardInterrupt):
            codex_judge._run_codex(
                [str(binary)], "prompt", dict(os.environ), 60.0,
                tmp_path / "out.txt",
            )

    child_pid = int(marker.read_text().strip())
    # SIGKILL is asynchronous; give it a moment, then the pid must be gone.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("the child survived the escape from communicate")


def test_undecodable_output_never_discards_a_paid_verdict(tmp_path: Path):
    """`text=True` with no errors= decoded strictly, so one bad byte in the
    diagnostic stream turned an exit-0 run with a written verdict into a
    per-unit error -- the most expensive way this module can be wrong.
    """
    binary = tmp_path / "codex"
    out_file = tmp_path / "out.txt"
    binary.write_text(
        "#!/bin/sh\n"
        "printf '\\377\\376 not utf-8\\n'\n"
        f"printf 'the verdict' > {out_file}\n"
        "exit 0\n"
    )
    binary.chmod(0o755)

    run = codex_judge._run_codex(
        [str(binary)], "prompt", dict(os.environ), 60.0, out_file
    )

    assert run.exit_code == 0
    assert run.last_message == "the verdict"
    assert failure_message(run) is None
```

Add the imports the tests need at the top of the file if absent: `import os`, `import signal`, `import time`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_codex_judge.py -k "escape_from_communicate or undecodable" -q`
Expected: the first FAILS (child survives — `os.kill(pid, 0)` succeeds through the deadline), the second FAILS with `UnicodeDecodeError`.

- [ ] **Step 3: Implement** in `_run_codex`:

```python
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        # Lenient on purpose, and asymmetric with nothing: the -o file is
        # already read with errors="replace", and a strict decode here turned
        # one bad byte in the DIAGNOSTIC stream into a discarded paid
        # verdict, and the SIGKILL-truncated recovery read into a
        # UnicodeDecodeError reported in place of the timeout it was.
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                # BOUNDED. (existing comment stays)
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", "(the killed process never closed its pipes)"

            raise CodexCallFailed(
                f"codex exec exceeded {timeout_s:.0f}s and its process group "
                f"was killed; stderr: {_tail(stderr)}"
            ) from None
        except BaseException:
            # Ctrl-C above all. The child is in its own session, so the
            # terminal's SIGINT never reached it -- and Popen.__exit__
            # assumes it did: on KeyboardInterrupt it waits a fraction of a
            # second and moves on, leaving a detached codex exec billing the
            # seat with nothing enforcing the timeout; on anything else it
            # blocks in an untimed wait(). Killing the group here is the only
            # thing that makes either path end.
            _kill_group(process)
            raise
        exit_code = process.returncode
```

with the kill hoisted into a module-level helper (it now has two callers):

```python
def _kill_group(process: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group, falling back to the leader.

    Codex spawns helpers; killing only the leader leaves them holding the
    pipe, and the `communicate` that follows would wait on a call already
    declared dead.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
```

Also add `encoding="utf-8", errors="replace"` to the `subprocess.run` in `codex_cli_version` (drop `text=True` there or keep it beside — `encoding=` implies text mode).

- [ ] **Step 4: Run the new tests, then the module's suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_codex_judge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/codex_judge.py bakeoff/tests/test_codex_judge.py
git commit -m "fix: every escape from communicate kills the codex group, and decode is lenient

Only TimeoutExpired had a kill path; a Ctrl-C left a detached child
billing the seat, and strict locale decoding could discard a paid exit-0
verdict over one bad diagnostic byte.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: the stop signal reaches the mantle backend, and wakes a sleeping backoff

**Files:**
- Modify: `bakeoff/src/bakeoff/judge.py` — `live_completion` (:1489+, closure :1547-1633)
- Modify: `bakeoff/src/bakeoff/codex_judge.py` — `codex_completion` (:833+)
- Modify: `bakeoff/scripts/judge.py` — `lazy_live_completion` (:673), the selection at :1779-1785
- Test: `bakeoff/tests/test_judge.py`, `bakeoff/tests/test_codex_judge.py`, `bakeoff/tests/test_judge_script.py`

**Interfaces:**
- Produces: `live_completion(judge_model_id=..., region=..., usage_totals=None, stop: threading.Event | None = None) -> CompleteFn` (keyword-only is fine; match the existing style of trailing keywords).
- Produces: `lazy_live_completion(judge_model_id, usage_totals=None, stop=None) -> CompleteFn`.
- The driver passes `stop_spending` on BOTH arms of the selection at :1779.

Why: `stop_spending`'s comment says "handed to the backend at construction", but only the codex arm receives it — an aborted concurrent mantle pass keeps spending, *including minting a fresh credential*, after the summary printed. And on the codex side the backoff sleeps via plain `time.sleep`, which the event cannot wake: a worker in a capped 480 s backoff holds the interpreter (atexit join) for up to 8 minutes after the report, and the docstring's "no further backoff is waited out" is false for a backoff already in progress.

- [ ] **Step 1: Write the failing tests**

In `bakeoff/tests/test_codex_judge.py`:

```python
def test_a_stop_request_wakes_a_backoff_already_in_progress(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    """`shutdown(cancel_futures=True)` cannot cancel a started call, and
    `time.sleep` cannot be woken -- so without this, a worker in a capped
    480s backoff holds the interpreter for up to 8 minutes after the
    summary, and the docstring's 'no further backoff is waited out' is
    false in exactly the window it matters.

    With no injected sleep and a stop event, the backoff must wait ON THE
    EVENT, so setting it mid-wait returns promptly instead of sleeping out
    the delay.
    """
    limited = CodexRun(1, _events(_failed("unexpected status 429")), "", "")
    _install(monkeypatch, [limited] * (CODEX_RATE_LIMIT_RETRIES + 1))
    stop = threading.Event()
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        stop=stop,
    )

    timer = threading.Timer(0.2, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CodexCallFailed, match="stopped before this call"):
            complete("prompt")
    finally:
        timer.cancel()

    # The first backoff alone is >= 15s (30 * 0.5 jitter floor). Waking
    # within a couple of seconds proves the wait was on the event.
    assert time.monotonic() - started < 5.0
```

(add `import time` to the test file's imports if absent)

In `bakeoff/tests/test_judge.py`, beside the existing `live_completion` tests (grep for `live_completion` fixtures there and reuse their fake-router pattern; they drive `complete` with a monkeypatched `_judge_router`/`_completion`):

```python
def test_a_stopped_live_completion_never_calls_and_never_mints(monkeypatch):
    """The stop event's contract is batch-wide: 'nothing further is bought'.
    Only the codex backend honoured it; a stopped mantle worker still made
    its call -- and on a 401 minted a fresh credential -- after the pass
    reported its totals.
    """
    import threading as _threading
    from bakeoff import judge as judge_module

    calls = {"n": 0}
    monkeypatch.setattr(
        judge_module, "_judge_router", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        judge_module, "_completion",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "reply",
    )
    stop = _threading.Event()
    complete = judge_module.live_completion(
        "openai.gpt-5.6-sol", usage_totals=None, stop=stop
    )
    stop.set()

    with pytest.raises(RuntimeError, match="stopped before this call"):
        complete("prompt")

    assert calls["n"] == 0
```

(Adapt the monkeypatch of `smoke_bedrock` resolution to whatever the existing `live_completion` tests in `test_judge.py` do at construction — reuse their exact fixture/monkeypatch pattern; `live_completion` resolves `scripts.smoke_bedrock` at construction, so the test may need `monkeypatch.setattr` on that import the same way neighboring tests do. Read the neighboring tests first and copy their arrangement.)

In `bakeoff/tests/test_judge_script.py`, extend the existing seam-built assertion (`test_the_reasoning_effort_reaches_the_backend_and_not_only_the_record`, :5560+) with a mantle twin:

```python
def test_the_mantle_backend_receives_the_batch_stop_event_too(
    tmp_path, monkeypatch
):
    """`stop_spending`'s comment says 'handed to the backend at
    construction'. Only the codex arm got it, so an aborted concurrent
    mantle pass kept minting and spending after the summary printed.
    """
    seen: dict = {}
    monkeypatch.setattr(
        judge_script, "lazy_live_completion",
        lambda model, usage=None, stop=None: seen.update(
            model=model, stop=stop is not None
        ) or FakeComplete(),
    )
    root = _two_arms(tmp_path)

    judge_event_log(root, [_task()], rubric=False)

    assert seen == {"model": JUDGE_MODEL_ID_DEFAULT, "stop": True}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_codex_judge.py tests/test_judge.py tests/test_judge_script.py -k "stop" -q`
Expected: the three new tests FAIL (`TypeError: unexpected keyword argument 'stop'` on the mantle two; the wake test times out the 5 s bound or errors).

- [ ] **Step 3: Implement**

(a) `bakeoff/src/bakeoff/judge.py`, `live_completion`: add `stop: threading.Event | None = None` to the signature. In the closure:

```python
    def complete(prompt: str) -> str:
        if stop is not None and stop.is_set():
            # Checked before the call rather than after, for the codex
            # backend's reason: the point is to not BUY it. RuntimeError
            # rather than a named class because the driver discards these --
            # the walk has already stopped committing when the event is set.
            raise RuntimeError("the pass stopped before this call was made")
        with build_lock:
            ...
```

and before the mint in the auth-refresh path:

```python
            if not is_auth_failure(exc):
                raise
            if stop is not None and stop.is_set():
                # A stopped pass must not mint: the fresh credential and the
                # retry it buys are spend after the summary. Re-raise the
                # auth failure -- it is the truthful reason this call ends.
                raise
            fresh = smoke_bedrock.derive_mantle_token(region)
```

Extend `live_completion`'s docstring with one short paragraph naming the stop contract (mirror the codex docstring's phrasing). Update the module's `stop_spending`-related claims in `scripts/judge.py` if any say codex-only.

(b) `bakeoff/scripts/judge.py`, `lazy_live_completion`: add `stop: threading.Event | None = None` parameter, pass through: `built["fn"] = live_completion(judge_model_id, usage_totals=usage_totals, stop=stop)`. At the selection (:1779-1785): `else lazy_live_completion(judge_model_id, judge_usage, stop_spending)`.

(c) `bakeoff/src/bakeoff/codex_judge.py`, `codex_completion`: make the default backoff wait interruptible. After the `stopped = ...` line:

```python
    if sleep is time.sleep and stop is not None:
        # The default wait is the EVENT's, so a stop set mid-backoff wakes
        # the worker instead of sleeping out up to 480s after the summary.
        # An injected sleep wins -- tests inject one to observe the waits.
        sleep = stop.wait
```

Fix the docstring sentence "no further backoff is waited out" to say a backoff in progress is woken as well. (`Event.wait(timeout)` returns a bool; the call site ignores the return value, which is fine.)

- [ ] **Step 4: Run the full offline suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/judge.py bakeoff/src/bakeoff/codex_judge.py bakeoff/scripts/judge.py bakeoff/tests/
git commit -m "fix: the stop signal reaches both backends and wakes a sleeping backoff

stop_spending was handed only to the codex arm -- an aborted concurrent
mantle pass kept minting and spending after the summary -- and time.sleep
cannot be woken, so a worker mid-backoff held the interpreter for up to
8 minutes after the report.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: the CLI refuses what it used to drop silently

**Files:**
- Modify: `bakeoff/scripts/judge.py` — `main()` argparse (:4289-4296) and validation block (:4315+), `judge_event_log` (:1610-1620)
- Modify: `bakeoff/src/bakeoff/codex_judge.py` — `build_codex_argv` (:340-355), `codex_harness` (:762)
- Test: `bakeoff/tests/test_judge_script.py`, `bakeoff/tests/test_codex_judge.py`

**Interfaces:**
- `--reasoning-effort` gains `choices=("minimal", "low", "medium", "high")` — the closed set `codex exec -c model_reasoning_effort=` accepts (do NOT annotate this as verified against the CLI; phrase the help as "the values the backend sends", since the set is enforced here, not asserted about codex).
- `main()` refuses `--reasoning-effort` with a non-codex judge id, and refuses a judge id whose lowercase starts with `codex:` when its exact spelling does not (mixed-case prefix), both via `parser.error` (exit 2).
- `judge_event_log` raises `ValueError` on `reasoning_effort` with a mantle backend (library callers get a loud refusal too).
- `build_codex_argv` raises `ValueError` when `reasoning_effort` is not lowercase-alphabetic (defense in depth for direct library callers; the f-string interpolates into TOML).
- `codex_harness` records `reasoning_effort or None` so a falsy string can never make the harness claim an effort the argv dropped.

- [ ] **Step 1: Write the failing tests**

In `bakeoff/tests/test_judge_script.py`:

```python
def test_the_reasoning_effort_is_refused_with_a_mantle_judge(tmp_path):
    """Silently dropped configuration is this repo's named enemy. The flag
    is the codex backend's one knob; accepted beside a mantle id it ran the
    whole paid pass at defaults with nothing anywhere saying so.
    """
    root = _two_arms(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--event-log", str(root), "--reasoning-effort", "high"])
    assert exit_info.value.code == 2

    with pytest.raises(ValueError, match="codex"):
        judge_event_log(root, [_task()], reasoning_effort="high")


def test_a_mixed_case_codex_prefix_is_refused_not_silently_mantled(tmp_path):
    """`is_codex_judge` is exact while the neutrality guard lowercases, so
    'Codex:...' passed the guard and ran against the mantle router --
    permanent gate-decided lines under a mistyped id, then a breaker abort.
    """
    root = _two_arms(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--event-log", str(root),
              "--judge-model", "Codex:gpt-5.2-codex"])
    assert exit_info.value.code == 2

    with pytest.raises(ValueError, match="case"):
        judge_event_log(root, [_task()], judge_model_id="CODEX:gpt-5.2-codex")


def test_an_unknown_reasoning_effort_value_is_refused_at_the_usage_line(
    tmp_path,
):
    root = _two_arms(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--event-log", str(root),
              "--judge-model", "codex:gpt-5.2-codex",
              "--reasoning-effort", 'hi"gh'])
    assert exit_info.value.code == 2
```

In `bakeoff/tests/test_codex_judge.py`:

```python
def test_the_argv_builder_refuses_an_effort_that_would_break_the_toml(
    tmp_path: Path,
):
    """The effort is interpolated inside -c model_reasoning_effort="...".
    A quote or backslash yields invalid TOML that fails EVERY paid call
    until the breaker aborts -- a typo that deserved exit 2 before anything
    was read. The CLI's choices= already refuses it; this is the guard for
    direct library callers.
    """
    for bad in ('hi"gh', "hi\\gh", "hi gh", "hi\ngh"):
        with pytest.raises(ValueError, match="reasoning effort"):
            build_codex_argv(
                "/bin/codex", "m", tmp_path, tmp_path / "o.txt", bad
            )


def test_a_falsy_effort_never_reaches_the_harness_record(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    """argv and judge_sampling drop a falsy effort; the harness recorded it
    verbatim -- two evidence fields disagreeing about one request."""
    harness = codex_judge.codex_harness(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        reasoning_effort="",
    )

    assert harness["reasoning_effort"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_judge_script.py tests/test_codex_judge.py -k "refus or falsy_effort" -q`
Expected: FAIL — no SystemExit/ValueError raised today.

- [ ] **Step 3: Implement**

(a) `main()` — add `choices=("minimal", "low", "medium", "high")` to the `--reasoning-effort` argument (keep the existing help text, append ", one of the values the backend sends"). After the existing `--max-consecutive-errors` validation block:

```python
    lowered = args.judge_model.lower()
    if lowered.startswith("codex:") and not is_codex_judge(args.judge_model):
        parser.error(
            f"{args.judge_model!r} spells the codex namespace with the wrong "
            "case: the prefix is 'codex:' exactly. Refused rather than "
            "normalised, because the id goes onto every line verbatim and "
            "two spellings would be two generations of one judge"
        )
    if args.reasoning_effort is not None and not is_codex_judge(args.judge_model):
        parser.error(
            "--reasoning-effort is the codex backend's knob and the mantle "
            "path cannot send it; with a mantle judge id the flag would be "
            "silently dropped, which is worse than this refusal"
        )
```

(b) `judge_event_log` — immediately after `backend = _judge_backend(judge_model_id)`:

```python
    if judge_model_id.lower().startswith(CODEX_MODEL_PREFIX) \
            and backend != "codex":
        raise ValueError(
            f"{judge_model_id!r} spells the codex namespace with the wrong "
            "case: the prefix is spelled 'codex:' exactly, and a normalised "
            "id would put two spellings of one judge in one file"
        )
    if reasoning_effort is not None and backend != "codex":
        raise ValueError(
            "reasoning_effort is a codex-backend knob; the mantle path "
            "cannot send it and must not record it"
        )
```

(`CODEX_MODEL_PREFIX` joins the existing `from bakeoff.codex_judge import` block at the top; `is_codex_judge` is already imported.)

(c) `build_codex_argv` — before the `if reasoning_effort:` append:

```python
    if reasoning_effort and not re.fullmatch(r"[a-z]+", reasoning_effort):
        raise ValueError(
            f"unusable reasoning effort {reasoning_effort!r}: the value is "
            "interpolated into a TOML -c override, so anything beyond "
            "lowercase letters would corrupt every call's config rather "
            "than fail one"
        )
```

(`re` is already imported in `codex_judge.py`.)

(d) `codex_harness` — the returned dict's last entry becomes `"reasoning_effort": reasoning_effort or None,` with the comment `# `or None`: argv and sampling drop a falsy value, and the harness must not claim what they dropped.`

- [ ] **Step 4: Run the full offline suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS. (`test_the_reasoning_effort_reaches_the_record_as_sent` and friends use `"high"`, which the choices admit.)

- [ ] **Step 5: Commit**

```bash
git add bakeoff/scripts/judge.py bakeoff/src/bakeoff/codex_judge.py bakeoff/tests/
git commit -m "fix: refuse dropped-config shapes at the usage line

--reasoning-effort with a mantle judge was silently ignored; a
mixed-case codex: prefix silently selected the mantle backend; an
arbitrary effort string was interpolated into TOML and a falsy one was
recorded by the harness while the argv dropped it.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: resolution hardening — the judge home's contents, the binary's executability

**Files:**
- Modify: `bakeoff/src/bakeoff/codex_judge.py` — `_resolve_codex_home` (:674-698), `_resolve_codex_bin` (:660-672)
- Test: `bakeoff/tests/test_codex_judge.py`

**Interfaces:** unchanged signatures; both still raise `CodexUnavailable` with remediation text.

Why: the module docstring calls the home-holding-ONLY-auth.json the load-bearing hermeticism layer, but `_resolve_codex_home` never looks at anything except `auth.json`'s existence — a `config.toml` or `AGENTS.md` beside it rides into every verdict unrecorded. And `_resolve_codex_bin` checks `exists()` only, so `BAKEOFF_CODEX_BIN=/Applications/ChatGPT.app` (a directory — the exact mistake the constant's docstring anticipates) passes resolution and fails later as an unclassified per-unit `PermissionError` that never names the env var.

The home check refuses the two *named injection files*, not everything-but-auth.json: codex may leave its own harmless artifacts (logs, version stamps) in `CODEX_HOME`, and a strict allowlist would kill a healthy pass over a file codex itself wrote. The two files below are the ones whose injection the docstring documents.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_config_toml_or_agents_md_in_the_judge_home_is_refused(
    monkeypatch, tmp_path: Path, codex_bin: Path
):
    """The purpose-built home is the LOAD-BEARING hermeticism layer -- the
    flags are the second layer, and their AGENTS.md coverage is inference.
    A home that holds either injection file must be refused at resolution,
    the pruned-mirror lesson applied here: re-check the invariant against
    the artifact, not the setup story.
    """
    for name in ("config.toml", "AGENTS.md"):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
        (home / name).write_text("injected")
        complete = codex_completion(
            PINNED, codex_bin=str(codex_bin), codex_home=str(home)
        )

        with pytest.raises(CodexUnavailable, match=name):
            complete("prompt")


def test_a_directory_or_non_executable_binary_is_refused_with_the_env_var(
    monkeypatch, judge_home: Path, tmp_path: Path
):
    """exists() passed a directory (the ChatGPT.app path itself -- the
    documented likely mistake) and a non-executable file; both then failed
    per unit as a raw OSError that named neither the binary nor
    BAKEOFF_CODEX_BIN, marching the breaker into an abort."""
    a_directory = tmp_path / "ChatGPT.app"
    a_directory.mkdir()
    not_executable = tmp_path / "codex-noexec"
    not_executable.write_text("#!/bin/sh\n")
    not_executable.chmod(0o644)

    for wrong in (a_directory, not_executable):
        complete = codex_completion(
            PINNED, codex_bin=str(wrong), codex_home=str(judge_home)
        )
        with pytest.raises(CodexUnavailable,
                           match=codex_judge.CODEX_BIN_ENV):
            complete("prompt")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_codex_judge.py -k "config_toml_or_agents or non_executable" -q`
Expected: FAIL (no refusal today; the binary test dies later with `PermissionError`/`IsADirectoryError`).

- [ ] **Step 3: Implement**

In `_resolve_codex_home`, after the `auth.json` existence check:

```python
    home = Path(resolved).expanduser()
    # The docstring above calls "holds ONLY auth.json" the load-bearing
    # layer, so it is re-checked against the directory itself rather than
    # trusted from the setup story -- the pruned-mirror rule. Two named
    # files rather than an allowlist: codex leaves harmless artifacts of
    # its own in CODEX_HOME, and refusing one of those mid-pass would kill
    # a healthy batch over nothing.
    for name in ("config.toml", "AGENTS.md"):
        if (home / name).exists():
            raise CodexUnavailable(
                f"{resolved!r} holds {name} beside auth.json: that file "
                "would ride into every verdict unrecorded. The judge home "
                "must hold ONLY auth.json -- move or delete it, or point "
                f"{CODEX_HOME_ENV} at a home created solely by "
                f"`{CODEX_HOME_ENV}=<dir> ... codex login`"
            )
    return str(home)
```

In `_resolve_codex_bin`:

```python
    path = Path(resolved)
    if not (path.is_file() and os.access(resolved, os.X_OK)):
        raise CodexUnavailable(
            f"no runnable codex binary at {resolved!r} (missing, a "
            f"directory, or not executable): set {CODEX_BIN_ENV} to the "
            "CLI itself -- it ships INSIDE the ChatGPT app at "
            f"{CODEX_BIN_DEFAULT!r}, and the app bundle's own path is the "
            "usual wrong answer"
        )
    return resolved
```

(This replaces the `exists()` check; a missing file fails `is_file()` and keeps the same exception class and env-var mention, so `test_a_missing_binary_refuses_and_names_the_environment_variable` stays green.)

- [ ] **Step 4: Run the module suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_codex_judge.py tests/test_judge_script.py -q`
Expected: PASS (the `_codex_home`/`judge_home` fixtures hold only `auth.json`, so nothing else trips the new check).

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/codex_judge.py bakeoff/tests/test_codex_judge.py
git commit -m "fix: resolution re-checks the invariants it documents

The judge home's ONLY-auth.json property was claimed load-bearing and
checked nowhere; the binary check accepted a directory or a
non-executable file and failed later as an OSError naming neither the
path nor the env var.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: the window counts running futures, bounded by uncommitted ones

**Files:**
- Modify: `bakeoff/scripts/judge.py` — `_walk_concurrently._top_up` (:2340-2352) and the walk's docstring price paragraph (:2311-2315)
- Test: `bakeoff/tests/test_judge_script.py`

**Interfaces:** none beyond the walk's internals.

Why: `in_flight` counts every enqueued future — done or running — against `width`, so when the head unit is slow, finished workers idle and effective parallelism collapses toward 1 for the duration of every slow unit (codex latencies legitimately span 30 s–20 min; on a 9,600-call pass that is roughly 2–3× the ideal wall clock). The fix: count only not-done futures against the worker budget, and cap total uncommitted paid entries at `2 * width` so the abort-time discard stays O(width) — it moves from `width - 1` to at most `2 * width - 1`, which the docstring must say.

- [ ] **Step 1: Write the failing test**

```python
def test_a_slow_head_unit_does_not_idle_the_rest_of_the_window(tmp_path):
    """`in_flight` counted done-but-uncommitted futures against the width,
    so one slow head unit idled every other worker until it committed --
    on real codex latencies (30s..20min) that is serial execution wearing
    a --concurrency flag. The window must keep submitting while the head
    runs, bounded at 2*width uncommitted so the abort-time discard stays
    O(width).
    """
    root = _four_vote_collection(tmp_path)
    head_may_finish = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    class _BlockingHead(FakeComplete):
        def __call__(self, prompt: str) -> str:
            with lock:
                started.append(prompt)
                first = len(started) == 1
            if first:
                assert head_may_finish.wait(timeout=10.0), \
                    "the walk never released the head unit"
            return super().__call__(prompt)

    seam = _BlockingHead()
    walked: list = []
    walker = threading.Thread(
        target=lambda: walked.append(
            _run(root, complete=seam, rubric=False, concurrency=2)
        )
    )
    walker.start()
    try:
        # Four paid units, width 2. With the head blocked, the old window
        # stalled at 2 submissions; the fixed window keeps going to the
        # uncommitted cap (2 * width = 4).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with lock:
                if len(started) >= 4:
                    break
            time.sleep(0.02)
        with lock:
            assert len(started) >= 3, (
                f"only {len(started)} calls started behind a blocked head: "
                "the window is still counting finished futures as in flight"
            )
    finally:
        head_may_finish.set()
        walker.join(timeout=30.0)
    assert walked and walked[0]["errors"] == []
    # Commit order is still the worklist's, whatever order the race ran.
    assert [ln.vote_index for ln in _lines(root)] == [0, 1, 0, 1]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_judge_script.py -k "slow_head" -q`
Expected: FAIL — `started` stalls at 2.

- [ ] **Step 3: Implement** — replace `_top_up`'s body:

```python
            def _top_up() -> None:
                nonlocal cursor
                # RUNNING futures gate the worker budget; ALL uncommitted
                # paid entries gate the buffer. Counting done futures
                # against the width -- the first version -- idled every
                # worker behind one slow head unit, which is serial
                # execution wearing a --concurrency flag. The buffer cap is
                # what keeps the abort-time discard O(width): at most
                # 2*width - 1 paid results can be uncommitted when the walk
                # stops.
                running = sum(
                    1 for _, future, _ in queue
                    if future is not None and not future.done()
                )
                buffered = sum(
                    1 for _, future, _ in queue if future is not None
                )
                while (cursor < len(worklist) and running < width
                       and buffered < 2 * width):
                    unit = worklist[cursor]
                    cursor += 1
                    call = _paid_call(unit)
                    clock: dict = {}
                    if call is None:
                        queue.append((unit, None, clock))
                        continue
                    queue.append(
                        (unit, pool.submit(_timed, clock, call), clock)
                    )
                    running += 1
                    buffered += 1
```

Update the walk docstring's price paragraph: "up to `width - 1`" becomes "up to `2 * width - 1` paid calls in flight or buffered", with one sentence on why the buffer exists (the head-of-line stall).

- [ ] **Step 4: Run the concurrency tests, then the full suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_judge_script.py -k "concurren or slow_head or breaker or storage" -q` then `.venv/bin/python -m pytest tests/ -q`
Expected: PASS — commit order, breaker order, and storage-failure tests unaffected (all keyed to the commit loop, which is untouched).

- [ ] **Step 5: Commit**

```bash
git add bakeoff/scripts/judge.py bakeoff/tests/test_judge_script.py
git commit -m "fix: the window budget counts running futures, not finished ones

One slow head unit idled the whole pool -- serial execution wearing a
--concurrency flag on exactly the latency profile the flag exists for.
The uncommitted buffer is capped at 2*width so the abort-time discard
stays O(width).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: the timeout path folds the usage it already captured

**Files:**
- Modify: `bakeoff/src/bakeoff/codex_judge.py` — `_run_codex` (:392-430, as amended by Task 2), `_attempt_once` (:895-930)
- Test: `bakeoff/tests/test_codex_judge.py`

**Interfaces:**
- `CodexCallFailed` raised by the timeout path now carries an `events: tuple[dict, ...]` attribute (default `()` via `getattr` at the reader — no `__init__` change).

Why: the timeout handler raises without constructing a `CodexRun`, so a `turn.completed` usage block already sitting in the recovered stdout is discarded — tokens that were reported as spent vanish from the total, with `calls_without_usage` silent (it counts only successful runs by design). "A spend figure must never be wrong in the low direction" is the module's own rule; this path is its one structural exception.

- [ ] **Step 1: Write the failing test**

```python
def test_a_timeout_still_folds_the_usage_the_stream_already_reported(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    """Tokens codex reported were spent whatever happened afterwards -- the
    rule that folds a completed-then-401 run. The timeout path raised
    without building a CodexRun, so a turn that completed and then wedged
    before exit dropped its reported spend from the total with every
    counter silent.
    """
    totals = new_usage_totals()
    timed_out = CodexCallFailed("codex exec exceeded 1s")
    timed_out.events = _events(_completed(700, 30))
    # Two timeouts: the transport retry burns its one retry, then fails.
    _install(monkeypatch, [timed_out, CodexCallFailed("codex exec exceeded 1s")])
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        usage_totals=totals,
    )

    with pytest.raises(CodexCallFailed):
        complete("prompt")

    assert totals["calls"] == 2
    assert totals["prompt_tokens"] == 700
    assert totals["completion_tokens"] == 30
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_codex_judge.py -k "timeout_still_folds" -q`
Expected: FAIL — `prompt_tokens == 0`.

- [ ] **Step 3: Implement**

(a) In `_run_codex`'s `TimeoutExpired` handler (post-Task-2 shape), attach the recovered stream's events to the exception it raises:

```python
            exc = CodexCallFailed(
                f"codex exec exceeded {timeout_s:.0f}s and its process group "
                f"was killed; stderr: {_tail(stderr)}"
            )
            # The recovered stdout can already hold a turn.completed block:
            # a turn that finished and then wedged before exit. Those tokens
            # were reported as spent, and a raise that dropped them made the
            # total wrong in the one direction a spend figure must never be.
            exc.events = parse_events(stdout)
            raise exc from None
```

(b) In `_attempt_once`, the timeout catch folds before continuing:

```python
            try:
                run = _run_codex(argv, prompt, env, timeout_s, output_file)
            except CodexCallFailed as exc:  # the timeout path
                _fold_usage(usage_totals, CodexRun(
                    # exit_code 124 marks a timed-out run as FAILED for
                    # `_fold_usage`'s outcome test, so the fold takes the
                    # tokens and never the calls_without_usage bump --
                    # a failed run has no usage to read, not usage that
                    # could not be read.
                    exit_code=124,
                    events=getattr(exc, "events", ()),
                    stderr="",
                    last_message="",
                ))
                last = exc
                continue
```

- [ ] **Step 4: Run the module suite, then the full suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_codex_judge.py -q` then `.venv/bin/python -m pytest tests/ -q`
Expected: PASS. (`_fold_usage` with empty `events` folds nothing and — because `failure_message` of an exit-124 run is non-None — bumps no counter, so the existing timeout tests are unaffected.)

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/codex_judge.py bakeoff/tests/test_codex_judge.py
git commit -m "fix: a timed-out call keeps the spend its stream already reported

The timeout path raised without a CodexRun, so a turn that completed and
then wedged dropped its reported tokens from the total -- wrong in the
one direction a spend figure must never be, with every counter silent.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: the attestation assert can fail

**Files:**
- Modify: `bakeoff/tests/test_integration_codex.py` (:100-109)

**Interfaces:** none.

Why: `assert harness["auth_seat"]` is vacuous — `codex_harness` falls back to the truthy `"unattested"`, so the paid smoke that exists as the pre-production gate passes green with `BAKEOFF_CODEX_SEAT` unset, and a production pass would permanently stamp `auth_seat: "unattested"` onto ~9,600 lines in the one field a residency audit reads.

- [ ] **Step 1: Change the assertion** (this is a paid `codex_live` test — it cannot run here; the edit is reviewed, not executed):

```python
    # Vacuous before: the fallback "unattested" is truthy, so the one test
    # gating the attestation passed with the variable unset and a
    # production pass would stamp auth_seat: "unattested" onto every line.
    from bakeoff.codex_judge import CODEX_SEAT_ENV, CODEX_SEAT_UNATTESTED

    assert harness["auth_seat"] != CODEX_SEAT_UNATTESTED, (
        f"set {CODEX_SEAT_ENV} before the paid smoke: auth_seat is the "
        "operator's attestation of WHOSE seat signs the pass, and it is "
        "about to be recorded verbatim on every production line"
    )
```

(Adjust the existing import block instead of a function-level import if the file style prefers; `CODEX_HOME_ENV` is already imported from `bakeoff.codex_judge` at the top — add the two names there.)

- [ ] **Step 2: Verify the offline suite still collects the module without running it**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_integration_codex.py --collect-only -q`
Expected: 2 tests collected, deselected in the default run.

- [ ] **Step 3: Commit**

```bash
git add bakeoff/tests/test_integration_codex.py
git commit -m "test: the attestation assert can now fail

assert harness['auth_seat'] was vacuous -- the 'unattested' fallback is
truthy -- so the pre-production gate passed with BAKEOFF_CODEX_SEAT
unset.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Deliberately NOT in this plan (skipped findings, adjudicated)

- **`turn.failed` trumping exit 0 + a written `-o` file** (review finding 9): changing that precedence contradicts a deliberate, documented design choice (`failure_message`'s asymmetry) on the strength of a speculated codex behavior nobody has observed. Revisit only if a live pass shows exit-0 + `turn.failed` + verdict actually occurring.
- **Concurrent 401s each minting a mantle token** (review finding 13): bounded waste (N−1 mints, once per ~1 h window) on the secondary backend; a single-flight refresh is a real design change to `live_completion`'s retry policy and deserves its own task with its own tests, not a rider here. Task 3's stop check already prevents the post-abort variant, which was the expensive half.

## Verification (after all tasks)

- `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — full offline suite green.
- `cd bakeoff && .venv/bin/python -m pytest tests/test_judge_script.py tests/test_codex_judge.py tests/test_judge.py -q` — the three touched suites green on their own.
- Re-report the review findings with outcomes (`fixed` for tasks 1–8's findings, `skipped` for the two above).
