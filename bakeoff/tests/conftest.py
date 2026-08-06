"""Fixtures for container integration tests.

The image ships git rather than installing it at runtime. RunContainer
creates containers with network_mode="none" (spec section 5.1), so
`apk add git` inside the container cannot work -- and a missing git makes
snapshot_diff return empty output, which is indistinguishable from a clean
tree. That failure mode passes tests while measuring nothing, so git has to
be baked into the image.

Bind mounts on macOS: the Docker VM mounts $HOME, not /var/folders, so a
repo under pytest's default tmp_path mounts as an EMPTY directory with no
error. Run integration tests with --basetemp under $HOME:

    pytest -m integration --basetemp="$HOME/.cache/bakeoff-pytest"

test_repo_is_actually_mounted guards this: if the mount is silently empty,
it fails loudly instead of letting the snapshot tests pass vacuously.
"""

import subprocess
import time
from pathlib import Path

import pytest

# Alpine-based and ships git, so `command -v git` short-circuits install_git
# and busybox wget is available for the network-isolation test.
FIXTURE_IMAGE = "alpine/git:latest"


def _digest_for(tag: str) -> str:
    subprocess.run(["docker", "pull", tag], check=True, capture_output=True)
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", tag],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture(scope="session")
def image_digest():
    return _digest_for(FIXTURE_IMAGE)


@pytest.fixture
def alpine_container(image_digest, tmp_path):
    from bakeoff.container import RunContainer

    (tmp_path / "repo").mkdir()
    with RunContainer(
        image=image_digest, repo_path=str(tmp_path / "repo"), base_sha=""
    ) as container:
        yield container


@pytest.fixture
def git_container(image_digest, tmp_path):
    from bakeoff.container import RunContainer

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")

    def run(*args):
        return subprocess.run(args, cwd=repo, check=True, capture_output=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "eval@pindrop.test")
    run("git", "config", "user.name", "eval")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "base")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with RunContainer(
        image=image_digest, repo_path=str(repo), base_sha=sha, install_git=True
    ) as container:
        yield container


