# Lessons

Patterns worth not repeating. One entry per mistake, with what it cost and the
rule that prevents it.

## A reused path makes a stale artifact read as a current measurement

**2026-08-13.** Twice in one day, in two different systems, from the same cause.

**In the harness.** `run_matrix`'s artifacts root was `CACHE/artifacts/<cell>` —
a pure function of the cell, with no collection episode in it — and `run_cell`
rmtree'd that directory before every attempt. A second matrix over the same
cells deleted the first's artifacts and left its records pointing at the
replacement. 12 of 24 stored records disagree with the file they point at, and
`WireLogger`'s `"x"` guard never fired because the delete came first. Fixed by
stamping the root per invocation and adding `collection_id`.

**In my own verification, hours later.** I wrote `verify_logger.py` output to
`/tmp/vl3.log` and armed a monitor that waited for `GATE …` to appear in it. The
file already existed from 12 hours earlier. The monitor fired instantly on the
old content and **I reported a gate failure that had not happened** — the giveaway
was in the file itself: `"schema_version": "3.6.0"`, from before that day's bump.

**Why it is the same bug.** In both cases the identity of an artifact was its
path, the path was not unique across episodes, and the reader had no way to tell
which episode it was holding.

**Rules:**

- A file that records the result of a run must be named for *that run*, not for
  its purpose. `/tmp/gate.log` is a bug; `/tmp/gate-<stamp>.log` is not.
- When waiting on a file, the wait condition must include freshness, not just
  content — `[ "$out" -nt "$marker" ]` alongside the grep. A `grep` on a
  filename answers "does this string exist somewhere", never "did my run
  produce it".
- Before reporting any measurement read from a file, check one field that could
  only have come from the current code. The schema version caught this one.
- Applies to the whole class: event logs, wire logs, artifacts, caches, temp
  output. Anything whose name is a function of *what it is* rather than *when it
  was made*.

## A parameter nobody passes is invisible to a unit test on the function that receives it

**2026-08-13.** `collection_id` was added to `assemble_record` and not to
`execute_run`. Every real caller — `run_matrix`, `smoke_test`, `dry_run` —
raised `TypeError`, and **all 472 unit tests passed**, because every one of them
drives `assemble_record` directly. Six adversarial plan-review rounds missed it,
including one that implemented the plan in a scratch repo. The §6.6 gate caught
it on the offline smoke, one layer from a paid run.

This is the second instance in this codebase: `cache_state` was a parameter
nobody passed through schema 2.0.0, so every record asserted `warm: false`.

**Rules:**

- A new field on a record needs a test that drives the **outermost** entry point
  (`execute_run`), not the assembler. `test_fault_injection.py` exists for this
  and its first rule says so.
- A mutation on the record-level assignment does not cover the threading. Anchor
  a second one on the call site.
- End-to-end gates are not redundant with unit tests. They are the only thing
  that runs the wiring.

## Do not narrow a claim to what was measured on one sample

**2026-08-13.** After one four-arm run where all four resolved the task, I wrote
in `TASKS.md` that it was a **ceiling task with no discriminating signal**. A
re-run that evening, same task and arms, resolved 2 of 4 — one arm burned its
turn cap, another wrote 0 bytes.

Neither run supports a claim about the task. What both together show is that at
temperature 1.0 and N=1 the between-run variance exceeds the between-arm spread,
which is an argument for repeats and not a property of the task.

**Rule:** an N=1 result describes that run. Before writing a property of a task,
a model or an arm into a durable document, ask what N it rests on — and if it is
1, write the observation and the N, not the conclusion.
