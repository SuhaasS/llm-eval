import json
import os
import tempfile
from pathlib import Path

import pytest

from bakeoff.claude_runner import (
    BackendResult,
    ClaudeCodeConfig,
    ClaudeCodeRunner,
    HostBackend,
    build_command,
    build_env,
    container_env,
    config_digest,
)


def make_config(**overrides) -> ClaudeCodeConfig:
    base = dict(
        model="gemma-4-31b",
        base_url="http://127.0.0.1:4000",
        auth_token="test-token",
        settings_path="/cfg/eval_settings.json",
        config_dir="/run/artifacts/r-001/claude-config",
        max_turns=60,
        wall_clock_timeout_s=1800,
    )
    base.update(overrides)
    return ClaudeCodeConfig(**base)


def test_command_disables_all_mcp_servers():
    """Spec section 5.2: MCP servers change agent behavior and must not
    leak into eval runs."""
    cmd = build_command(make_config())
    assert "--strict-mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == '{"mcpServers":{}}'


def test_command_pins_explicit_settings_file():
    cmd = build_command(make_config())
    idx = cmd.index("--settings")
    assert cmd[idx + 1] == "/cfg/eval_settings.json"


def test_command_sets_max_turns():
    cmd = build_command(make_config(max_turns=42))
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "42"


def test_command_runs_non_interactively_with_stream_json():
    cmd = build_command(make_config())
    assert "-p" in cmd
    idx = cmd.index("--output-format")
    assert cmd[idx + 1] == "stream-json"


def test_env_points_at_litellm_proxy():
    env = build_env(make_config())
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-token"
    assert env["ANTHROPIC_MODEL"] == "gemma-4-31b"


def test_env_disables_telemetry_and_autoupdate():
    env = build_env(make_config())
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_config_digest_is_stable_across_calls():
    assert config_digest(make_config()) == config_digest(make_config())


def test_config_digest_changes_when_any_setting_changes():
    assert config_digest(make_config()) != config_digest(make_config(max_turns=99))


def test_config_digest_ignores_model_so_arms_are_comparable():
    """Every model must run under an identical configuration. The digest
    proves it, so it must not vary with the model under test."""
    assert config_digest(make_config(model="kimi-k2-5")) == config_digest(
        make_config(model="claude-sonnet-5")
    )


# --- configuration isolation (spec section 5.2) ------------------------------


def test_env_isolates_config_dir_from_operator_setup():
    """CLAUDE_CONFIG_DIR must be SET, not stripped.

    `--settings` loads *additional* settings (verified against the claude
    2.1.220 help text), so it merges on top of ~/.claude/settings.json
    rather than replacing it. Stripping CLAUDE_CONFIG_DIR makes Claude Code
    fall back to ~/.claude -- the operator's real hooks, skills, plugins and
    CLAUDE.md, which section 5.2 names as the highest-risk contamination
    source. Pointing it at an empty per-run directory is the actual lever.
    """
    config = make_config()
    env = build_env(config)
    assert env["CLAUDE_CONFIG_DIR"] == config.config_dir


def test_env_does_not_leak_ambient_claude_variables(monkeypatch):
    """The parent environment is not a safe base to start from.

    A harness run is itself often launched from a Claude Code session, so
    these are set in practice, not hypothetically.
    """
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")

    env = build_env(make_config())

    for leaked in (
        "CLAUDECODE",
        "CLAUDE_EFFORT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_ENTRYPOINT",
    ):
        assert leaked not in env


def test_env_cannot_silently_bypass_the_proxy(monkeypatch):
    """Spec section 6.2 makes wire logging mandatory, and the wire log only
    exists because every call goes through the LiteLLM proxy.

    CLAUDE_CODE_USE_BEDROCK and CLAUDE_CODE_USE_VERTEX (both present in the
    claude 2.1.220 binary) make the CLI ignore ANTHROPIC_BASE_URL and call
    the provider directly. Inheriting either -- plausible in a shell at a
    Bedrock shop -- produces a run that looks normal and has an empty wire
    log. This is the failure that must not be possible.
    """
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-key")

    env = build_env(make_config())

    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"


