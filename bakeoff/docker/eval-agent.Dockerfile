# Reference eval task image (spec section 5.1).
#
# Real task images are the dataset plan's job -- each one pins a repo, its
# lockfile dependencies, and its toolchain. This file exists so the harness
# is testable end to end and so those builds have a worked example of the
# one part that is the same for every task: getting a pinned Claude Code
# into the image.
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

# Pinned deliberately. This value becomes versions.claude_code for every run
# built from this image, and the harness compares arms on the assumption
# that all of them ran the same agent. Bump it in one place, rebuild, and
# the new digest makes the change visible in every record.
ARG CLAUDE_CODE_VERSION=2.1.220

ENV PATH=/root/.local/bin:$PATH

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

# Mount points. claude-config is mounted from the host per run and must NOT
# live under /repo -- `git add -A` would sweep the whole config tree, and
# every transcript in it, into each checkpoint diff.
RUN mkdir -p /repo /eval/claude-config
WORKDIR /repo

# RunContainer overrides this with `sleep infinity` and drives the container
# through exec, but an image-declared ENTRYPOINT would otherwise prefix that
# command. Cleared so the override behaves the same way for every task image.
ENTRYPOINT []
