"""Drive Claude Code as a subprocess under a controlled configuration.

Spec section 5.2: CLAUDE.md, settings, hooks, MCP servers, and skills all
change agent behavior. Any of them leaking into a run contaminates results,
and unevenly across models. Everything is pinned explicitly here, and
config_digest() is stored on every run record as proof.

Two levers do the pinning, and neither is optional:

  CLAUDE_CONFIG_DIR   points at an empty per-run directory. `--settings`
                      loads *additional* settings, so it merges on top of
                      the operator's ~/.claude/settings.json rather than
                      replacing it; relocating the config dir is what
                      actually detaches user settings, CLAUDE.md, skills
                      and plugins. It also relocates projects/, which is
                      why transcript discovery is unambiguous.

  an env allowlist    rather than a denylist. A denylist has to anticipate
                      every contaminant; the one that matters most is
                      CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX,
                      either of which makes the CLI ignore
                      ANTHROPIC_BASE_URL and call the provider directly --
                      bypassing the proxy and leaving the mandatory wire
                      log (section 6.2) empty, with the run still looking
                      normal.

Verified against claude 2.1.220.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

EMPTY_MCP_CONFIG = '{"mcpServers":{}}'


def classify_stdout_line(line: str) -> str:
    """One of "assistant", "other", "malformed", "blank".

    Tolerant on purpose: stdout carries system, user and result events too,
    and a partial line during shutdown must not take the run down.

    It returns a CLASS rather than a bool because the bool it replaced could
    not tell "a system event" from "a line that did not parse". Both answered
    False, so unreadable stdout was dropped with no counter -- and dropping it
    undercounts `turns_streamed`, which exists precisely to cross-check the
    other two turn counts. A miscount in the count that catches miscounts is
    the one that cannot be caught.

    Blank is its own class and is not malformed: line-buffered output produces
    them routinely and counting them as damage would put a permanent non-zero
    in a field that is supposed to mean something went wrong.
    """
    line = line.strip()
    if not line:
        return "blank"
    if not line.startswith("{"):
        return "malformed"
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return "malformed"
    return "assistant" if record.get("type") == "assistant" else "other"

# The only ambient variables the subprocess inherits. It reaches Bedrock
# through the LiteLLM proxy over HTTP with a bearer token, so it needs no
# AWS credentials and none are passed.
PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE")

# Per-run or per-arm values: they must not move the digest, or the digest
# stops proving that the arms were configured identically.
DIGEST_EXCLUDED_ENV = frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        # Carries the run_id, so it differs by construction on every run.
        "ANTHROPIC_CUSTOM_HEADERS",
    }
)


@dataclass(frozen=True)
class ClaudeCodeConfig:
    model: str
    base_url: str
    auth_token: str
    settings_path: str
    config_dir: str
    max_turns: int
    wall_clock_timeout_s: int
    # Uniform across arms, but see the tokenizer note in config/README:
    # Sonnet 5's tokenizer emits ~30% more tokens for the same text, so an
    # identical numeric cap is not an identical text budget.
    max_output_tokens: int = 16384
    # Applied by the LiteLLM proxy, not by this process -- Claude Code has
    # no temperature flag. Carried here only so the run record can state
    # what the proxy was configured to send (spec section 5.3); None means
    # "send no temperature", which is mandatory for Sonnet 5.
    temperature: float | None = None
    # Extra request headers, "Name: Value" per line. Verified against claude
    # 2.1.220: ANTHROPIC_CUSTOM_HEADERS in that format appears on every
    # POST /v1/messages. The harness stamps the run_id here so the proxy-side
    # wire log can attribute each call to a run (see proxy_callback).
    custom_headers: str = ""


@dataclass(frozen=True)
class RunnerResult:
    exit_code: int
    transcript_path: Path | None
    stdout: str
    stderr: str
    wall_clock_ms: int
    timed_out: bool
    # Turns seen live on stdout. May exceed the transcript's turn count if
    # the run was killed before the last record was flushed to disk.
    turns_streamed: int = 0
    # Stdout lines that were not readable as stream-json. Any non-zero value
    # means `turns_streamed` is a floor rather than a count, so the three-way
    # turn cross-check has to be read with that in mind instead of silently
    # reconciling one short.
    stdout_malformed_lines: int = 0


def build_command(config: ClaudeCodeConfig) -> list[str]:
    return [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP_CONFIG,
        "--settings",
        config.settings_path,
        "--max-turns",
        str(config.max_turns),
    ]


def _eval_env(config: ClaudeCodeConfig) -> dict[str, str]:
    """The variables the harness sets deliberately.

    A key added here CONDITIONALLY -- emitted only when some config field is
    set, the way `ANTHROPIC_CUSTOM_HEADERS` is -- must also be forced by
    `pinned_env_keys()`'s sentinel config below. That function derives its set
    by calling this one, so a key absent under the sentinel is a key
    `tasks._env_map` will happily let a task image set, and the agent's exec
    then silently overrides it: preflight and the grader see one environment
    and the agent sees another. The disjointness test cannot catch it, because
    the key is missing from the set the test compares against.
    """
    extra = (
        {"ANTHROPIC_CUSTOM_HEADERS": config.custom_headers}
        if config.custom_headers
        else {}
    )
    return {
        **extra,
        "CLAUDE_CONFIG_DIR": config.config_dir,
        "ANTHROPIC_BASE_URL": config.base_url,
        "ANTHROPIC_AUTH_TOKEN": config.auth_token,
        "ANTHROPIC_MODEL": config.model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # DISABLE_AUTOUPDATER only stops the background check; DISABLE_UPDATES
        # blocks every update path. An agent that upgraded itself mid-eval
        # would change versions.claude_code between runs without the record
        # ever saying so.
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(config.max_output_tokens),
    }


def build_env(config: ClaudeCodeConfig) -> dict[str, str]:
    """Environment for a host subprocess: allowlist plus the eval's own keys."""
    env = {key: os.environ[key] for key in PASSTHROUGH_ENV if key in os.environ}
    env.update(_eval_env(config))
    return env


