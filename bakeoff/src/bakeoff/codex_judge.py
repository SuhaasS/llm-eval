"""The Codex-CLI judge backend: a `CompleteFn` over a hermetic `codex exec`.

See `docs/superpowers/plans/2026-08-20-codex-judge.md` (D1, D5, D6, D9, D10)
and `docs/superpowers/specs/2026-08-18-judge-design.md` §4.3.

This is a PEER of `judge.live_completion` and never a second agentic harness.
It answers exactly the same contract -- one rendered prompt in, the model's
raw text out -- so everything above the seam (payload whitelisting, prompts,
the two forced positions, the strict parsers, `majority`) is untouched and
cannot tell which backend answered.

**Its own module rather than a second function in `judge.py`,** for three
reasons that do not overlap. `judge.py` is AST-scanned by
`test_only_payload_inputs_from_touches_a_run_record_or_a_grade_record`, so
every addition there has to argue with a whitelist test that has nothing to do
with subprocesses. `judge.py` is imported by the payload builders and the leak
tests and must stay importable without paying for litellm; a codex backend has
the identical property with a different dependency, and `scripts/judge.py`
must not require the codex binary to exist for a mantle pass or a fully
gate-decided one. And this file handles ONLY strings, dicts and subprocesses --
it imports no record type, so it structurally cannot leak a `RunRecord` field
into a payload, which is a property a reviewer can check by reading the import
block rather than by trusting a test.

**Hermetic invocation is two layers, and the load-bearing one is not a flag.**

The first layer is a purpose-built `CODEX_HOME` holding ONLY `auth.json`. A
`config.toml` there would inject plugins, MCP servers, feature flags and a
shell-environment policy; an `AGENTS.md` there would inject the operator's own
standing instructions into every verdict. An absent file cannot be injected
regardless of what any flag means this release, which is why the home is
REFUSED rather than defaulted (`_resolve_codex_home`): defaulting to
`~/.codex` would silently judge on whatever personal account and whatever
instruction file happen to be on the operator's machine, and the resulting
table would look exactly like a clean one.

The second layer is the flag set in `build_codex_argv`, and it is real rather
than decorative. Measured 2026-08-20 against `codex-cli 0.145.0-alpha.18`, ONE
prompt asked twice: 18,042 input tokens with the user config loaded, 14,606
with `--ignore-user-config`. The flag removes ~3.4k tokens of it.

What those 3.4k ARE is inference and not measurement. The operator's
`AGENTS.md` and the tool schemas their `config.toml` pulls in are the two
candidates, and one aggregate delta cannot separate them -- which matters
because the purpose-built home above is what actually closes the question,
and a decomposition asserted as fact would make the flag look load-bearing
when it is the second layer.

The model's own answer to "were you told about X" was UNRELIABLE in the same
experiment: it said yes at both token counts. The token count is the evidence
and the self-report is not.

**~14.6k tokens of Codex scaffolding ride on every call, and nothing hashes
them.** That floor is the agent system prompt and tool schemas Codex wraps
around the rendered judge prompt; it is present at every setting above and
cannot be turned off. Two consequences the record has to carry rather than
hide. `judge_prompt_sha` attests the USER TURN only on this backend, so the
compensating identity is `judge_harness.codex_cli_version` -- see `judge.py`'s
`judge_prompt_sha` docstring. And the judge is answering inside a
CODING-AGENT frame rather than a bare completion, which is a real difference
from the mantle path and is why the two backends are separate judge
generations (`codex:` ids can never equal mantle ids) rather than two routes to
one number.

**Failure classification reads a STRUCTURED event, not a string marker.**
Codex ends a failed turn with `{"type": "turn.failed", "error": {"message":
...}}` and that message carries the HTTP status verbatim ("unexpected status
401 Unauthorized: ..."). Classifying on the status is why this module does not
depend on error prose that a codex release can reword. Measured against a
logged-out `CODEX_HOME` on 2026-08-20: exit 1, five websocket retries, a
transport fallback to HTTPS, five more, then the `turn.failed` above.

The three classes are kept apart because they need opposite responses, which
is `judge.is_auth_failure`'s reasoning applied to a different transport:

* **401/403 is auth and is NEVER retried here.** Unlike the mantle bearer,
  nothing is mintable -- `codex login` is a human act in a browser -- so a
  retry could only burn the breaker faster while changing nothing. It carries
  `status_code = 401` so `judge.is_auth_failure` recognises it without being
  edited, which keeps ONE classifier answering for both backends.
* **429 is rate limiting and IS retried, with backoff, inside the backend.**
  A seat's usage window is an expected recurring condition on a 9,600-call
  pass rather than an error, and letting it reach the driver would abort a
  batch that needed to wait. It must never be classified as auth: the abort
  message would then send the operator to re-login over a wait.
* **Everything else is one retry and then a per-unit failure.** Codex already
  retries transport internally (measured: 5 websocket + 5 HTTPS attempts), so
  a second layer here is deliberately thin.

An EMPTY reply is not a failure: it returns `""` so the strict parser upstream
raises `MalformedVerdict` and `_ask_and_parse` re-asks the identical prompt.
That mirrors `judge._completion`'s `content or ""` exactly, and it keeps the
malformed-verdict budget and the transport budget as two separate counters.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bakeoff.judge import USAGE_LOCK, CompleteFn

#: Where the Codex CLI ships on a Mac that installed the ChatGPT app. NOT on
#: PATH, which is why this is a constant rather than a bare `"codex"`: a
#: `FileNotFoundError` three frames into a batch is a worse way to learn the
#: binary moved than a refusal that names the path it looked at.
CODEX_BIN_DEFAULT = "/Applications/ChatGPT.app/Contents/Resources/codex"

CODEX_BIN_ENV = "BAKEOFF_CODEX_BIN"

#: REQUIRED, never defaulted -- see the module docstring. The variable is what
#: makes the auth source a deliberate act rather than whatever was lying in
#: `~/.codex`.
CODEX_HOME_ENV = "BAKEOFF_CODEX_HOME"

#: Operator attestation, carried into `judge_harness` verbatim. A string the
#: operator sets rather than anything read off a token: `auth.json` proves a
#: session, not which organisation's seat granted it, and a field that guessed
#: would be a false record in the one place a residency question gets audited.
CODEX_SEAT_ENV = "BAKEOFF_CODEX_SEAT"
CODEX_SEAT_UNATTESTED = "unattested"

#: The namespace that selects this backend. `judge_model_id` is the identity
#: every resume key and every generation partition already carries, so a
#: `codex:` id cannot collide with a mantle id and the two backends can never
#: be pooled into one number by accident. That is why there is no
#: `--judge-backend` flag: a flag and an id can disagree, and this cannot.
CODEX_MODEL_PREFIX = "codex:"

#: One call's ceiling. Generous because a judge prompt carries two full diffs
#: and the model reasons over them; the thing it is against is a hung process
#: holding a worker slot for the rest of a nine-hour pass, not a slow verdict.
CODEX_CALL_TIMEOUT_S = 1200.0

#: Rate-limit retries INSIDE the backend, with the wait between them. A seat's
#: window is minutes-to-hours, so the backoff is coarse on purpose -- a tight
#: loop against a usage limit buys nothing and looks like a hot spin in the
#: progress output.
CODEX_RATE_LIMIT_RETRIES = 5
CODEX_RATE_LIMIT_BASE_S = 30.0
CODEX_RATE_LIMIT_CAP_S = 480.0

#: The sandbox the judge runs under, recorded into `judge_harness`. Read-only
#: rather than `danger-full-access`: the scratch dir is empty and the judge has
#: nothing to do with a filesystem, so anything the agent layer tries to read
#: is a sign the prompt was misunderstood rather than a capability to grant.
CODEX_SANDBOX = "read-only"

LAST_MESSAGE_NAME = "last_message.txt"

#: Cooperative shutdown, as a per-closure `threading.Event` the CALLER owns
#: and passes in -- deliberately not module state.
#:
#: It exists because `ThreadPoolExecutor.shutdown(cancel_futures=True)` can
#: only cancel calls that have not STARTED. A worker already inside the
#: rate-limit ladder would otherwise wake from a backoff after the summary has
#: printed and start a fresh `codex exec` -- real money spent on a verdict
#: nobody will commit, and spent AFTER the pass reported what it had spent, so
#: the printed `calls` is short by exactly those spawns. "Nothing further will
#: be written" was already true; this is what makes "nothing further will be
#: spent" true as well.
#:
#: A MODULE-LEVEL EVENT WAS THE FIRST ATTEMPT AND IT WAS WRONG, in a way worth
#: recording because it looked simpler. A process-global flag has to be
#: re-armed, only one code path did the re-arming, and the flag was set
#: unconditionally -- so a process that ran any concurrent batch and then a
#: second one would have every paid unit of the second killed by a stop
#: request the first pass made, straight into a breaker abort. And re-arming
#: it would revive the first batch's still-running workers, which then spend
#: against totals that were already reported. Both disappear when the event's
#: lifetime is the closure's: a new batch builds a new closure, so there is
#: nothing to re-arm and nothing to revive.
#:
#: `CompleteFn` is untouched by this -- the seam is still one string in and
#: one string out, and the event is a construction-time argument that the
#: driver keeps its own reference to.

#: HTTP statuses that mean the CREDENTIAL rather than the request, spelled the
#: same way `judge._AUTH_STATUS` spells them so the two backends agree about
#: what auth means.
_AUTH_STATUS = (401, 403)
_RATE_LIMIT_STATUS = 429

#: `unexpected status 401 Unauthorized: ...` is the observed shape. Anchored on
#: the word rather than scanning for any three digits: a request id or a
#: cf-ray in the same message holds digits too, and a classifier that matched
#: one of those would answer a credential question with a random number.
_STATUS_RE = re.compile(r"status\s+(\d{3})\b", re.IGNORECASE)


class CodexUnavailable(RuntimeError):
    """The backend cannot be built: no binary, no home, or no `auth.json`.

    Raised on the FIRST PROMPT rather than at construction, because the
    closure is built lazily for a batch that may be entirely gate-decided --
    see `scripts/judge.lazy_codex_completion`. A batch that never asks the
    judge anything must not require a credential to exist.
    """


class CodexAuthFailure(RuntimeError):
    """A 401/403 from the seat. `status_code` is what makes it recognisable.

    Carries the attribute rather than relying on its class name so that
    `judge.is_auth_failure` -- the ONE classifier, read by both the backend
    retry policy and the driver's abort-message chooser -- answers True for it
    with no edit. Two narrowness rules drifting apart is how the terminal and
    the abort message stop agreeing about the same exception.
    """

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class CodexRateLimited(RuntimeError):
    """The seat's usage window closed, and the backoff budget ran out.

    Its OWN class and deliberately NOT an auth failure. `status_code` is 429
    so that `judge.is_auth_failure` answers False -- the abort message must
    say "wait" and not "log in again", and on a 9,600-call pass that is the
    difference between a resume and a wild goose chase.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.status_code = _RATE_LIMIT_STATUS