def test_env_preserves_what_the_subprocess_needs_to_start():
    """The allowlist must not be so tight that `claude` cannot be resolved
    or run. A test that only asserts absence would pass on an empty dict."""
    env = build_env(make_config())
    assert env["PATH"]
    assert env["HOME"]


def test_container_env_does_not_forward_host_paths():
    """Docker merges this with the image's own environment.

    Forwarding the host PATH would replace a working container PATH with
    directories that do not exist in the image, so `claude` would stop
    resolving -- and any path that did happen to exist would resolve to
    something the image never installed. HOME decides where the agent
    writes state, with the same problem.
    """
    env = container_env(make_config())
    assert "PATH" not in env
    assert "HOME" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert env["CLAUDE_CONFIG_DIR"] == "/run/artifacts/r-001/claude-config"


def test_pinned_env_keys_covers_the_key_that_is_only_set_when_non_empty():
    """The sentinel in `pinned_env_keys` is the whole reason it is a function.

    `_eval_env` emits ANTHROPIC_CUSTOM_HEADERS only when `custom_headers` is
    truthy, and the dataclass default is "". Built from a default config, the
    set would be missing exactly the key that carries the run id -- so a task
    image could set it, the agent's exec would override it, and preflight and
    the grader would read one value while the agent's calls carried another.
    """
    from bakeoff.claude_runner import pinned_env_keys

    keys = pinned_env_keys()

    assert "ANTHROPIC_CUSTOM_HEADERS" in keys
    assert "ANTHROPIC_BASE_URL" in keys
    assert "CLAUDE_CONFIG_DIR" in keys


def test_pinned_env_keys_includes_the_host_allowlist_and_the_base_image_pin():
    """PATH and HOME come from PASSTHROUGH_ENV; PYTHONDONTWRITEBYTECODE comes
    from the base image and is the one member nothing in this process can
    derive.

    A task image overriding PATH would stop `claude` resolving; overriding
    PYTHONDONTWRITEBYTECODE re-arms the stale-pyc defect that already made
    verify_logger.py fail on 2 of 3 consecutive runs and shipped a `.pyc` as
    the first hunk of a live submission diff.
    """
    from bakeoff.claude_runner import pinned_env_keys

    keys = pinned_env_keys()

    assert {"PATH", "HOME", "PYTHONDONTWRITEBYTECODE"} <= keys


def test_config_digest_covers_environment_not_just_command():
    """The digest is stored as proof the arms ran identically, but every
    behavior knob except --max-turns lives in the environment. A digest over
    the command alone would certify two differently-configured runs as the
    same."""
    assert config_digest(make_config()) != config_digest(
        make_config(max_output_tokens=4096)
    )


def test_config_digest_ignores_per_model_sampling():
    """Spec section 5.3 freezes sampling at each model's lab-recommended
    setting, and those differ: Sonnet 5 returns 400 on any non-default
    sampling parameter, Kimi K2.5 stalls for minutes at 0, Gemma and
    Nemotron document 1.0. What is identical across arms is the policy, not
    the number.

    Digesting the number would make config_digest -- the artifact that
    certifies the arms ran under one configuration -- differ per arm for a
    reason that is not a configuration difference.
    """
    assert config_digest(make_config(temperature=None)) == config_digest(
        make_config(temperature=1.0)
    )


def test_config_digest_ignores_the_per_run_run_id_header():
    """`custom_headers` carries the run_id, so it differs BY CONSTRUCTION on
    every single run.

    config_digest exists to certify that every arm ran under one
    configuration. Folding in a value that is unique per run would make
    every digest unique, which does not merely weaken that certificate --
    it makes two runs of the same arm look differently configured, so
    nothing in 2,400 records could ever be compared on it. The header is
    excluded in two places, the config field and the env var, and both are
    load-bearing.
    """
    assert config_digest(make_config(custom_headers="X-Bakeoff-Run-Id: aaa")) == (
        config_digest(make_config(custom_headers="X-Bakeoff-Run-Id: bbb"))
    )
    # And a run with the header configured must match one without, so the
    # dry run and the proxy path stay comparable.
    assert config_digest(make_config(custom_headers="")) == config_digest(
        make_config(custom_headers="X-Bakeoff-Run-Id: aaa")
    )


