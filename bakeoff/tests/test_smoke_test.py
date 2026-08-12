"""Spec Phase 0c -- the parts of the smoke test that are checkable offline.

The smoke run itself needs credentials and spends money, so it cannot be a
test. What CAN be pinned here is everything that decides whether that run
produces usable evidence: that the settings file reaches the container, that
the effective-config dump is read correctly, and that the go/no-go criteria
fail on the conditions they exist to catch.

A criterion that silently passes on a broken run is worse than no gate: the
whole point of Phase 0c is to surface breakage on day one rather than in
week three.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakeoff.claude_runner import ClaudeCodeConfig
from bakeoff.eventlog import EventLog
from bakeoff.runner import TaskSpec, execute_run


# --- the settings mount ------------------------------------------------------
#
# Every call site passes settings_path="/eval/eval_settings.json" and nothing
# ever mounted a file there. Claude Code would then start with none of the
# pinned settings loaded -- including permissions.defaultMode -- so a real
# `claude -p` denies tool use, makes no edits, and lands no diff. On every
# arm. The smoke test would read that as four models failing the task.


class _CapturingContainer:
    """Records the kwargs RunContainer was constructed with."""

    seen: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def exec(self, *_args, **_kwargs):
        return None

    def snapshot_diff(self, base_sha: str) -> tuple[str, list[str]]:
        return ("", [])


class _NoopRunner:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout

    def run(self, prompt, cwd, on_turn=None):
        from bakeoff.claude_runner import RunnerResult

        return RunnerResult(
            exit_code=0,
            transcript_path=None,
            stdout=self.stdout,
            stderr="",
            wall_clock_ms=0,
            timed_out=False,
            turns_streamed=0,
        )


@pytest.fixture
def captured_mounts(monkeypatch, tmp_path):
    """Run execute_run against a fake container, return its extra_mounts."""
    import bakeoff.runner as runner_module

    seen: dict = {}

    def make_container(**kwargs):
        seen.update(kwargs)
        return _CapturingContainer()

    monkeypatch.setattr(runner_module, "RunContainer", make_container)
    monkeypatch.setattr(runner_module, "ContainerBackend", lambda *a, **k: None)
    def run(settings_host_path: Path | None, stdout: str = ""):
        monkeypatch.setattr(
            runner_module, "ClaudeCodeRunner", lambda *a, **k: _NoopRunner(stdout)
        )
        return _execute(settings_host_path)

    def _execute(settings_host_path: Path | None):
        execute_run(
            task=TaskSpec(
                task_id="t-smoke",
                task_version=1,
                repo="pindrop/example",
                base_sha="abc123",
                container_image_digest="python@sha256:" + "0" * 64,
                prompt="Fix it",
            ),
            model="gemma-4-31b",
            sample_index=0,
            config=ClaudeCodeConfig(
                model="gemma-4-31b",
                base_url="http://litellm:4000",
                auth_token="unused",
                settings_path="/eval/eval_settings.json",
                config_dir="/eval/claude-config",
                max_turns=10,
                wall_clock_timeout_s=60,
            ),
            event_log=EventLog(tmp_path / "log"),
            repo_path=str(tmp_path / "repo"),
            artifacts_root=tmp_path / "artifacts",
            settings_host_path=settings_host_path,
        )
        return seen.get("extra_mounts", {})

    return run


def test_the_settings_file_is_mounted_at_the_path_the_agent_is_told_to_read(
    captured_mounts, tmp_path
):
    """--settings names a container path. Without a mount it names nothing.

    Claude Code does not fail on a missing settings file, so the run looks
    normal while the pinned configuration -- permissions, hooks, the
    co-authored-by suppression -- is simply absent. Spec section 5.2 calls
    configuration the highest-risk contamination source, which makes a
    silently-unloaded settings file the worst available failure.
    """
    settings = tmp_path / "eval_settings.json"
    settings.write_text('{"permissions": {"defaultMode": "bypassPermissions"}}')

    mounts = captured_mounts(settings)

    assert mounts.get(str(settings)) == "/eval/eval_settings.json"


def test_no_settings_mount_is_requested_when_none_is_given(captured_mounts):
    """The dry run and the fault-injection suite pass no settings file --
    their stand-in agent ignores the flag. Mounting a phantom path would
    make Docker create a directory there and Claude Code read a directory
    as a settings file."""
    mounts = captured_mounts(None)

    assert not any(dest.endswith("eval_settings.json") for dest in mounts.values())


def test_the_config_dir_mount_survives_alongside_the_settings_mount(
    captured_mounts, tmp_path
):
    """The transcript is discovered through the config-dir mount. Replacing
    extra_mounts rather than adding to it would lose every trajectory --
    and a run with no transcript parses to zero turns, zero tokens, zero
    cost, which reads as a quiet run rather than as lost data."""
    settings = tmp_path / "eval_settings.json"
    settings.write_text("{}")

    mounts = captured_mounts(settings)

    assert "/eval/claude-config" in mounts.values()
    assert "/eval/eval_settings.json" in mounts.values()


# --- the section 5.2 effective-config dump -----------------------------------


def test_the_agent_stream_is_persisted_so_the_init_event_survives(
    captured_mounts, tmp_path
):
    """The init event is emitted on stdout and nowhere else.

    Verified against claude 2.1.220: a completed session's transcript holds
    queue-operation, user, attachment, assistant and last-prompt records and
    NO init event. Reading the effective config from the transcript would
    therefore always come back empty, and section 5.2 would be unverifiable
    while looking implemented.
    """
    from bakeoff.runner import STDOUT_NAME
    from scripts.smoke_test import effective_config

    init = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "permissionMode": "bypassPermissions",
            "mcp_servers": [],
            "tools": ["Edit"],
        }
    )
    captured_mounts(None, stdout=init + "\n")

    stream = tmp_path / "artifacts" / STDOUT_NAME
    assert stream.exists()
    assert effective_config(stream)["permissionMode"] == "bypassPermissions"


def test_an_empty_stream_leaves_no_stdout_artifact(captured_mounts, tmp_path):
    """A path recorded for a file that is not there would read as evidence
    that was captured and then lost, rather than never produced."""
    from bakeoff.runner import STDOUT_NAME

    captured_mounts(None)

    assert not (tmp_path / "artifacts" / STDOUT_NAME).exists()


def test_effective_config_is_read_from_the_init_event(tmp_path):
    """Spec section 5.2 requires dumping and diffing the effective config at
    session start. Claude Code emits it as the stream-json init event, and
    that is the only place the harness can observe what the session actually
    loaded rather than what it was told to load."""
    from scripts.smoke_test import effective_config

    transcript = tmp_path / "agent_stdout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "gemma-4-31b",
                "permissionMode": "bypassPermissions",
                "mcp_servers": [],
                "tools": ["Edit", "Bash"],
            }
        )
        + "\n"
        + json.dumps({"type": "assistant", "message": {"content": []}})
        + "\n"
    )

    config = effective_config(transcript)

    assert config["permissionMode"] == "bypassPermissions"
    assert config["tools"] == ["Edit", "Bash"]


def test_a_transcript_with_no_init_event_yields_nothing_rather_than_a_guess(tmp_path):
    """An absent dump must read as absent. Defaulting to the values that
    were REQUESTED would turn the one check that can detect an unloaded
    settings file into a check that always passes."""
    from scripts.smoke_test import effective_config

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"type": "assistant"}) + "\n")

    assert effective_config(transcript) == {}


def test_config_check_rejects_a_session_that_did_not_load_the_settings(tmp_path):
    """The load-bearing assertion of the whole settings-mount fix.

    permissionMode comes back as the Claude Code default when --settings
    named a path that does not exist. If that passes, the smoke run proceeds
    with an agent that cannot edit anything, and reports it as model
    failure.
    """
    from scripts.smoke_test import config_problems

    loaded = {
        "permissionMode": "bypassPermissions",
        "mcp_servers": [],
        "tools": ["Edit"],
    }
    assert config_problems(loaded) == []

    unloaded = dict(loaded, permissionMode="default")
    assert config_problems(unloaded)

    assert config_problems({}), "an empty dump must never read as a pass"


def test_config_check_rejects_a_session_with_mcp_servers_attached(tmp_path):
    """--strict-mcp-config with an empty server list is a section 5.2
    requirement. An MCP server that loaded anyway gives one arm tools the
    others did not have."""
    from scripts.smoke_test import config_problems

    contaminated = {
        "permissionMode": "bypassPermissions",
        "mcp_servers": [{"name": "something"}],
        "tools": ["Edit"],
    }
    assert config_problems(contaminated)


# --- the go/no-go criteria ---------------------------------------------------


class _Record:
    """Minimal stand-in shaped like the fields the criteria read."""

    def __init__(self, **kwargs):
        self.trajectory_parse_error = kwargs.get("parse_error", "")
        self.pricing_error = kwargs.get("pricing_error", "")
        self.cost_usd = kwargs.get("cost_usd", 0.012)
        self.turns_used = kwargs.get("turns", 3)
        self.isolated = kwargs.get("isolated", True)
        self.exclusion = kwargs.get("exclusion")
        self.tool_calls = type("T", (), {"total": kwargs.get("tools", 5)})()
        self.tokens = type(
            "U",
            (),
            {
                "input": kwargs.get("input_tokens", 1000),
                "output": kwargs.get("output_tokens", 200),
                "cache_read": kwargs.get("cache_read", 0),
                "cache_write": kwargs.get("cache_write", 0),
            },
        )()
        self.artifacts = type(
            "A", (), {"final_diff": kwargs.get("diff", "diff --git a/calc.py")}
        )()


def _live_problems(record, **kwargs):
    from scripts.smoke_test import LIVE, run_problems

    return run_problems(
        record,
        wire_entries=kwargs.get("wire_entries", 4),
        unattributed=kwargs.get("unattributed", 0),
        config=kwargs.get(
            "config",
            {"permissionMode": "bypassPermissions", "mcp_servers": [], "tools": ["Edit"]},
        ),
        expectations=LIVE,
    )


def test_a_healthy_live_run_passes_every_criterion():
    assert _live_problems(_Record()) == []


def test_a_trajectory_that_failed_to_parse_is_not_read_as_a_quiet_run():
    """A record whose transcript could not be parsed has every derived field
    empty. Those zeroes are the exact numbers Phase 0c exists to produce, so
    an unchecked parse error yields a confident table of zeroes."""
    assert _live_problems(_Record(parse_error="JSONDecodeError: line 4"))


def test_an_unpriced_run_is_reported_but_does_not_fail_the_gate():
    """A cache-token guard trip means the price is unknown, not that the run
    failed. The loop, the tool calls and the diff are all still evidence, and
    the tokens are recorded in full -- so the run is repriceable offline the
    moment AWS publishes rates, and gating on it would discard good data.

    This is the case that used to arrive as trajectory_parse_error with the
    whole trajectory zeroed."""
    assert (
        _live_problems(
            _Record(
                cost_usd=None,
                pricing_error="ValueError: kimi-k2-5: cache support unconfirmed",
                cache_read=15168,
            )
        )
        == []
    )


def test_an_unpriced_run_with_no_tokens_fails_because_it_can_never_be_repriced():
    """The one case where an unknown price IS fatal. Tokens are what make a
    run repriceable; without them the cost is lost permanently and no later
    rate table can recover it."""
    assert _live_problems(
        _Record(
            cost_usd=None,
            pricing_error="UnknownModelError: mystery-model",
            input_tokens=0,
            output_tokens=0,
        )
    )


def test_a_run_that_captured_no_wire_entries_fails():
    """Spec section 6.2 makes wire logging mandatory, and an isolated run
    with an empty wire log is the signature of capture being registered in
    the wrong process -- which shipped once already."""
    assert _live_problems(_Record(), wire_entries=0)


def test_lost_attribution_fails_even_when_the_wire_log_is_full():
    """Entries in unattributed.jsonl belong to a run nobody can name. The
    per-run file may still look complete while calls from this very run
    landed in the unattributed bucket."""
    assert _live_problems(_Record(), unattributed=1)


def test_a_loop_that_never_called_a_tool_fails():
    """A text-only reply proves the transport works and nothing else.
    Phase 0c exists to check that tool calls translate."""
    assert _live_problems(_Record(tools=0))


def test_an_empty_final_diff_fails():
    assert _live_problems(_Record(diff=""))


def test_a_non_isolated_run_fails():
    """isolated=False means the container had no route to the proxy, or had
    a route to everything. Either way section 5.1 did not hold."""
    assert _live_problems(_Record(isolated=False))


def test_an_excluded_run_fails():
    assert _live_problems(_Record(exclusion=object()))


def test_offline_mode_applies_the_full_criteria():
    """The stub speaks real streaming SSE and answers with a tool call, so
    nothing has to be waived offline.

    Pinned because the tempting simplification -- go back to a
    `mock_response` deployment -- silently costs the tool and diff checks,
    and would also stop exercising the streaming path that is the only one
    a real run uses.
    """
    from scripts.smoke_test import OFFLINE, run_problems

    assert OFFLINE.not_applicable == frozenset()
    assert _live_problems(_Record()) == []
    assert run_problems(
        _Record(tools=0),
        wire_entries=1,
        unattributed=0,
        config={
            "permissionMode": "bypassPermissions",
            "mcp_servers": [],
            "tools": ["Edit"],
        },
        expectations=OFFLINE,
    ), "offline mode must still require a tool call"


def test_a_waived_criterion_is_actually_waived_when_one_is_declared():
    """The waiver mechanism itself, since nothing currently uses it. If a
    future mode has to drop a check, it must drop that one and keep the
    rest."""
    from scripts.smoke_test import Expectations, run_problems

    waived = Expectations(name="partial", not_applicable=frozenset({"tool_calls"}))
    config = {
        "permissionMode": "bypassPermissions",
        "mcp_servers": [],
        "tools": ["Edit"],
    }

    assert (
        run_problems(
            _Record(tools=0), wire_entries=1, unattributed=0, config=config,
            expectations=waived,
        )
        == []
    )
    assert run_problems(
        _Record(tools=0, diff=""), wire_entries=1, unattributed=0, config=config,
        expectations=waived,
    ), "waiving tool_calls must not also waive the diff"


# --- run ordering, spec section 5.8 ------------------------------------------


def test_arm_major_ordering_runs_every_arm_back_to_back_with_itself():
    """The default, and the ordering every cost figure in TASKS.md came from.
    Named here so the confound is a documented property of the run rather than
    something a reader has to reconstruct from timestamps."""
    from scripts.smoke_test import adjacent_repeats, run_order

    order = run_order(["sonnet", "gemma"], repeats=3, interleave=False)

    assert order == [
        ("sonnet", 0), ("sonnet", 1), ("sonnet", 2),
        ("gemma", 0), ("gemma", 1), ("gemma", 2),
    ]
    assert adjacent_repeats(order) == ["sonnet", "sonnet", "gemma", "gemma"]


def test_interleaving_keeps_repeats_of_one_arm_apart():
    """Section 5.8's control: repeats of the same arm must never run
    back-to-back, and the spec says that must be verified rather than
    assumed -- so the check is a function, not a comment."""
    from scripts.smoke_test import adjacent_repeats, run_order

    order = run_order(["sonnet", "gemma", "kimi"], repeats=3, interleave=True)

    assert adjacent_repeats(order) == []
    assert [arm for arm, _ in order[:3]] == ["sonnet", "gemma", "kimi"]
    assert len(order) == 9


def test_interleaving_cannot_separate_a_single_arm():
    """One arm has nothing to interleave with, so the check must report the
    truth rather than the flag. A green line here would be the assumption
    section 5.8 exists to forbid."""
    from scripts.smoke_test import adjacent_repeats, run_order

    order = run_order(["sonnet"], repeats=3, interleave=True)
    assert adjacent_repeats(order) == ["sonnet", "sonnet"]


# --- the fixture -------------------------------------------------------------


def test_the_smoke_fixture_is_a_template_not_a_repository():
    """A committed .git inside the tree becomes an embedded-repo gitlink:
    git records a bare commit pointer and none of the files, so a fresh
    clone gets an empty directory and the smoke run has nothing to fix."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "smoke_task"

    assert (fixture / "calc.py").exists()
    assert not (fixture / ".git").exists()


def test_the_fixture_test_fails_against_the_seeded_bug():
    """If the seeded bug were ever fixed in the template, every arm would
    pass the task without editing anything and the gate would go green
    having measured nothing."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "smoke_task"
    namespace: dict = {}
    exec((fixture / "calc.py").read_text(), namespace)  # noqa: S102

    assert namespace["add"](2, 3) != 5
