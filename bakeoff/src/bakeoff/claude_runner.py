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

EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

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


@dataclass(frozen=True)
class RunnerResult:
    exit_code: int
    transcript_path: Path | None
    stdout: str
    stderr: str
    wall_clock_ms: int
    timed_out: bool


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
    """The variables the harness sets deliberately."""
    return {
        "CLAUDE_CONFIG_DIR": config.config_dir,
        "ANTHROPIC_BASE_URL": config.base_url,
        "ANTHROPIC_AUTH_TOKEN": config.auth_token,
        "ANTHROPIC_MODEL": config.model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(config.max_output_tokens),
    }


def build_env(config: ClaudeCodeConfig) -> dict[str, str]:
    env = {key: os.environ[key] for key in PASSTHROUGH_ENV if key in os.environ}
    env.update(_eval_env(config))
    return env


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
        if k not in {"model", "auth_token", "base_url", "config_dir", "temperature"}
    }
    payload["command_shape"] = build_command(config)[1:]
    payload["env_shape"] = {
        k: v for k, v in _eval_env(config).items() if k not in DIGEST_EXCLUDED_ENV
    }
    payload["env_passthrough"] = sorted(PASSTHROUGH_ENV)
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ClaudeCodeRunner:
    def __init__(self, config: ClaudeCodeConfig) -> None:
        self.config = config

    def run(self, prompt: str, cwd: str) -> RunnerResult:
        command = build_command(self.config)
        env = build_env(self.config)
        # The caller owns freshness: this directory must be empty at the
        # start of each run, or transcript discovery loses its guarantee.
        Path(self.config.config_dir).mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        timed_out = False

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.config.wall_clock_timeout_s,
            )
            exit_code = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")

        wall_clock_ms = int((time.monotonic() - started) * 1000)
        return RunnerResult(
            exit_code=exit_code,
            transcript_path=self._find_transcript(self.config.config_dir),
            stdout=stdout,
            stderr=stderr,
            wall_clock_ms=wall_clock_ms,
            timed_out=timed_out,
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