class CodexCallFailed(RuntimeError):
    """Any other non-zero exit, timeout, or failed turn. Per-unit."""


@dataclass(frozen=True)
class CodexRun:
    """One `codex exec` invocation, as it ended.

    `last_message` is the `-o` file's contents and is `""` when the file is
    absent or empty -- which is a real outcome (an empty completion) rather
    than an error, and is handed upstream as `""` for the strict parser to
    refuse. `events` is the parsed `--json` stream; `stderr` is kept because
    a failure that produced no parsable event at all still has to say
    something an operator can act on.
    """

    exit_code: int
    events: tuple[dict[str, Any], ...]
    stderr: str
    last_message: str


# --- identity ----------------------------------------------------------------


def is_codex_judge(judge_model_id: str) -> bool:
    """Whether this id selects the codex backend. See `CODEX_MODEL_PREFIX`."""
    return judge_model_id.startswith(CODEX_MODEL_PREFIX)


def codex_model_name(judge_model_id: str) -> str:
    """The `-m` value: the pinned id with its namespace stripped.

    The namespace stays on `judge_model_id` and is stripped only here, at the
    wire. That split is the whole of D2: the RECORD carries `codex:gpt-5.2` so
    no aggregation can pool it with a mantle line, while the CLI is handed the
    model name it actually knows. A backend that stored the stripped name
    would make the two ids collide on the day someone judges with a mantle
    model of the same name.
    """
    if not is_codex_judge(judge_model_id):
        raise ValueError(
            f"{judge_model_id!r} is not a codex judge id: the codex backend is "
            f"selected by the {CODEX_MODEL_PREFIX!r} namespace"
        )
    model = judge_model_id[len(CODEX_MODEL_PREFIX):]
    if not model:
        raise ValueError(
            f"{judge_model_id!r} names no model after {CODEX_MODEL_PREFIX!r}: "
            "the id must be PINNED, never an alias and never a bare namespace"
        )
    return model


