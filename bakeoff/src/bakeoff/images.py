"""Building the images a matrix runs in. See spec section 5.1.

Two layers, and the split is what keeps arms comparable:

  the BASE image (docker/eval-agent.Dockerfile) carries everything that must
  be identical for every task and every arm -- the pinned Claude Code, git,
  ripgrep, the non-root user, the mount points, the cleared entrypoint. It is
  built once per (RUNTIME, VERSION) -- `("python", "3.12")`, `("node", "22")`
  -- selected by `tasks.task_runtime`, so one Dockerfile per runtime covers
  every allowed version of it; every arm of a given task still runs the same
  one, which is what section 5.4 holds identical.

  the TASK image adds that task's dependencies, and nothing else.

Task Dockerfiles are GENERATED rather than hand-written, because the two most
expensive ways a task image can be wrong are structural:

  An image left as `USER root` makes Claude Code refuse `bypassPermissions`
  -- "cannot be used with root/sudo privileges for security reasons" -- and
  exit before emitting one stream-json event. Every arm records zero turns.

  An image that inherits an ENTRYPOINT turns RunContainer's `sleep infinity`
  into an argument to that entrypoint, so the container exits immediately and
  every exec fails.

Both read as total model failure and neither is visible in a record. A
generator cannot emit them; a template a task author edits can. That is the
whole reason the escape hatch does not exist yet.

`/repo` inside a built task image is a SCAFFOLD, present so dependency
resolution has something to resolve against. At run time the bind mount
replaces it. This is what makes `pip install -e .` correct and `pip install .`
silently wrong -- a non-editable install resolves imports to site-packages, so
the agent's edits to /repo change nothing and every arm fails identically.
preflight's green-after check is what catches that, and it is the reason that
check exists rather than being assumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROXY_TAG = "bakeoff-litellm-proxy:matrix"

#: One Dockerfile and one build arg per runtime. A dict rather than an
#: if/else so adding a runtime is one entry and the two facts about it cannot
#: drift apart.
#:
#: Two FILES rather than one with a switched `FROM`, on differences that are
#: not parameters: node:22-bookworm-slim already occupies uid 1000 with a
#: `node` user, so the python file's `useradd` fails there with exit 4
#: (measured 2026-09-01), and a shell conditional around a useradd is the
#: shape that half-succeeds and leaves an image running as root -- which
#: Claude Code refuses, emitting zero events on every arm. See the plan's D3.
_BASES = {
    "python": ("eval-agent.Dockerfile", "BASE_PYTHON_VERSION"),
    "node": ("eval-agent-node.Dockerfile", "BASE_NODE_VERSION"),
}


def base_tag(runtime: str, version: str) -> str:
    """The local tag for one base image.

    Per (RUNTIME, VERSION), because one tag for two bases means the second
    build silently replaces the first and every task afterwards resolves that
    tag to the wrong one. That failure builds, runs and goes green: the suite
    is executed by an interpreter -- or a runtime -- the task was not cut for,
    and only preflight's read-back says so.

    `bakeoff-eval-agent:base-3.12` became `base-python-3.12` when node joined.
    The tag STRING is the only thing that moved: the python Dockerfile is
    unchanged, so its image ID is unchanged, and every cache in this repo keys
    on the ID rather than the tag -- `preflight_cache_key`, `oracle_fingerprint`
    and `Versions.container_image_digest` alike. So no warm verdict is
    invalidated by this rename, and the old tag is left orphaned on any machine
    that has one, pointing at the same image.
    """
    if runtime not in _BASES:
        raise ImageError(f"no base image is defined for runtime {runtime!r}")
    return f"bakeoff-eval-agent:base-{runtime}-{version}"


class ImageError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ImageError(
            f"{' '.join(args[:3])}... failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def image_id(tag: str) -> str:
    """The image ID, not a tag.

    RunContainer refuses tags (spec section 5.1) and a locally built image has
    no registry digest until it is pushed, so the config digest -- which
    transitively pins every layer -- is the available content pin.
    """
    return _run(["docker", "inspect", "--format", "{{.Id}}", tag])


def image_entrypoint(tag: str) -> list[str]:
    raw = _run(["docker", "inspect", "--format", "{{json .Config.Entrypoint}}", tag])
    value = json.loads(raw)
    return list(value or [])


def _repo_paths_in_image(image: str) -> set[str]:
    # "/repo" is render_dockerfile's `COPY repo /repo`, which is the only place
    # this path is decided.
    #
    # No `.git` filter here, on purpose: `git archive` never writes one, so in
    # the normal case there is nothing to filter, and an `image.build` step that
    # ran `git init` leaves residue `docker cp` DOES export (measured
    # 2026-09-02) and an author does need to see. `_tree_paths` prunes the run
    # tree's own `.git` instead -- that is the clone's metadata, not repository
    # content, and letting it cancel build residue by name would hide exactly
    # this case.
    container = _run(["docker", "create", "--entrypoint", "true", image])
    try:
        proc = subprocess.Popen(
            ["docker", "cp", f"{container}:/repo/.", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        paths: set[str] = set()
        try:
            with tarfile.open(mode="r|", fileobj=proc.stdout) as tar:
                for member in tar:
                    if not (member.isfile() or member.issym() or member.islnk()):
                        continue
                    name = member.name[2:] if member.name.startswith("./") else member.name
                    if name:
                        paths.add(name)
        finally:
            # stderr is read only after the stream is drained or closed. Docker
            # writes progress to stderr, so a pipe that filled DURING the stream
            # would deadlock -- measured 2026-09-02 across all nine probe images
            # (up to 15 MB streamed, 0.08-1.75 s, `wait()` 0 every time, no
            # truncation and no hang), and closing stdout first gives `docker cp`
            # an EPIPE rather than a reader that never returns.
            if proc.stdout is not None:
                proc.stdout.close()
            stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            code = proc.wait()
        if code != 0:
            raise ImageError(f"docker cp {image}:/repo failed (exit {code}):\n{stderr}")
        return paths
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def _tree_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        here = Path(dirpath)
        for name in filenames:
            if name == ".git":       # a submodule's gitfile
                continue
            paths.add((here / name).relative_to(root).as_posix())
        for name in dirnames:
            if (here / name).is_symlink():
                paths.add((here / name).relative_to(root).as_posix())
    return paths


def scaffold_only_paths(image: str, run_tree: Path) -> list[str]:
    """Paths the built task image's `/repo` has that the run tree does not.

    `image.build` (`build_task_image`, below) runs against the build-time
    scaffold -- a `git archive base_sha` context -- and `tasks.materialize`
    builds the run tree separately, from a `--local` clone of the pruned
    mirror, and has never seen anything the build wrote. `container.
    RunContainer.__enter__` bind-mounts the run tree over `/repo` in full, so
    a file the build generated is visible only during the build and to no
    process afterward -- not the agent's `claude`, not the commands it
    invents, not preflight's suite runs, not the oracle's, not the grader's.

    Measured 2026-09-02: click (`flit_core` backend) writes nothing into the
    tree it built from -- its 149 build-context files and its image's 149
    `/repo` files agree name for name. `sqlglot-6927` and `pytest-10210`
    (both `setuptools`/`setuptools_scm`) write a `_version.py` plus an
    `*.egg-info/` apiece -- six and seven paths -- because `setuptools_scm`
    derives a version at build time and writes it into the tree, where
    `flit_core` writes into site-packages and never touches `/repo` at all.

    THIS MEASUREMENT REFUSES NOTHING. "The build generated this path" and
    "the run needs this path" are different claims, and no property of a
    path name tells them apart -- `sqlglot-6927` is a perfectly good task
    that generates six files whose absence its own `try/except ImportError`
    tolerates. What refuses is `preflight`'s bare-runner exit-code check,
    which measures the CONSEQUENCE rather than the path list.
    """
    return sorted(_repo_paths_in_image(image) - _tree_paths(Path(run_tree)))


@dataclass(frozen=True)
class BaseImage:
    """One base, whether this invocation had to build it, and why.

    A bare id cannot carry the second fact, and the driver's banner is the only
    place an operator learns that a Dockerfile edit was picked up -- or that a
    tag was found saying it was something else. `(built)` alone would not do
    that: it is byte-identical between a cold machine and a mutated tag, which
    is the "two absences that render identically" shape this repository refuses
    everywhere else. Returning the reason is also what keeps `prepare_bases`
    from asking the same question a second time for its message (the plan's
    section 2.7).

    `reason` is `None` exactly when `reused` is True.
    """
    image_id: str
    reused: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.reason is None) is not self.reused:
            raise ValueError(
                f"reused={self.reused} with reason={self.reason!r}: a built base "
                "must name why, and a reused one has nothing to name"
            )


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


def build_base_image(repo_root: Path, runtime: str, version: str) -> str:
    """Build one runtime's base Dockerfile at one version and return its ID.

    The version reaches the build as `--build-arg BASE_<RUNTIME>_VERSION`,
    which is what that Dockerfile's pre-FROM ARG consumes. See the comment
    there for why that name and not the bare `PYTHON_VERSION`/`NODE_VERSION`:
    both official base images set one as an ENV, and ENV beats a redeclared
    ARG after FROM, so the expansion would silently read the patch level.

    No allowlist check here. `tasks._python_version` and `tasks._node_version`
    are the gates, at load time with no daemon; a second copy of either set is
    how a value one gate accepted reaches a builder governed by another. This
    module cannot import `tasks` at module scope in any case --
    `build_task_image` already imports `ensure_mirror` locally to avoid the
    cycle.
    """
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

    Callers pass one entry per TASK; this deduplicates. Building per task pays
    a full image build for every duplicate, and on a 60-task set that turns
    the free offline half of `--preflight-only` into something nobody waits
    for.

    Sorted, so a build log reads the same way twice and a failure names the
    same base first.
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


def build_proxy_image(repo_root: Path, tag: str = PROXY_TAG) -> str:
    _run(
        [
            "docker", "build", "-q",
            "-f", str(Path(repo_root) / "docker" / "litellm-proxy.Dockerfile"),
            "-t", tag, str(repo_root),
        ]
    )
    return tag


#: The plan's D15, emitted after the last `image.build` RUN and only on a node
#: base. A task's own dependency install can REMOVE the pinned runners: `npm
#: ci`'s documented contract is to delete the installed tree before installing,
#: and at the `/` prefix that is exactly where the base put them. Whether it
#: fires depends on which package.json/package-lock.json pair npm resolves for
#: the given prefix and cwd -- which an `image.build` line decides by accident,
#: so `HARVESTING.md` can recommend `npm install --prefix / --omit=dev` but
#: cannot enforce it, and a convention that is right only under an unstated cwd
#: is one a task will eventually violate.
#:
#: A BUILD-time check rather than a preflight one on purpose. Preflight would
#: catch it too -- the f2p run would exit 127 -- but it would report it as a
#: broken ENVIRONMENT on a task whose image was already built and served from
#: the task-image cache, and the operator's remedy is a rebuild either way.
#: Failing the build names the cause at the step that caused it.
#:
#: The expected versions come from the BASE IMAGE's own ENV, never from a
#: second constant here: two copies of a pin is how one moves. Docker expands
#: an inherited ENV in a derived build, so `${BAKEOFF_VITEST_VERSION}` below is
#: the value the base actually installed and asserted -- verified against a
#: daemon 2026-09-02 rather than assumed: a derived build whose `image.build`
#: deleted /node_modules/jest failed HERE, naming the pin, and one running
#: `npm install --prefix / --omit=dev` built clean.
#:
#: Both runners are read from their INSTALLED package.json rather than from
#: `--version`, matching the base. For jest that is forced: measured
#: 2026-09-02, jest 30.5.0's CLI answers 30.4.2 from a stale string inlined
#: into @jest/core's published bundle, so a `--version` comparison would fail
#: every node task image build -- on every arm, over an upstream packaging bug
#: the model never saw.
#:
#: THE EMPTY PIN IS REFUSED FIRST, and without that line this whole check
#: passed VACUOUSLY on exactly the base it exists to defend. `${BAKEOFF_*}`
#: expands to the empty string when the base declares no such ENV -- a python
#: base, a hand-built one, a node base edited to drop the pair -- and
#: `case "" in "") ;;` MATCHES, so the vitest half went green against nothing.
#: jest's half was worse: `"${BAKEOFF_JEST_VERSION}"*` degrades to the bare
#: glob `*`, which matches every string including the empty one `node -p`
#: leaves behind when /node_modules/jest is gone. So the shape that could only
#: be caught here -- an `image.build` that deleted the runners, on a base whose
#: pins are missing -- built clean. Both halves are fixed together: the `-n`
#: guard, and an EXACT jest match (the trailing `*` bought nothing; the base
#: asserts the same package.json string exactly).
_NODE_RUNNER_REASSERTION = r'''RUN [ -n "$BAKEOFF_VITEST_VERSION" ] && [ -n "$BAKEOFF_JEST_VERSION" ] || { echo "the base declares no runner pins -- not a node base" >&2; exit 1; }; \
    v="$(node -p 'require("/node_modules/vitest/package.json").version' 2>/dev/null)"; \
    j="$(node -p 'require("/node_modules/jest/package.json").version' 2>/dev/null)"; \
    case "$v" in "${BAKEOFF_VITEST_VERSION}") ;; \
      *) echo "image.build changed the pinned test runner: expected vitest ${BAKEOFF_VITEST_VERSION}, got '${v}'. \
Use 'npm install --prefix / --omit=dev', never 'npm ci' -- npm ci deletes node_modules at the prefix before installing, and the cwd where it does not delete them is the one where it installs none of yours instead. The base saves the runners under 'dependencies', so --omit=dev never prunes them." >&2; exit 1 ;; \
    esac; \
    case "$j" in "${BAKEOFF_JEST_VERSION}") ;; \
      *) echo "image.build changed the pinned test runner: expected jest ${BAKEOFF_JEST_VERSION}, got '${j}'. This reads jest's installed package.json, never 'jest --version' -- measured 2026-09-02, jest 30.5.0's CLI answers 30.4.2 from a stale string inlined into @jest/core's bundle." >&2; exit 1 ;; \
    esac'''


def render_dockerfile(base_image: str, apt: list[str], pip: list[str],
                      build: list[str],
                      env: dict[str, str] | None = None,
                      runtime: str = "python") -> str:
    """The generated task Dockerfile, as text.

    Separate from the build so it is testable without a daemon: the
    structural guarantees this module claims -- ends as `USER eval`, clears
    ENTRYPOINT, never leaves /repo root-owned -- are properties of this
    string, and a test that has to build an image to check them is a test
    nobody runs.

    `runtime` selects the D15 re-assertion and nothing else: on `"node"` the
    generated file re-checks the base's pinned test runners after the last
    `image.build` step. `"python"` renders exactly the text this function
    rendered before the parameter existed.

    `env` is emitted AFTER every RUN, which is what keeps it out of the build.
    Placement does not change the final image config, but it decides what the
    build steps see -- and `image.build` is arbitrary shell, so a variable
    declared to make a test runner deterministic must not silently change how
    the image was assembled. Sorted and one line per key, so the file text
    (and therefore the image id `Versions.container_image_digest` records) is
    a function of the manifest rather than of YAML key order.
    """
    lines = [
        "# GENERATED by bakeoff.images.render_dockerfile. Do not edit by hand:",
        "# the structure below is what makes a root-owned or entrypoint-bearing",
        "# task image impossible, and both of those read as total model failure.",
        f"FROM {base_image}",
        "USER root",
    ]
    if apt:
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            + " ".join(apt)
            + " && rm -rf /var/lib/apt/lists/*"
        )
    if pip:
        # Task pins WIN over the base image's. The base pins pytest so the
        # reference fixture has a runner; a real repository pins its own, and
        # they will not always agree -- measured on pallets/click, whose suite
        # does not COLLECT under the base image's pytest 9.1.1 because the
        # repo sets `filterwarnings = error` and 9.x raises a deprecation
        # warning during collection. Section 5.1's "dependencies from
        # lockfile" is per task, so the task's pin has to come last.
        lines.append("RUN pip install --no-cache-dir " + " ".join(
            f'"{package}"' for package in pip
        ))
    # The scaffold. See the module docstring: replaced by the bind mount at
    # run time, and present only so `pip install -e .` has a project to read.
    lines.append("COPY repo /repo")
    for command in build:
        lines.append(f"RUN cd /repo && {command}")
    # AFTER the last image.build step, because that is what it is checking, and
    # ONLY for a node base -- so a python task image renders byte-identically
    # to what it rendered before this parameter existed, and no stored
    # `container_image_digest` or warm preflight verdict moves. That is also
    # why `runtime` defaults rather than being required.
    if runtime == "node":
        lines.append(_NODE_RUNNER_REASSERTION)
    lines.extend(
        [
            # The bind mount overrides this on macOS (virtiofs ignores
            # ownership) but not on a Linux host, where a root-owned /repo
            # left by the build would make the scaffold unreadable to the
            # eval user if the mount ever failed to attach.
            "RUN chown -R eval:eval /repo",
        ]
    )
    # AFTER every RUN, the chown included. See the docstring: an ENV here reaches every
    # process in the finished container -- preflight's runner, the oracle's,
    # the grader's, the agent's `claude` and the commands the agent invents --
    # and reaches no build step. Docker merges this into every exec's
    # environment, with the exec's own keys winning (measured 2026-09-01,
    # Docker 29.5.2), and `claude_runner.container_env` sets none of the keys
    # `tasks._IMAGE_ENV_ALLOWED` permits.
    for key in sorted(env or {}):
        lines.append(f'ENV {key}="{env[key]}"')
    lines.extend(
        [
            "USER eval",
            "WORKDIR /repo",
            "ENTRYPOINT []",
            "",
        ]
    )
    return "\n".join(lines)


def _strip_build_context(repo_dir: Path, strip_paths: list[str]) -> None:
    """Remove the manifest's `strip_paths` from the unpacked build context.

    The context is `git archive base_sha`, not the start state, so a stripped
    path is still here. For an agent file that is harmless -- the bind mount
    replaces /repo at run time and the image is never what the agent reads --
    but for the other half of what the key is for it is not: a committed venv
    or vendored tree left in the scaffold is on the import path when
    `image.build` runs `pip install -e .`, so the environment the image pins
    is resolved against a directory the run tree does not have, and nothing
    downstream compares the two. Same class as a non-editable install: the
    image and the run disagree silently, and preflight's green-after check
    only catches it when the disagreement happens to break the suite.

    The named paths' CONTENT goes; an emptied parent directory can remain
    (stripping `vendor/dep.py` leaves `vendor/`, while git tracks no
    directories so the run tree has none). That residue is deliberate: an
    empty directory carries no module and no dependency, and pruning parents
    would start guessing at which of them the archive was supposed to have.

    A path that matches nothing is NOT an error. `tasks._strip_paths_from_tree`
    raises on exactly that, and `run_matrix` builds the image before
    materializing, so raising here too means one typo is reported twice. That
    includes a path through a DANGLING intermediate symlink (`docs` pointing
    nowhere, `strip_paths: ["docs/x"]`): `target.is_symlink()` and
    `target.exists()` both read as absent for it (`lstat` fails on a
    component that resolves to nothing, and pathlib turns that `OSError` into
    `False` rather than raising), so it is a no-op, checked BEFORE the escape
    guard below. Only a path that resolves to something -- inside `repo_dir`
    or out -- reaches that guard; "nothing is there" and "something is there,
    outside repo_dir" are different claims and get different outcomes.

    `is_symlink()` before `is_dir()`: `is_dir()` follows the link and
    `shutil.rmtree` then raises "Cannot call rmtree on a symbolic link".
    Measured upstream -- sqlglot's CLAUDE.md is a symlink to AGENTS.md, which
    is why taskset/HARVESTING.md records that repository's date floor as
    covering both names.

    Escaping `repo_dir` through the DECLARED path itself is refused upstream:
    `tasks._validate_strip_paths` rejects an absolute entry, one carrying
    `..`, and one carrying pathspec magic. That does not cover an
    INTERMEDIATE symlink -- `git archive` preserves symlinks, so an upstream
    `docs -> /elsewhere` plus a strip path of `docs/x`, with a real file at
    `elsewhere/x`, resolves outside `repo_dir` and would unlink there, since
    only the final path component is ever checked with `is_symlink()`.
    Refused here too, once the no-op check above says something is actually
    there: the resolved parent of `target` must stay under `repo_dir`, or
    this raises rather than deleting outside it. (`tasks.materialize` would
    also refuse the same manifest afterward -- `git ls-files` sees nothing
    tracked through the escaped path -- but only after this step already
    deleted the wrong file.)

    A case-insensitive host filesystem is a second, unrelated way for this
    step and `tasks._strip_paths_from_tree` to disagree: `strip_paths:
    ["claude.md"]` against a tracked `CLAUDE.md` deletes it here, because
    APFS resolves the differently-cased path to the same file, while `git
    ls-files` treats the pathspec as case-sensitive, finds no tracked match,
    and `materialize` raises. Same outcome as the symlink case -- the
    manifest is refused, but by the later stage, after this one already
    mutated the build context.
    """
    import shutil

    repo_root = Path(repo_dir).resolve()
    for path in strip_paths:
        target = Path(repo_dir) / path
        if not target.is_symlink() and not target.exists():
            continue
        parent = target.parent.resolve()
        if repo_root != parent and repo_root not in parent.parents:
            raise ImageError(f"strip path {path!r} escapes repo_dir via a symlink")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def _extract_submodules(task, repo_dir: Path, cache_root: Path) -> None:
    """A second `git archive` per submodule, into the empty directory the first
    one left.

    Measured 2026-09-01 (git 2.50.1): `git archive <base_sha>` emits
    `.gitmodules` and an EMPTY directory entry for each submodule path. So the
    image built from a one-archive context has a directory the run tree will
    have content in -- `pip install -e .` resolves against the wrong tree, and
    nothing downstream compares the two. Same class as a non-editable install:
    the image and the run disagree silently.

    From the PRUNED mirror, not the full one, so the image layer and the run
    tree are built from the same object set. Nothing here writes a `.git`, and
    the tree is `base_sha`'s, so the test half -- the oracle -- is still absent;
    that is why this is two archives rather than a copy of a materialized run
    tree, which would carry both.

    A submodule declared unneeded in the manifest is skipped: no mirror, no
    archive, no `mkdir`. The empty directory the image needs is already in the
    context -- measured 2026-09-02, `git archive <base_sha> | tar -x` creates
    the gitlink's path as an empty directory -- so re-creating it here would
    make the image's tree an artifact of this function rather than of
    `base_sha`'s, which is not the same claim the moment anything reorders
    around the strip.
    """
    from bakeoff.tasks import ensure_pruned_mirror, task_submodules

    for sub in task_submodules(task, cache_root):
        if sub.declared_unneeded:
            continue
        mirror = ensure_pruned_mirror(sub.url, sub.sha, cache_root)
        target = Path(repo_dir) / sub.path
        target.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "archive", "--format=tar", sub.sha],
            cwd=mirror, capture_output=True,
        )
        if archive.returncode != 0:
            raise ImageError(
                f"git archive {sub.sha} for submodule {sub.path} failed: "
                f"{archive.stderr.decode('utf-8', 'replace')}"
            )
        extract = subprocess.run(
            ["tar", "-x", "-C", str(target)], input=archive.stdout,
            capture_output=True,
        )
        if extract.returncode != 0:
            raise ImageError(
                f"unpacking submodule {sub.path} at {sub.sha} failed: "
                f"{extract.stderr.decode('utf-8', 'replace')}"
            )


def build_task_image(
    task, base_image: str, build_root: Path, cache_root: Path
) -> str:
    """Build one task's image and return its image ID.

    The build context is `git archive base_sha`, plus one `git archive` per
    submodule gitlink (`_extract_submodules`), never the materialized run
    tree: the image must be a function of the manifest alone, and a context
    that included the test patch would bake the oracle into a layer where no
    later step could tell it apart from a dependency.
    """
    from bakeoff.tasks import ensure_mirror, task_runtime

    build_root = Path(build_root)
    context = build_root / f"image-{task.task_id}"
    repo_dir = context / "repo"
    if context.exists():
        import shutil

        shutil.rmtree(context)
    repo_dir.mkdir(parents=True)

    mirror = ensure_mirror(task.repo_url, task.base_sha, cache_root)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", task.base_sha],
        cwd=mirror, capture_output=True,
    )
    if archive.returncode != 0:
        raise ImageError(
            f"git archive {task.base_sha} failed: "
            f"{archive.stderr.decode('utf-8', 'replace')}"
        )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(repo_dir)], input=archive.stdout, capture_output=True
    )
    if extract.returncode != 0:
        raise ImageError(
            f"unpacking {task.base_sha} failed: "
            f"{extract.stderr.decode('utf-8', 'replace')}"
        )

    # ORDER IS DEFENCE IN DEPTH HERE, and it is worth saying that it is no
    # longer the primary guard. The shape that separates the two orders by
    # OUTCOME is a `strip_paths` entry that is an ANCESTOR of a submodule path
    # (`vendor`, with the submodule at `vendor/libdep`): extract-then-strip
    # honours it, while strip-then-extract deletes a tree that is not there
    # yet, is a silent no-op (a path matching nothing is deliberately not an
    # error, see `_strip_build_context`), and writes the submodule content
    # back into the context the operator asked to have it removed from -- on
    # the import path when `image.build` runs `pip install -e .`.
    #
    # That manifest is now refused at LOAD, in both directions
    # (`tasks._refuse_submodule_conflicts`), because it does something worse
    # at materialization time than it does here: the strip removes the
    # gitlink and `_init_submodules` then chdirs into a directory that no
    # longer exists. `_extract_submodules` goes through `task_submodules`, so
    # that refusal fires on the line below, before the strip runs at all.
    # The order stays because it is the one that is correct without the
    # refusal, and the refusal lives in another module.
    _extract_submodules(task, repo_dir, cache_root)
    _strip_build_context(repo_dir, list(task.strip_paths))

    (context / "Dockerfile").write_text(
        render_dockerfile(
            base_image,
            list(task.image.apt),
            list(task.image.pip),
            list(task.image.build),
            env=dict(task.image.env),
            # Derived from the TASK, never from `base_image` -- which is an
            # image id and says nothing about what is inside it. Half of D15 is
            # `render_dockerfile` growing the parameter; the other half is this
            # line, and a default that is never overridden is a check that
            # never runs.
            runtime=task_runtime(task)[0],
        )
    )
    tag = f"bakeoff-task-{task.task_id}:v{task.task_version}"
    _run(["docker", "build", "-q", "-t", tag, str(context)])
    return image_id(tag)