def test_the_run_id_header_still_reaches_the_agent():
    """The other half: excluded from the digest, but present in the
    environment. Dropping it would leave the proxy unable to attribute any
    call to a run, and every wire entry would land in unattributed.jsonl."""
    env = container_env(make_config(custom_headers="X-Bakeoff-Run-Id: r-42"))
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Bakeoff-Run-Id: r-42"
    # Absent, not empty, when unset: an empty header value is a header the
    # agent would still send.
    assert "ANTHROPIC_CUSTOM_HEADERS" not in container_env(make_config())


def test_config_digest_excludes_the_auth_token():
    """The digest lands in the run record, which is written to disk and
    shared. A secret must not be derivable from it, and the token does not
    affect behavior."""
    assert config_digest(make_config(auth_token="token-a")) == config_digest(
        make_config(auth_token="token-b")
    )


# --- transcript discovery ----------------------------------------------------


def test_transcript_is_never_taken_from_another_run(tmp_path):
    """A run that produced no transcript must report None.

    Selecting the newest *.jsonl under a shared project directory would hand
    back the PREVIOUS run's trajectory -- another model's turns, tokens and
    cost, attributed to this run. Task 10 reuses one repo path across the
    N=10 samples and across arms, so the neighbouring file is routinely a
    different model's.
    """
    config_dir = tmp_path / "run-002" / "claude-config"
    (config_dir / "projects" / "-work-repo").mkdir(parents=True)

    stale = tmp_path / "run-001" / "claude-config" / "projects" / "-work-repo"
    stale.mkdir(parents=True)
    (stale / "aaaaaaaa-0000-0000-0000-000000000000.jsonl").write_text(
        json.dumps({"type": "assistant", "sessionId": "previous-run"}) + "\n"
    )

    assert ClaudeCodeRunner._find_transcript(str(config_dir)) is None


def test_transcript_found_regardless_of_path_punctuation(tmp_path):
    """Claude Code hyphenates dots as well as separators when munging cwd
    into a project directory name -- /Users/x/.claude becomes
    -Users-x--claude. Reimplementing that rule with str.replace("/", "-")
    misses any repo path containing a dot, returns None, and Task 10 then
    records turns, tokens and cost as zero without complaint.

    Globbing the per-run config dir sidesteps the rule entirely.
    """
    config_dir = tmp_path / "claude-config"
    munged = config_dir / "projects" / "-work-repo-v1-2--hidden"
    munged.mkdir(parents=True)
    transcript = munged / "bbbbbbbb-0000-0000-0000-000000000000.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "sessionId": "this-run"}) + "\n"
    )

    assert ClaudeCodeRunner._find_transcript(str(config_dir)) == transcript


def test_missing_config_dir_reports_no_transcript(tmp_path):
    assert ClaudeCodeRunner._find_transcript(str(tmp_path / "never-created")) is None


# --- live turn boundaries ----------------------------------------------------


class ReplayBackend:
    """Feeds canned stdout lines through the runner's streaming path."""

    def __init__(self, lines):
        self.lines = lines
        self.command = None

    def transcript_root(self, config):
        return config.config_dir

    def env_for(self, config):
        return build_env(config)

    def execute(self, command, env, cwd, timeout_s, on_stdout_line):
        self.command = command
        for line in self.lines:
            on_stdout_line(line)
        return BackendResult(
            exit_code=0, stdout="\n".join(self.lines), stderr="", timed_out=False
        )


STREAM = [
    '{"type":"system","subtype":"init","session_id":"s1"}',
    '{"type":"assistant","message":{"content":[]}}',
    '{"type":"user","message":{"content":[]}}',
    '{"type":"assistant","message":{"content":[]}}',
    "",
    "not json at all",
    '{"type":"result","subtype":"success"}',
]


def test_on_turn_reports_turns_completed_not_events_seen(tmp_path):
    """An assistant message announces the tool calls a turn is ABOUT to
    make, so at message k the working tree holds k-1 turns of work.

    Reporting k would file turn k-1's work under turn k and understate
    every checkpoint by one turn -- on the exact axis the cost-at-budget-K
    curve is plotted against. Nothing fires for the first message: no turn
    has finished and the tree is still the base commit.
    """
    seen = []
    runner = ClaudeCodeRunner(
        make_config(config_dir=str(tmp_path / "cfg")), backend=ReplayBackend(STREAM)
    )
    result = runner.run("fix the bug", cwd=str(tmp_path), on_turn=lambda t, ms: seen.append(t))

    assert seen == [1]
    assert result.turns_streamed == 2


