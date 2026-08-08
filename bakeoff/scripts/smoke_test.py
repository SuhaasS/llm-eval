#!/usr/bin/env python3
"""Spec Phase 0c -- the end-to-end go/no-go gate.

One trivial task, one run, each arm, through the real Claude Code binary in
the pinned container, through a real LiteLLM proxy container, to a real
model. A model that cannot complete a loop, emit tool calls and land a diff
here will not work at scale, and that has to surface on day one rather than
in week three after the dataset is built.

Two modes, because the two halves have different prerequisites:

    --mode offline   mock deployment, no credentials, no spend. Proves the
                     topology, the loop, header attribution, wire capture
                     and the section 5.2 config dump. Cannot prove tool
                     translation or a diff -- and says so rather than
                     quietly passing a weaker gate under the same name.

    --mode live      real Bedrock, real spend. Full criteria.

Run scripts/verify_logger.py first. A smoke run that spends money on a
broken logger wastes both.

Usage:
    python scripts/smoke_test.py --mode offline
    python scripts/smoke_test.py --mode live
    python scripts/smoke_test.py --mode live --models claude-sonnet-5
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

AGENT_TAG = "bakeoff-eval-agent:smoke"
PROXY_TAG = "bakeoff-litellm-proxy:smoke"

# The four arms of the eval. NOT one transport: Sonnet 5 runs on
# bedrock-runtime and the three candidates on bedrock-mantle.
#
# That split is not a preference. The mantle passthrough derives
# `anthropic-beta` HTTP headers from Claude Code's context_management and
# output_config and mantle rejects them, so the mantle Sonnet arm 400s on
# every call with "invalid beta flag" -- measured twice on 2026-08-07, and
# additional_drop_params cannot reach a header. bedrock-runtime carries beta
# values as a request-body field instead and completes the task.
#
# The cost is a section 6.4 confound on the reference arm, tracked in
# TASKS.md: Sonnet is not transport-identical to the arms it is the reference
# for. `claude-sonnet-5` stays reachable through --models as the control that
# demonstrates the difference, but it does not earn a slot in the default set.
EVAL_ARMS = [
    "claude-sonnet-5-runtime",
    "gemma-4-31b",
    "nemotron-3-super-120b",
    "kimi-k2-5",
]

# Phase 0c's exit criterion. One run per arm structurally cannot see a failure
# RATE, only a failure -- and the two 2026-08-07 runs proved the point: Kimi
# failed both times and in two different ways, so either run alone would have
# read as one deterministic bug. GO requires every repeat of every arm to pass;
# the per-arm rates are reported either way, because "which arm, how often, in
# what way" is the output Phase 3 sizing actually needs.
LIVE_REPEATS = 3

# The offline gate stays at one. The stub replies from a fixed script, so
# repeats are byte-identical and would only triple verify_logger's runtime.
OFFLINE_REPEATS = 1

PROMPT = (
    "The test in tests/test_calc.py fails. Fix the bug in calc.py so that it "
    "passes. Do not modify the test."
)


# --- what the run has to demonstrate ----------------------------------------


@dataclass(frozen=True)
class Expectations:
    """Which criteria apply, and which are honestly out of reach.

    `not_applicable` is printed, never silently skipped. A gate that drops
    checks without saying which ones reads exactly like the full gate to
    anyone scanning the output, which is how a weaker check becomes a
    claimed guarantee.
    """

    name: str
    not_applicable: frozenset[str] = field(default_factory=frozenset)
    why: str = ""


LIVE = Expectations(name="live")

# Nothing is dropped offline. That is a property of the stub, not a claim
# about the gate: fixtures/anthropic_stub.py speaks real streaming SSE and
# answers with a tool call, so tool translation and the diff are both
# reachable. A `mock_response` deployment could reach neither, and would
# have needed them marked not-applicable here.
#
# What offline still cannot show is anything about a MODEL: the stub's reply
# is a fixed script. Only --mode live covers that, and no criterion here
# pretends otherwise.
OFFLINE = Expectations(name="offline")


def run_problems(
    record: Any,
    wire_entries: int,
    unattributed: int,
    config: dict,
    expectations: Expectations,
) -> list[str]:
    """Every reason this run is not evidence. Empty means GO."""
    problems: list[str] = []

    # First, because it zeroes everything downstream: the derived fields are
    # empty and must not be read as "the agent did nothing".
    if record.trajectory_parse_error:
        problems.append(f"trajectory did not parse: {record.trajectory_parse_error}")

    # An unpriced run is NOT a failure. The three candidates raise on any
    # observed cache token (their Bedrock cache pricing is unconfirmed), and
    # the loop, the tool calls and the diff are all still evidence -- only the
    # price is missing. This used to arrive as trajectory_parse_error, which
    # zeroed the record; now the tokens survive and the run is repriceable
    # offline, so it is reported rather than gated.
    #
    # It becomes a real problem only when the tokens are ALSO empty, because
    # then nothing can reprice it and the cost is lost permanently.
    if record.cost_usd is None:
        repriceable = record.tokens.input or record.tokens.output or (
            record.tokens.cache_read or record.tokens.cache_write
        )
        if not repriceable:
            problems.append(
                f"cost unknown AND no tokens recorded: {record.pricing_error} "
                "-- this run can never be repriced"
            )

    if record.turns_used <= 0:
        problems.append("no turns: the agent never completed a loop")

    if "tool_calls" not in expectations.not_applicable and record.tool_calls.total <= 0:
        problems.append("no tool calls: transport works, tool translation unproven")

    # Spec section 6.2. An isolated run with an empty wire log is the
    # signature of capture registered in the wrong process, which shipped
    # once already.
    if wire_entries <= 0:
        problems.append("wire log is empty: section 6.2 capture is dead")
    if unattributed:
        problems.append(
            f"{unattributed} call(s) landed in unattributed.jsonl: attribution lost"
        )

    if "final_diff" not in expectations.not_applicable and not (
        record.artifacts.final_diff or ""
    ):
        problems.append("no diff: nothing was submitted")

    if not record.isolated:
        problems.append("isolated=False: section 5.1 did not hold")
    if record.exclusion is not None:
        problems.append("run was excluded as an infra or adapter failure")

    problems.extend(config_problems(config))
    return problems


# --- spec section 5.2: what the session actually loaded ----------------------


def effective_config(stream: Path) -> dict:
    """The stream-json init event, or {} if there is not one.

    Read from the agent's STDOUT, not from the session transcript. The
    transcript records messages; the init event carrying the effective
    model, tools, MCP servers and permission mode is emitted only on the
    stream and is nowhere on disk otherwise. Verified against claude
    2.1.220 -- a transcript from a completed run contains queue-operation,
    user, attachment, assistant and last-prompt records, and no init.

    Section 5.2 requires dumping and diffing the effective config at session
    start. This is the only place the harness can see what the session
    LOADED rather than what it was told to load -- and the difference is
    the whole point, since Claude Code does not fail on a --settings path
    that does not exist.
    """
    try:
        lines = stream.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event
    return {}


def config_problems(config: dict) -> list[str]:
    """Section 5.2 checks against the dump.

    An empty dump is a failure, never a pass. Treating "nothing observed" as
    "nothing wrong" would turn the one check that can catch an unloaded
    settings file into a check that always succeeds.
    """
    if not config:
        problems = ["no init event: cannot verify what the session loaded"]
        return problems

    problems = []
    mode = config.get("permissionMode")
    if mode != "bypassPermissions":
        # The tell for a --settings path that does not exist. Without the
        # settings the agent cannot edit anything, so every arm lands no
        # diff and a harness bug reads as four capability findings.
        problems.append(
            f"permissionMode is {mode!r}, not 'bypassPermissions': "
            "the settings file did not load"
        )
    if config.get("mcp_servers"):
        problems.append(
            f"{len(config['mcp_servers'])} MCP server(s) loaded despite "
            "--strict-mcp-config: this arm had tools the others did not"
        )
    if not config.get("tools"):
        problems.append("no tools in the init event: the agent had nothing to call")
    return problems


def config_differences(configs: dict[str, dict]) -> list[str]:
    """Section 5.2's diff half: identical across arms except the model.

    Two arms configured differently are not comparable, and the comparison
    is the entire deliverable.
    """
    compared = ("permissionMode", "mcp_servers", "tools", "slash_commands")
    baseline_arm, baseline = next(iter(configs.items()))
    differences = []
    for arm, config in configs.items():
        for key in compared:
            if config.get(key) != baseline.get(key):
                differences.append(
                    f"{arm}.{key} differs from {baseline_arm}: "
                    f"{config.get(key)!r} vs {baseline.get(key)!r}"
                )
    return differences


# --- the fixture -------------------------------------------------------------


def build_smoke_repo(workdir: Path) -> tuple[Path, str]:
    """Copy the fixture template and make it a real repo.

    base_sha is derived from the commit made here rather than passed in: a
    hand-recorded SHA goes stale the first time the fixture changes, and
    `git checkout --detach <stale sha>` is a plain exec whose failure the
    orchestrator does not raise on. The run would proceed on whatever state
    the tree happened to be in.
    """
    repo = workdir / "repo"
    shutil.copytree(REPO / "fixtures" / "smoke_task", repo)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "eval@pindrop.test"],
        ["config", "user.name", "eval"],
        ["add", "-A"],
        ["commit", "-q", "-m", "base: add() has a sign bug"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, sha


# --- images ------------------------------------------------------------------


def build_images() -> str:
    """Build both images; return the agent image ID.

    An ID, not a tag: RunContainer refuses tags (section 5.1) and a locally
    built image has no registry digest until it is pushed.
    """
    for tag, dockerfile in (
        (AGENT_TAG, "docker/eval-agent.Dockerfile"),
        (PROXY_TAG, "docker/litellm-proxy.Dockerfile"),
    ):
        subprocess.run(
            ["docker", "build", "-q", "-f", str(REPO / dockerfile), "-t", tag, str(REPO)],
            check=True,
            capture_output=True,
        )
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", AGENT_TAG],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


# --- the proxy ---------------------------------------------------------------


class Proxy:
    """The LiteLLM proxy container, on two networks.

    The agent must be isolated (section 5.1) and the proxy must reach
    Bedrock. Those are incompatible on one network: `internal=True` removes
    the external route by definition. So the agent joins the internal
    network only, and the proxy joins both -- it is the recording hop, and
    the only thing on the eval network with a way out.

        agent --[internal]-- proxy --[egress]-- Bedrock

    Names are unique per invocation. A leftover `litellm` container from a
    crashed integration run would otherwise satisfy the healthcheck and
    answer with the wrong config.
    """

    def __init__(
        self,
        config_name: str,
        wire_dir: Path,
        env: dict[str, str],
        tag: str,
        with_stub: bool = False,
    ):
        self.config_name = config_name
        self.wire_dir = wire_dir
        self.env = env
        self.tag = tag
        self.with_stub = with_stub
        self.stub = None
        self.internal_name = f"bakeoff-smoke-internal-{tag}"
        self.egress_name = f"bakeoff-smoke-egress-{tag}"
        self.container = None
        self.internal = None
        self.egress = None

    def __enter__(self):
        import docker

        client = docker.from_env()
        self.wire_dir.mkdir(parents=True, exist_ok=True)
        self.internal = client.networks.create(
            self.internal_name, driver="bridge", internal=True
        )
        self.egress = client.networks.create(self.egress_name, driver="bridge")
        if self.with_stub:
            # On the internal network only. It stands in for Bedrock, so if
            # it could be reached any other way the offline gate would stop
            # proving that the agent's only route is through the proxy.
            self.stub = client.containers.run(
                PROXY_TAG,
                entrypoint=["python", "/app/fixtures/anthropic_stub.py"],
                command=[],
                name=f"stub-{self.tag}",
                network=self.internal_name,
                volumes={
                    str(REPO / "fixtures"): {"bind": "/app/fixtures", "mode": "ro"}
                },
                detach=True,
            )
            self.internal.disconnect(self.stub)
            self.internal.connect(self.stub, aliases=["stub"])
        self.container = client.containers.run(
            PROXY_TAG,
            command=[
                "--config",
                f"/app/config/{self.config_name}",
                "--port",
                "4000",
                "--host",
                "0.0.0.0",
            ],
            # The hostname the agent resolves through Docker's embedded DNS.
            # base_url is http://litellm:4000 for exactly this reason: an
            # internal network has no host route, so 127.0.0.1 is the
            # agent's own container.
            name=f"litellm-{self.tag}",
            hostname="litellm",
            network=self.internal_name,
            environment=self.env,
            volumes={
                str(REPO / "src"): {"bind": "/app/src", "mode": "ro"},
                str(REPO / "config"): {"bind": "/app/config", "mode": "ro"},
                str(self.wire_dir): {"bind": "/eval/wire", "mode": "rw"},
            },
            detach=True,
        )
        # Aliased so the name stays `litellm` on the internal network even
        # though the container name is unique.
        self.internal.disconnect(self.container)
        self.internal.connect(self.container, aliases=["litellm"])
        self.egress.connect(self.container)
        self._wait()
        return self

    def _wait(self, timeout_s: int = 120) -> None:
        """Probe from INSIDE the container: the internal network has no host
        route, so there is nothing to curl from here."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.container.reload()
            if self.container.status != "running":
                raise RuntimeError(
                    "proxy exited before serving:\n"
                    + self.container.logs(tail=40).decode("utf-8", "replace")
                )
            probe = self.container.exec_run(
                [
                    "python",
                    "-c",
                    "import urllib.request;"
                    "urllib.request.urlopen("
                    "'http://127.0.0.1:4000/health/liveliness', timeout=2)",
                ]
            )
            if probe.exit_code == 0:
                return
            time.sleep(2)
        raise RuntimeError(
            "proxy did not become ready:\n"
            + self.container.logs(tail=40).decode("utf-8", "replace")
        )

    def logs(self, tail: int = 40) -> str:
        if self.container is None:
            return ""
        return self.container.logs(tail=tail).decode("utf-8", "replace")

    def request_count(self) -> int:
        """POSTs the proxy actually served, counted from its own log.

        The independent check on capture: every call the proxy answered must
        appear in the wire log or in unattributed.jsonl. A call that lands in
        neither is silently missing evidence, and section 6.2 makes the wire
        log the record of what went over the wire -- an incomplete one is
        worse than an absent one, because it looks complete.
        """
        return self.logs(tail=10000).count("POST /v1/messages")

    def __exit__(self, *_exc):
        # Kept unconditionally: on a teardown after a failure this is the
        # only account of what the proxy saw.
        try:
            (self.wire_dir.parent / "proxy.log").write_text(self.logs(tail=10000))
        except Exception:  # noqa: BLE001
            pass
        # Containers first, and networks only after. A network with an
        # attached container refuses to go, and Network.remove() takes no
        # `force` -- passing one raises TypeError, which a bare except then
        # swallows, leaking a network per invocation until the daemon runs
        # out of address space.
        for container in (self.container, self.stub):
            if container is None:
                continue
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass
        for network in (self.internal, self.egress):
            if network is None:
                continue
            for _ in range(10):
                try:
                    network.remove()
                    break
                except Exception:  # noqa: BLE001 - container removal is async
                    time.sleep(1)
        return False


