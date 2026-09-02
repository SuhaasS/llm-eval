# The eight node report shapes, captured

`node_adapter.classify` branches on these and on nothing else, so they are
replayed from disk rather than re-measured: regenerating one needs a Docker
daemon, a network and a 73 MB `npm install`, and a fixture whose provenance is
not written down is a fixture nobody dares regenerate.

Captured **2026-09-02** in `node:22-bookworm-slim` (node v22.23.2, npm 10.9.8),
`vitest 3.2.7` and `jest 30.5.0` — the versions `docker/eval-agent-node.Dockerfile`
pins. Note that `jest --version` prints **30.4.2** for the 30.5.0 package
(`node_modules/jest/package.json` and `node_modules/jest-cli/package.json` both
say `30.5.0`); the CLI's own banner lags its package, which matters to anything
asserting a runner pin by parsing `--version`.

## What each file is

Every run wrote its report to a file, and `exit` is the process exit status.
Read the first column against the exit column: **there is nothing in the exit
code to classify on**, which is the whole reason this adapter exists.

| fixture | argv after the report flags | vitest exit | jest exit | report written |
|---|---|---|---|---|
| `pass` | `tests/pass.test.js` | 0 | 0 | yes |
| `fail` | `tests/fail.test.js` | 1 | 1 | yes |
| `import_error` | `tests/broken.test.js` | 1 | 1 | yes |
| `syntax_error` | `tests/syntax.test.js` | 1 | 1 | yes |
| `no_files` | `tests/nosuchfile.test.js` | 1 | 1 | yes (empty `testResults`) |
| `t_nomatch` | `tests/pass.test.js -t '^(?:no such test)$'` | **0** | **0** | yes |
| `t_match` | `tests/pass.test.js -t '^(?:outer adds)$'` | 0 | 0 | yes |
| `mixed` | `tests/pass.test.js tests/broken.test.js` | 1 | 1 | yes |
| *(no fixture)* a broken config file | — | 1 | 1 | **NO FILE AT ALL** |

The last row is why there is no `config_error.*.json` to commit: the signal is
the *absence* of the file, which the tests pass as `report=None`. Measured the
same day, on a `vitest.config.js` with unbalanced brackets and a
`jest.config.cjs` with `testEnvironment: 999`.

The `t_nomatch` row is the dangerous one and has no pytest analogue: a `-t`
pattern matching no test exits **0** with `numPendingTests: 3` and a summary
that reads like success, where pytest answers the same input with exit 4 and
`ERROR: not found:`.

The report `name` fields were rewritten from the capture roots
(`/work/projv`, `/work/projj`) to **`/repo`**, and `/work/node_modules` to
`/node_modules`, so the fixtures exercise the rootdir-relative conversion
`_relpath` does on a real run and carry the paths the real image has (the
runners live at `/node_modules`, never in the bind-mounted tree).

## Recipe

Under `$HOME` — the Docker VM does not mount `/private/tmp`, and a bind mount
from there appears inside the container as a **silently empty directory**.

```bash
W="$HOME/.cache/bakeoff-nodefixtures"
mkdir -p "$W/projv/tests" "$W/projj/tests" "$W/out"
printf '{"name":"bakeoff-node-fixtures","private":true,"version":"1.0.0"}' > "$W/package.json"
```

Two projects, because the same test source cannot serve both: vitest's fixtures
import `describe`/`it`/`expect` from `vitest`, jest's use its injected globals
and CommonJS `require`. Both use the *same relative paths* under their own root,
so after the `/repo` rewrite the two frameworks' reports name the same files and
one parametrized test covers both.

`projv/tests/` and `projj/tests/` each hold four files:

- `pass.test.js` — `describe('outer')` with `it('adds')` and `it('subs')`, plus a
  top-level `it('top level')`. Three tests, `fullName`s `outer adds`,
  `outer subs`, `top level`.
- `fail.test.js` — one `it('will fail')` asserting `expect(1).toBe(2)`.
- `broken.test.js` — imports `../src/missing.js`, which does not exist.
- `syntax.test.js` — `it('never parses', (=> {` — unparseable.

`projj/jest.config.cjs` is `module.exports = { rootDir: __dirname, testEnvironment: 'node' }`.

Then, in the container:

```bash
docker run --rm -v "$W:/work" -w /work node:22-bookworm-slim \
  npm install --no-audit --no-fund vitest@3.2.7 jest@30.5.0

# vitest, from /work/projv:
vitest run --no-cache --reporter=json --outputFile=/work/out/<shape>.vitest.json <argv>
# jest, from /work/projj:
jest -c jest.config.cjs --json --outputFile=/work/out/<shape>.jest.json <argv>
```

`rm -f` the output file before each invocation and record whether it came back —
that absence is the environment signal, and a stale file from the previous
invocation would stand in as this run's evidence.

Finally rewrite the roots and pretty-print:

```python
text = raw.replace("/work/projv", "/repo").replace("/work/projj", "/repo")
text = text.replace("/work/node_modules", "/node_modules")
json.dump(json.loads(text), out, indent=2, sort_keys=True)
```

## Two argv facts measured beside these, which the adapter is built around

Neither produces a fixture; both are pinned by tests in `test_runners.py`.

1. **A second `-t` is not "last one wins".** `vitest run -t a -t b tests/pass.test.js`
   → `Error: Expected a single value for option "-t, --testNamePattern <pattern>",
   received ["a", "b"]`, exit 1, **no report file**. `jest -t adds -t subs`
   comma-joins them (`Ran all test suites … with tests matching "adds,subs"`),
   matches neither test, and exits **0**. Hence one `p2p_argvs` owning the whole
   argv, and hence the test that counts the `-t` flags.
2. **jest's `--testPathIgnorePatterns` is a greedy yargs array, so the
   positionals must come FIRST.** Measured: `jest tests/ --testPathIgnorePatterns=…`
   drops the named file and runs the rest, while
   `jest --testPathIgnorePatterns=fail.test.js tests/` swallows `tests/` into the
   ignore array and runs **nothing**, at exit 1 with an empty `testResults`.
   `p2p_argvs` emits the scope before the flags for exactly this reason; the
   trailing `-t` is safe because it starts with `-`, which ends the array.
3. **vitest's ignore flag is `--exclude=<path>`, the equals form, not a
   space-separated pair.** Measured 2026-09-02 alongside jest's
   `--testPathIgnorePatterns=<path>`: both frameworks are called with `=`,
   and `p2p_argvs` emits it that way for both. Pinned in `test_runners.py`
   (`test_node_ignore_flags_are_per_framework_and_jest_keeps_its_default`) as
   the exact argv `["tests/", "--exclude=tests/x.js"]`, not a two-element
   `["--exclude", "tests/x.js"]`.
