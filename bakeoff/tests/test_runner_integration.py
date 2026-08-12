"""Live-capture tests. These need a Docker daemon.

The point of the suite is one claim: checkpoints record what the working
tree looked like AT each turn. Capturing after the run instead -- which is
what this replaces -- yields N checkpoints holding byte-identical content
under different turn numbers, a progression that never happened. A test
asserting only that N checkpoints exist passes on both.
"""

from pathlib import Path

import pytest

from bakeoff.checkpoints import CheckpointRecorder
from bakeoff.claude_runner import ClaudeCodeConfig, ClaudeCodeRunner, ContainerBackend

integration = pytest.mark.integration


def _config(config_dir: str) -> ClaudeCodeConfig:
    return ClaudeCodeConfig(
        model="gemma-4-31b",
        base_url="http://litellm:4000",
        auth_token="unused-by-the-fake-agent",
        settings_path="/eval/eval_settings.json",
        config_dir=config_dir,
        max_turns=10,
        wall_clock_timeout_s=60,
    )


@integration
def test_checkpoints_record_distinct_states_during_the_run(agent_container, tmp_path):
    recorder = CheckpointRecorder(agent_container, agent_container.base_sha)
    backend = ContainerBackend(agent_container, str(tmp_path / "cfg"))
    runner = ClaudeCodeRunner(_config("/tmp/cfg"), backend=backend)

    result = runner.run("do the thing", cwd="/repo", on_turn=recorder.maybe_capture)

    # Two assistant messages, so exactly one turn had COMPLETED while the
    # stream was live. The final turn's tools run after the last message and
    # are execute_run's force_capture to record.
    assert result.turns_streamed == 2, result.stdout
    assert len(recorder.captured) == 1

    only = recorder.captured[0]
    assert only.turn == 1
    assert only.files_touched == ["first.txt"], (
        "checkpoint holds the final tree -- capture is not happening live"
    )


@integration
def test_checkpoint_turn_numbers_match_the_stream(agent_container, tmp_path):
    """Turn numbers must come from the stream, not from a running count of
    captures. Numbering by count silently renumbers every checkpoint after
    a skipped capture, so a diff gets attributed to the wrong turn."""
    recorder = CheckpointRecorder(agent_container, agent_container.base_sha)
    backend = ContainerBackend(agent_container, str(tmp_path / "cfg"))
    runner = ClaudeCodeRunner(_config("/tmp/cfg"), backend=backend)

    runner.run("do the thing", cwd="/repo", on_turn=recorder.maybe_capture)

    # Labelled by turns COMPLETED, not by the index of the event that
    # arrived: at assistant message 2, one turn of work is on disk.
    assert [c.turn for c in recorder.captured] == [1]


@integration
def test_checkpoint_elapsed_times_are_measured(agent_container, tmp_path):
    """The post-hoc version passed elapsed_ms=0 for every intermediate
    capture, which makes the cost-at-budget-K curve unbuildable."""
    recorder = CheckpointRecorder(agent_container, agent_container.base_sha)
    backend = ContainerBackend(agent_container, str(tmp_path / "cfg"))
    runner = ClaudeCodeRunner(_config("/tmp/cfg"), backend=backend)

    runner.run("do the thing", cwd="/repo", on_turn=recorder.maybe_capture)

    elapsed = [c.elapsed_ms for c in recorder.captured]
    assert all(ms > 0 for ms in elapsed), elapsed
    assert elapsed == sorted(elapsed)


@integration
def test_config_dir_never_appears_in_a_checkpoint_diff(agent_container, tmp_path):
    """CLAUDE_CONFIG_DIR must be mounted outside the repo.

    Under /repo, `git add -A` would sweep the whole config tree -- settings,
    state, and every transcript -- into each checkpoint diff and into the
    final diff the grader reads.
    """
    agent_container.exec(["sh", "-c", "mkdir -p /eval/cfg && echo secret > /eval/cfg/state.json"])
    agent_container.exec(["sh", "-c", "echo real-work > /repo/edited.py"])

    diff, files = agent_container.snapshot_diff(agent_container.base_sha)

    assert files == ["edited.py"]
    assert "state.json" not in diff


@integration
def test_every_k_turns_skips_captures_without_renumbering(agent_container, tmp_path):
    recorder = CheckpointRecorder(
        agent_container, agent_container.base_sha, every_k_turns=2
    )
    backend = ContainerBackend(agent_container, str(tmp_path / "cfg"))
    runner = ClaudeCodeRunner(_config("/tmp/cfg"), backend=backend)

    runner.run("do the thing", cwd="/repo", on_turn=recorder.maybe_capture)

    assert [c.turn for c in recorder.captured] == []


