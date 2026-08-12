# Reference eval task image (spec section 5.1).
#
# Real task images are the dataset plan's job -- each one pins a repo, its
# lockfile dependencies, and its toolchain. This file exists so the harness
# is testable end to end and so those builds have a worked example of the
# TWO parts that are the same for every task: getting a pinned Claude Code
# into the image, and giving the agent a way to run the task's tests.
#
# THE SECOND ONE IS NOT OPTIONAL. Spec section 3.3 measures a loop -- the
# agent reads, edits, runs tests, sees failures, and self-corrects without
# human input. An image with no test runner truncates that loop after
# "edits", and every arm is then scored on a single unverified guess. This
# is not hypothetical: the image shipped without pytest through all of
# Phase 0c, and Gemma spent 30 of its 30 turns re-running
# `python3 tests/test_calc.py`, which under this image raised
# ModuleNotFoundError whether or not the bug had been fixed. Its 9/9 failure
# was read as capability. A task image that cannot turn its own fixture
# green is measuring the image.
#
# Build:
#   docker build -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent .
#
# Then pin by digest, never by tag, when handing it to RunContainer:
#   docker inspect --format '{{.Id}}' bakeoff-eval-agent
#
# The agent runs INSIDE this image with no route off the host except the
# LiteLLM proxy, so anything it needs at run time must be here already --
# `apt-get install` during a run cannot reach a mirror, by design.

FROM python:3.12-slim-bookworm

# ripgrep is a Claude Code runtime dependency; git is what snapshot_diff
# and the base_sha checkout need. `timeout` (coreutils) enforces the
# wall-clock budget from inside the container, because Docker offers no way
# to kill a running exec from outside.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        curl \
        git \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# The test runner, pinned for the same reason CLAUDE_CODE_VERSION is: it is
# part of the environment every arm is compared in, so a floating version
# would be an unrecorded difference between two runs the log swears were
# identical. Installed system-wide, before the USER switch, so `pytest` and
# `python -m pytest` both resolve for the eval user without a per-run
# install -- which could not reach a mirror anyway (see above).
ARG PYTEST_VERSION=9.1.1
RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}" \
    && installed="$(pytest --version)" \
    && case "$installed" in \
         *"${PYTEST_VERSION}"*) echo "pytest ${PYTEST_VERSION} pinned" ;; \
         *) echo "expected pytest ${PYTEST_VERSION}, got ${installed}" >&2; exit 1 ;; \
       esac

# Pinned deliberately. This value becomes versions.claude_code for every run
# built from this image, and the harness compares arms on the assumption
# that all of them ran the same agent. Bump it in one place, rebuild, and
# the new digest makes the change visible in every record.
ARG CLAUDE_CODE_VERSION=2.1.220

# NOT root, and this is load-bearing rather than hygiene. Claude Code
# refuses bypassPermissions under root -- "cannot be used with root/sudo
# privileges for security reasons" -- and exits before emitting a single
# stream-json event. `--allow-dangerously-skip-permissions` does not lift
# the guard either; only a non-root uid does. Spec section 5.2 pins
# bypassPermissions because a `-p` session that has to ask for permission
# auto-denies, so the agent would make no edits at all and every arm would
# be scored as having failed the task.
#
# Mount points are created and chowned here: the bind-mounted repo appears
# as 0:0 through the macOS VM's virtiofs, which ignores ownership, but a
# Linux host honours it and the agent must be able to write to /repo.
RUN useradd --create-home --uid 1000 eval \
    && mkdir -p /repo /eval/claude-config \
    && chown -R eval:eval /repo /eval

USER eval
ENV HOME=/home/eval \
    PATH=/home/eval/.local/bin:$PATH

# Installed as the user that will run it: the installer writes into $HOME,
# and a binary under /root is unreadable at mode 700 by anyone else.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"

# Set AFTER the install, not before. The installer goes through the same
# update machinery these variables disable, so declaring them earlier makes
# the build print "Updates are disabled by your administrator" and leave no
# binary behind -- while still exiting 0, so the failure surfaces several
# steps later as `claude: not found`.
ENV CLAUDE_CONFIG_DIR=/eval/claude-config \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    DISABLE_TELEMETRY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Fail the BUILD if the installed version is not the pinned one. Without
# this the image silently ships whatever the installer resolved, and the
# drift would only surface as an unexplained behaviour change between two
# arms that the record swears were identical.
RUN installed="$(claude --version)" \
    && case "$installed" in \
         "${CLAUDE_CODE_VERSION} "*) echo "claude ${CLAUDE_CODE_VERSION} pinned" ;; \
         *) echo "expected ${CLAUDE_CODE_VERSION}, got ${installed}" >&2; exit 1 ;; \
       esac

# /repo and /eval/claude-config are created above, before the USER switch,
# so they can be chowned. claude-config is mounted from the host per run and
# must NOT live under /repo -- `git add -A` would sweep the whole config
# tree, and every transcript in it, into each checkpoint diff.
WORKDIR /repo

# RunContainer overrides this with `sleep infinity` and drives the container
# through exec, but an image-declared ENTRYPOINT would otherwise prefix that
# command. Cleared so the override behaves the same way for every task image.
ENTRYPOINT []