SSO_LOGIN_HINT = (
    "  AWS_CONFIG_FILE=bakeoff/.aws/config aws sso login --profile pindrop-bakeoff"
)


def freeze_sigv4_credentials(region: str) -> dict[str, str]:
    """Resolve the ambient AWS session into literal keys for the container.

    The bedrock-runtime arms sign SigV4, and the proxy container cannot
    resolve an SSO profile itself: it has no ~/.aws, no SSO cache and no
    browser to re-authenticate with. Freezing on the host and passing the
    triple in is the only way those arms reach Bedrock at all.

    These are temporary STS credentials and inherit the SSO session's expiry,
    the same hours-not-days horizon as the mantle token -- the unattended
    multi-day run in TASKS.md P1 needs a refresh path that does not exist yet,
    for both transports rather than just one.
    """
    import boto3

    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        return {}
    frozen = credentials.get_frozen_credentials()
    env = {
        "AWS_ACCESS_KEY_ID": frozen.access_key,
        "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
        # Region twice on purpose: LiteLLM reads AWS_REGION_NAME, botocore
        # reads AWS_DEFAULT_REGION, and a deployment without aws_region_name
        # would otherwise sign against a region it guessed.
        "AWS_REGION_NAME": region,
        "AWS_DEFAULT_REGION": region,
    }
    if frozen.token:
        env["AWS_SESSION_TOKEN"] = frozen.token
    return env