def container_env(config: ClaudeCodeConfig) -> dict[str, str]:
    """Environment for an in-container exec: the eval's own keys only.

    The host allowlist must NOT be forwarded here. Docker merges this with
    the image's environment, so passing the host's PATH would replace a
    valid container PATH with directories that do not exist in the image --
    `claude` would stop resolving, and any path that happened to exist would
    resolve to something the image never installed. The same applies to HOME,
    which decides where the agent writes state.
    """
    return _eval_env(config)


#: The base image's own ENV, and the one member of `pinned_env_keys` that
#: nothing in this process can derive -- it is set in
#: docker/eval-agent.Dockerfile, not by `_eval_env`. Listed here so the
#: refusal in tasks.py has one source for the whole set.
#:
#: CPython invalidates a .pyc on (source mtime in whole seconds, source size)
#: and both halves are ordinary here, so a task image that turned this off
#: would feed section 3.3's self-correction loop the code the agent already
#: replaced. Measured 2026-08-13 in the eval image, and it made
#: verify_logger.py fail on 2 of 3 consecutive runs.
_BASE_IMAGE_ENV = frozenset({"PYTHONDONTWRITEBYTECODE"})


def pinned_env_keys() -> frozenset[str]:
    """Every environment key the harness itself decides. `tasks.py` refuses these.

    Docker MERGES an exec's environment into the image's, with the exec's keys
    winning (measured 2026-09-01, Docker 29.5.2). So a task image declaring a
    key this function names would be overridden on the AGENT's process --
    which alone gets `container_env` -- while still applying to preflight's
    and the grader's execs, which pass no env. That is two environments for
    one task, and nothing in the record would say which one produced a result.

    DERIVED from `_eval_env` and `PASSTHROUGH_ENV` rather than hand-listed,
    for `_GRADING_KEYS`' reason: a copy goes stale the first time a key is
    added, and the failure of a stale list is the silent one.

    The sentinel matters. `_eval_env` emits ANTHROPIC_CUSTOM_HEADERS only when
    `custom_headers` is truthy and the dataclass default is "", so a default
    config would leave the set missing exactly the key that carries the run id
    -- the one whose loss makes every call unattributed.
    """
    sentinel = ClaudeCodeConfig(
        model="", base_url="", auth_token="", settings_path="",
        config_dir="", max_turns=0, wall_clock_timeout_s=0,
        custom_headers="X-Bakeoff-Run-Id: sentinel",
    )
    return (
        frozenset(_eval_env(sentinel))
        | frozenset(PASSTHROUGH_ENV)
        | _BASE_IMAGE_ENV
    )