# A stand-in for `claude` that speaks the same stream-json protocol on
# stdout. It exists to prove the checkpoint progression is real: against
# the post-hoc capture this replaces, every checkpoint held identical
# content, and a test that only counted checkpoints would have passed just
# as happily.
#
# The event order mirrors Claude Code's and that ordering is the point. An
# assistant message announces the tool calls a turn is ABOUT to make, so
# the file write comes after it, and the working tree at assistant message
# k still holds only k-1 turns of work. A fake that wrote before
# announcing would hide an off-by-one in turn numbering instead of
# catching it.
#
# The sleep between an assistant message and its file write stands in for
# real tool latency, and it is what makes this test deterministic.
#
# It also marks a real limitation rather than papering over one: a snapshot
# triggered by assistant message k+1 races with turn k+1's first tool call.
# A tool that writes faster than `git add -A` completes can land part of
# turn k+1 in turn k's checkpoint. Nothing outside the agent can close that
# window without pausing the agent, which the harness cannot do -- so the
# checkpoint boundary is approximate by a few hundred milliseconds, and the
# fake keeps that from turning into a flaky test about something else.
# It also writes a session transcript where Claude Code would, so the whole
# downstream chain runs for real: transcript discovery inside the
# container, parse_trajectory, cost_usd, scan_destructive, and the per-turn
# records. Without it trajectory_path is None and everything after the
# orchestrator is exercised only by unit tests with hand-built fixtures.
# When ANTHROPIC_BASE_URL is set it also makes a REAL HTTP call to the proxy,
# carrying ANTHROPIC_CUSTOM_HEADERS exactly as Claude Code does. Verified
# against claude 2.1.220 by pointing it at a local listener: the value lands
# on the wire verbatim as `X-Bakeoff-Run-Id: <id>` on every POST /v1/messages.
# Without a real request the proxy-side callback never fires, and the wire
# capture path would be tested only in the harness process -- which is the
# defect this exists to catch.
FAKE_AGENT = """#!/bin/sh
SESSION="${CLAUDE_CONFIG_DIR:-/tmp/cfg}/projects/-repo"
mkdir -p "$SESSION"
T="$SESSION/fake-session.jsonl"

if [ -n "$ANTHROPIC_BASE_URL" ]; then
  curl -s -o /dev/null -X POST "$ANTHROPIC_BASE_URL/v1/messages" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ANTHROPIC_AUTH_TOKEN:-none}" \
    -H "${ANTHROPIC_CUSTOM_HEADERS:-X-Unused: 1}" \
    -d "{\\"model\\":\\"${ANTHROPIC_MODEL:-mock-ok}\\",\\"max_tokens\\":64,\\"messages\\":[{\\"role\\":\\"user\\",\\"content\\":\\"hi\\"}]}" \
    || true
fi

echo '{"type":"system","subtype":"init","session_id":"fake"}'

echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Write"}]}}'
echo '{"type":"assistant","timestamp":"2026-08-06T00:00:00.000Z","version":"2.1.220","requestId":"req-1","message":{"model":"gemma-4-31b","stop_reason":"tool_use","usage":{"input_tokens":1200,"output_tokens":300},"content":[{"type":"tool_use","name":"Write","input":{}}]}}' >> "$T"
sleep 2
echo turn-one > /repo/first.txt
echo '{"type":"user","timestamp":"2026-08-06T00:00:02.000Z","toolUseResult":{"ok":true},"message":{"content":[{"type":"tool_result"}]}}' >> "$T"
echo '{"type":"user","message":{"content":[{"type":"tool_result"}]}}'

echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"}]}}'
echo '{"type":"assistant","timestamp":"2026-08-06T00:00:02.100Z","version":"2.1.220","requestId":"req-2","message":{"model":"gemma-4-31b","stop_reason":"end_turn","usage":{"input_tokens":1500,"output_tokens":420},"content":[{"type":"tool_use","name":"Bash","input":{"command":"rm -rf tests/test_a.py"}}]}}' >> "$T"
sleep 2
echo turn-two > /repo/second.txt
echo '{"type":"user","timestamp":"2026-08-06T00:00:04.100Z","toolUseResult":{"ok":true},"message":{"content":[{"type":"tool_result"}]}}' >> "$T"
echo '{"type":"user","message":{"content":[{"type":"tool_result"}]}}'

echo '{"type":"result","subtype":"success"}'
"""


@pytest.fixture
def agent_container(git_container):
    """git_container with a fake `claude` on the container's PATH."""
    git_container.exec(
        [
            "sh",
            "-c",
            f"cat > /usr/local/bin/claude <<'EOF'\n{FAKE_AGENT}EOF\n"
            "chmod +x /usr/local/bin/claude",
        ]
    )
    check = git_container.exec(["sh", "-c", "command -v claude"])
    assert check.exit_code == 0, "fake agent did not land on PATH"
    return git_container


