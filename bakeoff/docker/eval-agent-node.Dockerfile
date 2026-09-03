# The node eval base image (spec section 5.1), the sibling of
# eval-agent.Dockerfile. Broadening 7.
#
# TWO FILES RATHER THAN ONE WITH A SWITCHED `FROM`, on three measured
# differences that are not parameters:
#
#   node:22-bookworm-slim already ships a `node` user at uid 1000, so a
#   `useradd --create-home` at that uid fails here with exit 4 (measured
#   2026-09-01) where it succeeds on python:3.12-slim-bookworm. A shell
#   conditional around a useradd is exactly the shape that half-succeeds and
#   leaves an image running as root -- which Claude Code refuses, emitting zero
#   stream-json events, on every arm of every task built from it.
#
#   The test runners are installed OUTSIDE the repo scaffold, which has no
#   python analogue.
#
#   PYTHONDONTWRITEBYTECODE has no analogue at all (see below).
#
# What must stay identical to the python file -- the pinned Claude Code and its
# build-time assertion, git, ripgrep, coreutils, the non-root eval user at uid
# 1000, the CLAUDE_CONFIG_DIR/DISABLE_* block, WORKDIR /repo, ENTRYPOINT [] --
# is asserted by tests/test_images.py, which reads both files.
#
# Build:
#   docker build -f docker/eval-agent-node.Dockerfile \
#     --build-arg BASE_NODE_VERSION=22 -t bakeoff-eval-agent:base-node-22 .
#
# The DRIVERS build these. A hand build passes no BAKEOFF_BASE_DOCKERFILE_SHA,
# so the image stamps an empty sha and the next driver run rebuilds it.

# BASE_NODE_VERSION, never NODE_VERSION. The official node: images set their
# own `ENV NODE_VERSION` (measured 22.23.2), and ENV beats a redeclared ARG
# after FROM -- so a later edit that wanted the version after the FROM would
# silently read the patch-level string instead of the build arg, and the value
# would look plausible.
ARG BASE_NODE_VERSION=22
FROM node:${BASE_NODE_VERSION}-bookworm-slim

# ripgrep is a Claude Code runtime dependency; git is what snapshot_diff and
# the base_sha checkout need; `timeout` (coreutils) enforces the wall-clock
# budget from inside the container, because Docker offers no way to kill a
# running exec from outside.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        curl \
        git \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# THE TEST RUNNERS, INSTALLED AT THE CONTAINER ROOT AND NOT UNDER THE REPO.
