import json

import pytest

from bakeoff.claude_runner import (
    ClaudeCodeConfig,
    ClaudeCodeRunner,
    build_command,
    build_env,
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