def proxy_environment(mode: str) -> dict[str, str]:
    """Credentials for the proxy container. Live mode only.

    The agent is passed none of these and never sees them: it reaches
    Bedrock through the proxy over HTTP, which is what makes both credentials
    single-hop secrets.

    Both transports are provisioned, because Sonnet 5 is served over
    bedrock-runtime while the three candidates are served over
    bedrock-mantle (see config/litellm_config.yaml). The mantle token is
    passed as BAKEOFF_MANTLE_TOKEN and NOT as AWS_BEARER_TOKEN_BEDROCK:
    LiteLLM's bedrock/ handler falls back to the AWS-named variable when a
    deployment has no api_key, so setting it here would bearer-authenticate
    every runtime arm and fail them with `bedrock:CallWithBearerToken`.
    """
    if mode != "live":
        return {}

    import os

    from scripts.smoke_bedrock import (
        ENV_FILE,
        LITELLM_BEARER_ENV,
        MANTLE_ENV,
        derive_mantle_token,
        load_env_file,
        normalize_mantle_token,
        resolve_aws_paths,
        scrub_placeholders,
    )

    load_env_file(ENV_FILE)
    scrub_placeholders()
    resolve_aws_paths()
    normalize_mantle_token()

    region = os.environ.get("AWS_REGION_NAME") or "us-east-1"

    token = os.environ.get(MANTLE_ENV, "")
    if not token:
        # Minted from the current SSO session, in memory, never written to
        # disk. A long-lived key in .env would outlive the run that needed
        # it and sit there with no expiry anyone tracks.
        token = derive_mantle_token(region)
        if token:
            print(f"mantle    derived short-term bearer token (len {len(token)})")
    if not token:
        raise SystemExit("No mantle credential. Run:\n" + SSO_LOGIN_HINT)

    sigv4 = freeze_sigv4_credentials(region)
    if not sigv4:
        raise SystemExit(
            "No SigV4 credentials -- every bedrock-runtime arm would fail.\n"
            + SSO_LOGIN_HINT
        )
    print(f"sigv4     froze session credentials for the proxy ({region})")

    environment = {MANTLE_ENV: token, **sigv4}
    # Belt and braces: the container inherits nothing from this process, but
    # an image or compose layer that ever sets the AWS-named variable would
    # silently move the runtime arms onto bearer auth.
    assert LITELLM_BEARER_ENV not in environment
    return environment


