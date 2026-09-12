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
# The DRIVERS build these. A hand build passes no BAKEOFF_BASE_DOCKERFILE_SHA,
# so the image stamps an empty sha and the next driver run rebuilds it.
#
# Then pin by digest, never by tag, when handing it to RunContainer:
#   docker inspect --format '{{.Id}}' bakeoff-eval-agent
#
# The agent runs INSIDE this image with no route off the host except the
# LiteLLM proxy, so anything it needs at run time must be here already --
# `apt-get install` during a run cannot reach a mirror, by design.

# The interpreter, per task. `image.python` in a manifest selects which of
# these bases the drivers build and hand to `build_task_image`; every ARM of
# a task runs the same one, so this is not a section 5.4 divergence -- what
# that section holds identical is the environment two arms are compared in.
#
# NOT named PYTHON_VERSION, and the name is load-bearing. Measured 2026-09-01
# (Docker 29.5.2, legacy builder): the official python: images set their own
# `ENV PYTHON_VERSION` -- 3.11.16, 3.12.13, 3.13.15 -- and ENV beats ARG, so
# after this FROM the expansion resolves to the base image's PATCH level and
# not to the build arg, whether or not the ARG is redeclared:
#
#   after-FROM without redeclare: [3.11.16]
#   after-FROM WITH redeclare:    [3.11.16]
#
# Nothing below expands it, so today that is latent. A later edit that wants
# to -- a version-conditional pip line, an ENV, broadening 7's node install --
# would read a plausible wrong value with no error. Under this name the
# redeclaration behaves: `[3.11] vs image ENV [3.11.16]`.
#
# The default matches `bakeoff.tasks._DEFAULT_PYTHON` and is pinned equal by
# tests/test_images.py. Measured: with no --build-arg this file built to the
# byte-identical image id it built before the ARG existed
# (sha256:dfd2cc069bad6346465ec1ecfe0a704faac3cb0e86ddbcfbc424b60f9380915a), so
# the ARG itself moved no stored record's container_image_digest and no cached
# preflight verdict. THAT ID HELD UNTIL 2026-09-03, when the bakeoff.base.*
# LABEL block at the end of this file moved it once, deliberately -- see that
# block, and the "one-time costs" note in
# docs/superpowers/plans/2026-09-03-round2-13-base-rebuild-and-dead-code.md.
ARG BASE_PYTHON_VERSION=3.12
FROM python:${BASE_PYTHON_VERSION}-slim-bookworm

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
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    # CPython invalidates a .pyc on (source mtime in whole seconds, source
    # size). An edit that changes neither is invisible, and BOTH halves of that
    # are ordinary here: an operator swap, an off-by-one or a boolean flip
    # preserves byte count, and an agent edits and re-runs the suite inside the
    # same second. Measured 2026-08-13 in this image: fix applied, source
    # correct on disk, pytest still red, and the pyc header confirming
    # `mtime=1786605617 size=32` on both sides.
    #
    # Spec section 3.3 measures a loop that ends in "runs tests, sees failures,
    # self-corrects". A stale pyc feeds that loop the OLD behaviour after a
    # correct fix, so the agent corrects away from the right answer -- and it
    # is scored as capability. It already made `verify_logger.py`, the section
    # 6.6 gate, fail on 2 of 3 consecutive runs.
    #
    # Never `-B` on one runner: the agent runs its own commands and this has to
    # hold for every process in the container, including the ones it invents.
    PYTHONDONTWRITEBYTECODE=1

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

# WHAT THIS IMAGE SAYS ABOUT ITSELF, so a driver can tell a base it already has
# from a tag that merely resolves. The tag is local and mutable: `docker tag`
# moves it, an earlier Dockerfile leaves a stale one behind, and a cache-hit
# `docker build -t` silently retags the right image back on top of a wrong one
# -- measured 2026-09-02 at 0.04 s, which is how the base-tag mutability probe's
# own mutation was undone before preflight could see it.
#
# LAST IN THE FILE ON PURPOSE. `LABEL` invalidates every instruction below it,
# so higher up this would re-run apt, pip and the installer on any fingerprint
# change instead of reusing the cached layers.
#
# THE `ARG` REDECLARE BELOW IS LOAD-BEARING. Measured 2026-09-02: without it
# `${BASE_PYTHON_VERSION}` expands to the EMPTY STRING here, with no warning --
# and an empty version can never match, so the driver would rebuild this base on
# every invocation forever and nothing would say why.
#
# `${BASE_PYTHON_VERSION}` and not `${PYTHON_VERSION}`, for the reason the ARG
# comment at the top of this file gives: the official image sets its own
# `ENV PYTHON_VERSION` and ENV beats a redeclared ARG, so the other name would
# stamp the PATCH level here. Measured 2026-09-02: this label reads "3.13", the
# image's ENV reads "3.13.15".
#
# THE LABEL IS NOT A GATE. It decides whether the driver rebuilds; whether the
# task may RUN is decided by preflight's `python --version` read-back inside the
# container, which is never skipped and never compared against this value. Both
# are recorded -- `base_image_labels` beside `python_observed` -- because a claim
# the build stamped and an answer the interpreter gave are different kinds of
# fact and a disagreement between them is the finding.
#
# It describes ANCESTRY, not identity: docker propagates these keys into every
# derived image, so a task image built FROM this base claims to be it. See
# `images.base_is_current`.
ARG BASE_PYTHON_VERSION
ARG BAKEOFF_BASE_DOCKERFILE_SHA=""
LABEL bakeoff.base.runtime="python" \
      bakeoff.base.version="${BASE_PYTHON_VERSION}" \
      bakeoff.base.dockerfile_sha="${BAKEOFF_BASE_DOCKERFILE_SHA}"