@integration
def test_execute_run_writes_one_record_with_live_checkpoints(agent_image, tmp_path):
    """The whole orchestrator, end to end, against the fake agent.

    No credentials and no spend: the fake agent speaks stream-json but never
    calls a model. What this proves is the wiring -- container, live
    capture, wire logger, event log -- not model behaviour.
    """
    import subprocess

    from bakeoff.eventlog import EventLog
    from bakeoff.runner import TaskSpec, execute_run

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n")

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "eval@pindrop.test")
    git("config", "user.name", "eval")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    task = TaskSpec(
        task_id="t-live",
        task_version=1,
        repo="pindrop/example",
        base_sha=sha,
        container_image_digest=agent_image,
        prompt="do the thing",
        test_paths=[],
    )
    config = _config("/eval/claude-config")
    log = EventLog(tmp_path / "log")

    record = execute_run(
        task=task,
        model="gemma-4-31b",
        sample_index=0,
        config=config,
        event_log=log,
        repo_path=str(repo),
        artifacts_root=tmp_path / "artifacts",
    )

    assert log.list_runs() == [record.run_id]
    assert log.read_run(record.run_id) == record

    # One checkpoint per turn, no duplicates: turn 1 comes off the live
    # stream, turn 2 from the final capture, because the last turn's tools
    # run after the stream's last message.
    assert [c.turn for c in record.checkpoints] == [1, 2]
    assert record.checkpoints[0].files_touched == ["first.txt"]
    assert sorted(record.checkpoints[1].files_touched) == ["first.txt", "second.txt"]
    assert record.artifacts.final_diff == record.checkpoints[-1].diff_vs_base
    # No network was passed, so section 5.1 did not hold and the record has
    # to say so rather than let container_image_digest imply otherwise.
    assert record.isolated is False