# --- the run -----------------------------------------------------------------


def run_arm(
    arm: str,
    image: str,
    workdir: Path,
    wire_dir: Path,
    event_log,
    network: str,
    max_turns: int,
    timeout_s: int,
    repeat: int = 0,
) -> tuple[Any, dict, int]:
    """One run of one arm. `repeat` indexes runs of the same arm.

    It has to reach two places, and both are load-bearing:

      the workdir     each repeat builds its own repo. Sharing one would let
                      repeat 2 start from repeat 1's dirty tree and report a
                      diff its own agent never made.

      sample_index    run_id is a hash of (task, model, sample, attempt) and
                      write_run opens mode "x", so a second repeat at
                      sample_index=0 raises ImmutabilityError out of
                      execute_run rather than overwriting -- correct, and
                      fatal to the matrix if the index does not move.
    """
    from bakeoff.claude_runner import ClaudeCodeConfig
    from bakeoff.proxy_callback import unattributed_count
    from bakeoff.runner import TaskSpec, execute_run

    arm_dir = workdir / f"{arm}-{repeat}"
    repo, base_sha = build_smoke_repo(arm_dir)

    task = TaskSpec(
        task_id="smoke-001",
        task_version=1,
        repo="fixtures/smoke_task",
        base_sha=base_sha,
        container_image_digest=image,
        prompt=PROMPT,
        test_paths=["tests/test_calc.py"],
    )
    config = ClaudeCodeConfig(
        model=arm,
        base_url="http://litellm:4000",
        # The proxy has no master_key, so any value passes. Sent anyway:
        # Claude Code requires one, and an empty token changes the request
        # shape rather than the authorization result.
        auth_token="sk-eval-local",
        settings_path="/eval/eval_settings.json",
        config_dir="/eval/claude-config",
        max_turns=max_turns,
        wall_clock_timeout_s=timeout_s,
    )

    before = unattributed_count(wire_dir)
    record = execute_run(
        task=task,
        model=arm,
        sample_index=repeat,
        config=config,
        event_log=event_log,
        repo_path=str(repo),
        artifacts_root=arm_dir / "artifacts",
        network=network,
        proxy_wire_dir=wire_dir,
        settings_host_path=REPO / "config" / "eval_settings.json",
    )

    config_dump = {}
    if record.artifacts.container_stdout:
        config_dump = effective_config(Path(record.artifacts.container_stdout))
    (arm_dir / "artifacts" / "effective_config.json").write_text(
        json.dumps(config_dump, indent=2, sort_keys=True)
    )

    # Delta, not absolute: unattributed.jsonl is shared across arms, so a
    # later arm would inherit an earlier arm's failure and every arm after
    # the first would fail for someone else's reason.
    return record, config_dump, unattributed_count(wire_dir) - before


