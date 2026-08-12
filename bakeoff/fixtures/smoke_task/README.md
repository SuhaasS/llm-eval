# Smoke task (spec Phase 0c)

The smallest task that still exercises the whole loop: read a failing test,
locate a one-character bug, edit a file, **run the test and see it pass**.
`add` returns `a - b`.

The last step is the one that has to keep working. `pytest.ini` is what makes
`from calc import add` resolve, and the eval image is what supplies `pytest`.
Break either and the fixture is red before the fix and red after it, so a
correct run and an idle run leave identical evidence -- which is how Phase 0c
scored an environment defect as a model failure.

Verify the fixture itself, not just the harness:

    pytest -q        # 1 failed: assert -1 == 5
    # change calc.py to `a + b`
    pytest -q        # 1 passed

**This directory is a template, not a git repository.** `scripts/smoke_test.py`
copies it under `$HOME` and runs `git init` there at run time, deriving
`base_sha` from the commit it just made.

Committing it with a `.git` inside would make git record an embedded-repo
gitlink rather than the files, and `base_sha` written into the plan by hand
would go stale the first time the fixture changed.
