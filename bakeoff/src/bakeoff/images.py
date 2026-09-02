"""Building the images a matrix runs in. See spec section 5.1.

Two layers, and the split is what keeps arms comparable:

  the BASE image (docker/eval-agent.Dockerfile) carries everything that must
  be identical for every task and every arm -- the pinned Claude Code, git,
  ripgrep, the non-root user, the mount points, the cleared entrypoint. It is
  built once per PYTHON VERSION, selected by a manifest's `image.python`, so
  one Dockerfile covers every allowed interpreter; every arm of a given task
  still runs the same one, which is what section 5.4 holds identical.

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

import subprocess
from collections.abc import Iterable
from pathlib import Path

PROXY_TAG = "bakeoff-litellm-proxy:matrix"

#: Restated rather than imported: `build_task_image` already imports `tasks`
#: locally rather than at module scope, and a module-scope import here would
#: undo that. Pinned equal to `tasks._DEFAULT_PYTHON` AND to the Dockerfile's
#: `ARG BASE_PYTHON_VERSION` default by
#: tests/test_images.py::test_every_copy_of_the_default_version_says_the_same_thing
#: -- three copies that can drift is a manifest declaring nothing loading as a
#: task whose base nobody built.
_DEFAULT_PYTHON = "3.12"


def base_tag(python_version: str) -> str:
    """The local tag for one base image.

    Per VERSION, because one tag for two interpreters means the second build
    silently replaces the first and every task afterwards resolves that tag to
    the wrong base. That failure builds, runs and goes green: the suite is
    executed by an interpreter the task was not cut for, and only preflight's
    `python --version` read-back says so.

    Named for the version rather than for Python. When broadening 7 adds a
    node base, this function and `build_base_images` are the only two places
    that learn about a second axis.
    """
    return f"bakeoff-eval-agent:base-{python_version}"


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
    import json

    value = json.loads(raw)
    return list(value or [])


def build_base_image(repo_root: Path, python_version: str = _DEFAULT_PYTHON) -> str:
    """Build docker/eval-agent.Dockerfile at one Python and return its image ID.

    The version reaches the build as `--build-arg BASE_PYTHON_VERSION`, which
    is what the Dockerfile's pre-FROM ARG consumes. See the comment there for
    why that name and not `PYTHON_VERSION`.

    No allowlist check here. `tasks._python_version` is the one gate, at load
    time with no daemon; a second copy of the set is how a value one gate
    accepted reaches a builder governed by another. This module cannot import
    `tasks` at module scope in any case -- `build_task_image` already imports
    `ensure_mirror` locally to avoid the cycle.
    """
    tag = base_tag(python_version)
    _run(
        [
            "docker", "build", "-q",
            "--build-arg", f"BASE_PYTHON_VERSION={python_version}",
            "-f", str(Path(repo_root) / "docker" / "eval-agent.Dockerfile"),
            "-t", tag, str(repo_root),
        ]
    )
    return image_id(tag)


def build_base_images(repo_root: Path,
                      versions: Iterable[str]) -> dict[str, str]:
    """Every base a task set needs, built once each, keyed by version.

    Callers pass one entry per TASK; this deduplicates. Building per task pays
    a full image build for every duplicate, and on a 60-task set that turns
    the free offline half of `--preflight-only` into something nobody waits
    for.

    Sorted, so a build log reads the same way twice and a failure names the
    same version first.
    """
    return {
        version: build_base_image(repo_root, version)
        for version in sorted(set(versions))
    }


def build_proxy_image(repo_root: Path, tag: str = PROXY_TAG) -> str:
    _run(
        [
            "docker", "build", "-q",
            "-f", str(Path(repo_root) / "docker" / "litellm-proxy.Dockerfile"),
            "-t", tag, str(repo_root),
        ]
    )
    return tag


def render_dockerfile(base_image: str, apt: list[str], pip: list[str],
                      build: list[str],
                      env: dict[str, str] | None = None) -> str:
    """The generated task Dockerfile, as text.

    Separate from the build so it is testable without a daemon: the
    structural guarantees this module claims -- ends as `USER eval`, clears
    ENTRYPOINT, never leaves /repo root-owned -- are properties of this
    string, and a test that has to build an image to check them is a test
    nobody runs.

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
    """
    from bakeoff.tasks import ensure_pruned_mirror, task_submodules

    for sub in task_submodules(task, cache_root):
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
    from bakeoff.tasks import ensure_mirror

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
        )
    )
    tag = f"bakeoff-task-{task.task_id}:v{task.task_version}"
    _run(["docker", "build", "-q", "-t", tag, str(context)])
    return image_id(tag)