def test_non_assistant_events_are_not_counted_as_turns(tmp_path):
    """stdout carries system, user and result events, blank lines, and a
    possibly-partial final line. Counting any of them as a turn would
    fabricate checkpoints for turns that never happened."""
    runner = ClaudeCodeRunner(
        make_config(config_dir=str(tmp_path / "cfg")), backend=ReplayBackend(STREAM)
    )
    result = runner.run("p", cwd=str(tmp_path))
    assert result.turns_streamed == 2  # not 7


def test_prompt_reaches_the_command_but_not_the_digest(tmp_path):
    """Two arms run different tasks under one configuration. A digest that
    moved with the prompt could never show that."""
    backend = ReplayBackend([])
    config = make_config(config_dir=str(tmp_path / "cfg"))
    ClaudeCodeRunner(config, backend=backend).run("fix the bug", cwd=str(tmp_path))

    assert backend.command[-1] == "fix the bug"
    assert "fix the bug" not in json.dumps(build_command(config))


def test_host_backend_streams_real_subprocess_output(tmp_path):
    """Exercises the actual streaming machinery, not just the replay stub."""
    seen = []
    result = HostBackend().execute(
        command=[
            "sh",
            "-c",
            'printf \'{"type":"assistant"}\\n{"type":"result"}\\n\'',
        ],
        env=dict(os.environ),
        cwd=str(tmp_path),
        timeout_s=30,
        on_stdout_line=seen.append,
    )
    assert result.exit_code == 0
    assert seen == ['{"type":"assistant"}', '{"type":"result"}']


# --- stdout that could not be read is damage, and damage gets counted --------


def test_unparseable_stdout_is_distinguished_from_a_non_assistant_event():
    """The predicate this replaced answered False to both, so a truncated line
    and a system event were the same non-event. Dropping the first undercounts
    `turns_streamed` -- one of the three turn counts that exist to cross-check
    each other, which makes it the miscount that cannot be caught by the others.
    """
    from bakeoff.claude_runner import classify_stdout_line

    assert classify_stdout_line('{"type": "assistant"}') == "assistant"
    assert classify_stdout_line('{"type": "system", "subtype": "init"}') == "other"
    assert classify_stdout_line('{"type": "assis') == "malformed"
    assert classify_stdout_line("Error: ENOSPC") == "malformed"
    # Blank is NOT malformed. Line-buffered output produces them routinely, and
    # counting them as damage puts a permanent non-zero in a field whose whole
    # job is to be zero on a clean run.
    assert classify_stdout_line("") == "blank"
    assert classify_stdout_line("   ") == "blank"


def test_the_runner_reports_how_many_stdout_lines_it_could_not_read():
    """A count, not a log line. `RunnerResult.turns_streamed` is a floor rather
    than a count whenever this is non-zero, and nothing else in the record says
    so."""
    from bakeoff.claude_runner import ClaudeCodeConfig, ClaudeCodeRunner

    class _Backend:
        def transcript_root(self, config):
            return str(Path(tempfile.mkdtemp()) / "cfg")

        def env_for(self, config):
            return {}

        def execute(self, command, env, cwd, timeout_s, on_stdout_line):
            from bakeoff.claude_runner import BackendResult

            for line in (
                '{"type": "assistant", "message": {}}',
                "{ truncated",
                "npm WARN something",
                '{"type": "result"}',
                '{"type": "assistant", "message": {}}',
            ):
                on_stdout_line(line)
            return BackendResult(exit_code=0, stdout="", stderr="", timed_out=False)

    config = ClaudeCodeConfig(
        model="m", base_url="", auth_token="", settings_path="/s",
        config_dir="/c", max_turns=5, wall_clock_timeout_s=10,
    )
    result = ClaudeCodeRunner(config, backend=_Backend()).run("go", cwd=".")

    assert result.turns_streamed == 2
    assert result.stdout_malformed_lines == 2