def wire_entry_count(record) -> int:
    import gzip

    path = Path(record.artifacts.wire_log_gz)
    if not path.exists():
        return 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


# --- output ------------------------------------------------------------------


def print_table(rows: list[dict]) -> None:
    header = (
        f"{'run':30} {'outcome':18} {'turns':>5} {'tools':>6} {'wire':>5} "
        f"{'out_tok':>8} {'cache_r':>8} {'cache_w':>8} {'cost$':>9} "
        f"{'wall_s':>7} {'diff_b':>7}  verdict"
    )
    print("\n" + header)
    print("-" * len(header))
    for row in rows:
        # cost is None when the arm's tokens could not be priced. A float
        # format spec raises TypeError on that, and this table prints AFTER
        # the whole matrix has been paid for -- the arm that triggers it would
        # take the entire report down with it.
        cost = "  unpriced" if row["cost"] is None else f"{row['cost']:>9.5f}"
        print(
            f"{row['label']:30} {row['outcome']:18} {row['turns']:>5} "
            f"{row['tools']:>6} {row['wire']:>5} {row['out_tok']:>8} "
            f"{row['cache_read']:>8} {row['cache_write']:>8} "
            f"{cost} {row['wall_s']:>7.1f} {row['diff_b']:>7}  "
            f"{row['verdict']}"
        )


