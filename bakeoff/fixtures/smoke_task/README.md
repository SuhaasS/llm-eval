# Smoke task (spec Phase 0c)

The smallest task that still exercises the whole loop: read a failing test,
locate a one-character bug, edit a file. `add` returns `a - b`.

**This directory is a template, not a git repository.** `scripts/smoke_test.py`
copies it under `$HOME` and runs `git init` there at run time, deriving
`base_sha` from the commit it just made.

Committing it with a `.git` inside would make git record an embedded-repo
gitlink rather than the files, and `base_sha` written into the plan by hand
would go stale the first time the fixture changed.