def config_digest(config: ClaudeCodeConfig) -> str:
    """Digest of everything that must be identical across arms.

    Deliberately excludes `model` (that is the independent variable),
    `auth_token` (a secret, and not behavior-affecting), `base_url` and
    `config_dir` (per-run paths), and `temperature`.

    Temperature is excluded because it is necessarily per-model: Sonnet 5
    returns 400 on any non-default sampling parameter, Kimi K2.5 stalls for
    minutes at 0, and the other arms document 1.0. Spec section 5.3's "or
    lab-recommended" clause means the thing held identical across arms is
    the policy -- each model at its documented operating point -- not the
    number. Folding a per-model number into a cross-arm identity digest
    would make the digest assert something false. The values themselves are
    recorded per run in RunRecord.sampling.

    Covers the environment as well as the command, because every knob
    except --max-turns lives in the environment. The passthrough *keys* are
    included so that widening the allowlist moves the digest; their values
    are not, since they are machine paths rather than eval configuration.
    """
    payload = {
        k: v
        for k, v in asdict(config).items()
        if k
        not in {
            "model",
            "auth_token",
            "base_url",
            "config_dir",
            "temperature",
            "custom_headers",
        }
    }
    payload["command_shape"] = build_command(config)[1:]
    payload["env_shape"] = {
        k: v for k, v in _eval_env(config).items() if k not in DIGEST_EXCLUDED_ENV
    }
    payload["env_passthrough"] = sorted(PASSTHROUGH_ENV)
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BackendResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class HostBackend:
    """Run the agent as a subprocess of the harness.

    Retained for tests and for machines without a Docker daemon. It gives
    the agent the host's network, toolchain and filesystem, so a run made
    this way satisfies none of spec section 5.1 -- execute_run records that
    rather than letting the pinned image digest imply otherwise.
    """

    def transcript_root(self, config: ClaudeCodeConfig) -> str:
        return config.config_dir

    def env_for(self, config: ClaudeCodeConfig) -> dict[str, str]:
        return build_env(config)

    def execute(
        self,
        command: list[str],
        env: dict[str, str],
        cwd: str,
        timeout_s: int,
        on_stdout_line: Callable[[str], None],
    ) -> BackendResult:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout_s
        stdout_lines: list[str] = []
        timed_out = False

        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_lines.append(line)
            on_stdout_line(line.rstrip("\n"))
            if time.monotonic() > deadline:
                timed_out = True
                proc.kill()
                break

        stderr = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        return BackendResult(
            exit_code=proc.returncode,
            stdout="".join(stdout_lines),
            stderr=stderr,
            timed_out=timed_out,
        )


class ContainerBackend:
    """Run the agent inside the pinned container (spec section 5.1).

    The container's working directory is the mounted repo, so `cwd` is
    ignored. The config directory is bind-mounted separately: it must not
    sit under the repo, or `git add -A` would sweep the whole config tree
    and every transcript into each checkpoint diff.

    Timeouts are enforced by `timeout` inside the container, not by
    abandoning the read loop. Docker exposes no way to kill a running exec,
    so a harness that merely stopped reading would leave the agent running.
    `timeout` reports 124 on expiry, which is how timed_out is detected.
    """

    TIMEOUT_EXIT_CODE = 124

    def __init__(self, container: Any, host_config_dir: str) -> None:
        self.container = container
        self.host_config_dir = host_config_dir

    def transcript_root(self, config: ClaudeCodeConfig) -> str:
        return self.host_config_dir

    def env_for(self, config: ClaudeCodeConfig) -> dict[str, str]:
        return container_env(config)

    def execute(
        self,
        command: list[str],
        env: dict[str, str],
        cwd: str,
        timeout_s: int,
        on_stdout_line: Callable[[str], None],
    ) -> BackendResult:
        result = self.container.exec_stream(
            ["timeout", "-s", "TERM", str(timeout_s), *command],
            on_stdout_line=on_stdout_line,
            env=env,
        )
        return BackendResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.exit_code == self.TIMEOUT_EXIT_CODE,
        )