def print_rates(rows: list[dict], repeats: int) -> None:
    """Per-arm pass rate and the distinct ways each arm failed.

    The reason N=3 exists. A summed failure count would have reported Kimi's
    two 2026-08-07 runs as "2 failures" when they were two *different*
    failures -- one mid-stream death, one clean exit that never called the
    edit tool -- and those need different fixes. Grouping by run_problems'
    returned reasons keeps them apart without inventing a second taxonomy.
    """
    if repeats <= 1:
        return
    print(f"\nper-arm rates over {repeats} run(s)")
    for arm in dict.fromkeys(row["arm"] for row in rows):
        arm_rows = [row for row in rows if row["arm"] == arm]
        passed = sum(1 for row in arm_rows if not row["problems"])
        print(f"  {arm:26} passed {passed}/{len(arm_rows)}")
        modes: dict[str, int] = {}
        for row in arm_rows:
            if row["problems"]:
                modes["; ".join(row["problems"])] = (
                    modes.get("; ".join(row["problems"]), 0) + 1
                )
        for mode, count in sorted(modes.items(), key=lambda item: -item[1]):
            print(f"    {count}x  {mode}")


def print_cache_tokens(rows: list[dict]) -> None:
    """Whether an arm returns cache tokens at all, stated rather than assumed.

    costs.py prices cache tokens for Sonnet and raises for the three
    candidates, whose Bedrock cache pricing is unconfirmed. That trip used to
    zero the whole record; parse_trajectory now keeps the turns and tokens and
    records the cost as unknown, so the remaining job of this line is to say
    which arms are affected -- and therefore which arms need a rate before any
    cost figure is published.

    Measured on 2026-08-07: the cache warms on turn 2 of a single run (not at
    N=10, as TASKS.md assumed), and Kimi does return cache tokens once it runs
    a real multi-turn loop. The earlier reading that the candidates returned
    none came from runs that died at turn 2.
    """
    print("\ncache tokens (costs.py has no confirmed rate for any candidate)")
    for arm in dict.fromkeys(row["arm"] for row in rows):
        arm_rows = [row for row in rows if row["arm"] == arm]
        reads = sum(row["cache_read"] for row in arm_rows)
        writes = sum(row["cache_write"] for row in arm_rows)
        priced = arm.startswith("claude-sonnet-5")
        if reads or writes:
            note = (
                "priced"
                if priced
                else "UNPRICED -- cost recorded as null, tokens intact"
            )
        else:
            note = "none returned"
        print(f"  {arm:26} read={reads:<9} write={writes:<9} {note}")