#
# `/repo` in a built task image is a scaffold that the bind mount REPLACES at
# run time. For python that is survivable because `pip install -e .` puts the
# package in site-packages; node has no site-packages, and a tree installed
# under the scaffold is simply gone once the mount lands -- measured
# 2026-09-01: "node_modules PRESENT" without the mount, "node_modules GONE"
# with it.
#
# Installing at `/` works because node's resolver walks up from the importing
# file, looking for a `node_modules` at each level: for /repo/tests/x.test.js
# that is /repo/tests, then /repo, then `/` -- where this install put one.
# Measured as uid 1000: vitest and jest both run, a test file's import of a
# transitive dependency resolves, and a /repo/vitest.config.js is still
# discovered. The legacy module-search environment variable is deliberately
# left unset (the plan's D4) -- it was not needed, it is ignored by ESM
# resolution and by both frameworks' own resolvers, and setting it would hide
# which mechanism actually worked.
#
# Not writable by the eval user (measured: `Permission denied`), so the agent
# cannot corrupt the runner it is being measured with.
#
# Pinned for the reason CLAUDE_CODE_VERSION is: these are part of the
# environment every arm is compared in, so a floating version would be an
# unrecorded difference between two runs the log swears were identical.
# `/package.json` is written FIRST and the install runs with `cd /` and NO
# `--prefix`. Measured 2026-09-02: `npm install --prefix / <pkgs>` into a tree
# that does not exist yet fails outright --
#
#     npm error Tracker "idealTree" already exists          (exit 1)
#
# -- from any cwd. `--prefix` is fine once the tree at `/` is there, which is
# why a task's `image.build` keeps it.
#
# The runners land under `dependencies`, NOT under dev ones (measured:
# `deps: {"jest":"^30.5.0","vitest":"^3.2.7"}  dev: undefined`), and that is
# the coupling the whole scheme rests on: a task's later
# `npm install --prefix / --omit=dev <its deps>` does not prune them. Do not
# "tidy" this into a dev-dependency install -- `--omit=dev` would then delete
# the runners on the first task that installs anything, and the failure
# reaches the model as exit 127 on every arm.
#
# BOTH RUNNERS ARE ASSERTED FROM THEIR INSTALLED package.json, NOT FROM
# `--version`. For jest that is forced rather than stylistic. Measured
# 2026-09-02 on a clean node:22-bookworm-slim: an
# `npm install jest@30.5.0` that resolves jest, jest-cli and @jest/core all to
# 30.5.0 still answers
#
#     $ /node_modules/.bin/jest --version
#     30.4.2
#
# because jest's CLI prints `@jest/core`'s `getVersion()`, and that bundle
# carries an INLINED `module.exports = {"version":"30.4.2"}` -- a stale string
# in the published 30.5.0 artifact. So the CLI's answer is not the version of
# anything that is installed, and pinning against it would either fail this
# build forever or force BAKEOFF_JEST_VERSION to a number no package.json in
# the image agrees with. The manifest version is what npm resolved and what
# `--omit=dev` would prune, so it is what both this assertion and the task
# image's re-assertion (the plan's D15) compare.
#
# `vitest --version` DOES answer 3.2.7 and could have been matched directly.
# It is read the same way anyway, because two probes answering one question in
# two shapes is how one of them quietly stops being checked: a reader who sees
# the vitest half comparing `--version` has no reason to believe the jest half
# is doing something else on purpose. Each `--version` call is kept with its
# output discarded, because it still proves the shim EXECUTES; the `command -v`
# half of that claim is what tests/test_images.py probes.
ARG VITEST_VERSION=3.2.7
ARG JEST_VERSION=30.5.0
RUN printf '{"name":"bakeoff-runners","private":true}\n' > /package.json \
    && cd / \
    && npm install --no-audit --no-fund \
        "vitest@${VITEST_VERSION}" "jest@${JEST_VERSION}" \
    && v="$(node -p 'require("/node_modules/vitest/package.json").version')" \
    && /node_modules/.bin/vitest --version >/dev/null \
    && case "$v" in \
         "${VITEST_VERSION}") echo "vitest ${VITEST_VERSION} pinned" ;; \
         *) echo "expected vitest ${VITEST_VERSION}, got ${v}" >&2; exit 1 ;; \
       esac \
    && j="$(node -p 'require("/node_modules/jest/package.json").version')" \
    && /node_modules/.bin/jest --version >/dev/null \
    && case "$j" in \
         "${JEST_VERSION}") echo "jest ${JEST_VERSION} pinned" ;; \
         *) echo "expected jest ${JEST_VERSION}, got ${j}" >&2; exit 1 ;; \
       esac

# Exported so the GENERATED task Dockerfile can re-assert them after its own
# image.build steps (the plan's D15) without a second copy of the numbers in
# images.py -- two copies of a pin is how one moves. The check is needed
# because a task's dependency install can remove these: `npm ci`'s documented
# contract is to delete the installed tree before installing, and at the `/`
# prefix that is exactly where the runners live. Whether it fires depends on
# which package.json/package-lock.json pair npm resolves for the given prefix
# and cwd, which an image.build line decides by accident -- so HARVESTING
# recommends `npm install --prefix / --omit=dev` (measured additive) and this
# pair of variables is what makes the recommendation enforceable.
ENV BAKEOFF_VITEST_VERSION=${VITEST_VERSION} \
    BAKEOFF_JEST_VERSION=${JEST_VERSION}

# Pinned deliberately, and IDENTICAL to eval-agent.Dockerfile's. This value
# becomes versions.claude_code for every run built from this image, and the
# harness compares arms on the assumption that all of them ran the same agent.
# Nothing in a record compares it ACROSS tasks, so two bases carrying two
# values would be invisible -- run_matrix.assert_one_agent is what refuses it.
ARG CLAUDE_CODE_VERSION=2.1.220