# --- invocation --------------------------------------------------------------


def build_codex_argv(
    codex_bin: str,
    model: str,
    scratch_dir: Path,
    output_file: Path,
    reasoning_effort: str | None = None,
) -> list[str]:
    """The hermetic argv. PURE, so the flag set is testable without spawning.

    Every flag is here for a named failure, and the set is asserted whole by
    `test_the_argv_carries_every_hermetic_flag_and_reads_the_prompt_from_stdin`
    -- one dropped flag is a contamination nothing downstream can see.

    * `--ignore-user-config` drops `config.toml`: plugins, MCP servers,
      feature flags and the shell-environment policy. Measured worth ~3.4k
      tokens of injected instructions on this machine (module docstring).
    * `--ignore-rules` drops execpolicy `.rules`.
    * `--skip-git-repo-check` and `-C <empty scratch>` together mean there is
      no repository and no project `AGENTS.md` to discover. The scratch dir is
      fresh per call, so nothing one verdict leaves behind can reach the next.
    * `--ephemeral` writes no session file. A judge pass is 9,600 calls; the
      session store is not a place to put them, and a resumed session is the
      shared context the `CompleteFn` contract forbids.
    * `-s read-only` bounds anything the agent layer tries anyway.
    * `--json` is the only way to see `turn.completed`'s usage block and
      `turn.failed`'s status; `-o` is the clean final message, which beats
      re-deriving it from the event stream.
    * `--color never` keeps ANSI escapes out of a reply the parser will scan
      for a balanced JSON object.
    * A trailing `-` puts the PROMPT ON STDIN. Not argv: a pairwise prompt
      carries two full diffs and would hit `ARG_MAX` on a large task, which
      fails as a confusing `OSError` rather than as anything about diffs.

    `reasoning_effort` is passed through `-c` only when set, and it is the
    ONLY sampling knob this backend has -- there is no temperature on `codex
    exec`. Whatever is sent here is what `judge_sampling` records; see D7.
    """
    argv = [
        codex_bin,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s",
        CODEX_SANDBOX,
        "-C",
        str(scratch_dir),
        "-m",
        model,
    ]
    if reasoning_effort:
        argv += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    argv += ["--json", "-o", str(output_file), "--color", "never", "-"]
    return argv