@pytest.fixture(scope="session")
def agent_image(tmp_path_factory):
    """Build an image that ships the fake agent.

    execute_run creates its own container, so the agent has to be in the
    image rather than installed afterwards. Building it here keeps the
    production path free of a test-only injection hook -- the orchestrator
    runs exactly the code it will run in the eval.

    Referenced by image ID: a locally built image has no registry digest,
    and the ID pins the config and every layer.
    """
    context = tmp_path_factory.mktemp("agent-image")
    (context / "claude").write_text(FAKE_AGENT)
    (context / "Dockerfile").write_text(
        f"FROM {FIXTURE_IMAGE}\n"
        # curl is how the fake agent reaches the proxy. Installed at BUILD
        # time deliberately: during a run the container is on an internal
        # network with no route to a mirror, which is the point.
        "RUN apk add --no-cache curl\n"
        "COPY claude /usr/local/bin/claude\n"
        "RUN chmod +x /usr/local/bin/claude\n"
        "ENTRYPOINT []\n"
    )
    subprocess.run(
        ["docker", "build", "-q", "-t", "bakeoff-fake-agent:test", str(context)],
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", "bakeoff-fake-agent:test"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


PROXY_TAG = "bakeoff-litellm-proxy:test"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def proxy_image():
    """Build the LiteLLM proxy image once per session.

    Real image, real litellm, real callback dispatch. A mocked proxy would
    prove nothing here: the defect being covered is that LiteLLM's callback
    dispatch happens in the PROXY process, and only a real proxy process can
    demonstrate that the callback fires there.
    """
    subprocess.run(
        [
            "docker", "build", "-q",
            "-f", str(REPO_ROOT / "docker" / "litellm-proxy.Dockerfile"),
            "-t", PROXY_TAG, str(REPO_ROOT),
        ],
        check=True,
        capture_output=True,
    )
    return PROXY_TAG


@pytest.fixture
def fault_proxy(proxy_image, tmp_path):
    """An internal network carrying a real LiteLLM proxy that answers from
    mock_response.

    Yields (network_name, host_wire_dir). Every deployment is mocked, so
    this costs nothing and needs no credentials -- a gate that costs money
    is a gate that stops being run.
    """
    import docker

    client = docker.from_env()
    wire_dir = tmp_path / "wire"
    wire_dir.mkdir()

    network = client.networks.create(
        "bakeoff-fault-net", driver="bridge", internal=True
    )
    proxy = client.containers.run(
        proxy_image,
        command=[
            "--config", "/app/config/litellm_fault_injection.yaml",
            "--port", "4000", "--host", "0.0.0.0",
        ],
        name="litellm",
        network=network.name,
        volumes={
            str(REPO_ROOT / "src"): {"bind": "/app/src", "mode": "ro"},
            str(REPO_ROOT / "config"): {"bind": "/app/config", "mode": "ro"},
            str(wire_dir): {"bind": "/eval/wire", "mode": "rw"},
        },
        detach=True,
    )
    try:
        _wait_for_proxy(proxy)
        yield network.name, wire_dir
    finally:
        proxy.remove(force=True)
        network.remove()


def _wait_for_proxy(proxy, timeout_s: int = 90) -> None:
    """Block until the proxy answers.

    Probed from INSIDE the container: the network is internal, so there is
    no host route to poll. A fixed sleep would either flake or waste time on
    every run.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = proxy.exec_run(
            ["python", "-c",
             "import urllib.request;"
             "urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=2)"]
        )
        if result.exit_code == 0:
            return
        time.sleep(2)
    logs = proxy.logs(tail=40).decode("utf-8", errors="replace")
    raise RuntimeError(f"litellm proxy did not become ready:\n{logs}")


@pytest.fixture
def internal_network(image_digest):
    """An internal Docker network with a stand-in proxy on it.

    Spec section 5.1 allows the run to reach a recording proxy and nothing
    else. `internal=True` removes the route off the host, so the proxy --
    resolvable by container name through Docker's embedded DNS -- is the
    only endpoint that answers.
    """
    import docker

    client = docker.from_env()
    network = client.networks.create("bakeoff-test-net", driver="bridge", internal=True)
    proxy = client.containers.run(
        image_digest,
        entrypoint=["sh"],
        # busybox nc, not httpd -- alpine/git ships no httpd. -lk -e gives a
        # persistent listener that answers every connection.
        command=[
            "-c",
            "while true; do printf 'HTTP/1.0 200 OK\\r\\n\\r\\nPROXY-OK\\n' | nc -l -p 4000; done",
        ],
        name="litellm",
        network=network.name,
        detach=True,
    )
    try:
        yield network.name
    finally:
        proxy.remove(force=True)
        network.remove()
