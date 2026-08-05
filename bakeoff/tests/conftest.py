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