def print_sampling(rows: list[dict]) -> None:
    """Task 11 carried this here, and it is a finding either way.

    Section 5.3 puts sampling entirely in proxy config because Claude Code
    has no temperature flag. If the configured value does not appear on the
    wire, three arms ran at unspecified sampling with nothing in the record
    saying so -- and config/litellm_config.yaml's per-arm values are
    decorative. Reported, never gated: a dropped temperature is a finding,
    not a reason to call the run invalid.
    """
    print("\nsampling as it appears on the wire (spec section 5.3)")
    # One line per ARM, not per run: sampling comes from proxy config, which
    # does not vary between repeats of the same arm.
    seen: set[str] = set()
    for row in rows:
        if row["arm"] in seen:
            continue
        seen.add(row["arm"])
        sampling = row["sampling"] or {}
        temperature = sampling.get("temperature")
        shown = "absent" if temperature is None else repr(temperature)
        expected = "none by API constraint" if row["arm"].startswith(
            "claude-sonnet-5"
        ) else "1.0 configured"
        print(
            f"  {row['arm']:26} temperature={shown:<8} "
            f"max_output_tokens={sampling.get('max_output_tokens')}   "
            f"<- {expected}"
        )


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], required=True)
    parser.add_argument(
        "--models", help="comma-separated subset of the arms (live mode)"
    )
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument(
        "--repeats",
        type=int,
        default=None,
        help=f"runs per arm (default {LIVE_REPEATS} live, {OFFLINE_REPEATS} offline). "
        "GO requires every repeat of every arm to pass.",
    )
    parser.add_argument("--keep", action="store_true", help="keep artifacts on disk")
    args = parser.parse_args()

    from bakeoff.eventlog import EventLog

    expectations = LIVE if args.mode == "live" else OFFLINE
    if args.mode == "live":
        config_name = "litellm_config.yaml"
        arms = args.models.split(",") if args.models else list(EVAL_ARMS)
    else:
        config_name = "litellm_smoke_offline.yaml"
        # Two arms, not one. The second configures a temperature, which is
        # how the section 5.3 question gets answered offline: if a
        # deployment-level temperature does not reach the wire, three arms
        # of the real eval run at unspecified sampling with nothing in the
        # record saying so. It also gives the section 5.2 cross-arm diff
        # something to compare.
        arms = args.models.split(",") if args.models else ["claude-sonnet-5", "gemma-4-31b"]

    # Under $HOME, never /var/folders: the Docker VM on macOS mounts $HOME
    # only, and a repo bind-mounted from elsewhere appears inside the
    # container as a silently EMPTY directory -- the run then edits nothing
    # and reports it as the model's failure.
    stamp = subprocess.run(
        ["date", "-u", "+%Y%m%dT%H%M%SZ"], check=True, capture_output=True, text=True
    ).stdout.strip()
    workdir = Path.home() / ".cache" / "bakeoff-smoke" / stamp
    workdir.mkdir(parents=True)
    wire_dir = workdir / "wire"

    repeats = args.repeats
    if repeats is None:
        repeats = LIVE_REPEATS if args.mode == "live" else OFFLINE_REPEATS
    if repeats < 1:
        raise SystemExit("--repeats must be at least 1")

    print(f"mode      {args.mode}  ({config_name})")
    print(f"arms      {', '.join(arms)}")
    print(f"repeats   {repeats} per arm ({len(arms) * repeats} runs)")
    print(f"workdir   {workdir}")
    if expectations.not_applicable:
        print(
            f"NOT CHECKED in this mode: {', '.join(sorted(expectations.not_applicable))}"
            f" -- {expectations.why}"
        )

    environment = proxy_environment(args.mode)
    print("building images ...")
    image = build_images()
    print(f"image     {image[:19]}...")

    # One event log for the invocation, under a fresh timestamped root:
    # run_id is a hash of (task, model, sample, attempt), and write_run
    # opens "x", so re-running into a previous root raises ImmutabilityError
    # out of execute_run.
    event_log = EventLog(workdir / "eventlog")

    rows: list[dict] = []
    configs: dict[str, dict] = {}
    failures: dict[str, list[str]] = {}

    with Proxy(
        config_name, wire_dir, environment, stamp, with_stub=args.mode == "offline"
    ) as proxy:
        # Arm-major: all repeats of one arm before moving on. Keeps a NO-GO
        # arm's runs adjacent in the output, and keeps the repeats of one arm
        # close together in time so a rate is not confounded by drift in
        # Bedrock-side load across the whole matrix.
        for arm in arms:
            for repeat in range(repeats):
                label = arm if repeats == 1 else f"{arm}/{repeat + 1}"
                print(f"\nrunning {label} ...")
                record, config_dump, unattributed = run_arm(
                    arm,
                    image,
                    workdir,
                    wire_dir,
                    event_log,
                    proxy.internal_name,
                    args.max_turns,
                    args.timeout_s,
                    repeat=repeat,
                )
                entries = wire_entry_count(record)
                problems = run_problems(
                    record,
                    wire_entries=entries,
                    unattributed=unattributed,
                    config=config_dump,
                    expectations=expectations,
                )
                if problems:
                    failures[label] = problems
                # One representative per arm: section 5.2 compares arms
                # against each other, and repeats of one arm are configured
                # identically by construction.
                configs.setdefault(arm, config_dump)
                rows.append(
                    {
                        "arm": arm,
                        "label": label,
                        "outcome": record.outcome.value,
                        "turns": record.turns_used,
                        "tools": record.tool_calls.total,
                        "wire": entries,
                        "out_tok": record.tokens.output,
                        "cache_read": record.tokens.cache_read,
                        "cache_write": record.tokens.cache_write,
                        "cost": record.cost_usd,
                        "wall_s": record.time.wall_clock_total_ms / 1000,
                        "diff_b": len(record.artifacts.final_diff or ""),
                        "unattributed": unattributed,
                        "sampling": record.sampling,
                        "problems": problems,
                        "verdict": "NO-GO" if problems else "go",
                        "wire_log": record.artifacts.wire_log_gz,
                    }
                )

        if not rows:
            print("\nno arms ran")
            return 1

        # Cross-check capture against the proxy's own access log. Counting
        # only the entries we captured cannot detect a call we failed to
        # capture -- the wire log would simply be short, and short is
        # indistinguishable from quiet.
        served = proxy.request_count()
        captured = sum(row["wire"] for row in rows) + sum(
            row["unattributed"] for row in rows
        )
        if served != captured:
            failures.setdefault("wire capture", []).append(
                f"the proxy served {served} POST /v1/messages but {captured} "
                "were recorded: some calls were captured nowhere"
            )

        print_table(rows)
        print_rates(rows, repeats)
        print_cache_tokens(rows)
        print_sampling(rows)

        differences = config_differences(configs) if len(configs) > 1 else []
        print("\neffective config across arms (spec section 5.2)")
        if differences:
            for difference in differences:
                print(f"  DIFFERS: {difference}")
        else:
            print("  identical on permissionMode, mcp_servers, tools")

        if failures or differences:
            print("\nNO-GO")
            for label, problems in failures.items():
                print(f"  {label}")
                for problem in problems:
                    print(f"    - {problem}")
                wire = next(
                    (r["wire_log"] for r in rows if r.get("label") == label), None
                )
                if wire:
                    print(f"    wire log: {wire}")
            if differences:
                print("  arms were not configured identically; see above")
            print(
                "\nRead the wire log before concluding a model is at fault: "
                "spec section 6.4 requires telling adapter failure from model "
                "failure, and that distinction is what this gate exists to "
                "surface."
            )
            print("\nproxy logs (tail):")
            print(proxy.logs())
            return 1

    print(
        f"\nGO ({expectations.name} criteria): every arm completed a loop "
        f"on all {repeats} of {repeats} run(s)."
    )
    if repeats < LIVE_REPEATS and args.mode == "live":
        # Said out loud, because a GO at N=1 reads exactly like a GO at N=3
        # to anyone scanning the output -- and the whole reason this knob
        # exists is that N=1 cannot see a rate.
        print(
            f"WEAKER THAN THE PHASE 0c CRITERION: {repeats} run(s) per arm, "
            f"not {LIVE_REPEATS}. A failure rate below roughly "
            f"1-in-{repeats + 1} is invisible at this N."
        )
    if expectations.not_applicable:
        print(
            f"NOT checked in this mode: {', '.join(sorted(expectations.not_applicable))}"
        )
    if args.mode == "offline":
        print(
            "This is the OFFLINE gate: the topology, the loop, tool "
            "translation, capture and the section 5.2 dump are all real, but "
            "the replies came from a fixed script. It says nothing about any "
            "model. Only --mode live does."
        )
    else:
        print("Use wall_s and out_tok above to size parallelism for OPEN-3.")
    if args.keep:
        print(f"artifacts kept at {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
