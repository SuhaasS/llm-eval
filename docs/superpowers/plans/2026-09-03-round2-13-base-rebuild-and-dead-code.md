# Round 2, items 13 and 15 — the base image says what it is, and two dead pieces go

**Revision 2** (review 1 folded in; see "Review 1 → changes" at the end).

**One plan, one commit.** Both items are `images.py` housekeeping and they touch
the same three functions, so splitting them would mean two rounds of the same
call-site churn.

- **Item 13** (`TASKS.md`, "…`run_matrix.py --preflight-only` rebuilds every base
  image unconditionally before `resolve_tasks` runs…"): `prepare_bases` stops
  rebuilding a base the daemon already carries, and decides that by reading
  three labels the base image stamps on **itself**. A tag that does not say it
  is this base — wrong labels, no labels, no such tag — is still rebuilt, and
  the driver says which reason fired.
- **Item 15** (`TASKS.md`, "`images.py` carries two dead pieces from broadening
  5's single-runtime signature"): `images._DEFAULT_PYTHON` and
  `build_base_image`'s `tag: str | None = None` parameter go.

> **Line numbers.** Another agent is implementing round-2 item 3 and edits
> `scripts/run_matrix.py`, and items 5/7/8/11/12 edit `preflight.py`. Every
> location below is given as a **symbol name**; the few line numbers that appear
> are marked **re-locate** and were read at HEAD `8232032` via `git show`. The
> implementer anchors on the symbol, never on the number. This is not
> theoretical: `PREFLIGHT_VERSION` read `"13"` at that HEAD and reads **`"14"`**
> in the working tree today (item 3 landed mid-plan), so a transcribed literal
> would have been a silent no-op on the cache key.

---

## 1. The measured defect

### 1.1 What the probe found

`~/.cache/bakeoff-probe/reports/w5-python-version.md`, Finding 1, measured
2026-09-02. `bakeoff-eval-agent:base-python-3.13` was retagged by hand to point
at the **3.11** image (confirmed: `docker run --rm --entrypoint python
bakeoff-eval-agent:base-python-3.13 --version` → `Python 3.11.16`). The
documented gate command then ran:

```
.venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
  --task-set ~/.cache/bakeoff-probe/ts-py --tasks werkzeug-3037-py313
```

Exit **0**, `preflight PASS`, evidence `python_observed: "Python 3.13.15"`. The
mutation was gone before preflight's read-back ran: `main` calls `prepare_bases`
→ `images.build_base_images` → `images.build_base_image`, which runs
`docker build -q --build-arg BASE_PYTHON_VERSION=3.13 … -t
bakeoff-eval-agent:base-python-3.13 .` with **no existence check**; the build
cache resolved every layer, `docker build -t` **retagged** the original image id
onto the mutated tag, and `resolve_tasks` started its container inside a base
that was correct again.

### 1.2 What was measured, 2026-09-02, Docker 29.5.2

Legacy builder — `docker build --progress=plain` answers `unknown flag:
--progress` and the daemon prints `DEPRECATED: The legacy builder is
deprecated`. Every row below was measured by the planner and **independently
reproduced by review 1** on the same machine and date; where the two runs differ
in a number, both are given.

**M1 — a cache-hit build retags, and that is all it does.** Two throwaway images
built from one Dockerfile at 3.13 and 3.11; `docker tag`ged the 3.11 image onto
the 3.13 name; rebuilt the 3.13 name. The tag came back to the original id in
**0.10 s** (review: **0.04 s**). The unconditional rebuild's only observable
effect on a warm machine is the tag assignment.

**M2 — a cache-hit build is not a freshness guarantee.** The legacy builder's
cache is keyed on the instruction **string**. Neither base Dockerfile has a
`COPY` (`grep -c COPY docker/eval-agent.Dockerfile docker/eval-agent-node.Dockerfile`
→ `0` and `0`), so the build context is unused and the only inputs a rebuild
re-reads are the Dockerfile text and the build args. A republished
`python:3.13-slim-bookworm`, a changed `claude.ai/install.sh`, a moved apt or npm
package — a plain `docker build` sees none of them. Picking those up needs
`--pull --no-cache`, which the driver has never passed. **Skipping a build that
would hit cache therefore loses nothing the build was providing.**

**M3 — cache-hit rebuild costs, real bases.** python 3.13: **3.41 s**, **2.29 s**,
**2.15 s**. node 22: **4.33 s**, **3.33 s**, **3.34 s** (review: 3.1–4.7 s).
`docker image inspect --format '{{json .Config.Labels}}'`: **0.03 s** (review:
0.011 s missing, 0.022 s present). Most of the build cost is `sys` time streaming
the `bakeoff/` build context to the daemon for a Dockerfile that does not use it.

**M4 — the node base's image id churns on every invocation; the python base's
does not.** Six consecutive `docker build -q` invocations of
`docker/eval-agent-node.Dockerfile` at 22 returned **six different image ids**
(planner: `6369b7fd…`, `bf7f559d…`, `edd8d5da…`, `8f4b71d3…`, `a04c9675…`,
`5867d905…`; review, independently: `fa169de6…`, `2d8051bd…`, `cd4e0b99…`,
`c4cf77a6…`, `4cd1be5f…`, `55f2fdc9…`). The step log is identical every time:
steps 1–15 all report `---> Using cache`, including the `npm install` and the
`curl … install.sh`, and **`Step 16/16 : ENTRYPOINT []` reports `---> Running in
…`** and mints a new image. The parent is stable (`WORKDIR /repo` →
`4c2027501df2` on every run) and 42 children of that parent were already on the
machine (review, same day, counting all runtimes: **713 dangling images**). The
same instruction on the python chain *does* cache: `docker build -q` of
`eval-agent.Dockerfile` at 3.13 returned `sha256:4eb69f78dc68…` on every
invocation, `Step 14/14 : ENTRYPOINT [] ---> Using cache`. A control confirms it
is the chain and not the instruction: a throwaway python Dockerfile ending in
`LABEL` built three times returned `sha256:8919a2c690ff…` three times. **The
mechanism is not established** — the parent is stable, matching children exist,
and the lookup misses anyway — and this plan does not claim one. §6 files it as
its own backlog entry (review ruling O3).

**M4a — the LABEL block does not stop the churn; only the skip does.** Measured
by review 1: `eval-agent-node.Dockerfile` copied with exactly §3.1's node block
appended, built three times → `d905b5504a9f`, `76a654fa6729`, `a6300e5b16e3`,
labels stamping correctly. So the churn is routed around, not closed; §10 says
which paths stay cold.

**M4 is the expensive half of item 13.** `build_task_image` renders
`FROM {base_image}` with the base **image id**, so a churned base id makes every
node task's Dockerfile a different file and every layer below it — the task's
`apt` and `pip`/`npm` install — a cache miss. `preflight_cache_key` is
`f"{task.manifest_digest}|{image}|{start_sha}|{PREFLIGHT_VERSION}"` and
`oracle_fingerprint` hashes `f"{task.manifest_digest}|{image}|{ORACLE_VERSION}"`,
both over the **task** image id, so every warm node verdict and every cached
quarantine is invalidated as well. On a node task set, `--preflight-only` re-did
all of that on **every** invocation, in the half of the run documented as free.
(Chain confirmed end to end by review 1.)

**M5 — `LABEL` is readable, `${ARG}` expands in it, and the redeclare is
mandatory.** A Dockerfile with

```
ARG BASE_PYTHON_VERSION=3.12
FROM python:${BASE_PYTHON_VERSION}-slim-bookworm
ARG BASE_PYTHON_VERSION
LABEL bakeoff.base.runtime="python" \
      bakeoff.base.version="${BASE_PYTHON_VERSION}" \
      bakeoff.base.dockerfile_sha="deadbeef"
```

built with `--build-arg BASE_PYTHON_VERSION=3.13` answers

```
$ docker image inspect --format '{{json .Config.Labels}}' lblprobe:a
{"bakeoff.base.dockerfile_sha":"deadbeef","bakeoff.base.runtime":"python","bakeoff.base.version":"3.13"}
```

`"3.13"`, **not** `"3.13.15"` — the `BASE_` prefix is what saves it, exactly as
both Dockerfiles' own comments predict (`ENV PYTHON_VERSION` beats a redeclared
`ARG PYTHON_VERSION`; there is no `ENV BASE_PYTHON_VERSION`). The node side is the
same on `node:22-bookworm-slim`, whose `ENV NODE_VERSION=22.23.2`:
`"bakeoff.base.version":"22"`.

**Without the `ARG` redeclared after `FROM` the label is silently empty.**
Measured by review 1: the identical file with the redeclare removed stamps
`"bakeoff.base.version":""`, with no warning. An empty version can never equal
`"3.13"`, so the base would be rebuilt on **every** invocation forever and
nothing would say why. The redeclare is a load-bearing line, not tidiness, and
§5.1's `test_the_label_the_dockerfile_stamps_is_the_one_the_harness_expects`
pins it in both files.

**M6 — labels are inherited by a derived image, and that cuts both ways.**
`FROM <labelled base>` + `RUN true`, inspected: the parent's three keys
byte-for-byte. So a **task** image carries its base's three labels, which is what
lets preflight read them off the artifact it was handed (§3.5) — and it is also
the one shape that can make a non-base image claim to be this base (finding 5,
§2.1's residue and §10's bullet).

**M7 — the three answers `docker image inspect` gives.**

| state | output | exit |
|---|---|---|
| no such image | `Error response from daemon: No such image: lblprobe:nope` on stderr | 1 |
| image exists, no labels | the four bytes `null` on stdout (measured against the real `bakeoff-eval-agent:base-python-3.13`) | 0 |
| image exists, labels | a JSON object | 0 |

The first two must not collapse: an absent tag and an unlabelled one are
different facts and only one of them is evidence of anything.

**M8 — a label cannot be moved by `docker tag`, and forging one mints a new id
that the read-back still catches.** Measured by review 1:
`docker tag bakeoff-eval-agent:base-python-3.11 forgeprobe:b` leaves
`forgeprobe:b`'s labels `null` — labels live in the image config, and a tag
writes only a name. Building `FROM bakeoff-eval-agent:base-python-3.11` with a
forged `bakeoff.base.version="3.13"` label produces labels that lie **and** a new
image id (`sha256:95b984a97f3e` vs the base's `sha256:e670945930d0`), and
`docker run --entrypoint python` on it still answers **`Python 3.11.16`**. So a
mutated-tag-with-correct-labels is not reachable by a slip, and if someone
deliberately builds one, preflight's read-back files the NO-GO.

### 1.3 What is *not* the defect

The refusal in `preflight` is correct and is **already pinned by a test that
calls `preflight()` directly** —
`tests/test_preflight.py::test_an_interpreter_that_is_not_the_declared_one_is_refused`
(re-locate; ~L2097 at HEAD), which drives `preflight()` with a `_ScriptedContainer`
answering `Python 3.13.15` against a manifest declaring `3.11` and asserts the
NO-GO. Option (b) below is therefore already delivered; nothing in item 13 needs
it written.

Two of the three shapes that refusal's own comment names are **fully reachable
through the driver today** and stay that way: "a task image built against the
wrong entry of the bases map", and "an `image.build` step that puts another
interpreter earlier on PATH". Only the third — a hand-mutated base **tag** — is
the one the driver heals first.

---

## 2. The design

### 2.1 Chosen: option (a) — the base image says what it is, and the driver reads it

Both base Dockerfiles gain a trailing `LABEL` block stamping three keys:

| label | value | source |
|---|---|---|
| `bakeoff.base.runtime` | `python` / `node` | a literal in each file — each file knows its own runtime |
| `bakeoff.base.version` | `${BASE_PYTHON_VERSION}` / `${BASE_NODE_VERSION}` | the build arg, with the `ARG` redeclared after `FROM` (M5) |
| `bakeoff.base.dockerfile_sha` | `${BAKEOFF_BASE_DOCKERFILE_SHA}` | a new `ARG`, computed by the harness over the Dockerfile bytes + the build arg |

`images.build_base_images` then asks the tag what it is before building it. A
tag whose three labels are exactly what this Dockerfile at this version would
stamp is **reused**; anything else is **rebuilt**, and the driver prints the
reason it refused.

**The rule stated precisely** (corrected from revision 1, which over-claimed).
The labels describe **ancestry, not identity**. What `base_is_current` accepts is
"an image that was built from this Dockerfile at this version, *or is descended
from one*" — because docker propagates a parent's labels verbatim (M6) and no
cheap discriminator separates the two: every label is inherited, and
`Config.Entrypoint`, `WorkingDir` and layer count separate nothing here. The
residue is named in §10 rather than papered over: the one state that reaches it
is a hand mistag of the base tag onto a `bakeoff-task-*` image, which the
unconditional build did heal.

**Why the argument from "configuration is never reported as observation" lands
on both sides of this.** The label is what the image says about itself: a
build-time claim, stamped from configuration. `python_observed` is what the
interpreter says when it is asked. They are different kinds of fact and the
record has always been required to keep them apart, so:

- the **label** decides only whether to rebuild — a cheap, reversible,
  never-recorded decision whose worst outcome is a redundant 3 s build;
- the **read-back** stays the authority on whether a *python* task may run, and
  is never weakened, never skipped, and never compared against the label;
- **both** are written to preflight evidence, under `base_image_labels` beside
  `python_observed`, so a reader of a cached verdict can see the image's claim
  and the interpreter's answer and tell which one was wrong. A disagreement
  between them is a finding, and it is recorded rather than resolved.

**The node backstop, because the read-back is python-only.** `preflight` runs no
interpreter probe on a node task, by design (a node manifest may not declare
`image.python` at all). The equivalent there is
`images._NODE_RUNNER_REASSERTION`, emitted into every generated **task**
Dockerfile on a node base and therefore re-run on every invocation — task images
are never reused — whose first line refuses a base that is not a node base:
`the base declares no runner pins -- not a node base`. Saying "the read-back is
the sole authority" without this rider would claim a coverage that does not
extend to node, so both are named wherever the claim is made.

**Why this is not weaker than the build it replaces.** By M2 a cache-hit
`docker build` re-verifies nothing about the image's content; it re-runs the tag
assignment. The label check re-asserts the same binding by inspecting the tag
itself, and it re-asserts one thing the build did not: that the image under that
tag was built from **this** Dockerfile at **this** version. The one input the
label cannot see — upstream content drift in `python:`/`node:`, the installer,
apt, npm — is the one input `docker build` could not see either.

**What a mutated tag does now.** Labels say `bakeoff.base.version: "3.11"`, the
driver wants `3.13` → mismatch → rebuild → cache hit → the correct image is
retagged, exactly as today. The difference is that the healing is **decided and
named** instead of being an accident of retagging:

```
base py3.13  sha256:<12 hex>...  claude 2.1.220  (built: the tag said bakeoff.base.version='3.11', this base is '3.13')
```

and a cold machine reads `(built: the tag names no image)`, which is a different
sentence — the two are not allowed to render identically.

**What this honestly does not do.** It does **not** make the base-tag-mutation
shape of preflight's read-back reachable through the driver. It makes it
*unreachable by construction* — by M8, a tag cannot carry forged labels without
someone deliberately building an image, and preflight still catches that — which
is the correct outcome, because a stale base should be repaired before a
container starts, not diagnosed after one does. Item 13's own text asks for the
finding to be recorded as "defence exists, not reachable via the driver"; §6
records it in the closing note and in `preflight`'s comment at the refusal.

**Side effect that pays for the change on its own:** by M4, skipping the node
rebuild is what stops the node base id from churning on the **repeat**
invocation. By M4a the LABEL alone does not do this — only the skip does — and
§10 names the paths that still churn.

### 2.2 Rejected: option (b) — keep the unconditional rebuild, record the finding, add a direct-`preflight()` test

The test already exists (§1.3), so option (b)'s only deliverable is a
`TASKS.md` note. It leaves M4 in place — every node task rebuilt and re-gated
every invocation — and it leaves the driver silently repairing a mutated tag,
which is the behaviour that made a measurement wrong. Rejected as the whole
answer; its documentation half is **kept** and folded into §6.

### 2.3 Rejected: option (c) — a `--no-rebuild-bases` flag

A flag makes correctness an operator choice, and the operator who most needs the
rebuild is the one who would pass the flag to save 3 s. It also answers the
wrong question: the fix is not "rebuild less", it is "rebuild when the image is
not the one this Dockerfile builds", which no operator can evaluate by hand.
Rejected.

### 2.4 Rejected: a `--force-base-build` escape hatch (review ruling O1: no)

The rebuild such a flag would force is M1's **0.04 s retag**, which re-verifies
nothing: the legacy builder keys on the instruction string, neither base file has
a `COPY`, so a plain rebuild reads only the Dockerfile text and the build args —
the same two inputs `base_fingerprint` already covers. The flag would hand the
operator a control that cannot do the thing they want it for. The genuinely fresh
build needs `--pull --no-cache`, which the flag would not be.

**The ruling's condition:** the documented remedy counts as sufficient only if
the operator meets it *where the failure appears*. So `docker rmi <base tag>`
and the `--pull --no-cache` build go into **preflight's refusal message**
(§3.5), not only into `docs/BUILDING-A-TASK-SET.md` §3.6.

```bash
docker rmi bakeoff-eval-agent:base-python-3.13     # next run rebuilds
# or, to also re-pull the upstream base and re-run every RUN:
cd bakeoff && docker build --pull --no-cache --build-arg BASE_PYTHON_VERSION=3.13 \
  -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent:base-python-3.13 .
```

### 2.5 Rejected: hashing the build context into the fingerprint

Neither base Dockerfile has a `COPY` (M2), so the context contributes nothing to
either image. Hashing `bakeoff/` would make the fingerprint change on every
source edit in the repo and rebuild both bases for a change to `costs.py`.

### 2.6 Rejected: putting the `LABEL` block near the top of each Dockerfile

`LABEL` invalidates every instruction below it. Placed after `FROM`, a
fingerprint change would re-run apt, pip/npm and the installer. Placed **last**,
after `ENTRYPOINT []`, every existing cached layer is reused and the rebuild
stays the 2–4 s of M3.

### 2.7 Rejected: `prepare_bases` calling the check itself for its banner

Considered, because it keeps `build_base_images`' return type. It asks one
question twice — once for the message and once for the decision — with a window
between the answers, which is the shape this repository refuses everywhere else.
The decision **and its reason** are returned from the function that makes it
(§3.2).

---

## 3. The change, file by file

### 3.1 `bakeoff/docker/eval-agent.Dockerfile` and `bakeoff/docker/eval-agent-node.Dockerfile`

Two edits per file: the `ARG` redeclare + `LABEL` block at the **end**, and a
correction to a dated measurement the block falsifies.

**(a) Append to the end of `eval-agent.Dockerfile`, after `ENTRYPOINT []`.**
Transcribe exactly.

```dockerfile
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
```

**(b) Append to the end of `eval-agent-node.Dockerfile`, after `ENTRYPOINT []`.**

```dockerfile
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
```

**(c) `eval-agent.Dockerfile`'s `ARG BASE_PYTHON_VERSION` comment carries a
dated measurement this change falsifies.** Its last sentence reads:

> Measured: with no --build-arg this file builds to the byte-identical image id
> it built before the ARG existed
> (sha256:dfd2cc069bad6346465ec1ecfe0a704faac3cb0e86ddbcfbc424b60f9380915a),
> so no stored record's container_image_digest and no cached preflight verdict
> moves.

Adding the `LABEL` moves that id (§10). Replace the sentence with:

```
# Measured: with no --build-arg this file built to the byte-identical image id
# it built before the ARG existed
# (sha256:dfd2cc069bad6346465ec1ecfe0a704faac3cb0e86ddbcfbc424b60f9380915a), so
# the ARG itself moved no stored record's container_image_digest and no cached
# preflight verdict. THAT ID HELD UNTIL 2026-09-03, when the bakeoff.base.*
# LABEL block at the end of this file moved it once, deliberately -- see that
# block, and the "one-time costs" note in
# docs/superpowers/plans/2026-09-03-round2-13-base-rebuild-and-dead-code.md.
```

**(d) Both files' header build commands** show a hand build
(`docker build -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent .` and the
node file's `-t bakeoff-eval-agent:base-node-22` form). Add one line under each:

```
# The DRIVERS build these. A hand build passes no BAKEOFF_BASE_DOCKERFILE_SHA,
# so the image stamps an empty sha and the next driver run rebuilds it.
```

### 3.2 `bakeoff/src/bakeoff/images.py`

Module-scope `import json`, `import hashlib` and `from dataclasses import
dataclass` beside `import subprocess`, and delete the function-local `import
json` inside `image_entrypoint` (a second copy of one import is the same drift
shape as a second copy of one constant).

```python
@dataclass(frozen=True)
class BaseImage:
    """One base, whether this invocation had to build it, and why.

    A bare id cannot carry the second fact, and the driver's banner is the only
    place an operator learns that a Dockerfile edit was picked up -- or that a
    tag was found saying it was something else. `(built)` alone would not do
    that: it is byte-identical between a cold machine and a mutated tag, which
    is the "two absences that render identically" shape this repository refuses.
    Returning the reason is also what keeps `prepare_bases` from asking the same
    question a second time for its message (the plan's 2.7).

    `reason` is `None` exactly when `reused` is True.
    """
    image_id: str
    reused: bool
    reason: str | None = None


def base_fingerprint(repo_root: Path, runtime: str, version: str) -> str:
    """Every input to this base image that this harness controls, hashed.

    The Dockerfile TEXT plus the one build arg that reaches it. Deliberately
    NOT the build context: neither base file has a COPY (measured 2026-09-02),
    so the context contributes no layer, and hashing `bakeoff/` would rebuild
    both bases for an edit to `costs.py`.

    Deliberately NOT the upstream `python:`/`node:` tag's content, the
    claude.ai installer, apt or npm -- and `docker build` cannot see those
    either. The legacy builder's cache is keyed on the instruction STRING, so a
    rebuild that hits cache is a retag and not a freshness guarantee (measured
    2026-09-02: a mutated tag was restored to the original image id in 0.04 s).
    Picking up upstream drift needs `--pull --no-cache`, which no driver here
    has ever passed; preflight's mismatch refusal and
    `docs/BUILDING-A-TASK-SET.md` section 3.6 both name that command rather than
    hiding it behind a flag.
    """
    # `base_tag` FIRST, for the reason `build_base_image` gives: it carries the
    # named refusal for an unknown runtime, and indexing `_BASES` before it
    # would answer with a bare KeyError.
    base_tag(runtime, version)
    dockerfile, build_arg = _BASES[runtime]
    text = (Path(repo_root) / "docker" / dockerfile).read_bytes()
    return hashlib.sha256(
        text + b"\0" + f"{build_arg}={version}".encode()
    ).hexdigest()


def base_labels(repo_root: Path, runtime: str, version: str) -> dict[str, str]:
    """What a base built from this Dockerfile at this version must say it is.

    ONE definition, read twice: `build_base_image` passes the sha in as a build
    arg and `base_is_current` compares what came back. The label keys are
    spelled in the Dockerfiles too, which is a second copy that can drift --
    tests/test_images.py::test_the_label_the_dockerfile_stamps_is_the_one_the_harness_expects
    reads both files and pins them, because a key spelled two ways here is a
    base that is rebuilt on every invocation forever and nothing that says so.
    """
    return {
        "bakeoff.base.runtime": runtime,
        "bakeoff.base.version": version,
        "bakeoff.base.dockerfile_sha": base_fingerprint(repo_root, runtime, version),
    }


def image_labels(image: str) -> dict[str, str] | None:
    """The labels an image carries, or `None` when nobody could be asked.

    TWO absences, not three, and they are named together on purpose. `None`
    covers both "nothing resolves under that name" -- `docker image inspect`
    exits 1 with `Error response from daemon: No such image: <name>` -- and
    "the probe could not run at all", which is what an absent `docker` binary
    gives: `subprocess.run` raises `FileNotFoundError` rather than returning
    non-zero, and this function is called from `preflight`, which runs inside an
    OFFLINE unit suite where no daemon is assumed. Both mean "this tag told us
    nothing", and both mean rebuild.

    `{}` is the third state and is a real observation: an image that exists and
    declares no labels. That is what EVERY base built before this change
    reports -- measured 2026-09-02 against the real
    `bakeoff-eval-agent:base-python-3.13`, `--format '{{json .Config.Labels}}'`
    prints the four bytes `null`. Collapsing `{}` into `None` makes an
    unlabelled image and an absent one the same fact in `preflight`'s evidence,
    where only one of them says the gate looked.

    Takes an id as readily as a tag, which is what lets `preflight` read the
    labels a TASK image inherited from its base: docker propagates a parent's
    labels verbatim into a derived image (measured 2026-09-02).

    `subprocess.run` and not `_run`, because `_run` raises on a non-zero exit
    and "no such image" is an answer here rather than a failure.
    """
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}",
             image],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if probe.returncode != 0:
        return None
    return json.loads(probe.stdout.strip() or "null") or {}


def base_is_current(repo_root: Path, runtime: str,
                    version: str) -> tuple[str | None, str | None]:
    """The id the base tag resolves to when the IMAGE says it is this base.

    Returns `(image_id, None)` or `(None, reason)` -- exactly one is not None.
    The reason is carried so the driver's banner can distinguish a cold machine
    from a tag that was found saying something else; a bare "built" cannot.

    THE LABELS DESCRIBE ANCESTRY, NOT IDENTITY. Docker propagates a parent's
    labels verbatim, so an image DERIVED from this base claims to be it --
    every `bakeoff-task-*` image does, since `render_dockerfile` emits no LABEL
    of its own. There is no cheap discriminator (every label is inherited, and
    entrypoint, workdir and layer count separate nothing here), so the residue
    is named rather than papered over: a base tag hand-mistagged onto a task
    image is accepted here, and the unconditional build this replaced did heal
    that one state. See the plan's section 10.

    A SUBSET comparison: an image may legitimately carry other labels, and these
    three are the only ones this harness has an opinion about.
    """
    tag = base_tag(runtime, version)
    labels = image_labels(tag)
    if labels is None:
        return None, "the tag names no image"
    expected = base_labels(repo_root, runtime, version)
    if not any(key in labels for key in expected):
        return None, "the tag carries no bakeoff.base.* labels"
    for key, want in expected.items():
        if labels.get(key) == want:
            continue
        # The sha is 64 hex on both sides and says nothing to a reader in a
        # banner; the other two keys are the whole message. Named, not printed.
        #
        # A PARTIALLY labelled image renders `the tag said
        # bakeoff.base.version=None, this base is '3.13'`. That reads like a
        # value and is not one -- docker label values are always strings, so
        # `None` here can only mean the key is absent, which is why it is left
        # as the bare repr rather than dressed up in a sentinel a real label
        # could collide with.
        if key == "bakeoff.base.dockerfile_sha":
            return None, "the tag was built from a different base Dockerfile"
        return None, (f"the tag said {key}={labels.get(key)!r}, "
                      f"this base is {want!r}")
    return image_id(tag), None
```

`build_base_image` **keeps building unconditionally** — it is the function that
means "build this base" — and gains the sha build arg. Its `tag` parameter goes
(item 15, §4):

```python
def build_base_image(repo_root: Path, runtime: str, version: str) -> str:
    ...
    # `base_tag` FIRST: it carries the named refusal for an unknown runtime,
    # and indexing `_BASES` before it would answer with a bare KeyError.
    tag = base_tag(runtime, version)
    dockerfile, build_arg = _BASES[runtime]
    _run(
        [
            "docker", "build", "-q",
            "--build-arg", f"{build_arg}={version}",
            # What the image will then say it is. `base_is_current` compares
            # what came back against `base_labels`, so this argument and that
            # comparison read ONE definition.
            "--build-arg",
            f"BAKEOFF_BASE_DOCKERFILE_SHA="
            f"{base_fingerprint(repo_root, runtime, version)}",
            "-f", str(Path(repo_root) / "docker" / dockerfile),
            "-t", tag, str(repo_root),
        ]
    )
    return image_id(tag)
```

`build_base_images` returns `BaseImage`s and skips what it can:

```python
def build_base_images(repo_root: Path,
                      runtimes: Iterable[tuple[str, str]],
                      ) -> dict[tuple[str, str], BaseImage]:
    """Every base a task set needs, built once per distinct pair -- and only
    when the daemon does not already carry it.

    THE SKIP IS NOT AN OPTIMISATION ALONE. `run_matrix.main` calls this before
    `resolve_tasks`, so an unconditional `docker build -t` retags the correct
    image over a mutated tag before preflight's read-back ever runs inside it --
    measured 2026-09-02, which is how a probe of that read-back recorded PASS on
    a base it had deliberately broken. Reading the image's own labels makes the
    repair a DECISION the driver reports instead of a side effect.

    A cache-hit rebuild is cheap but not free, and on the node chain it is not
    idempotent: measured 2026-09-02, six consecutive builds of
    eval-agent-node.Dockerfile reported `Using cache` for steps 1-15 and minted
    a NEW image id at step 16 every time. `build_task_image` renders
    `FROM <base image id>`, and `preflight_cache_key` and `oracle_fingerprint`
    both hash the resulting task image, so that churn rebuilt every node task
    image and invalidated every warm node verdict on every invocation of the
    offline half documented as free. The skip removes that from the REPEAT
    invocation; a genuine rebuild still mints a fresh node id, and that is
    correct rather than regrettable.
    """
    built: dict[tuple[str, str], BaseImage] = {}
    for pair in sorted(set(runtimes)):
        current, reason = base_is_current(repo_root, *pair)
        built[pair] = (
            BaseImage(current, reused=True) if current is not None
            else BaseImage(build_base_image(repo_root, *pair), reused=False,
                           reason=reason)
        )
    return built
```

### 3.3 `bakeoff/scripts/run_matrix.py` — `prepare_bases` (re-locate; ~L191 at HEAD `8232032`)

`prepare_bases` unwraps, so `assert_one_agent`, `resolve_tasks` and every
consumer of `bases` keep taking a `dict[pair, str]` of ids and are **not
touched**. Replace the body's last four lines:

```python
    built = build_base_images(REPO, keys)
    bases = {key: base.image_id for key, base in built.items()}
    expected = assert_one_agent(bases)
    for key in keys:
        base = built[key]
        state = "reused" if base.reused else f"built: {base.reason}"
        print(f"base {_short_base_label(key)}  {base.image_id[:19]}...  "
              f"claude {expected}  ({state})")
    return bases, expected
```

and extend the docstring with:

```
    A base the daemon already carries is REUSED rather than rebuilt, and the
    banner says which happened AND why. `images.build_base_images` decides that
    by reading the three `bakeoff.base.*` labels off the tag; anything that does
    not say it is exactly this base -- a mutated tag, an unlabelled image, no
    such tag -- is rebuilt with the reason named. That reason is the whole point
    of returning the decision rather than asking for it twice: `(built)` alone
    reads identically on a cold machine and on a tag someone mistagged, and the
    second is the case a probe of preflight's read-back was silently defeated by
    on 2026-09-02.
```

The banner's leading form (`base py3.12  sha256:...  claude 2.1.220`) is
preserved byte-for-byte and the state is appended, so `_short_base_label`'s
docstring claim about stored logs still holds for the prefix. Add one sentence to
that docstring saying the line now carries a trailing `(reused)` or
`(built: <reason>)`.

### 3.4 `bakeoff/scripts/grade.py` — `task_resolver`'s closure (re-locate; ~L360 at HEAD)

```python
            built.update({key: base.image_id for key, base
                          in build_base_images(REPO, [key]).items()})
```

`built` stays `dict[tuple[str, str], str]`. Add to `task_resolver`'s docstring:

```
    `build_base_images` returns a `BaseImage` per key and this unwraps to the
    id: the grader has no banner to print the reused/built decision on, and a
    driver that runs after the money is spent has nothing to do with it.
```

### 3.5 `bakeoff/src/bakeoff/preflight.py`

Import `image_labels` **and `base_tag`** from `bakeoff.images` at module scope,
beside `from bakeoff.container import RunContainer`. `base_tag` is imported
rather than the tag format being spelled a second time in §3.5(c)'s refusal
string — the same one-definition argument `base_labels` makes two sections
earlier, and a second spelling of a tag format is how a message starts naming a
tag nothing builds. **No cycle:** `images.py` imports
only `subprocess`, `pathlib`, `dataclasses`, `json`, `hashlib` and
`collections.abc` at module scope, and reaches `tasks` through function-local
imports only (its module docstring says why); nothing in `images` imports
`preflight`.

**(a) Declare the key. CONDITIONAL — round-2 item 5 lands first and replaces
the mechanism this used to write into.** Item 5 (`docs/superpowers/plans/
2026-09-03-round2-5-evidence-schema.md`) adds `preflight.EVIDENCE_KEYS`, a tuple
in write order; `_evidence_seed()`, returning `dict.fromkeys(EVIDENCE_KEYS)`;
`PreflightResult.evidence`'s `default_factory = _evidence_seed`; and a
`__post_init__` that **raises** when the evidence key set is not exactly
`EVIDENCE_KEYS`. Its Task 2 explicitly **deletes** `evidence["python_observed"]
= None` along with every other per-key `= None` seed. So after item 5 there is
no pre-write block to append to, and a key that is not in `EVIDENCE_KEYS` does
not merely go unseeded — **every** `PreflightResult`, on every path of every
task, raises. Decide by grep, not by assumption:

```bash
grep -n 'EVIDENCE_KEYS' src/bakeoff/preflight.py
```

**If it matches (item 5 has landed — the expected case):**

- Add `"base_image_labels"` to `EVIDENCE_KEYS` **in write order, immediately
  after `"python_observed"`**, since that is where §3.5(b) writes it. Order is
  presentation — the comparison is `set(...) == set(EVIDENCE_KEYS)` — but
  `dict.fromkeys` preserves it and `<cache>/preflight/<task_id>.json` is read
  top-to-bottom by a human.
- Carry the rationale as a `#:` comment on that entry (item 5's Task 2 rule:
  attach a recorded finding to the key it explains, never to a seed statement
  that no longer exists):

  ```python
      "python_declared", "python_observed",
      #: Read HOST-side and needing no container, yet deliberately written
      #: beside the interpreter probe rather than earlier where it could be:
      #: reading it before the runner-gate early return would leave that path
      #: carrying a label observation beside a `python_observed: None` -- one
      #: evidence family answering on a path the other cannot, which is the
      #: asymmetry a reader diffing two verdicts cannot resolve.
      "base_image_labels",
  ```

- Add **nothing** to `preflight()`'s body here. `_evidence_seed()` already seeds
  it `None`, and hand-seeding it rebuilds the "a hand-seeded subset is the
  schema" reading item 5's tuple exists to remove — that plan says so in as many
  words.
- Item 5's own tests are the safety net in both directions, and neither needs
  editing for this key: `__post_init__` raises at run time on an unregistered
  key, and its AST-static
  `test_evidence_keys_lists_exactly_what_preflight_writes` fails at edit time on
  a key written but not listed. Item 13 is the "next item that adds a key" that
  test's docstring anticipates; if it goes red on `base_image_labels`, the fix is
  this bullet, not the test.

**If it does not match (item 5 has not landed):** write the seed by hand,
immediately after the `evidence["python_observed"] = None` line (re-locate;
~L843 at HEAD `8232032`), and say in the review package that item 5 will have to
fold it into `EVIDENCE_KEYS` when it lands:

```python
    #: `None`, and it shares `python_observed`'s reachability exactly rather
    #: than being read earlier where it could be: this is a HOST-side inspect
    #: and needs no container, so reading it before the runner-gate early
    #: return would leave that path carrying a label observation beside a
    #: `python_observed: None` -- one evidence family answering on a path the
    #: other cannot, which is the asymmetry a reader diffing two verdicts
    #: cannot resolve. Filled in beside the interpreter probe below.
    evidence["base_image_labels"] = None
```

**(b) Fill it in** inside the `with RunContainer(...)` block, immediately
**before** the `python = (container.exec(["python", "--version"]) …)` probe, so
the image's claim and the interpreter's answer are written next to each other:

```python
        # WHAT THE IMAGE SAYS ABOUT ITSELF, beside what the interpreter says.
        # A task image is `FROM <base image id>` and docker propagates a
        # parent's labels verbatim (measured 2026-09-02), so these are the base
        # image's own three keys read off the artifact under test -- no second
        # lookup of which base tag produced it, and correct for a node task,
        # which makes no interpreter claim at all.
        #
        # RECORDED, NEVER REFUSED ON, and the asymmetry is the point. A label is
        # configuration the build stamped; `python_observed` below is an
        # observation. When they disagree the interpreter is the authority and
        # this file carries both, because a verdict cached under this key
        # outlives the code that wrote it and "the image claimed 3.11 and ran
        # 3.13" is a finding a reader has to be able to reconstruct.
        #
        # `{}` is an image carrying no `bakeoff.base.*` keys -- every base built
        # before those labels existed, and every task image derived from one.
        # `None` is "nobody could be asked": no such image, or no docker binary
        # to ask with. Two absences that render identically are the same defect
        # one layer down; these two do not.
        base_seen = image_labels(image)
        evidence["base_image_labels"] = (
            None if base_seen is None
            else {k: v for k, v in base_seen.items()
                  if k.startswith("bakeoff.base.")}
        )
```

**(c) The mismatch refusal names a remedy that this change can turn into a
no-op.** After the skip, `run_matrix.py --preflight-only` rebuilds only when the
labels disagree — so for the inputs the labels cannot see (a republished
upstream image, a changed installer, a moved apt/pip package) the operator's
documented remedy does nothing and they loop. Replace the problem string in the
`_parse_python_version(observed_python) != declared_python` branch with:

```python
                problems.append(
                    f"the container runs {observed_python!r} but the manifest "
                    f"declares image.python: {declared_python!r}. The base tag "
                    "is local and mutable, so this is a stale or mismatched "
                    "base rather than a manifest error: rebuild the bases "
                    "(`run_matrix.py --preflight-only` builds the set the task "
                    "set needs) and rebuild this task's image against the "
                    "right one. That command REUSES a base whose "
                    "`bakeoff.base.*` labels already match what its Dockerfile "
                    "would stamp, so if it does not fix this, delete the tag "
                    f"first -- `docker rmi {base_tag('python', declared_python)}`"
                    " -- or rebuild it with "
                    "`--pull --no-cache`, which is the only way to pick up a "
                    "republished upstream image, a changed claude.ai installer "
                    "or a moved apt/pip package"
                )
```

The mutation anchor on the line above it
(`if _parse_python_version(observed_python) != declared_python:`) is untouched;
verify that before editing.

**(d) Extend the comment above the interpreter read-back** (the block starting
"The interpreter, read back out of the container rather than trusted from the
manifest") with item 13's finding, so the code carries it:

```
        # ONE OF THE THREE SHAPES BELOW IS NO LONGER REACHABLE THROUGH THE
        # DRIVER, and that is deliberate rather than a gap. A hand-mutated base
        # TAG is now repaired by `images.build_base_images` before this
        # container starts -- it reads the tag's own `bakeoff.base.*` labels,
        # finds they are not this base's, and rebuilds, naming the reason.
        # Measured 2026-09-02, BEFORE that check existed the driver repaired it
        # anyway, silently, via a cache-hit `docker build -t` retag, and a probe
        # of this very refusal recorded PASS on a base it had deliberately
        # broken. A tag cannot carry FORGED labels either: `docker tag` writes a
        # name and not a config, so the only way to a lying label is to build an
        # image -- and measured, such an image still answers 3.11.16 here and
        # still lands on this refusal. The other two shapes -- a task image
        # built against the wrong entry of the bases map, an `image.build`
        # putting another interpreter first on PATH -- are reachable through the
        # driver and are what a real task set produces. THIS PROBE IS
        # PYTHON-ONLY; the node analogue is `images._NODE_RUNNER_REASSERTION`,
        # re-run at every task image build. `tests/test_preflight.py
        # ::test_an_interpreter_that_is_not_the_declared_one_is_refused` drives
        # `preflight()` directly and is what pins the refusal itself.
```

**(e) `_declared_python`'s docstring strands a deleted constant** (re-locate;
~L391-392 at HEAD). It currently reads *"Three copies of the default already
exist (the Dockerfile's ARG, `tasks._DEFAULT_PYTHON`, `images._DEFAULT_PYTHON`)
and are pinned equal to each other by tests/test_images.py"*. After item 15 that
names a constant that does not exist. Replace that paragraph with:

```
    The fallback is IMPORTED, never restated. TWO copies of the default exist --
    the Dockerfile's `ARG BASE_PYTHON_VERSION` and `tasks._DEFAULT_PYTHON` --
    and they are pinned equal by
    tests/test_images.py::test_every_copy_of_the_default_version_says_the_same_thing.
    This module restates neither; a third copy here would be the only unpinned
    one, and it would sit in the gate that exists to catch exactly this class of
    disagreement. (There were three: `images._DEFAULT_PYTHON` was the other, and
    round 2 item 15 removed it -- it had no reader but its own pin.)
```

**(f) `PREFLIGHT_VERSION`** — evidence gains a key, so it moves. **Re-locate and
increment by one**; it reads `"14"` in the working tree today (it read `"13"` at
HEAD `8232032`; item 3 landed in between, and items 5/7/8/11/12 may move it
again). Do **not** transcribe a literal. Extend `preflight_cache_key`'s docstring
and `test_the_preflight_version_moved_with_the_new_assertion`'s narrative — and
its trailing `assert PREFLIGHT_VERSION == "<N>"` — with one clause in the
established form:

> *N* → *N+1* is `base_image_labels`: a verdict cached under *N* was written by a
> gate that recorded what the interpreter answered and not what the image
> claimed to be, so a base whose labels and interpreter disagree is
> indistinguishable in the older file from one where they agree.

### 3.6 No schema move

No `RunRecord` field changes. `Versions.container_image_digest` already pins the
task image, and `base_image_labels` lives in the **preflight evidence** file,
which is versioned by `PREFLIGHT_VERSION` and not by `SCHEMA_VERSION`. Do not
move `SCHEMA_VERSION`.

---

## 4. Item 15 — the two dead pieces

Both verified dead by grep across `bakeoff/src`, `bakeoff/scripts`,
`bakeoff/tests` and `bakeoff/docker` at HEAD `8232032`, and re-verified by
review 1.

### 4.1 `build_base_image(..., tag: str | None = None)` — fully dead, delete

Every caller passes exactly `(repo_root, runtime, version)`:
`images.build_base_images` (via `*pair`), `tests/test_images.py` L609, L629,
L698, L755, L927, `tests/test_integration_node_task.py` L189 (all re-locate).
`.superpowers/broaden/b5-plan-review-1.md` already flagged it
("`build_base_image`'s `tag=None` parameter has no caller — drop it") and
broadening 7 reintroduced it in the signature it copied forward. Removing it also
removes the `tag = tag or base_tag(runtime, version)` line, whose `or` was the
only thing keeping a caller-supplied name possible; §3.2's rewrite already shows
the replacement. **No test changes** — nothing passes it, and
`test_build_base_image_passes_the_version_as_a_build_arg` already pins that the
`-t` value comes from `base_tag`.

### 4.2 `images._DEFAULT_PYTHON` — one reader, and it is a test, so the test is rewritten first

The constant has **no production reader**. `preflight` imports
`_DEFAULT_PYTHON` from **`tasks`** (`preflight.py:48`, re-locate). Its only
reader in the whole tree is
`tests/test_images.py::test_every_copy_of_the_default_version_says_the_same_thing`
(re-locate; the `assert images._DEFAULT_PYTHON == manifest_default` at ~L673) —
an assertion that exists only because the constant does. A constant kept alive by
its own pin is not a pin.

Order matters. **Rewrite the test first, then delete the constant**, so the drift
pin the constant was standing in for survives it. The pin that actually matters —
a manifest declaring no `python:` loading as a task whose base nobody built — is
between `tasks._DEFAULT_PYTHON` and the Dockerfile's `ARG BASE_PYTHON_VERSION`
default, and both of those stay:

```python
def test_every_copy_of_the_default_version_says_the_same_thing():
    """TWO copies of "3.12" remain, and the pin is between them: the
    Dockerfile's `ARG BASE_PYTHON_VERSION` default and `tasks._DEFAULT_PYTHON`
    (which is `TaskImage.python`'s default). Two that can drift means a manifest
    declaring no `python:` loads as a task whose base nobody built -- the build
    succeeds, the suite runs, and the interpreter is not the one the default
    named.

    There were THREE. `images._DEFAULT_PYTHON` was the third, restated in that
    module because it cannot import `tasks` at module scope; broadening 7 made
    the runtime and version explicit at every call site, which left it with no
    reader but this assertion. A constant kept alive by the test that pins it is
    not a pin -- it is a second copy of a value with a test guaranteeing the
    copy exists. Removed in round 2, item 15; this test keeps the two copies
    that are still read.

    `preflight` is deliberately not a copy either: it imports `_DEFAULT_PYTHON`
    from `tasks`."""
    from bakeoff.tasks import _DEFAULT_PYTHON as manifest_default

    dockerfile = (
        Path(__file__).resolve().parent.parent / "docker" / "eval-agent.Dockerfile"
    ).read_text()

    assert f"ARG BASE_PYTHON_VERSION={manifest_default}\n" in dockerfile
    assert "FROM python:${BASE_PYTHON_VERSION}-slim-bookworm\n" in dockerfile
```

Then delete `images._DEFAULT_PYTHON` and its `#:` comment block (`images.py`
L47-54, re-locate).

**Two prose sites the deletion strands, both named rather than left to a
sweep:**

- `preflight.py::_declared_python`'s docstring — §3.5(e) above. This is a
  condition on the deletion, not a nicety: leaving it turns a removed constant
  into a false claim in a docstring, which is worse than the constant.
- `docker/eval-agent.Dockerfile`'s ARG comment — its first sentence ("The
  default matches `bakeoff.tasks._DEFAULT_PYTHON` and is pinned equal by
  tests/test_images.py") stays true and needs no edit; its *second* sentence is
  the falsified measurement handled by §3.1(c). Do not conflate the two.

`git grep _DEFAULT_PYTHON` after the change must return only `tasks.py`,
`preflight.py`'s import and `_declared_python` body, `tests/test_tasks.py:2417`
(`tasks._DEFAULT_PYTHON in tasks._PYTHON_VERSIONS`, untouched), and the rewritten
`test_images.py` — no `images.` prefix anywhere.

---

## 5. Tests, by name

### 5.1 `bakeoff/tests/test_images.py`

**Fixture, written once and used by every reuse test.** `base_labels` reads a
real Dockerfile, so a `Path("/repo")` root (the analogy every neighbouring test
in this file invites) raises `FileNotFoundError`. Define at module scope:

```python
#: The real repo root, because `base_fingerprint` reads
#: docker/eval-agent{,-node}.Dockerfile off disk and the reuse tests compare
#: against `base_labels(_REPO_ROOT, ...)` computed from the same root. The
#: `Path("/repo")` roots the older tests in this file use are safe only where
#: `image_labels` is faked to `None`, which returns before the read.
_REPO_ROOT = Path(__file__).resolve().parent.parent
```

All offline unless marked. The reuse tests monkeypatch `images.image_labels`
(not `images._run`), so the decision is exercised without a daemon.

| test | asserts |
|---|---|
| `test_a_base_whose_labels_match_is_not_rebuilt` | root `_REPO_ROOT`; `image_labels` returns exactly `images.base_labels(_REPO_ROOT, "python", "3.12")`; `build_base_images` returns `BaseImage(reused=True, reason=None)` and `images._run` recorded **no** `docker build` argv |
| `test_a_base_tag_that_names_nothing_is_built` | `image_labels` → `None`; a build ran, `reused is False`, `reason == "the tag names no image"` |
| `test_a_base_carrying_no_labels_is_rebuilt` | `image_labels` → `{}` — every base on every machine that predates this change; a build ran, `reason == "the tag carries no bakeoff.base.* labels"`. Pins that `{}` and `None` both mean rebuild while staying distinguishable |
| `test_a_tag_mutated_to_another_version_is_rebuilt` | root `_REPO_ROOT`; `image_labels` returns `images.base_labels(_REPO_ROOT, "python", "3.11")` while `("python", "3.13")` is asked; a build ran, and `reason == "the tag said bakeoff.base.version='3.11', this base is '3.13'"`. **This is the probe's Finding 1, as a test** |
| `test_a_tag_carrying_a_stale_dockerfile_sha_is_rebuilt` | root `_REPO_ROOT`; labels match on runtime and version, `bakeoff.base.dockerfile_sha` is `"0" * 64`; a build ran, `reason == "the tag was built from a different base Dockerfile"` — the sha is not printed |
| `test_a_base_carrying_extra_labels_is_still_reused` | root `_REPO_ROOT`; the three expected keys plus `org.opencontainers.image.source`; `reused is True`. Pins the subset comparison |
| `test_a_reused_base_names_no_reason_and_a_built_one_always_does` | across the rows above: `reason is None` iff `reused`. The `BaseImage` invariant, so the banner cannot print `built: None` |
| `test_the_fingerprint_moves_when_the_dockerfile_does` | a `tmp_path` root carrying `docker/eval-agent.Dockerfile`; `base_fingerprint` before ≠ after appending one byte |
| `test_the_fingerprint_covers_the_build_arg_and_not_only_the_file` | one `tmp_path` Dockerfile, `("python","3.11")` vs `("python","3.12")` → two different fingerprints |
| `test_the_fingerprint_refuses_an_unknown_runtime` | `base_fingerprint(_REPO_ROOT, "ruby", "3.3")` raises `ImageError` naming `ruby`, not `KeyError` |
| `test_the_sha_the_harness_computes_is_the_one_the_build_stamps` | fake `_run`; the argv carries `--build-arg BAKEOFF_BASE_DOCKERFILE_SHA=<base_fingerprint(...)>` — one definition, two readers |
| `test_the_label_the_dockerfile_stamps_is_the_one_the_harness_expects` | reads **both** real Dockerfiles; each contains `bakeoff.base.runtime="<its runtime>"`, `bakeoff.base.version="${<its _BASES build arg>}"` and `bakeoff.base.dockerfile_sha="${BAKEOFF_BASE_DOCKERFILE_SHA}"`; **each redeclares its own `ARG <build arg>` after the `FROM` line** (measured: without it the label stamps `""`, silently, and the base is rebuilt forever); every key in `base_labels(...)` appears in the file. The offline drift pin between `images.py` and the two files |
| `test_image_labels_answers_three_ways` | fake `subprocess.run`: exit 1 → `None`; exit 0 with `"null"` → `{}`; exit 0 with a JSON object → that dict |
| `test_image_labels_survives_a_missing_docker_binary` | `subprocess.run` raises `FileNotFoundError` → `None`, no exception. This is what keeps `preflight`'s 58 offline unit tests offline |
| `test_one_build_per_distinct_version_not_per_request` | **existing** (re-locate, ~L634), updated **two ways**: (a) monkeypatch `images.image_labels` → `None` alongside the existing `build_base_image` fake, or `base_is_current` runs unpatched and issues a real `docker image inspect` per pair — and on a machine that has the real labelled tags it would then call `base_labels(Path("/r"), …)` and raise `FileNotFoundError`; (b) the values are now `BaseImage`, so the final assertion becomes `{key: base.image_id for key, base in bases.items()} == {("python", "3.11"): "sha256:3.11", ("python", "3.12"): "sha256:3.12"}`. The `Path("/r")` root stays correct **because** `image_labels` is faked to `None` and `base_is_current` returns before reading any Dockerfile — say so in the test |
| `test_build_base_images_builds_each_pair_exactly_once` | **existing** (re-locate, ~L727), updated: monkeypatch `images.image_labels` → `None` so both pairs build; `set(built)` unchanged; `len(calls) == 2` |
| `test_a_built_base_really_carries_the_labels_the_harness_expects` | `@pytest.mark.integration @pytest.mark.task_image`. Builds the **python** base at `"3.11"`; `image_labels(base_tag("python", "3.11"))` is a superset of `base_labels(REPO_ROOT, "python", "3.11")`. **No churn claim** — the python chain is stable with and without the fix (three builds, one id), so an id assertion here would pass either way |
| `test_a_node_base_is_reused_rather_than_reminted` | `@pytest.mark.integration @pytest.mark.task_image`. Two consecutive `build_base_images(REPO_ROOT, [("node", "22")])`; the second returns `reused=True` and **the same `image_id`**. This is the real M4 control: measured 2026-09-02, six unconditional builds of that file gave six ids, and appending the LABEL block did not change that — only the skip does. Warm on any machine that has run the node task tests |

### 5.2 `bakeoff/tests/test_preflight.py`

**Helper change first, and it covers 58 existing tests.** `_run_preflight`
(re-locate; ~L1196) fakes only `RunContainer`, so after §3.5 every test that
reaches the container block would issue a real `docker image inspect`
(`grep -c "_run_preflight("` → **58**). Add one line inside it, before the
`return`:

```python
    # `preflight` now inspects the image's own labels, host-side. This suite is
    # documented as offline and `verify_logger.py` runs it as the section 6.6
    # gate, so the subprocess is faked here rather than 58 times; the tests that
    # care override it afterwards. `{}` is the honest default: an image that
    # exists and carries no bakeoff.base.* labels.
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: {})
```

(`image_labels`' `except OSError` in §3.2 is the belt to this braces: it is what
keeps a machine with no `docker` binary from crashing any test that bypasses the
helper.)

| test | asserts |
|---|---|
| `test_the_image_labels_are_recorded_beside_the_interpreter` | labels `{"bakeoff.base.runtime":"python","bakeoff.base.version":"3.11","bakeoff.base.dockerfile_sha":"a"*64}`, container answers `Python 3.11.16`, manifest declares `3.11` → `result.ok`, `evidence["base_image_labels"]` is exactly those three, `evidence["python_observed"] == "Python 3.11.16"` |
| `test_a_label_that_disagrees_with_the_interpreter_is_recorded_and_not_refused` | labels say `version: "3.11"`, container answers `Python 3.13.15`, manifest declares `3.13` → **`result.ok` is True**, and evidence carries both. Pins that the label is not a gate and the read-back is the authority |
| `test_a_label_that_agrees_does_not_rescue_a_wrong_interpreter` | labels say `version: "3.11"`, manifest declares `3.11`, container answers `Python 3.13.15` → NO-GO, and the problem names `3.11` and `3.13`. The converse: the label cannot vouch for the interpreter |
| `test_the_mismatch_refusal_names_a_remedy_that_still_works_after_the_skip` | the same NO-GO; the problem string contains `docker rmi bakeoff-eval-agent:base-python-3.11` and `--pull --no-cache`. §3.5(c), and review 1's condition on ruling O1 |
| `test_only_the_bakeoff_base_keys_are_recorded` | labels include `maintainer` and `org.opencontainers.image.source`; evidence carries the three `bakeoff.base.*` keys and nothing else |
| `test_an_image_with_no_bakeoff_labels_records_an_empty_set_not_a_null` | `image_labels` → `{}` → evidence `{}`, and `is not None` |
| `test_a_label_probe_that_could_not_answer_is_a_null_not_an_empty_set` | `image_labels` → `None` (no such image, or no docker to ask) → evidence `None`, and `result.ok` is unaffected |
| `test_the_label_evidence_is_none_on_the_path_that_never_starts_a_container` | the runner-gate early return (`tests.runner=("nose",)`) leaves `base_image_labels is None` **and** `python_observed is None` — the two evidence families agreeing on that path. After item 5 lands, that route also carries `early_return == EARLY_RETURN_RUNNER_MISMATCH`; assert it here too, so the row names the route by item 5's closed code rather than by a runner string this test would otherwise be the last reader of. **Presence** of the key on every route is item 5's `test_every_evidence_key_is_present_on_every_route`; this row asserts the *value*, which that one does not |
| `test_the_preflight_version_moved_with_the_new_assertion` | **existing** (re-locate, ~L2158), extended with the `base_image_labels` clause and the new literal |

### 5.3 `bakeoff/tests/test_run_matrix.py`

| test | asserts |
|---|---|
| `test_the_version_set_comes_from_the_task_set_not_from_the_allowlist` | **existing** (re-locate; `tests/test_run_matrix.py:104`, its `_fake_build(root, versions)` at L120 and its `bases ==` assertion at L131-132). This is the **only** test in the tree that fakes `build_base_images` and calls `prepare_bases`, and leaving it unnamed is review-1 finding 2's twin in the other file: its `_fake_build` returns plain strings, and §3.3's `{key: base.image_id for key, base in built.items()}` then raises `AttributeError: 'str' object has no attribute 'image_id'`. (Revision 1 named `test_one_base_is_built_per_distinct_version_not_per_task`, which exists nowhere; the near-miss `test_one_build_per_distinct_version_not_per_request` is in `test_images.py` and is handled by §5.1.) `_fake_build` now returns `{pair: images.BaseImage("sha256:" + pair[1], reused=False, reason="the tag names no image") for pair in versions}`; the `bases ==` assertion against plain strings is **unchanged**, which is itself the pin that `prepare_bases` still hands id strings downstream |
| `test_the_driver_says_which_bases_it_built_and_which_it_reused` | `capsys`; `_fake_build` returns one `reused=True` and one `reused=False, reason="the tag said bakeoff.base.version='3.11', this base is '3.13'"`; the two banner lines end `(reused)` and `(built: the tag said bakeoff.base.version='3.11', this base is '3.13')`, and each still begins `base py3.11  sha256:` |
| `test_a_cold_machine_and_a_mutated_tag_do_not_print_the_same_line` | two `prepare_bases` runs, `reason="the tag names no image"` and the mismatch reason; the printed lines differ. The "two absences that render identically" pin, and the reason finding 1 exists |
| `test_prepare_bases_hands_assert_one_agent_ids_not_wrappers` | `assert_one_agent` faked to record its argument; every value is a `str`. Pins that the unwrap happens before it, so `resolve_tasks`, `assert_one_agent` and `resolve_task` stay untouched |

### 5.4 Integration call sites

Mechanical, no new tests. `build_base_images(REPO_ROOT, [key])[key]` becomes
`…[key].image_id` at `test_integration_grader.py` L150 and L629 and
`test_integration_submodules.py` L228 and L472 (all re-locate).
`test_integration_node_task.py` L189 calls `build_base_image(REPO_ROOT, "node",
"22")` and is unchanged — that function still returns an id.

`tests/test_grade_script.py` names neither `build_base_images` nor
`task_resolver` (grepped at HEAD) and needs no change.

### 5.5 `bakeoff/scripts/mutation_check.py` — two anchors

Tuple shape `(name, file, exact_old_line, mutated_line, "<pytest -k selector>",
marker_expr)`. **Re-locate and verify uniqueness before adding**; the script
fails loudly on a stale anchor.

```python
    (
        # The measured defect, as a mutation. Accepting a tag whose labels say
        # something else is what an unconditional cache-hit `docker build -t`
        # did in reverse -- it repaired the mutation silently, seconds before
        # preflight's read-back could see it (measured 2026-09-02). With the
        # comparison never firing, a tag pointing anywhere is accepted as this
        # base and nothing downstream re-derives it.
        "images: accept a base tag whose labels say it is something else",
        "src/bakeoff/images.py",
        "        if labels.get(key) == want:",
        "        if True:",
        "tests/test_images.py -k mutated_to_another_version_is_rebuilt",
        "not integration",
    ),
    (
        # `null` from the daemon and a non-zero exit are different facts: an
        # image that exists and declares no labels versus nobody to ask. Both
        # rebuild, so the mutation is invisible in the DECISION -- it shows up
        # in `preflight`'s evidence, where `{}` says the gate looked and found
        # nothing and `None` says it could not look.
        "images: collapse an unlabelled image into an absent one",
        "src/bakeoff/images.py",
        '    return json.loads(probe.stdout.strip() or "null") or {}',
        '    return json.loads(probe.stdout.strip() or "null")',
        "tests/test_images.py -k image_labels_answers_three_ways",
        "not integration",
    ),
```

Anchor 1's test must use `_REPO_ROOT` (§5.1's fixture): with the comparison
mutated, `expected = base_labels(repo_root, …)` still executes, so a fake root
would kill the mutant with `FileNotFoundError` rather than with the assertion —
red for the wrong reason, which is what this gate exists to prevent.

---

## 6. Docs and backlog

**`docs/BUILDING-A-TASK-SET.md` §3.6 "Run the gate"** — the sentence "The first
run builds the base image, mirrors the upstream repo, builds the task image and
materializes the start state" is now only half true. Replace with:

> The first run builds the base image, mirrors the upstream repo, builds the
> task image and materializes the start state. Later runs **reuse** a base the
> daemon already carries: each base stamps three labels on itself
> (`bakeoff.base.runtime`, `bakeoff.base.version`, `bakeoff.base.dockerfile_sha`,
> a hash of the base Dockerfile and its build arg), and the driver rebuilds only
> when the tag does not say it is exactly that base. The banner says which
> happened, and why when it rebuilt:
>
> ```
> base py3.12  sha256:<12 hex>...  claude 2.1.220  (reused)
> base py3.13  sha256:<12 hex>...  claude 2.1.220  (built: the tag names no image)
> ```
>
> Editing `docker/eval-agent.Dockerfile` or `docker/eval-agent-node.Dockerfile`
> moves the sha and is picked up on the next run without a flag. What the label
> **cannot** see is upstream drift — a republished `python:3.12-slim-bookworm`,
> a new `claude.ai/install.sh`, a moved apt or npm package — and neither could
> the unconditional rebuild it replaced, whose cache is keyed on the instruction
> string. To take those:
>
> ```bash
> docker rmi bakeoff-eval-agent:base-python-3.12     # next run rebuilds
> # or, to re-pull the upstream base and re-run every step:
> cd bakeoff && docker build --pull --no-cache --build-arg BASE_PYTHON_VERSION=3.12 \
>   -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent:base-python-3.12 .
> ```
>
> Preflight's interpreter-mismatch refusal names the same two commands, because
> that is where an operator meets the problem.
>
> A base image built by hand without
> `--build-arg BAKEOFF_BASE_DOCKERFILE_SHA` carries an empty sha and will be
> rebuilt by the next driver run. That is intended: the drivers own these tags.

No digest is quoted in that example. Revision 1 used
`sha256:dfd2cc069bad...`, which is the real py3.12 base id today and which this
change invalidates on the day it ships.

**§10 "Failure modes that do not announce themselves"** — add one row:

> **A base tag pointing at the wrong image.** `docker tag` moves it and nothing
> in a record names the base. Before round 2 the driver repaired it silently via
> a cache-hit rebuild, which is how a probe of preflight's `python --version`
> read-back recorded PASS on a base it had deliberately broken (2026-09-02). The
> driver now names what it found — `(built: the tag said
> bakeoff.base.version='3.11', this base is '3.13')` — and preflight's
> read-back, which the label never substitutes for, remains the authority on
> what a python container actually runs. On node there is no interpreter
> read-back; the equivalent is the runner-pin re-assertion every task image
> build re-runs.

**`CLAUDE.md`** — no new invariant; the existing "Configuration is never reported
as observation" bullet already states the rule this change applies. No edit
unless a later reviewer asks.

**`TASKS.md`** — close both entries and open one.

- Item 13 (`grep -n 'rebuilds every base image' TASKS.md`, re-locate; ~L1091):
  delete, and add a `tasks/todo.md` review-log section recording (i) the fix,
  (ii) the M4 churn it routes around, and (iii) item 13's own request — that the
  read-back's base-tag shape is now **unreachable by construction** rather than
  unreachable by accident (M8: a tag cannot carry forged labels; an image built
  to carry them is still caught), that the other two shapes its comment names
  remain reachable through the driver, that the probe is python-only with
  `_NODE_RUNNER_REASSERTION` as node's equivalent, and that
  `test_an_interpreter_that_is_not_the_declared_one_is_refused` is the direct
  `preflight()` test pinning the refusal itself.
- Item 15 (`grep -n 'two dead pieces' TASKS.md`, re-locate; ~L1530): delete;
  record in `tasks/todo.md` that the drift pin the constant existed for survives
  as the Dockerfile-ARG ↔ `tasks._DEFAULT_PYTHON` assertion, and that
  `preflight._declared_python`'s docstring was the site that would otherwise have
  been stranded.

**New `TASKS.md` entry (review ruling O3), P3, in the same section as the other
environment findings.** Written out so the implementer transcribes it:

> - [ ] **The node base image's id is minted fresh on every `docker build`, and
>   nobody knows why.** Measured 2026-09-02 (Docker 29.5.2, legacy builder), two
>   independent runs of six consecutive `docker build -q --build-arg
>   BASE_NODE_VERSION=22 -f docker/eval-agent-node.Dockerfile`: **six different
>   image ids each time** (`6369b7fd`, `bf7f559d`, `edd8d5da`, `8f4b71d3`,
>   `a04c9675`, `5867d905`; and `fa169de6`, `2d8051bd`, `cd4e0b99`, `c4cf77a6`,
>   `4cd1be5f`, `55f2fdc9`), 3.1–4.7 s each. The step log is identical on every
>   run: steps 1–15 report `---> Using cache`, including the `npm install` and
>   the `curl … install.sh`, and **`Step 16/16 : ENTRYPOINT []` reports `--->
>   Running in …`** and mints a new image. The parent is stable (`WORKDIR /repo`
>   → `4c2027501df2`) and dozens of matching children of it already exist, so
>   the cache lookup misses with candidates available. **The identical
>   instruction on the python base caches**: three builds of
>   `eval-agent.Dockerfile` at 3.13 → `sha256:4eb69f78dc68` three times, `Step
>   14/14 : ENTRYPOINT [] ---> Using cache`; and a throwaway python Dockerfile
>   ending in `LABEL` is stable across three builds, so it is the chain and not
>   the instruction. **Appending the `bakeoff.base.*` LABEL block does not fix
>   it** (three builds: `d905b550`, `76a654fa`, `a6300e5b`). Round 2 item 13
>   routes *around* this — `build_base_images` now reuses a labelled base, so
>   the repeat invocation stops building at all — but every cold path still
>   churns: a machine with no node base, a new `_NODE_VERSIONS` entry, any edit
>   to `eval-agent-node.Dockerfile`, `docker rmi`, `--pull --no-cache`. Each
>   mints a fresh id, which re-renders `FROM <base id>` in every node task's
>   Dockerfile and invalidates `preflight_cache_key` and `oracle_fingerprint`
>   for that runtime, and each leaks a dangling image (713 on the machine that
>   measured this). Not urgent — nothing is *wrong*, only rebuilt — but the next
>   person to watch a node base id move should not have to re-derive any of
>   this. Cleaning up the accumulated dangling images is an operator
>   `docker image prune`, not a harness action.

---

## 7. Version constants

| constant | moves? | why |
|---|---|---|
| `preflight.PREFLIGHT_VERSION` | **yes**, +1 (re-locate; `"14"` in the working tree, `"13"` at HEAD `8232032`) | evidence gains `base_image_labels`; a verdict cached under the old value was written by a gate that never asked what the image claimed to be |
| `schema.SCHEMA_VERSION` | **no** | no `RunRecord` field changes; the labels live in the preflight evidence file |
| `grade_schema.GRADER_VERSION` | **no** | the grader is untouched |
| `oracle.ORACLE_VERSION` | **no** | the oracle is untouched; its cache is invalidated once by the base id move, which is data and not a version question |
| `TaskManifest` / `task_version` | **no** | no manifest key changes |

---

## 8. Verification

Run from `bakeoff/`, in order.

1. **Unit.** `.venv/bin/python -m pytest tests/ -v`. Baseline is
   `1539 passed, 62 deselected` (plus whatever items 1–12 added); expect that
   plus the new tests in §5.1–5.3. **That baseline is not reachable in the tree
   as of 2026-09-02** — 17 `test_preflight.py` tests fail on another item's
   in-flight `_NodeFlavour.select_args` edit. Item 13 is implemented after those
   land, so take the baseline from the tree at that point rather than from this
   number.
2. **Offline really is offline.** `PATH=/usr/bin:/bin .venv/bin/python -m pytest
   tests/test_preflight.py -q` with no `docker` on `PATH` — must pass, which is
   what §3.2's `except OSError` and §5.2's helper line exist for.
3. **Targeted.**
   `.venv/bin/python -m pytest tests/test_images.py tests/test_preflight.py tests/test_run_matrix.py -v`
4. **Mutation, solo** (nothing else may touch the tree):
   `.venv/bin/python scripts/mutation_check.py` — expect the baseline count plus
   the two anchors of §5.5, and no stale-anchor failure.
5. **§6.6 gate.** `.venv/bin/python scripts/verify_logger.py` → `GATE PASSED`.
6. **Integration, base labels.**
   `.venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest" tests/test_images.py`
7. **The measured defect, end to end.** Reproduce the probe's Finding 1 and show
   it now refuses to be silent. `~/.cache/bakeoff-probe/ts-py/werkzeug-3037-py313`
   is the vehicle (`image.python: "3.13"`, gated green on 2026-09-02).

   ```bash
   cd bakeoff
   # a) cold: the label is absent on the existing base, so it rebuilds once
   .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
     --task-set ~/.cache/bakeoff-probe/ts-py --tasks werkzeug-3037-py313
   #    expect: "... (built: the tag carries no bakeoff.base.* labels)"
   # b) warm: nothing to do
   .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
     --task-set ~/.cache/bakeoff-probe/ts-py --tasks werkzeug-3037-py313
   #    expect: "... (reused)", and the SAME sha256 as (a)
   # c) mutate the tag and re-run
   docker tag bakeoff-eval-agent:base-python-3.13 bakeoff-eval-agent:base-python-3.13-backup
   docker tag bakeoff-eval-agent:base-python-3.11 bakeoff-eval-agent:base-python-3.13
   .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
     --task-set ~/.cache/bakeoff-probe/ts-py --tasks werkzeug-3037-py313
   #    expect: "... (built: the tag said bakeoff.base.version='3.11', this base
   #             is '3.13')", then preflight PASS
   #    NOTE (c) only fires if the 3.11 base already carries labels; if it does
   #    not, the reason reads "the tag carries no bakeoff.base.* labels", which
   #    is also correct. Build the 3.11 base first to see the mismatch text.
   # d) restore -- the BACKUP IS TAGGED FROM THE REAL 3.13 IMAGE, which is the
   #    correction the probe's own Finding 2 had to make to its brief
   docker tag bakeoff-eval-agent:base-python-3.13-backup bakeoff-eval-agent:base-python-3.13
   docker rmi bakeoff-eval-agent:base-python-3.13-backup
   docker run --rm --entrypoint python bakeoff-eval-agent:base-python-3.13 --version
   #    expect: Python 3.13.15
   ```

8. **The M4 churn, routed around.** A node vehicle —
   `~/.cache/bakeoff-probe/taskset/yaml-474-single-newline-empty-value` (vitest):

   ```bash
   .venv/bin/python scripts/run_matrix.py --preflight-only \
     --task-set ~/.cache/bakeoff-probe/taskset --tasks yaml-474-single-newline-empty-value
   .venv/bin/python scripts/run_matrix.py --preflight-only \
     --task-set ~/.cache/bakeoff-probe/taskset --tasks yaml-474-single-newline-empty-value
   ```

   Expect the second invocation to print `(reused)` with the **same** base
   `sha256:`, the same task `image  sha256:`, and `preflight cached PASS` — none
   of which held before this change (six builds, six ids). Record both banner
   blocks in the commit message. This is the one place the node churn is
   observed end to end; §5.1's integration test is its offline sibling.

9. **Read back one evidence file.**
   `python -c "import json;print(json.load(open('$HOME/.cache/bakeoff/preflight/werkzeug-3037-py313.json'))['evidence']['base_image_labels'])"`
   → the three `bakeoff.base.*` keys, with `bakeoff.base.version == "3.13"`,
   beside `python_observed == "Python 3.13.15"`.

---

## 9. Open questions

All four of revision 1's questions were ruled on by review 1 and are adopted; the
rulings are folded into the sections above rather than left open.

- **O1 — no `--force-base-build` flag.** §2.4, with the ruling's condition met by
  §3.5(c) putting `docker rmi` and `--pull --no-cache` into preflight's refusal
  message, and §5.2's
  `test_the_mismatch_refusal_names_a_remedy_that_still_works_after_the_skip`.
- **O2 — renamed to `base_image_labels`** (plural), matching every other
  collection-valued evidence key (`submodules`, `runner_cache_flags`,
  `duplicate_full_names`, `scope_files_run`) against the scalar ones
  (`framework`, `python_observed`, `head`). Revision 1 attributed the singular to
  the round-2 brief; that was **wrong** — no file outside this plan contains the
  string, the name was the plan's own, and the attribution is withdrawn.
- **O3 — M4 gets its own `TASKS.md` entry.** Written out in §6.
- **O4 — `images._DEFAULT_PYTHON` is deleted**, conditional on §3.5(e) fixing
  `preflight._declared_python`'s docstring, which §4.2 now names as a required
  site rather than leaving to a sweep.

Nothing is open for review 2 beyond the usual: whether every transcribed string
matches the source it will replace.

---

## 10. What this does NOT do

- It does **not** grade, record, or refuse on a label. The label decides one
  thing — rebuild or reuse — and preflight's `python --version` read-back stays
  the authority on whether a **python** task may run;
  `images._NODE_RUNNER_REASSERTION`, re-run at every task image build, is node's
  equivalent.
- **It does not make the base tag an identity.** Labels describe **ancestry**:
  docker propagates them into every derived image (measured), and
  `render_dockerfile` emits no `LABEL`, so every `bakeoff-task-*` image claims to
  be the base it was built from. A base tag hand-mistagged onto a task image
  therefore passes `base_is_current`, is reused, and becomes the `FROM` of every
  task image in the set — carrying that other task's `/repo` scaffold, apt
  packages and pip pins into all of them, invisibly to a read-back that sees the
  same interpreter. The unconditional build healed exactly that state. There is
  no cheap discriminator (every label is inherited; entrypoint, workdir and layer
  count separate nothing here), the state needs a deliberate mistag of one
  specific tag onto one specific image to reach, and it is named here rather than
  papered over.
- **It does not stop the node base id churning; it stops the repeat invocation
  from triggering it.** Measured: appending the LABEL block leaves three builds
  giving three ids. Every cold path still mints a fresh id and still leaks a
  dangling image — a machine with no node base, a new `_NODE_VERSIONS` entry, any
  edit to `eval-agent-node.Dockerfile` (the fingerprint moves, so it *must*
  rebuild), the `docker rmi` remedy, the `--pull --no-cache` build, and the first
  run after this lands on every machine. That is correct rather than regrettable:
  a rebuild is supposed to produce a new image. §6's new backlog entry carries
  the unexplained mechanism.
- It does **not** make a base build reproducible or content-addressed. The
  fingerprint covers the Dockerfile text and one build arg; the upstream base
  image, the Claude Code installer, apt, pip and npm are unpinned by content
  before and after, and `docker build`'s own cache never pinned them either.
- It does **not** add a `RunRecord` field, move `SCHEMA_VERSION`, or change what
  any collection records about the base it ran in.
- It does **not** touch `assert_one_agent`, `resolve_tasks`, `resolve_task`,
  `build_task_image`, `render_dockerfile`, `preflight`'s ladder, the runner
  adapters, the grader or the oracle. `prepare_bases` and `task_resolver` unwrap
  the new type at the boundary so nothing downstream sees it.
- It does **not** clean up the dangling node images already on the machine —
  `docker image prune` is an operator action, not a harness one.
- It does **not** touch `bakeoff/.env`'s commented-out credential lines, which
  are the *other* bullet near item 15 in `TASKS.md` and a separate item.
- It does **not** change `tasks._DEFAULT_PYTHON`, `tasks._PYTHON_VERSIONS`, or
  the Dockerfile `ARG` default — item 15 removes the third copy of that value,
  not the value.
- **One-time costs it does incur, stated so they are not read as regressions.**
  Adding the `LABEL` block changes both base image ids once. Every task image is
  therefore rebuilt once (`FROM <base id>`), every cached preflight verdict is
  invalidated once (image id **and** `PREFLIGHT_VERSION` both move), and every
  cached oracle quarantine is invalidated once (`oracle_fingerprint` hashes the
  image). The python base's `sha256:dfd2cc069bad…`, which
  `docker/eval-agent.Dockerfile` records as having survived the `ARG` change, is
  one of the ids that moves — §3.1(c) dates the correction in the file rather
  than leaving a falsified measurement in place. Records already written keep the
  `container_image_digest` they carry; nothing in the append-only log is
  rewritten, and a reader comparing a pre-change digest with a post-change one is
  comparing two real images.

---

## Review 1 → changes

All 12 findings adopted; none disputed. All four §9 rulings adopted.

| # | finding | what changed |
|---|---|---|
| 1 | §3.3/§2.1/§8-6c disagreed on the banner; `(built)` alone cannot distinguish a cold machine from a mutated tag | `BaseImage` gains `reason: str \| None` (`None` iff `reused`); `base_is_current` returns `tuple[str \| None, str \| None]` and carries four written-out reasons; §3.3 prints `(reused)` / `(built: <reason>)`; §2.1, §6 and §8 now quote the same strings. New tests `test_a_reused_base_names_no_reason_and_a_built_one_always_does` (§5.1) and `test_a_cold_machine_and_a_mutated_tag_do_not_print_the_same_line` (§5.3) |
| 2 | existing `test_one_build_per_distinct_version_not_per_request` unnamed; breaks on the type **and** runs a real `docker image inspect` / `FileNotFoundError` | named in §5.1 as existing-updated, with both halves written out — monkeypatch `images.image_labels` → `None`, and the new `{key: base.image_id …}` assertion — plus the note that `Path("/r")` stays safe *because* the fake returns before any Dockerfile read |
| 3 | `image_labels` in `preflight` lands a real subprocess in 58 offline unit tests, and crashes all of them with no `docker` binary | §5.2 adds one `monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: {})` line inside `_run_preflight`, covering all 58 and every future test; §8 step 2 runs the suite with `docker` off `PATH` as the check |
| 4 | `None` documented as two different absences; the one it is *named* for escapes as an exception | `image_labels` wraps the `subprocess.run` in `except OSError: return None`; both docstrings and the §3.5(b) evidence comment now say `None` covers "no such image" **and** "the probe could not run", with `{}` reserved for "exists, no labels". Test renamed `test_a_label_probe_that_could_not_answer_is_a_null_not_an_empty_set`; new `test_image_labels_survives_a_missing_docker_binary` |
| 5 | label inheritance used as a feature and never accounted for as a weakening; §2.1's rule over-claimed | §2.1's rule restated as **ancestry, not identity**, with the residue named; a paragraph in `base_is_current`'s docstring; a paragraph in the Dockerfile LABEL comment; a full bullet in §10 spelling out the base-tag-onto-a-task-image state and that the unconditional build healed it |
| 6 | deleting the constant strands `preflight._declared_python`'s docstring, which the plan never pointed at | §3.5(e) names the site and writes the replacement paragraph out; §4.2 lists it as a **condition** on the deletion, with the post-change `git grep _DEFAULT_PYTHON` expectation |
| 7 | the LABEL falsifies a dated measured claim in `eval-agent.Dockerfile`, and §6 quoted a digest this change invalidates | §3.1(c) replaces the Dockerfile parenthetical with a dated correction; §6's banner examples use `sha256:<12 hex>...`; §10's one-time-costs bullet names `dfd2cc069bad…` as one of the ids that moves |
| 8 | preflight's mismatch refusal now names a remedy that can be a no-op | §3.5(c) rewrites the problem string to add `docker rmi bakeoff-eval-agent:base-python-<declared>` and `--pull --no-cache` with what each is for; pinned by `test_the_mismatch_refusal_names_a_remedy_that_still_works_after_the_skip` |
| 9 | four reuse tests' `repo_root` unstated; a `Path("/repo")` by analogy raises, and it weakens mutation anchor 1 | §5.1 defines `_REPO_ROOT = Path(__file__).resolve().parent.parent` as a module-scope fixture, names it in every row that reaches `base_labels`, and §5.5 states that anchor 1's test must use it or the mutant dies for the wrong reason |
| 10 | the integration test's "M4 churn control" controls for nothing — the python chain does not churn | the churn claim is deleted from the python test (label-superset only); new `test_a_node_base_is_reused_rather_than_reminted` (integration + task_image) asserts the second `build_base_images` returns `reused=True` with the same id on the **node** base; §8 step 8's real node task is named as its end-to-end sibling |
| 11 | O2's premise false — `base_image_label` appears in no file outside the plan | attribution withdrawn in §9; the name was the plan's own |
| 12 | the churn is stopped only on the warm path; the plan never said which paths stay cold | §2.1's side-effect paragraph now says "repeat invocation"; `build_base_images`' docstring says it; §10 gains a bullet listing all six cold paths; M4a records the reviewer's measurement that the LABEL alone does not fix it |
| O1 | no flag; condition: the remedy must appear where the failure does | adopted — §2.4, met by finding 8 |
| O2 | rename to `base_image_labels` | adopted — every occurrence in §3.5, §5.2, §6, §7, §8 |
| O3 | M4 gets its own backlog entry | adopted — written out in full in §6, carrying both runs' six ids, the step-16 log line, the python control, the LABEL-does-not-help measurement, and the `docker image prune` note |
| O4 | delete the constant, conditional on finding 6 | adopted — §4.2 with the condition stated |

Two reviewer measurements the plan did not have are folded in as first-class
evidence rather than footnotes: **M4a** (the LABEL block does not stop the node
churn — three builds, three ids — so only the skip does) and the **`ARG`
redeclare requirement** (without it the label stamps `""` silently, and the base
would be rebuilt forever with nothing saying why), which now drives a line in
§3.1's Dockerfile blocks, a comment explaining it, and an assertion in
`test_the_label_the_dockerfile_stamps_is_the_one_the_harness_expects`. **M8**
(labels cannot be moved by `docker tag`; a forged one mints a new id and still
answers 3.11.16 to the read-back) is added as a numbered measurement and is what
lets §2.1 say "unreachable by construction" without hand-waving.

### Review 2 → changes

Both open findings adopted; neither disputed. All four nits folded as well.

| # | finding | what changed |
|---|---|---|
| 13 | §3.5(a) wrote a hand seed immediately after a line item 5 **deletes**, and an evidence key that is not in `EVIDENCE_KEYS` makes `PreflightResult.__post_init__` raise on every path of every task — the plan modelled item 5 as a line-number mover | §3.5(a) is now conditional on `grep -n 'EVIDENCE_KEYS' src/bakeoff/preflight.py` and writes **both** branches. The landed branch adds `"base_image_labels"` to `EVIDENCE_KEYS` in write order immediately after `"python_observed"`, moves the `#:` rationale onto that entry (item 5's Task 2 rule: attach a finding to the key it explains, not to a seed statement that no longer exists), and adds **nothing** to `preflight()`'s body — `_evidence_seed()` seeds it, and a hand seed would rebuild the family-A reading item 5's tuple exists to remove. It also names item 5's two safety nets and which one fires when: `__post_init__` at run time, `test_evidence_keys_lists_exactly_what_preflight_writes` at edit time. The un-landed branch keeps revision 2's hand-written line and says to flag it for item 5 |
| 14 | §5.3's first row named a test that exists nowhere; the real one keeps a `_fake_build` returning plain strings, which §3.3's unwrap turns into `AttributeError` | renamed to `test_the_version_set_comes_from_the_task_set_not_from_the_allowlist` with its real coordinates (`tests/test_run_matrix.py:104`, `_fake_build(root, versions)` at L120, `bases ==` at L131-132), a note that it is the only test in the tree that fakes `build_base_images` and calls `prepare_bases`, and a note that it is review-1 finding 2's twin in the other file. The body revision 2 already wrote is correct and is kept verbatim |
| nit | "three written-out reasons" — there are four | corrected in the review-1 changes table |
| nit | §3.5(c) spelled `bakeoff-eval-agent:base-python-` by hand, a second copy of `images.base_tag`'s format inside a module that imports from `images` after §3.5 | the refusal string now interpolates `base_tag('python', declared_python)`, and §3.5's import line takes `base_tag` beside `image_labels`, with the one-definition argument stated |
| nit | a partially labelled image renders `…version=None…`, which reads like a value | left as the bare repr — a sentinel could collide with a real label — with a comment in `base_is_current` saying why: docker label values are always strings, so `None` there can only mean absent |
| nit | §8 step 1's `1539 passed` baseline is unreachable in the tree today (17 `test_preflight.py` failures from an in-flight `_NodeFlavour.select_args` edit) | step 1 now says so and tells the implementer to take the baseline from the tree at implementation time |

§5.2's `test_the_label_evidence_is_none_on_the_path_that_never_starts_a_container`
also picks up item 5's D4 `early_return` code, as finding 13 asked: it now asserts
`early_return == EARLY_RETURN_RUNNER_MISMATCH` on that route alongside the two
`None`s, and the row says explicitly that key *presence* on every route is item
5's `test_every_evidence_key_is_present_on_every_route` while this row asserts the
*value* — coverage that test does not have.
