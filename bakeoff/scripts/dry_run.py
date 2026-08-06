#!/usr/bin/env python3
"""End-to-end dry run of the harness. No credentials, no spend.

Executes the real orchestrator -- real container, real git pinning, real
live checkpoint capture, real event log -- against a stand-in agent that
speaks Claude Code's stream-json protocol and writes a session transcript
where Claude Code writes one.

What this DOES prove: every seam between Tasks 1-10 holds on real data,
and a complete, immutable, inspectable run record comes out the far end.

What this does NOT prove: that a real model completes a real task. Nothing
here calls Bedrock or the LiteLLM proxy. That is Phase 0c (Task 12), and
it needs the proxy running as a container on the eval's internal network.

Usage:
    python scripts/dry_run.py
    python scripts/dry_run.py --keep    # leave artifacts on disk to inspect
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from tests.conftest import FAKE_AGENT  # noqa: E402

BASE_IMAGE = "alpine/git:latest"
AGENT_TAG = "bakeoff-dry-run-agent:latest"


def sh(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def build_agent_image(workdir: Path) -> str:
    context = workdir / "image"
    context.mkdir()
    (context / "claude").write_text(FAKE_AGENT)
    (context / "Dockerfile").write_text(
        f"FROM {BASE_IMAGE}\n"
        "COPY claude /usr/local/bin/claude\n"
        "RUN chmod +x /usr/local/bin/claude\n"
        "ENTRYPOINT []\n"
    )
    sh("docker", "pull", BASE_IMAGE)
    sh("docker", "build", "-q", "-t", AGENT_TAG, str(context))
    # Image ID, not a tag: RunContainer refuses tags (spec section 5.1), and
    # a locally built image has no registry digest until it is pushed.
    return sh("docker", "inspect", "--format", "{{.Id}}", AGENT_TAG)


def build_repo(workdir: Path) -> tuple[Path, str]:
    repo = workdir / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (repo / "a.py").write_text("import nonexistent\n")
    sh("git", "init", "-q", cwd=repo)
    sh("git", "config", "user.email", "eval@pindrop.test", cwd=repo)
    sh("git", "config", "user.name", "eval", cwd=repo)
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "base", cwd=repo)
    return repo, sh("git", "rev-parse", "HEAD", cwd=repo)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep artifacts")
    args = parser.parse_args()

    from bakeoff.claude_runner import ClaudeCodeConfig
    from bakeoff.eventlog import EventLog
    from bakeoff.runner import TaskSpec, execute_run

    # Under $HOME: the Docker VM on macOS mounts $HOME but not /var/folders,
    # and a repo mounted from there appears inside the container as a
    # silently empty directory.
    workdir = Path(tempfile.mkdtemp(prefix="bakeoff-dry-run-", dir=Path.home()))
    print(f"workdir  {workdir}")

    try:
        print("building stand-in agent image ...")
        image = build_agent_image(workdir)
        print(f"image    {image[:19]}...")

        repo, base_sha = build_repo(workdir)
        print(f"base_sha {base_sha[:12]}")

        task = TaskSpec(
            task_id="dry-run-001",
            task_version=1,
            repo="pindrop/example",
            base_sha=base_sha,
            container_image_digest=image,
            prompt="Fix the failing import in a.py",
            test_paths=["tests/test_a.py"],
        )
        config = ClaudeCodeConfig(
            model="gemma-4-31b",
            base_url="http://litellm:4000",
            auth_token="not-used-in-a-dry-run",
            settings_path="/eval/eval_settings.json",
            config_dir="/eval/claude-config",
            max_turns=60,
            wall_clock_timeout_s=300,
        )
        log = EventLog(workdir / "eventlog")

        print("executing run ...")
        record = execute_run(
            task=task,
            model="gemma-4-31b",
            sample_index=0,
            config=config,
            event_log=log,
            repo_path=str(repo),
            artifacts_root=workdir / "artifacts",
        )

        print()
        print(f"run_id          {record.run_id}")
        print(f"schema_version  {record.schema_version}")
        print(f"outcome         {record.outcome.value} / {record.terminated_by.value}")
        # A structural class (loop, truncation, gave-up) is readable from the
        # transcript alone. Anything needing the test oracle stays None for
        # the offline grader -- see classify.classify_failure.
        klass = record.failure_class.value if record.failure_class else "None"
        note = "structural" if record.failure_class else "open for the offline grader"
        print(f"failure_class   {klass}   <- {note}")
        print(f"isolated        {record.isolated}   <- no proxy network passed")
        print(f"turns_used      {record.turns_used}")
        print(f"claude_code     {record.versions.claude_code or '(none)'}")
        print(f"tokens          in={record.tokens.input} out={record.tokens.output}")
        print(f"cost_usd        ${record.cost_usd:.6f}")
        print(
            f"timing          inference={record.time.inference_ms}ms "
            f"tool_exec={record.time.tool_exec_ms}ms "
            f"wall={record.time.wall_clock_total_ms}ms"
        )
        print(f"parse_error     {record.trajectory_parse_error or '(none)'}")

        print("\ncheckpoints (this is the part that used to be fabricated)")
        for cp in record.checkpoints:
            print(
                f"  turn {cp.turn}  elapsed={cp.elapsed_ms:>6}ms  "
                f"files={cp.files_touched}  tests_pass={cp.tests_pass}"
            )
        distinct = {cp.diff_vs_base for cp in record.checkpoints}
        print(f"  distinct diff contents: {len(distinct)} across {len(record.checkpoints)}")

        print("\ndestructive events")
        for event in record.destructive_events or []:
            print(
                f"  turn {event.turn}  {event.category.value}  "
                f"severity={event.severity.value}  reverted={event.reverted_by_agent}"
            )
        if not record.destructive_events:
            print("  (none)")

        print("\nimmutability")
        try:
            log.write_run(record)
            print("  FAIL: re-write was accepted")
            return 1
        except Exception as exc:
            print(f"  re-write refused: {type(exc).__name__}")

        reread = log.read_run(record.run_id)
        print(f"  round-trips through JSON identical: {reread == record}")

        print("\nartifacts")
        for path in sorted((workdir / "artifacts").rglob("*")):
            if path.is_file():
                print(f"  {path.relative_to(workdir)}  ({path.stat().st_size} bytes)")
        print(f"  {(workdir / 'eventlog' / 'index.jsonl').relative_to(workdir)}")
        print(
            "  index entry: "
            + json.dumps(
                json.loads((workdir / "eventlog" / "index.jsonl").read_text().strip())
            )
        )

        print("\nDRY RUN OK -- Tasks 1-10 hold end to end.")
        print("NOT covered: a real model call. That is Phase 0c (Task 12).")
        if args.keep:
            print(f"\nartifacts kept at {workdir}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
