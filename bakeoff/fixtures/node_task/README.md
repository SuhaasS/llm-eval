# Node smoke task (broadening 7)

The node counterpart of `fixtures/smoke_task/`, and the smallest tree that
still exercises the whole node path: read a failing test, locate a
one-character bug in `src/calc.js`, edit it, **run the suite and see it pass**.
`add` returns `a - b`.

**This directory is a template, not a git repository, and not a task.**
`tests/test_integration_node_task.py` copies it under `$HOME` and runs
`git init` there, then writes a `task.yaml` naming that path as `repo.url` and
the commit it just made as `repo.base_sha`. A checked-in `task.yaml` cannot
replace that: `base_sha` does not exist until the commit does, and a sha
written in by hand goes stale the first time this tree changes. Committing a
`.git` inside would make the outer repository record an embedded-repo gitlink
rather than these files.

The two tests are the two halves a task needs, and neither is decoration:

    tests/calc.test.js::adds two numbers          -- f2p; red before the fix
    tests/calc.test.js::keeps subtracting elsewhere -- p2p; green on both sides

The node id shape is `<file>::<fullName>`: the file, `::`, then the reporter's
`fullName`, which is the enclosing `describe` titles and the test title joined
by single spaces. This fixture has no `describe` block, so `fullName` is the
title. Without the p2p test a green p2p run is vacuous and the fixture would
not exercise the grader's check 6 at all.

The f2p test arrives with the TEST HALF rather than living here, exactly as it
does for a real task -- the start state is `base_sha` plus that half -- so the
integration fixture overwrites this file with the p2p test alone before
committing.

## Why the bug is an operator swap

`a - b` -> `a + b` preserves the file's byte count, and the integration test
re-runs the suite inside the same second. That pair is precisely the input that
made CPython serve stale bytecode in this repository's own eval image on
2026-08-13 -- `.pyc` invalidation keys on (source mtime in whole seconds,
source size), and an operator swap moves neither -- feeding spec section 3.3's
"runs tests, sees failures, self-corrects" loop the OLD behaviour after a
correct fix, so the agent corrects away from the right answer and is scored on
it. `PYTHONDONTWRITEBYTECODE=1` closed that on the python base. The node base
declares no analogue, because measured 2026-09-01 there is nothing to close:
vite's `.vite` directory is a DEPENDENCY optimiser cache rather than a
source-transform one, and jest's cache is content-hash keyed. This fixture is
where that measurement stops being a probe result and becomes a test.

## No dependencies

`package.json` declares none, and that is deliberate: vitest and jest are
installed at `/node_modules` by the base image (the plan's D4), which node's
resolver reaches by walking up from `/repo/tests/`. So this task's manifest
needs no `image.build` at all. A task whose repository DOES have dependencies
declares `build: ["npm install --prefix / --omit=dev"]` -- never `npm ci`,
which deletes the tree at the prefix and takes the pinned runners with it.

## Verify the fixture itself, not just the harness

    vitest run --no-cache                # 1 failed: expected -1 to be 5
    # change src/calc.js to `a + b`
    vitest run --no-cache                # 2 passed