def codex_environment(
    codex_home: str, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    """The subprocess environment: the judge home, and no ambient API key.

    `OPENAI_API_KEY` is POPPED rather than merely not set. A key exported in
    the operator's shell would flip the call off the attested seat and onto an
    account nobody recorded, and the verdicts would be complete, well formed
    and wrong about their own provenance -- exactly the shape of
    `_judge_router`'s `AWS_BEARER_TOKEN_BEDROCK` scrub (`judge.py:1671`), for
    the same reason: the process did not set the variable, so the process must
    not silently inherit its authority.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["CODEX_HOME"] = codex_home
    env.pop("OPENAI_API_KEY", None)
    return env


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


def _run_codex(
    argv: list[str],
    prompt: str,
    env: dict[str, str],
    timeout_s: float,
    output_file: Path,
) -> CodexRun:
    """THE spawn. The one function in this module that starts a process.

    Monkeypatched by every unit test that exercises a verdict, which is why the
    classification, the retry policy and the usage folding all live above it
    and take a `CodexRun`. The two that drive it for real script a shell binary
    instead, because what it guarantees is about the PROCESS and a fake of it
    cannot be wrong in the ways this function can.

    `start_new_session=True` puts the child in its own process group, which is
    what lets an escape kill the GROUP (`_kill_group`) -- and is equally why it
    must: the child is detached from the terminal, so the SIGINT an operator's
    Ctrl-C sends the foreground group never reaches it. EVERY escape from
    `communicate` kills, not only the timeout. A KeyboardInterrupt used to
    leave a detached `codex exec` billing the seat with nothing left enforcing
    the timeout, because `Popen.__exit__` assumes the SIGINT was delivered and
    waits a quarter of a second on that assumption.
    """
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
                # BOUNDED. `process.kill()` is the fallback path, and it kills
                # only the leader -- a codex helper that survives holding the
                # pipe would keep an unbounded `communicate()` waiting
                # forever, on a worker thread the interpreter joins at exit.
                # That is the one way this module can hang a process rather
                # than merely outlive its report.
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

    return CodexRun(
        exit_code=exit_code,
        events=parse_events(stdout),
        stderr=stderr,
        last_message=_read_last_message(output_file),
    )


def _read_last_message(output_file: Path) -> str:
    """The `-o` file, or `""`.

    A missing file is NOT an error here. It is what a turn that produced no
    agent message leaves behind, and `""` is exactly what the strict parser
    upstream needs to see so it raises `MalformedVerdict` and re-asks -- the
    same handling `judge._completion` gives a `None` content.
    """
    try:
        return output_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_events(stdout: str) -> tuple[dict[str, Any], ...]:
    """The `--json` stream, one object per line, unparsable lines DROPPED.

    Tolerant on purpose, and `failure_message` is what makes the tolerance
    safe: success is the exit code rather than any event, so a line this
    version of codex emits in a shape this parser has never seen costs a
    token count or an error message and never a paid verdict.

    Dropping a `turn.completed` on a run that still exited 0 leaves
    `calls_without_usage` saying the total is short -- `_fold_usage` keys on
    the run's OUTCOME, so a successful run whose usage cannot be read is
    counted rather than silently skipped. Dropping a `turn.failed` leaves the
    exit code and stderr to say the run failed, without the status that would
    have classified it.
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return tuple(events)


# --- classification ----------------------------------------------------------


def _status_in(text: str) -> int | None:
    match = _STATUS_RE.search(text or "")
    return int(match.group(1)) if match else None


def failure_message(run: CodexRun) -> str | None:
    """Why this run failed, or `None` when it did not.

    TWO SIGNALS, and their asymmetry is deliberate. A `turn.failed` event is
    taken as authoritative because it is the only place the HTTP status is
    spelled out, and the status is what tells auth from rate limiting from
    everything else. Success, though, is the EXIT CODE plus the absence of
    that event -- NOT the presence of `turn.completed`.

    That asymmetry is the whole content of this function. Requiring
    `turn.completed` would make the harness depend on a diagnostic event's
    NAME: a codex release that renamed or reshaped it would turn every
    successful call -- `-o` file written, verdict inside it, exit 0 -- into a
    failure, which is then retried (a second paid spawn) and finally recorded
    as a per-unit error. A paid verdict thrown away because the telemetry
    moved is the most expensive way this module can be wrong, and it would
    happen to every unit at once. The `-o` file is the authority on the
    answer; the event stream is the authority only on the failure.

    An exit-0 run whose `-o` file is empty is therefore NOT a failure here.
    It returns `""` upstream, the strict parser refuses it as a
    `MalformedVerdict`, and `_ask_and_parse` re-asks the identical prompt --
    which is the right answer to an empty completion and is what the mantle
    path does with a `None` content.

    Falls back to stderr when no event survived parsing, so a codex that died
    before emitting anything still produces a message an operator can read.
    """
    failed = [event for event in run.events if event.get("type") == "turn.failed"]
    if failed:
        error = failed[-1].get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return str(message) if message else "codex reported a failed turn"

    if run.exit_code == 0:
        return None
    return (
        f"codex exec exited {run.exit_code} and reported no failed turn; "
        f"stderr: {_tail(run.stderr)}"
    )


def classify_failure(message: str) -> RuntimeError:
    """One failure message -> the exception class that names what to do next.

    The status is read from the message because codex puts it there verbatim
    ("unexpected status 401 Unauthorized: ..."), which is a far more durable
    signal than the surrounding prose. When no status can be found the answer
    is the GENERIC class, deliberately: a false `CodexCallFailed` costs the
    operator a confused minute reading the data paragraph, while a false
    `CodexAuthFailure` sends them to re-login over a transient and, worse,
    tells the abort message to print the one paragraph that cannot help.
    """
    status = _status_in(message)
    if status in _AUTH_STATUS:
        return CodexAuthFailure(
            f"the codex seat rejected the call ({status}): {message}",
            status_code=status,
        )
    if status == _RATE_LIMIT_STATUS:
        return CodexRateLimited(f"the codex seat is rate limited: {message}")
    return CodexCallFailed(message)


# --- usage -------------------------------------------------------------------

#: `turn.completed`'s usage block as codex spells it, measured 2026-08-20
#: against `codex-cli 0.145.0-alpha.18`:
#: `{"input_tokens", "cached_input_tokens", "cache_write_input_tokens",
#:   "output_tokens", "reasoning_output_tokens"}`.
_CODEX_INPUT_FIELD = "input_tokens"
_CODEX_OUTPUT_FIELD = "output_tokens"


def usage_from_events(events: Iterable[dict[str, Any]]) -> dict[str, int] | None:
    """`turn.completed`'s token counts, mapped onto `new_usage_totals`' keys.

    `None` when no readable block exists, which the caller turns into
    `calls_without_usage` -- the counter that tells an operator their total is
    an under-count rather than a measurement.

    **`total_tokens` is DERIVED here, and that is a departure worth naming.**
    `judge._add_usage` refuses to sum the halves because the mantle endpoint
    REPORTS a total, and a reported total its own parts do not add up to is
    reporting something (reasoning, a cached prefix) that a driver-side
    addition would discard. Codex reports no total at all, so there is nothing
    to discard and nothing to contradict: the sum is the only number available
    and it is labelled as a derivation rather than passed off as a reading.

    `reasoning_output_tokens` is NOT added to `output_tokens`. In the Responses
    API reasoning tokens are a BREAKDOWN of the output tokens rather than a
    sibling of them, and codex flattens that nesting into one block. The check
    that would falsify this: a high-effort call whose `reasoning_output_tokens`
    exceeds its `output_tokens` would prove the two disjoint, and the mapping
    here would then be under-counting completion tokens by the reasoning half.
    """
    for event in reversed(tuple(events)):
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt_tokens = _int_or_none(usage.get(_CODEX_INPUT_FIELD))
        completion_tokens = _int_or_none(usage.get(_CODEX_OUTPUT_FIELD))
        if prompt_tokens is None or completion_tokens is None:
            return None
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return None


def _int_or_none(value: Any) -> int | None:
    """`bool` refused alongside non-ints: `True` in a token slot would add 1
    and read as a measurement -- `judge._add_usage`'s rule, same reasoning."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _fold_usage(usage_totals: dict[str, int] | None, run: CodexRun) -> None:
    """One run's counts into the caller's dict, under the shared lock.

    THE LOCK IS NOT OPTIONAL. `--concurrency` puts these calls on worker
    threads and `d[k] += v` is a read, an add and a store -- three bytecodes
    the interpreter may switch between -- so an unlocked accumulator silently
    under-reports spend on exactly the passes big enough to need concurrency.
    It is `judge.USAGE_LOCK` and not a private one so that both backends
    serialise against the same object.
    """
    if usage_totals is None:
        return

    counts = usage_from_events(run.events)

    # TWO DECISIONS, and they key on different things -- which is the whole
    # correctness of this function, and the thing two earlier versions each
    # got half of.
    #
    # WHETHER TO FOLD keys on the counts alone. Tokens codex reported were
    # spent, and they were spent whatever the run did afterwards: a turn that
    # completed and then failed on a 401 still burned its input. A spend
    # figure must never be wrong in the low direction -- the same rule that
    # counts `calls` before the wire.
    #
    # WHETHER TO RAISE `calls_without_usage` keys on the run's OUTCOME. That
    # counter says one thing only: the token TOTAL is an under-count. A failed
    # run has no usage to READ rather than usage that could not be read, so
    # counting it would bury the signal under every auth failure and every
    # dead endpoint. But a SUCCESSFUL run whose counts are unreadable is
    # exactly what it is for -- and keying that on `turn.completed` being
    # present, as an earlier version did, breaks in the precise scenario
    # `failure_message` exists to survive: once success is the exit code, a
    # codex release that renames that event leaves every successful call
    # folding no tokens AND raising no counter, so the pass reports `calls=N`
    # beside a zero total with the under-count warning suppressed.
    with USAGE_LOCK:
        if counts is not None:
            for field, value in counts.items():
                usage_totals[field] += value
        elif failure_message(run) is None:
            usage_totals["calls_without_usage"] += 1


def _count_call(usage_totals: dict[str, int] | None) -> None:
    """Counted BEFORE the spawn, for `judge._completion`'s reason: an attempt
    that reached the wire may well have been billed, and a total that drops it
    is wrong in the one direction a spend figure must never be wrong in."""
    if usage_totals is None:
        return
    with USAGE_LOCK:
        usage_totals["calls"] += 1


# --- resolution --------------------------------------------------------------


def _resolve_codex_bin(codex_bin: str | None) -> str:
    resolved = codex_bin or os.environ.get(CODEX_BIN_ENV) or CODEX_BIN_DEFAULT
    if not Path(resolved).exists():
        raise CodexUnavailable(
            f"no codex binary at {resolved!r}: set {CODEX_BIN_ENV} to the "
            "path of the Codex CLI (it ships inside the ChatGPT app and is "
            "not on PATH)"
        )
    return resolved


def _resolve_codex_home(codex_home: str | None) -> str:
    """The judge's `CODEX_HOME`. REFUSED when unset -- never defaulted.

    Defaulting to `~/.codex` is the quiet failure this guard exists for: that
    directory holds the operator's personal session, their `config.toml` and
    their `AGENTS.md`, so a pass that fell back to it would judge on an
    unattested account with the operator's own standing instructions folded
    into every verdict, and would produce a complete table saying none of it.
    """
    resolved = codex_home or os.environ.get(CODEX_HOME_ENV)
    if not resolved:
        raise CodexUnavailable(
            f"no codex judge home: set {CODEX_HOME_ENV} to a directory holding "
            "ONLY auth.json for the attested seat, created with "
            f"`{CODEX_HOME_ENV}=<dir> ... codex login`. It is never defaulted "
            "to ~/.codex, which carries a personal session, a config.toml and "
            "an AGENTS.md -- all three would ride into every verdict unrecorded"
        )
    if not (Path(resolved).expanduser() / "auth.json").exists():
        raise CodexUnavailable(
            f"{resolved!r} holds no auth.json: run "
            f"`CODEX_HOME={resolved} <codex> login` and confirm with "
            f"`CODEX_HOME={resolved} <codex> login status` before judging"
        )
    return str(Path(resolved).expanduser())


def codex_cli_version(codex_bin: str) -> str:
    """`codex --version`, or a marker string. Recorded, never asserted on.

    This is the ONLY identity the record can carry for the ~14.6k tokens of
    agent scaffolding Codex wraps around every prompt (module docstring), so a
    version that could not be read is stored as such rather than omitted: an
    absent field reads as "mantle-era", and this line is not that.
    """
    try:
        result = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            # `encoding=` implies text mode, and `errors="replace"` is what
            # keeps a banner byte the locale codec cannot read from raising a
            # UnicodeDecodeError past the `SubprocessError` guard below --
            # losing the whole harness record over a decoration in a version
            # string that is recorded and never asserted on.
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _auth_mode(codex_home: str) -> str:
    """`auth.json`'s `auth_mode`, and NOTHING else from that file.

    One named key, copied by name. The file also holds tokens, and a helper
    that returned the parsed document would put them one careless log line
    away from a record that is written to disk and read by other people.
    """
    try:
        with open(Path(codex_home) / "auth.json", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return "unknown"
    mode = document.get("auth_mode") if isinstance(document, dict) else None
    return str(mode) if isinstance(mode, str) else "unknown"


def codex_harness(
    judge_model_id: str,
    *,
    codex_bin: str | None = None,
    codex_home: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """The `judge_harness` block for a codex line. See D7.

    Every field answers a question the mantle record answered implicitly and
    this one cannot: which agent wrapper was around the prompt, which model
    the CLI resolved, whose credential signed it, and under what sandbox.
    `auth_seat` is OPERATOR-ATTESTED (`BAKEOFF_CODEX_SEAT`) rather than read
    off a token, because `auth.json` proves a session and not which
    organisation granted it -- and a guessed seat is a false answer in the one
    field a residency audit would read.
    """
    return {
        "backend": "codex-cli",
        "codex_cli_version": codex_cli_version(_resolve_codex_bin(codex_bin)),
        "codex_model": codex_model_name(judge_model_id),
        "auth_mode": _auth_mode(_resolve_codex_home(codex_home)),
        "auth_seat": os.environ.get(CODEX_SEAT_ENV) or CODEX_SEAT_UNATTESTED,
        "sandbox": CODEX_SANDBOX,
        "reasoning_effort": reasoning_effort,
    }


def codex_sampling(reasoning_effort: str | None) -> dict[str, Any]:
    """`judge_sampling` for a codex line: ONLY what was actually sent.

    Never `temperature`. `codex exec` has no temperature flag and the models
    behind it do not take one, so a block carrying `temperature: 0.0` would be
    a false record in the field whose entire job is to say what went out --
    and it would make a codex verdict look reproducible in a way it is not.
    The spec amendment carries the consequence for the vote protocol.
    """
    return {"model_reasoning_effort": reasoning_effort} if reasoning_effort else {}


# --- the seam ----------------------------------------------------------------


def codex_completion(
    judge_model_id: str,
    usage_totals: dict[str, int] | None = None,
    *,
    codex_bin: str | None = None,
    codex_home: str | None = None,
    reasoning_effort: str | None = None,
    timeout_s: float = CODEX_CALL_TIMEOUT_S,
    sleep: Any = time.sleep,
    stop: threading.Event | None = None,
) -> CompleteFn:
    """The live codex `CompleteFn`: rendered prompt in, raw model text out.

    Resolution happens on the FIRST PROMPT, not at construction -- the
    `lazy_live_completion` pattern one layer in. A batch every comparison of
    which is decided by the deterministic ladder must not require a codex
    binary, a judge home or a login to exist at all.

    Stateless per call, which is the `CompleteFn` contract and here it is
    structural rather than promised: every call is its own process, with
    `--ephemeral` so no session is written and a fresh scratch dir so nothing
    one verdict leaves behind can reach the next. The two votes over one
    comparison cannot share context even when they run concurrently.

    `sleep` is injected so the backoff is testable without a test suite that
    waits minutes.

    `stop` is the caller's own `threading.Event` -- see the note above the
    constants. When it is set, no further call is SPAWNED, no further backoff
    is entered, and a backoff ALREADY IN PROGRESS is woken: with `time.sleep`
    the default wait becomes `stop.wait`, because `sleep` cannot be
    interrupted and a worker inside the capped 480s wait would otherwise hold
    the interpreter (`concurrent.futures` registers an atexit join) for up to
    eight minutes after the report is on the terminal. Calls already on the
    wire run to their timeout, which is the part a cooperative signal cannot
    reach. `None` means this closure never stops early, which is right for a
    caller that makes one call and waits for it.
    """
    resolved: dict[str, str] = {}
    # A LOCK AROUND THE RESOLUTION, not a bare `if not resolved`. Under
    # `--concurrency` the first prompts of a batch arrive together, and the
    # unguarded version fills the dict key by key: a second thread sees a
    # non-empty dict, decides the work is done, and reads `resolved["home"]`
    # one statement before it exists. That unit dies on a `KeyError: 'home'`
    # -- a nonsense error that charges the breaker -- while nothing about the
    # seat or the collection is actually wrong.
    #
    # The lock covers ONLY the fill. Holding it across the call would
    # serialise every paid call in the batch onto one thread, which is the
    # whole of what `--concurrency` buys.
    resolve_lock = threading.Lock()
    stopped = (lambda: False) if stop is None else stop.is_set
    if sleep is time.sleep and stop is not None:
        # The default wait is the EVENT's, so a stop set mid-backoff wakes
        # the worker instead of sleeping out up to 480s after the summary.
        # An injected sleep wins -- tests inject one to observe the waits.
        sleep = stop.wait

    def complete(prompt: str) -> str:
        with resolve_lock:
            if not resolved:
                resolved.update(
                    bin=_resolve_codex_bin(codex_bin),
                    home=_resolve_codex_home(codex_home),
                    model=codex_model_name(judge_model_id),
                )

        env = codex_environment(resolved["home"])

        for attempt in range(CODEX_RATE_LIMIT_RETRIES + 1):
            run = _attempt_once(
                resolved, env, prompt, usage_totals, timeout_s,
                reasoning_effort, stopped,
            )
            if isinstance(run, CodexRun):
                return run.last_message

            if (isinstance(run, CodexRateLimited)
                    and attempt < CODEX_RATE_LIMIT_RETRIES
                    and not stopped()):
                # Jittered so a concurrent pool does not resynchronise
                # every worker onto one wake-up and hit the window together,
                # and the cap is applied AFTER the jitter: capping first lets
                # the multiplier carry the wait half again past the ceiling
                # the constant advertises.
                wait = CODEX_RATE_LIMIT_BASE_S * (2 ** attempt)
                sleep(min(wait * (0.5 + random.random()), CODEX_RATE_LIMIT_CAP_S))
                continue
            # The last classified failure, raised with its own message. There
            # is deliberately no summarising raise after this loop: every path
            # above returns, continues or raises, so one would be unreachable
            # code claiming to describe an outcome it can never see.
            raise run

        raise AssertionError("unreachable: the retry loop always exits")

    return complete


def _attempt_once(
    resolved: dict[str, str],
    env: dict[str, str],
    prompt: str,
    usage_totals: dict[str, int] | None,
    timeout_s: float,
    reasoning_effort: str | None,
    stopped: Callable[[], bool] = lambda: False,
) -> CodexRun | RuntimeError:
    """One spawn, with the transport retry. Returns the run OR the exception.

    Returned rather than raised so the rate-limit backoff above reads as a
    loop over outcomes instead of a `try` nested inside a `for` inside a
    `try`. An auth failure comes back as an object here and is raised by the
    caller unretried -- see the module docstring for why a codex 401 is not
    the mantle 401.

    ONE transport retry, and thin on purpose: codex already retries internally
    (measured: five websocket attempts, a fallback to HTTPS, five more) before
    it reports anything, so a generous second layer here would multiply a
    dead endpoint by two rather than survive a blip.
    """
    last: RuntimeError | None = None

    for _ in range(2):
        if stopped():
            # Checked before the spawn rather than after: the point is to not
            # BUY the call, and a check after it has already been paid for
            # would only decide whether to look at the answer.
            return CodexCallFailed(
                "the pass stopped before this call was made"
            )
        scratch = Path(tempfile.mkdtemp(prefix="bakeoff-judge-"))
        try:
            output_file = scratch / LAST_MESSAGE_NAME
            argv = build_codex_argv(
                resolved["bin"], resolved["model"], scratch, output_file,
                reasoning_effort,
            )
            _count_call(usage_totals)
            try:
                run = _run_codex(argv, prompt, env, timeout_s, output_file)
            except CodexCallFailed as exc:  # the timeout path
                last = exc
                continue

            _fold_usage(usage_totals, run)
            message = failure_message(run)
            if message is None:
                return run

            failure = classify_failure(message)
            if isinstance(failure, (CodexAuthFailure, CodexRateLimited)):
                # Neither is a transport blip: one is unfixable here and the
                # other has its own budget upstairs. Retrying either in this
                # loop would spend the wrong budget on the wrong problem.
                return failure
            last = failure
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    return last or CodexCallFailed("codex exec failed with no reported cause")


def _tail(text: str, limit: int = 300) -> str:
    """The last of a stderr blob. Codex retries verbosely, so the FIRST lines
    of a failure are the least informative ones it has."""
    stripped = (text or "").strip()
    return stripped[-limit:] if stripped else "(no stderr)"