# The `node` user occupies uid 1000 on this base (measured:
# `node:x:1000:1000::/home/node:/bin/bash`), so it is removed first. uid 1000
# is not cosmetic: it must match eval-agent.Dockerfile's, because the
# bind-mounted repo's ownership is honoured on a Linux host.
#
# NOT root, and this is load-bearing rather than hygiene: Claude Code refuses
# bypassPermissions under root and exits before emitting a single stream-json
# event.
RUN userdel -r node \
    && useradd --create-home --uid 1000 eval \
    && mkdir -p /repo /eval/claude-config \
    && chown -R eval:eval /repo /eval

USER eval
# /node_modules/.bin on PATH, and this is a measurement rather than a
# convenience. The install above puts the shims there and changes no
# environment: measured 2026-09-02, `command -v vitest` finds nothing on the
# default PATH, so a bare `vitest` is exit 127. `npx vitest` and `npm test`
# both work, because npx and npm resolve `.bin` themselves -- and section 3.3
# measures a loop run with commands THE AGENT INVENTS, so leaving the three
# spellings inconsistent means the agent discovers the rule by failing, inside
# the turn budget it is being scored on.
ENV HOME=/home/eval \
    PATH=/node_modules/.bin:/home/eval/.local/bin:$PATH

# Installed as the user that will run it: the installer writes into $HOME, and
# a binary under /root is unreadable at mode 700 by anyone else.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"

# Set AFTER the install: the installer goes through the same update machinery
# these disable, so declaring them earlier makes the build print "Updates are
# disabled by your administrator", leave no binary behind, and still exit 0.
#
# THERE IS NO PYTHONDONTWRITEBYTECODE ANALOGUE HERE, and that is a measurement
# rather than an omission. CPython invalidates a .pyc on (source mtime in whole
# seconds, source size), and an agent's edit routinely preserves both -- so a
# correct fix is served the OLD behaviour and section 3.3's self-correction
# loop corrects away from the right answer. Measured 2026-09-01 on both node
# frameworks: overwrite a source with the fix at the SAME byte count inside the
# SAME second, re-run, and both go green. vite's `.vite` directory is its
# DEPENDENCY optimiser cache, not a source-transform cache, and jest's cache is
# content-hash keyed. The `--no-cache` that vitest tasks carry in tests.runner
# is there so the suite writes nothing into the TREE (section 5.6 stages
# everything), not for staleness -- do not delete it as redundant with a
# staleness problem that was never the problem.
ENV CLAUDE_CONFIG_DIR=/eval/claude-config \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    DISABLE_TELEMETRY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Fail the BUILD if the installed version is not the pinned one. Without this
# the image silently ships whatever the installer resolved, and the drift would
# only surface as an unexplained behaviour change between two arms that the
# record swears were identical.
RUN installed="$(claude --version)" \
    && case "$installed" in \
         "${CLAUDE_CODE_VERSION} "*) echo "claude ${CLAUDE_CODE_VERSION} pinned" ;; \
         *) echo "expected ${CLAUDE_CODE_VERSION}, got ${installed}" >&2; exit 1 ;; \
       esac

WORKDIR /repo

# RunContainer overrides this with `sleep infinity` and drives the container
# through exec; an image-declared ENTRYPOINT would prefix that command.
ENTRYPOINT []

# What this image says about itself. See eval-agent.Dockerfile's copy of this
# block for why it is last, why the `ARG` must be redeclared here (without it
# the label stamps the empty string, silently), why the ARG name carries the
# BASE_ prefix (`ENV NODE_VERSION=22.23.2` on this base, measured; this label
# reads "22"), and why it is not a gate.
ARG BASE_NODE_VERSION
ARG BAKEOFF_BASE_DOCKERFILE_SHA=""
LABEL bakeoff.base.runtime="node" \
      bakeoff.base.version="${BASE_NODE_VERSION}" \
      bakeoff.base.dockerfile_sha="${BAKEOFF_BASE_DOCKERFILE_SHA}"