class ClaudeCodeRunner:
    def __init__(
        self, config: ClaudeCodeConfig, backend: HostBackend | ContainerBackend | None = None
    ) -> None:
        self.config = config
        self.backend = backend or HostBackend()

    def run(
        self,
        prompt: str,
        cwd: str,
        on_turn: Callable[[int, int], None] | None = None,
    ) -> RunnerResult:
        """Execute one agent run.

        on_turn(completed_turns, elapsed_ms) fires at each turn boundary,
        which is the only moment a checkpoint can observe work in progress.

        The argument is how many turns have FINISHED, not the index of the
        event that just arrived. An assistant message announces the tool
        calls a turn is about to make, so when assistant message k lands the
        working tree still holds the result of k-1 turns. Passing k would
        file turn k-1's work under turn k and understate progress at every
        checkpoint by exactly one turn -- which is the axis the
        cost-at-budget-K curve is plotted against.

        Nothing fires for the first assistant message: no turn has completed
        yet, and the tree is still the base commit. The caller is
        responsible for the final turn, whose tools run after the last
        message on the stream (see execute_run's force_capture).

        The boundary is approximate by however long a snapshot takes. The
        callback runs while the agent is live, so a tool call that writes
        faster than the snapshot completes can put part of turn k+1 into
        turn k's checkpoint. Closing that window would mean pausing the
        agent, which no external observer can do -- the alternative,
        snapshotting after the run, is not approximate but simply wrong:
        every checkpoint then holds the same end state.
        """
        # The prompt is appended here rather than inside build_command so
        # that config_digest stays a property of the configuration and does
        # not change with the task under test.
        command = [*build_command(self.config), prompt]
        env = self.backend.env_for(self.config)
        # The caller owns freshness: this directory must be empty at the
        # start of each run, or transcript discovery loses its guarantee.
        Path(self.backend.transcript_root(self.config)).mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        turns = 0
        malformed = 0

        def handle_line(line: str) -> None:
            nonlocal turns, malformed
            kind = classify_stdout_line(line)
            if kind == "malformed":
                malformed += 1
                return
            if kind != "assistant":
                return
            turns += 1
            if on_turn is not None and turns > 1:
                on_turn(turns - 1, int((time.monotonic() - started) * 1000))

        result = self.backend.execute(
            command=command,
            env=env,
            cwd=cwd,
            timeout_s=self.config.wall_clock_timeout_s,
            on_stdout_line=handle_line,
        )

        return RunnerResult(
            exit_code=result.exit_code,
            transcript_path=self._find_transcript(
                self.backend.transcript_root(self.config)
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            wall_clock_ms=int((time.monotonic() - started) * 1000),
            timed_out=result.timed_out,
            turns_streamed=turns,
            stdout_malformed_lines=malformed,
        )

    @staticmethod
    def _find_transcript(config_dir: str) -> Path | None:
        """Locate this run's transcript under <config_dir>/projects/.

        Globbing rather than reconstructing the project directory name:
        Claude Code hyphenates dots as well as separators when munging cwd
        (/Users/x/.claude becomes -Users-x--claude), so a replace("/", "-")
        rule silently misses any repo path containing a dot.

        The directory is fresh per run, so anything found belongs to this
        run and an empty result is an honest "no transcript" rather than a
        neighbouring run's. Sub-sessions write their own files; the most
        recently written one is the main session, which ends last.
        """
        projects = Path(config_dir) / "projects"
        if not projects.is_dir():
            return None
        transcripts = sorted(
            projects.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        return transcripts[0] if transcripts else None
