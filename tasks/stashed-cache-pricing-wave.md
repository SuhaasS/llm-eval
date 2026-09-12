# Stashed: the candidate cache-pricing wave (2026-09-08, stashed 2026-09-09)

**Where it is.** `git stash list` on this clone, entry
`On openrouter-provider: cache-pricing wave, stashed before openrouter plan execution 2026-09-09`.
The tracked half is also saved beside this file as
[stashed-cache-pricing-wave.patch](stashed-cache-pricing-wave.patch)
(`git stash show -p stash@{0}`), so the work survives a dropped stash. The
untracked half — `bakeoff/scripts/probe_cache.py`, 1,154 lines — lives only in
the stash's third parent: `git show 'stash@{0}^3:bakeoff/scripts/probe_cache.py'`.

**Why it was stashed.** It sat uncommitted on the working tree when the
OpenRouter plan started executing. It is unrelated to that plan, and the plan's
commits had to stage clean files, so it was set aside rather than mixed in.

**What it is.** Two rounds of work, both green at the time (`1135 passed`,
`mutation_check` 143/143), never committed. Its own log is the
`tasks/todo.md` hunk in the patch; the substance:

1. `costs.py`: the three Bedrock candidates' cache READ multipliers moved from
   `None` (raise on any cache token) to `1.0` — a price, derived: AWS sells no
   prompt cache for them, `prompt_tokens` is inclusive of `cached_tokens`, so a
   reused token bills like a fresh one. Cache WRITE multipliers stay `None`
   (0/179 records, 0/951 wire entries ever carried a candidate write; one
   appearing is the signal a billed product shipped and must land in
   `pricing_error`, not a dollar figure). Its `PRICING_BASIS` segment is
   `+candidate-nocache-2026-08-23`.
2. Tests caught up: a shared `unpriceable_model` fixture in `conftest.py` so
   the None-means-raise mechanism is tested on a fake model rather than on a
   candidate that no longer trips it; `test_costs.py`, `test_trajectory.py`,
   `test_fault_injection.py`, `test_smoke_test.py`, `test_runner.py` follow.
3. `smoke_test.py` `print_cache_tokens` reads pricedness off `row["cost"]`, not
   the arm-name prefix; the cache-tokens table relabelled "a Bedrock product
   on Sonnet; a KV lottery on the rest".
4. `probe_cache.py` (new script): the Bedrock cache measurement — mechanisms,
   lottery, regions modes; two-sided Fisher headline; expiry abort; `--regions`
   defaults, `--dry-run` pricing; retry latency excluded from medians.
5. Prose: eleven stale "None multipliers / raises for candidates" sites across
   `schema.py`, `trajectory.py`, `runner.py`, `smoke_test.py`,
   `mutation_check.py`, tests, spec §8, `CLAUDE.md` (adds the "Only Sonnet
   caches on purpose; the candidates win a lottery" gotcha), `TASKS.md`
   (2026-08-23 section rewritten; the retracted "44%" / "0/28" figures removed).
6. `litellm_config.yaml`: 57 added comment lines on the cache finding.

**Why it will not `git stash pop` cleanly.** The OpenRouter branch, now on
`main`, edited the same regions:

- `costs.py`: `PRICING_BASIS` is now
  `sonnet-list-2026-08-11+bedrock-2026-08-05+openrouter-2026-09-08+k26-crusoe-2026-09-11`;
  the stash rewrites the same line to end in `+candidate-nocache-2026-08-23`.
  Both segments belong; the merged line carries all of them, in date order of
  when each book took effect. The stash's candidate-row comments and the
  OpenRouter rows (`kimi-k2-6`, `kimi-k3`, added after the `-runtime`
  aliasing) do not overlap textually but sit close together.
- `runner.py`, `schema.py`, `mutation_check.py`, `test_runner.py`,
  `test_smoke_test.py`, `TASKS.md`, `CLAUDE.md`: both sides added text in the
  same files; expect ordinary 3-way conflicts, not semantic ones.
- `test_costs.py`: the stash rewrites tests the OpenRouter branch also touched
  (the K2.6/K3 rows, the basis assertion). Keep both sets.
- The `mutation_check.py` anchors on both sides must each still match their
  source byte-for-byte; run the check solo after re-applying.

**Re-apply, when it is time.**

```bash
git checkout -b cache-pricing-wave main
git stash apply stash@{0}            # apply, not pop: keep the stash until the branch is committed
# resolve costs.py (basis line: keep every segment), TASKS.md, CLAUDE.md, the tests
cd bakeoff && .venv/bin/python -m pytest tests/ -q -m "not integration" && .venv/bin/python scripts/mutation_check.py
```

Then commit, and only then `git stash drop stash@{0}`. One caveat the wave's
own notes carry: the 1.0 read multiplier and the OpenRouter branch's
`upstream_usage` finding are about different routes — Bedrock candidates
report `cached_tokens` that bill at full price, OpenRouter's pinned upstreams
report `cached_tokens` that bill at the published discount. The two books do
not contradict each other; `pricing_basis` is what keeps them apart.