@integration
def test_a_second_run_cannot_overwrite_the_first(agent_image, tmp_path):
    """Immutability is the whole point of the event log. Re-running the same
    (task, model, sample, attempt) must raise, not silently replace."""
    import subprocess

    from bakeoff.eventlog import EventLog, ImmutabilityError
    from bakeoff.runner import TaskSpec, execute_run

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "e@x"],
        ["config", "user.name", "e"],
        ["add", "-A"],
        ["commit", "-q", "-m", "base"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    task = TaskSpec(
        task_id="t-dup", task_version=1, repo="r", base_sha=sha,
        container_image_digest=agent_image, prompt="go", test_paths=[],
    )
    log = EventLog(tmp_path / "log")
    kwargs = dict(
        task=task, model="gemma-4-31b", sample_index=0, config=_config("/eval/claude-config"),
        event_log=log, repo_path=str(repo),
    )

    execute_run(artifacts_root=tmp_path / "a1", **kwargs)
    with pytest.raises(ImmutabilityError):
        execute_run(artifacts_root=tmp_path / "a2", **kwargs)


@integration
def test_reference_image_ships_the_pinned_agent_and_its_dependencies(tmp_path):
    """The reference image is the worked example every task image copies.

    Each of these is a runtime dependency with no fallback: the run has no
    route to a package mirror, so anything missing here cannot be installed
    later -- it just fails mid-run as though the model had.

    Skipped rather than built inline; `make eval-image` is a minute-long
    build and this asserts a contract, not a build.
    """
    import subprocess

    from bakeoff.container import RunContainer

    probe = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", "bakeoff-eval-agent"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("bakeoff-eval-agent not built; see docker/eval-agent.Dockerfile")

    (tmp_path / "repo").mkdir()
    with RunContainer(
        image=probe.stdout.strip(), repo_path=str(tmp_path / "repo"), base_sha=""
    ) as container:
        version = container.exec(["claude", "--version"])
        assert version.exit_code == 0, version.stderr
        assert version.stdout.startswith("2.1.220"), (
            f"image ships {version.stdout!r}; versions.claude_code would be wrong"
        )

        # pytest belongs on this list for the same reason the others do, and
        # its absence was not caught for the whole of Phase 0c: a missing
        # test runner does not fail the run, it just removes the agent's
        # only way to check its own work. Every arm then gets scored on one
        # unverified guess. Both spellings, because a model reaches for
        # either and only one of them is a console script.
        for binary in ("rg", "git", "timeout", "pytest"):
            found = container.exec(["sh", "-c", f"command -v {binary}"])
            assert found.exit_code == 0, f"{binary} missing from the eval image"

        module = container.exec(["python", "-m", "pytest", "--version"])
        assert module.exit_code == 0, module.stderr


@integration
def test_the_smoke_fixture_goes_red_then_green_inside_the_reference_image(tmp_path):
    """The offline half of this lives in test_smoke_fixture.py; this is the
    half only a real image can answer -- that the runner and the fixture's
    pytest.ini actually meet inside the container the eval runs in.

    `python3 tests/test_calc.py`, the command the image used to leave as the
    only option, raised ModuleNotFoundError both before and after a correct
    fix. That is what Gemma's 30 identical turns were.
    """
    import shutil
    import subprocess

    from bakeoff.container import RunContainer

    probe = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", "bakeoff-eval-agent"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("bakeoff-eval-agent not built; see docker/eval-agent.Dockerfile")

    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "smoke_task"
    repo = tmp_path / "repo"
    shutil.copytree(fixture, repo)

    with RunContainer(
        image=probe.stdout.strip(), repo_path=str(repo), base_sha=""
    ) as container:
        red = container.exec(["python", "-m", "pytest", "-q"])
        assert red.exit_code != 0, "fixture passes with the bug still in it"
        assert "assert -1 == 5" in red.stdout, red.stdout + red.stderr
        assert "ModuleNotFoundError" not in red.stdout + red.stderr

        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")

        green = container.exec(["python", "-m", "pytest", "-q"])
        assert green.exit_code == 0, green.stdout + green.stderr
        assert "1 passed" in green.stdout, green.stdout


@integration
def test_full_chain_runs_from_transcript_to_record(agent_image, tmp_path):
    """Everything downstream of the orchestrator, on real data.

    The fake agent writes a session transcript where Claude Code writes
    one, so this exercises in-container transcript discovery,
    parse_trajectory, cost_usd, scan_destructive and the per-turn records
    -- none of which run end to end when trajectory_path is None.
    """
    import subprocess

    from bakeoff.eventlog import EventLog
    from bakeoff.runner import TaskSpec, execute_run

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    for args in (
        ["init", "-q"], ["config", "user.email", "e@x"], ["config", "user.name", "e"],
        ["add", "-A"], ["commit", "-q", "-m", "base"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    record = execute_run(
        task=TaskSpec(
            task_id="t-chain", task_version=1, repo="r", base_sha=sha,
            container_image_digest=agent_image, prompt="go",
            test_paths=["tests/test_a.py"],
        ),
        model="gemma-4-31b",
        sample_index=0,
        config=_config("/eval/claude-config"),
        event_log=EventLog(tmp_path / "log"),
        repo_path=str(repo),
        artifacts_root=tmp_path / "artifacts",
    )

    assert not record.trajectory_parse_error, record.trajectory_parse_error
    assert record.turns_used == 2
    assert record.versions.claude_code == "2.1.220"

    # Real token accounting through the price book, not a hand-built stub.
    assert record.tokens.input == 2700
    assert record.tokens.output == 720
    assert record.cost_usd == pytest.approx(2700 * 0.14 / 1e6 + 720 * 0.40 / 1e6)

    # The transcript's Bash turn deletes a declared test path.
    assert [e.category.value for e in record.destructive_events] == ["test_deletion"]

    # Timing is split, not lumped: the parser reads tool execution off the
    # tool-result timestamps rather than leaving it at zero.
    assert record.time.inference_ms > 0
    assert record.time.tool_exec_ms > 0


@integration
def test_a_broken_container_still_writes_a_record(agent_image, tmp_path):
    """Spec section 6.6. A base_sha that does not exist makes the checkout
    fail inside the container; the run is lost but the record must not be.
    """
    import subprocess

    from bakeoff.eventlog import EventLog
    from bakeoff.runner import TaskSpec, execute_run
    from bakeoff.schema import Outcome, TerminationReason

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q"], ["config", "user.email", "e@x"], ["config", "user.name", "e"],
        ["add", "-A"], ["commit", "-q", "-m", "base"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    log = EventLog(tmp_path / "log")
    record = execute_run(
        task=TaskSpec(
            task_id="t-crash", task_version=1, repo="r",
            base_sha="0" * 40,  # no such commit
            container_image_digest=agent_image, prompt="go", test_paths=[],
        ),
        model="gemma-4-31b",
        sample_index=0,
        config=_config("/eval/claude-config"),
        event_log=log,
        repo_path=str(repo),
        artifacts_root=tmp_path / "artifacts",
    )

    assert record.outcome == Outcome.CRASHED
    assert record.terminated_by == TerminationReason.CRASH
    assert record.exclusion.reason_code == "container_crashed"
    assert log.read_run(record.run_id) == record
