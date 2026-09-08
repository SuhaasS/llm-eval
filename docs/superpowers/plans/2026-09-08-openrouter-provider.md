# OpenRouter Provider Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Kimi K2.6 and Kimi K3 through the existing LiteLLM proxy to OpenRouter, pinned to a named US upstream, with every wire-derived observability field the Bedrock route produces today, and OpenRouter as the default `--provider`.

**Architecture:** The three-process topology (Claude Code → litellm proxy → provider) is unchanged. A `--provider` flag selects a config file and a credential path; the proxy learns which provider it serves through one env var, `BAKEOFF_PROVIDER`, and gates the two mantle-only param rewrites on it while keeping the resolved-params capture live. Four additive record fields carry what OpenRouter reports about the upstream. A probe script gates the first paid run.

**Tech Stack:** Python 3.12, litellm 1.95.0 (pinned in `docker/litellm-proxy.Dockerfile`), pytest, Docker SDK, OpenRouter Chat Completions API.

**Spec:** `docs/superpowers/specs/2026-09-08-openrouter-provider-design.md`

## Global Constraints

- All commands run from `bakeoff/` with `.venv/bin/python`. Unit suite: `cd bakeoff && .venv/bin/python -m pytest tests/ -v -m "not integration"`.
- `bakeoff.litellm_patches` **must never be imported from `bakeoff/` harness code**. Tests that exercise it in-process already exist in `tests/test_litellm_patches.py`; follow that file's pattern.
- `SCHEMA_VERSION` moves for additive fields: `"3.8.0"` → `"3.9.0"` (Task 8).
- `PRICING_BASIS` gains the segment `+openrouter-2026-09-08` (Task 4).
- Every new record field defaults to the "not observed" value (`None`, `""`, `[]`), never to a positive claim.
- Configuration is never reported as observation: `provider_route`, `upstream_providers`, `cost_usd_provider` come from the proxy manifest and the wire log, never from the CLI flag or the yaml.
- Docstrings carry the *why* and the failure mode, cross-referenced to the spec (`spec §N`), in the style of the surrounding code.
- Commit after every task. Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Branch: `openrouter-provider` (already created; the spec is its first commit).
- **Pre-existing dirty files.** `CLAUDE.md`, `TASKS.md`, `bakeoff/config/litellm_config.yaml`, `bakeoff/src/bakeoff/costs.py`, `bakeoff/tests/test_costs.py`, `bakeoff/scripts/probe_cache.py` carry uncommitted changes from an earlier cache-pricing wave. Before Task 1 the operator decides whether to commit that wave first. Until they do, every commit in this plan stages files by name with `git add <path>` and Tasks 4 and 10 (which touch `costs.py`, `test_costs.py`, `CLAUDE.md`, `TASKS.md`) use `git add -p` to stage only their own hunks.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `bakeoff/src/bakeoff/envfile.py` (create) | `load_env_file`, moved out of `scripts/smoke_bedrock.py` so `proxy.py` can read `.env` without importing the bedrock preflight | 1 |
| `bakeoff/src/bakeoff/proxy.py` | provider constants, `proxy_environment(mode, provider)`, `credential_window(..., provider)`, per-provider `EVAL_ARMS` | 1 |
| `bakeoff/.env.example` | section 0, `OPENROUTER_API_KEY` | 1 |
| `bakeoff/tests/test_credentials.py` | openrouter branches of `proxy_environment` and `credential_window` | 1 |
| `bakeoff/scripts/run_matrix.py`, `bakeoff/scripts/smoke_test.py` | `--provider` flag, config selection, default arms, summary json | 2 |
| `bakeoff/config/litellm_config_openrouter.yaml` (create) | the two Kimi arms | 3 |
| `bakeoff/tests/test_config.py` | parametrized over both configs; openrouter pins | 3 |
| `bakeoff/src/bakeoff/costs.py`, `bakeoff/tests/test_costs.py` | `kimi-k2-6`, `kimi-k3`, `PRICING_BASIS` | 4 |
| `bakeoff/src/bakeoff/litellm_patches.py` | split wrapper; `BAKEOFF_PROVIDER` gate; manifest carries provider | 5 |
| `bakeoff/src/bakeoff/proxy_callback.py` | manifest `provider_route`; `upstream_provider`, `native_finish_reason`, `usage_cost` in metadata | 5, 7 |
| `bakeoff/src/bakeoff/wire.py` | mirror the three metadata keys as `None` | 7 |
| `bakeoff/tests/test_litellm_patches.py`, `test_proxy_callback.py`, `test_wire.py` | pins for 5 and 7 | 5, 7 |
| `bakeoff/src/bakeoff/classify.py`, `bakeoff/tests/test_classify.py` | `api_credits`, `router_no_endpoint`, new auth signature | 6 |
| `bakeoff/src/bakeoff/schema.py`, `bakeoff/src/bakeoff/runner.py`, `bakeoff/tests/test_runner.py`, `test_schema.py` | `Versions.provider_route`, `RunRecord.upstream_providers`, `terminal_native_finish_reason`, `cost_usd_provider`, 3.9.0 | 8 |
| `bakeoff/scripts/probe_openrouter.py` (create) | the six-check gate | 9 |
| `bakeoff/scripts/mutation_check.py`, `CLAUDE.md`, `TASKS.md` | anchors and docs | 10 |

---

### Task 1: Provider seam in `proxy.py`

**Files:**
- Create: `bakeoff/src/bakeoff/envfile.py`
- Modify: `bakeoff/scripts/smoke_bedrock.py:81-107` (replace `load_env_file` body with an import)
- Modify: `bakeoff/src/bakeoff/proxy.py:40-49` (constants), `:293-376` (`CredentialWindow`, `credential_window`), `:407-469` (`proxy_environment`)
- Modify: `bakeoff/.env.example` (new section 0 at top)
- Test: `bakeoff/tests/test_credentials.py`

**Interfaces:**
- Produces: `PROVIDERS = ("openrouter", "bedrock")`, `DEFAULT_PROVIDER = "openrouter"`, `PROVIDER_ENV = "BAKEOFF_PROVIDER"`, `OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"`, `EVAL_ARMS_BY_PROVIDER: dict[str, list[str]]`, `OPENROUTER_KEY_HINT: str`, `proxy_environment(mode: str, provider: str = DEFAULT_PROVIDER) -> dict[str, str]`, `credential_window(region: str, now: datetime | None = None, provider: str = DEFAULT_PROVIDER) -> CredentialWindow`, `bakeoff.envfile.load_env_file(path: Path) -> list[str]`.
- `EVAL_ARMS` keeps its name and its bedrock value so `scripts/smoke_test.py:50` and `scripts/run_matrix.py:62` still import; Task 2 switches them to the dict.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_credentials.py`:

```python
import os
import subprocess
import sys
from pathlib import Path

from bakeoff.proxy import (
    DEFAULT_PROVIDER,
    EVAL_ARMS_BY_PROVIDER,
    OPENROUTER_KEY_ENV,
    PROVIDER_ENV,
    PROVIDERS,
    proxy_environment,
)

BAKEOFF = Path(__file__).resolve().parents[1]


def test_openrouter_is_the_default_provider():
    assert DEFAULT_PROVIDER == "openrouter"
    assert set(PROVIDERS) == {"openrouter", "bedrock"}
    assert EVAL_ARMS_BY_PROVIDER["openrouter"] == ["kimi-k2-6", "kimi-k3"]
    assert EVAL_ARMS_BY_PROVIDER["bedrock"][0] == "claude-sonnet-5-runtime"


def test_the_openrouter_environment_is_exactly_the_key_and_the_provider_name(monkeypatch):
    """Spec §1. Two keys, no AWS names: one proxy image serves both providers,
    and an AWS variable leaking into an openrouter proxy is how a misrouted
    deployment would authenticate against the wrong cloud."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
    env = proxy_environment("live", "openrouter")
    assert env == {OPENROUTER_KEY_ENV: "sk-or-test", PROVIDER_ENV: "openrouter"}


def test_offline_mode_hands_the_proxy_nothing_for_either_provider():
    assert proxy_environment("offline", "openrouter") == {}
    assert proxy_environment("offline", "bedrock") == {}


def test_a_missing_openrouter_key_refuses_before_the_proxy_is_built(monkeypatch):
    monkeypatch.delenv(OPENROUTER_KEY_ENV, raising=False)
    # Point the .env lookup at an empty directory so the operator's real
    # key cannot make this test pass on one machine and fail on another.
    monkeypatch.setattr("bakeoff.proxy.ENV_FILE", Path("/nonexistent/.env"))
    with pytest.raises(SystemExit) as excinfo:
        proxy_environment("live", "openrouter")
    assert OPENROUTER_KEY_ENV in str(excinfo.value)


def test_an_unknown_provider_is_a_programming_error_not_a_fallback():
    with pytest.raises(ValueError):
        proxy_environment("live", "azure")


def test_the_openrouter_path_never_loads_botocore_or_the_bedrock_preflight():
    """Spec §1, §8. Asserted in a fresh interpreter: the bedrock branch's
    imports are module-level in scripts/smoke_bedrock.py, and a shared
    import would drag botocore into a run that has no AWS credentials and no
    reason to resolve any."""
    code = (
        "import sys\n"
        "from bakeoff.proxy import proxy_environment\n"
        "proxy_environment('live', 'openrouter')\n"
        "leaked = [m for m in ('botocore', 'boto3', 'scripts.smoke_bedrock', 'smoke_bedrock') if m in sys.modules]\n"
        "assert not leaked, leaked\n"
    )
    env = {**os.environ, OPENROUTER_KEY_ENV: "sk-or-test", "PYTHONPATH": str(BAKEOFF / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=BAKEOFF
    )
    assert result.returncode == 0, result.stderr


def test_the_openrouter_window_has_no_expiry_and_never_stops_a_cell():
    """A static key has no deadline to read. `None` here is 'nothing to
    read', distinguished from botocore's 'could not read' by `source`, and
    credential_stop must not refuse a cell over it -- the abort streak is the
    backstop for a revoked key, as it already is for static AWS keys."""
    window = credential_window("us-east-1", now=NOW, provider="openrouter")
    assert window.expires_at is None
    assert window.source == "openrouter-static"
    assert credential_stop(window, NOW, 10_000) == ""


def test_the_env_template_names_the_openrouter_key_first():
    template = (BAKEOFF / ".env.example").read_text()
    keys = [
        line.split("=", 1)[0].strip()
        for line in template.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    assert keys[0] == OPENROUTER_KEY_ENV, keys
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_credentials.py -v -k "openrouter or unknown_provider or offline_mode"`
Expected: FAIL, `ImportError: cannot import name 'DEFAULT_PROVIDER'`.

- [ ] **Step 3: Create `bakeoff/src/bakeoff/envfile.py`**

```python
"""Minimal .env loader, shared by the bedrock preflight and the proxy.

Lives here rather than in scripts/smoke_bedrock.py because `proxy.py` has to
read `.env` for the openrouter key WITHOUT importing that script: its
module-level imports pull in boto3, and a provider path that needs no AWS
credentials must not resolve any (spec §1, §8).

Does not overwrite variables already set in the environment -- an exported
value should win over a stale file. Tolerates `export KEY=value`, which is
the form credentials arrive in when pasted from a console. Skips unfilled
`<placeholder>` values so a template copied verbatim reads as "not set"
rather than as a literal angle-bracketed key.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path) -> list[str]:
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not value or value.startswith("<"):  # unfilled placeholder
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
```

- [ ] **Step 4: Make `scripts/smoke_bedrock.py` import it**

Replace the whole `def load_env_file(path: Path) -> list[str]:` function body at `bakeoff/scripts/smoke_bedrock.py:81-107` with:

```python
from bakeoff.envfile import load_env_file  # noqa: E402,F401 -- re-exported; callers import it from here
```

Keep the name importable from `smoke_bedrock` because `proxy_environment`'s bedrock branch and `probe_cache.py` import it from there.

- [ ] **Step 5: Add the constants to `proxy.py`**

Replace `bakeoff/src/bakeoff/proxy.py:40-49` (`EVAL_ARMS` through `SSO_LOGIN_HINT`) with:

```python
# The two provider routes behind the proxy (spec 2026-09-08 §1). Selected by
# `--provider` on the drivers, never inferred from which env var happens to be
# set: an implicit choice is configuration reported as observation.
PROVIDERS = ("openrouter", "bedrock")
DEFAULT_PROVIDER = "openrouter"
# How the PROXY learns which provider it serves. Read by litellm_patches to
# decide whether the two mantle-only rewrites apply, and written into the
# adapter manifest so the record can say which route answered.
PROVIDER_ENV = "BAKEOFF_PROVIDER"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

# The arms each provider's config serves. `run_matrix --models` defaults to the
# list for the chosen provider; a bedrock arm name against an openrouter config
# is an UnknownModelError after the tokens are spent.
EVAL_ARMS_BY_PROVIDER: dict[str, list[str]] = {
    "bedrock": [
        "claude-sonnet-5-runtime",
        "gemma-4-31b",
        "nemotron-3-super-120b",
        "kimi-k2-5",
    ],
    "openrouter": ["kimi-k2-6", "kimi-k3"],
}
# Kept under its old name for the two scripts that import it; Task 2 of the
# openrouter plan moves them to the dict.
EVAL_ARMS = EVAL_ARMS_BY_PROVIDER["bedrock"]

SSO_LOGIN_HINT = (
    "  AWS_CONFIG_FILE=bakeoff/.aws/config aws sso login --profile pindrop-bakeoff"
)
OPENROUTER_KEY_HINT = (
    f"  put {OPENROUTER_KEY_ENV}=<key> in bakeoff/.env (see .env.example, section 0)"
)

# src/bakeoff/proxy.py -> bakeoff/.env. The same file scripts/smoke_bedrock.py
# reads; resolved here so the openrouter branch needs nothing from that script.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
```

- [ ] **Step 6: Add the provider arm to `credential_window`**

Change the signature at `bakeoff/src/bakeoff/proxy.py:310` and add the branch as the first statement of the body:

```python
def credential_window(
    region: str, now: datetime | None = None, provider: str = DEFAULT_PROVIDER
) -> CredentialWindow:
```

Insert immediately after the docstring, before `now = now or datetime.now(timezone.utc)`:

```python
    if provider == "openrouter":
        # A static API key. There is no deadline to read, which is a
        # different state from "could not read one": `source` says so, and
        # credential_stop returns "" for it. The abort streak is the backstop
        # for a revoked or exhausted key, as it already is for static AWS keys
        # (CLAUDE.md, "That refusal cannot fire on static keys").
        return CredentialWindow(None, "openrouter-static", "static API key; no expiry to read")
```

Add to the docstring, one paragraph before `MUST be called after`:

```
    OPENROUTER. The key is static and the window is `None` with source
    `openrouter-static` -- see the branch below. Everything under this line is
    the bedrock provider.
```

- [ ] **Step 7: Split `proxy_environment`**

Replace `bakeoff/src/bakeoff/proxy.py:407-469` with:

```python
def proxy_environment(mode: str, provider: str = DEFAULT_PROVIDER) -> dict[str, str]:
    """Credentials for the proxy container. Live mode only.

    The agent is passed none of these and never sees them: it reaches the
    provider through the proxy over HTTP, which is what makes every credential
    here a single-hop secret.

    Two providers, two bodies, no shared code (spec 2026-09-08 §1). The
    openrouter branch must not import the bedrock preflight or botocore: a
    path that needs no AWS credential must not resolve one, and the test
    asserts it on `sys.modules` in a fresh interpreter.

    Every return carries PROVIDER_ENV. That is how the proxy process learns
    which rewrites to apply (litellm_patches) and what to write into the
    adapter manifest (`provider_route` on the record).
    """
    if mode != "live":
        return {}
    if provider == "openrouter":
        return _openrouter_environment()
    if provider == "bedrock":
        return {**_bedrock_environment(), PROVIDER_ENV: "bedrock"}
    raise ValueError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")


def _openrouter_environment() -> dict[str, str]:
    import os

    from bakeoff.envfile import load_env_file

    load_env_file(ENV_FILE)
    key = os.environ.get(OPENROUTER_KEY_ENV, "")
    if not key:
        raise SystemExit(f"No {OPENROUTER_KEY_ENV}. Run:\n" + OPENROUTER_KEY_HINT)
    return {OPENROUTER_KEY_ENV: key, PROVIDER_ENV: "openrouter"}


def _bedrock_environment() -> dict[str, str]:
    """Both bedrock transports are provisioned, because Sonnet 5 is served over
    bedrock-runtime while the three candidates are served over bedrock-mantle
    (see config/litellm_config.yaml). The mantle token is passed as
    BAKEOFF_MANTLE_TOKEN and NOT as AWS_BEARER_TOKEN_BEDROCK: LiteLLM's
    bedrock/ handler falls back to the AWS-named variable when a deployment
    has no api_key, so setting it here would bearer-authenticate every runtime
    arm and fail them with `bedrock:CallWithBearerToken`.
    """
    import os

    from scripts.smoke_bedrock import (
        ENV_FILE as BEDROCK_ENV_FILE,
        LITELLM_BEARER_ENV,
        MANTLE_ENV,
        derive_mantle_token,
        load_env_file,
        normalize_mantle_token,
        resolve_aws_paths,
        scrub_placeholders,
    )

    load_env_file(BEDROCK_ENV_FILE)
    scrub_placeholders()
    resolve_aws_paths()
    normalize_mantle_token()

    region = os.environ.get("AWS_REGION_NAME") or "us-east-1"

    token = os.environ.get(MANTLE_ENV, "")
    if not token:
        # Minted from the current SSO session, in memory, never written to
        # disk. A long-lived key in .env would outlive the run that needed
        # it and sit there with no expiry anyone tracks.
        token = derive_mantle_token(region)
        if token:
            print(f"mantle    derived short-term bearer token (len {len(token)})")
    if not token:
        raise SystemExit("No mantle credential. Run:\n" + SSO_LOGIN_HINT)

    sigv4 = freeze_sigv4_credentials(region)
    if not sigv4:
        raise SystemExit(
            "No SigV4 credentials -- every bedrock-runtime arm would fail.\n"
            + SSO_LOGIN_HINT
        )
    print(f"sigv4     froze session credentials for the proxy ({region})")

    environment = {MANTLE_ENV: token, **sigv4}
    # Belt and braces: the container inherits nothing from this process, but
    # an image or compose layer that ever sets the AWS-named variable would
    # silently move the runtime arms onto bearer auth.
    assert LITELLM_BEARER_ENV not in environment
    return environment
```

- [ ] **Step 8: Add section 0 to `.env.example`**

Insert after the opening comment block (after the line `# the smoke test reports it as SKIP rather than failing to parse.`) and before section 1:

```
# ---------------------------------------------------------------------------
# 0. openrouter arms  (kimi-k2-6, kimi-k3) -- THE DEFAULT PROVIDER
# ---------------------------------------------------------------------------
# An OpenRouter API key: https://openrouter.ai/settings/keys . Static, no
# expiry; a revoked or out-of-credit key surfaces as 401/402 on the first cell
# and the abort streak stops the matrix. Sections 1-2 are only read under
# `--provider bedrock`.
OPENROUTER_API_KEY=<paste-openrouter-key>

```

- [ ] **Step 9: Run the tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_credentials.py tests/test_config.py -v`
Expected: all PASS, including the pre-existing bedrock window tests (they call `credential_window("us-east-1", now=NOW)` and now take the default provider `openrouter`, so **they will fail**: fix them by passing `provider="bedrock"` to each `credential_window(...)` call at `tests/test_credentials.py:78,90,102,115,127` and any later ones; `grep -n 'credential_window("us-east-1"' tests/test_credentials.py`). Re-run until green.

- [ ] **Step 10: Run the whole unit suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q -m "not integration"`
Expected: PASS. `scripts/probe_cache.py` and `scripts/smoke_test.py` still import `load_env_file` from `smoke_bedrock`, which re-exports it.

- [ ] **Step 11: Commit**

```bash
git add bakeoff/src/bakeoff/envfile.py bakeoff/src/bakeoff/proxy.py bakeoff/scripts/smoke_bedrock.py bakeoff/.env.example bakeoff/tests/test_credentials.py
git commit -m "feat: a provider seam in proxy.py, openrouter default, bedrock untouched

proxy_environment and credential_window take a provider. The openrouter
branch reads one key from .env and imports neither botocore nor the bedrock
preflight; the bedrock branch is the previous body verbatim. load_env_file
moves to bakeoff.envfile so the proxy can read .env without the script.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `--provider` on the drivers

**Files:**
- Modify: `bakeoff/scripts/run_matrix.py:62` (import), `:265` (argparse), `:309` (default arms), `:408-431` (env + window), `:447-449` (config name), `:582-600` (summary json)
- Modify: `bakeoff/scripts/smoke_test.py:50` (import), `:709` (argparse), `:737` (default arms), `:785` (env)
- Test: `bakeoff/tests/test_run_matrix.py`

**Interfaces:**
- Consumes: `PROVIDERS`, `DEFAULT_PROVIDER`, `EVAL_ARMS_BY_PROVIDER`, `proxy_environment(mode, provider)`, `credential_window(region, provider=...)` from Task 1.
- Produces: `run_matrix.config_name_for(mode: str, provider: str) -> str` (pure, tested).

- [ ] **Step 1: Write the failing test**

Append to `bakeoff/tests/test_run_matrix.py`:

```python
from scripts.run_matrix import config_name_for


def test_the_config_file_follows_the_provider_and_offline_ignores_it():
    """Spec §1. The offline stub config is provider-neutral: nothing is
    spent and no credential is read, so both providers share it."""
    assert config_name_for("live", "openrouter") == "litellm_config_openrouter.yaml"
    assert config_name_for("live", "bedrock") == "litellm_config.yaml"
    assert config_name_for("offline", "openrouter") == "litellm_smoke_offline.yaml"
    assert config_name_for("offline", "bedrock") == "litellm_smoke_offline.yaml"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_run_matrix.py -v -k config_file`
Expected: FAIL, `ImportError: cannot import name 'config_name_for'`.

- [ ] **Step 3: Add `config_name_for` and the flag to `run_matrix.py`**

Change the import at `bakeoff/scripts/run_matrix.py:62` from `EVAL_ARMS,` to:

```python
    DEFAULT_PROVIDER,
    EVAL_ARMS_BY_PROVIDER,
    PROVIDERS,
```

Add the module-level function anywhere above `main()` (for example directly above `def main() -> int:`):

```python
def config_name_for(mode: str, provider: str) -> str:
    """Which proxy config a (mode, provider) pair runs on.

    Offline is provider-neutral on purpose: the stub answers, nothing is
    spent, no credential is read, and the offline gate certifies the same
    logger whichever provider the paid run will use.
    """
    if mode != "live":
        return "litellm_smoke_offline.yaml"
    return {
        "openrouter": "litellm_config_openrouter.yaml",
        "bedrock": "litellm_config.yaml",
    }[provider]
```

After `parser.add_argument("--mode", ...)` at `:265` add:

```python
    parser.add_argument(
        "--provider", choices=list(PROVIDERS), default=DEFAULT_PROVIDER,
        help="which route the proxy serves: openrouter (default) or bedrock",
    )
```

Change `:309` to:

```python
    models = args.models.split(",") if args.models else list(EVAL_ARMS_BY_PROVIDER[args.provider])
```

Change `:408` to:

```python
    environment = proxy_environment(args.mode, args.provider)
```

Change the `window = (...)` block at `:413-417` to:

```python
    window = (
        credential_window(
            os.environ.get("AWS_REGION_NAME") or "us-east-1", provider=args.provider
        )
        if args.mode == "live"
        else CredentialWindow(None, "offline")
    )
```

In the print block that follows, add a branch before the final `else`:

```python
    elif window.source == "openrouter-static":
        print("creds     static key, no expiry (openrouter); the abort streak is the backstop")
```

Change the `config_name = (...)` at `:447-449` to:

```python
    config_name = config_name_for(args.mode, args.provider)
```

In the summary `write_json(...)` at `:582`, add `"provider": args.provider,` directly after `"mode": args.mode,`.

- [ ] **Step 4: Same on `smoke_test.py`**

Change the import at `bakeoff/scripts/smoke_test.py:50` from `EVAL_ARMS,` to `DEFAULT_PROVIDER, EVAL_ARMS_BY_PROVIDER, PROVIDERS,` (one per line, alphabetical within the import block).

After `parser.add_argument("--mode", choices=["offline", "live"], required=True)` at `:709` add:

```python
    parser.add_argument(
        "--provider", choices=list(PROVIDERS), default=DEFAULT_PROVIDER,
        help="which route the proxy serves: openrouter (default) or bedrock",
    )
```

Change `:737` to:

```python
        arms = args.models.split(",") if args.models else list(EVAL_ARMS_BY_PROVIDER[args.provider])
```

Change `:785` to:

```python
    environment = proxy_environment(args.mode, args.provider)
```

Find where `smoke_test.py` chooses its config name (`grep -n "litellm_config.yaml\|litellm_smoke_offline" scripts/smoke_test.py`) and route it through `config_name_for` imported from `scripts.run_matrix` **only if** that import does not create a cycle; otherwise duplicate the four-line mapping inline with a comment `# mirrors run_matrix.config_name_for`.

- [ ] **Step 5: Run tests and both drivers' help**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_run_matrix.py tests/test_smoke_test.py -v`
Expected: PASS.

Run: `cd bakeoff && .venv/bin/python scripts/run_matrix.py --help | grep -A1 provider && .venv/bin/python scripts/smoke_test.py --help | grep -A1 provider`
Expected: both print the `--provider {openrouter,bedrock}` line with `(default: openrouter)` semantics shown in help.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/scripts/run_matrix.py bakeoff/scripts/smoke_test.py bakeoff/tests/test_run_matrix.py
git commit -m "feat: --provider on run_matrix and smoke_test, openrouter by default

Selects the proxy config, the credential path, the default arm list and the
credential window. Offline stays provider-neutral. The matrix summary
records which provider the invocation ran under.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `litellm_config_openrouter.yaml` and its pins

**Files:**
- Create: `bakeoff/config/litellm_config_openrouter.yaml`
- Modify: `bakeoff/tests/test_config.py` (parametrize; add openrouter pins)

**Interfaces:**
- Produces: two `model_name`s, `kimi-k2-6` and `kimi-k3`, which Task 4 prices.

- [ ] **Step 1: Write the failing tests**

At the top of `bakeoff/tests/test_config.py`, replace

```python
CONFIG = Path(__file__).resolve().parents[1] / "config" / "litellm_config.yaml"
```

with

```python
import pytest

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
BEDROCK = CONFIG_DIR / "litellm_config.yaml"
OPENROUTER = CONFIG_DIR / "litellm_config_openrouter.yaml"
CONFIG = BEDROCK  # the bedrock-only pins below read this name
CONFIGS = {"bedrock": BEDROCK, "openrouter": OPENROUTER}
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
```

and change `_model_list` to take a path:

```python
def _model_list(path: Path = BEDROCK):
    return yaml.safe_load(path.read_text())["model_list"]
```

Parametrize the provider-neutral tests. For each of `test_config_parses_and_lists_every_arm`, `test_every_configured_model_name_can_be_priced`, `test_the_production_config_registers_the_proxy_side_wire_callback`, `test_every_proxy_config_registers_both_callbacks`, `test_the_router_does_not_cool_down_a_single_deployment_group`, `test_no_deployment_targets_the_real_anthropic_api`, `test_the_registered_callback_path_resolves_to_a_dispatchable_instance`: add

```python
@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
```

and give the function a `config` parameter, passing it to `_model_list(config)` / `yaml.safe_load(config.read_text())`. For `test_config_parses_and_lists_every_arm` the expected set is per file:

```python
@pytest.mark.parametrize(
    "config, expected",
    [
        (BEDROCK, {"claude-sonnet-5", "gemma-4-31b", "nemotron-3-super-120b", "kimi-k2-5"}),
        (OPENROUTER, {"kimi-k2-6", "kimi-k3"}),
    ],
    ids=["bedrock", "openrouter"],
)
def test_config_parses_and_lists_every_arm(config, expected):
    names = {entry["model_name"] for entry in _model_list(config)}
    assert expected <= names
```

Leave the bedrock-only tests (`test_sonnet_5_arms_send_no_sampling_parameters`, `test_every_other_arm_pins_its_lab_recommended_sampling`, `test_gemma_sends_no_top_p_because_its_route_refuses_one`, `test_no_bedrock_arm_reaches_converse_with_ids_it_would_reject`, `test_no_mantle_arm_reads_the_bearer_variable_litellm_falls_back_to`, `test_the_env_template_names_the_variable_the_config_actually_reads`, `test_bedrock_runtime_arms_carry_no_api_key`, `test_every_candidate_arm_uses_the_openai_provider_prefix`, `test_gemma_has_no_bedrock_runtime_entry`) reading `_model_list()` with its bedrock default.

Append the openrouter pins:

```python
def _openrouter_arms():
    return _model_list(OPENROUTER)


def test_every_openrouter_arm_targets_openrouter_on_the_openai_provider():
    """Spec §2. `openai/`, not litellm's `openrouter/`: the resolved-params
    capture and the two mantle rewrites hang off OpenAIConfig.map_openai_params,
    which get_optional_params reaches only for custom_llm_provider == "openai".
    A different provider class would leave the wrapper installed and never
    called, and `resolved` would read null on every call."""
    for entry in _openrouter_arms():
        params = entry["litellm_params"]
        assert params["model"].startswith("openai/"), entry["model_name"]
        assert params["api_base"] == OPENROUTER_BASE, entry["model_name"]
        assert params["api_key"] == "os.environ/OPENROUTER_API_KEY", entry["model_name"]


def test_every_openrouter_arm_is_pinned_to_one_upstream_with_no_fallback():
    """Spec §2. OpenRouter load-balances one model across upstreams with
    different quantizations and tool-call parsers. Unpinned, an arm is not one
    model across repeats; with fallbacks, `require_parameters` failing on the
    pinned upstream silently routes to whoever is left."""
    for entry in _openrouter_arms():
        provider = entry["litellm_params"]["extra_body"]["provider"]
        assert isinstance(provider.get("order"), list) and len(provider["order"]) == 1, entry["model_name"]
        assert provider["allow_fallbacks"] is False, entry["model_name"]
        assert provider["require_parameters"] is True, entry["model_name"]


def test_every_openrouter_arm_asks_for_provider_cost_and_drops_the_derived_effort():
    """`usage.include` is what makes `cost_usd_provider` exist. Dropping
    reasoning_effort is what keeps require_parameters from rejecting K2.6,
    whose endpoints list `reasoning` but not `reasoning_effort`; litellm
    derives one from Claude Code's thinking:adaptive on every call."""
    for entry in _openrouter_arms():
        params = entry["litellm_params"]
        assert params["extra_body"]["usage"] == {"include": True}, entry["model_name"]
        assert "reasoning_effort" in params["additional_drop_params"], entry["model_name"]
        assert "reasoning" in params["extra_body"], entry["model_name"]


def test_the_openrouter_config_pins_quantization_where_the_endpoint_declares_one():
    """CoreWeave declares fp4 for K2.6; Fireworks declares none for K3, so
    pinning there would match nothing."""
    by_name = {e["model_name"]: e["litellm_params"]["extra_body"]["provider"] for e in _openrouter_arms()}
    assert by_name["kimi-k2-6"]["order"] == ["coreweave"]
    assert by_name["kimi-k2-6"]["quantizations"] == ["fp4"]
    assert by_name["kimi-k3"]["order"][0].startswith("fireworks")
    assert "quantizations" not in by_name["kimi-k3"]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: openrouter-parametrized cases FAIL with `FileNotFoundError` on the missing yaml.

- [ ] **Step 3: Write the config**

Create `bakeoff/config/litellm_config_openrouter.yaml`:

```yaml
# Self-hosted LiteLLM proxy calling OpenRouter (spec 2026-09-08 §2).
# Selected by `--provider openrouter`, which is the default on run_matrix.py
# and smoke_test.py. The bedrock route is config/litellm_config.yaml.
#
# Same topology as the bedrock route: Claude Code -> this proxy -> provider.
# The candidates already reach bedrock-mantle through litellm's `openai/`
# provider with a per-deployment api_base; OpenRouter is the same provider
# class with a different host, so the wire callback fires on the same code
# path with the same kwargs, and every wire-derived record field is produced
# by the code that produces it today.
#
# `openai/`, NOT litellm's `openrouter/`. The resolved-params capture and the
# two mantle rewrites (bakeoff.litellm_patches) wrap OpenAIConfig.map_openai_params,
# which get_optional_params reaches only for custom_llm_provider == "openai".
# A different provider class leaves the wrapper installed and never called,
# and `resolved` reads null on every call.
#
# PROVIDER PINNING IS LOAD-BEARING. OpenRouter load-balances one model across
# upstreams with different quantizations, context caps and tool-call parsers.
# `order` + `allow_fallbacks: false` makes each arm one deployment across
# repeats; `require_parameters: true` refuses any upstream that does not
# accept every parameter sent, rather than dropping the parameter. The
# response's `provider` field is captured per call so the pin is verified,
# never assumed. Measured 2026-09-08 from /api/v1/models/<id>/endpoints:
#   kimi-k2.6  coreweave/fp4  $0.65/$3.41  cache read $0.15   tools: yes
#   kimi-k3    fireworks      $3.00/$15.00 cache read $0.30   tools: yes
#   kimi-k3    fireworks/fast                                 tools: NO -- never let the router pick it
#
# reasoning_effort IS DROPPED HERE, NOT PINNED. litellm derives one from
# Claude Code's thinking:{"type":"adaptive"} on every arm. K2.6's endpoints
# list `reasoning` but not `reasoning_effort`, so under require_parameters a
# derived effort is zero eligible providers. `extra_body.reasoning` is the
# knob; `enabled: true` is the eval decision (thinking on -- both models ship
# that way and off is the setting nobody would deploy). Flip per arm to
# `{enabled: false}` for a thinking-off run; nothing else changes.
#
# usage.include asks OpenRouter to return `usage.cost`, which the record
# carries as cost_usd_provider beside the price book's cost_usd.
#
# model_name doubles as the PRICE_BOOK key in bakeoff.costs.

model_list:
  - model_name: kimi-k2-6
    litellm_params:
      model: openai/moonshotai/kimi-k2.6
      api_base: https://openrouter.ai/api/v1
      api_key: os.environ/OPENROUTER_API_KEY
      additional_drop_params: ["reasoning_effort"]
      temperature: 1.0
      top_p: 0.95
      extra_body:
        provider:
          order: ["coreweave"]
          allow_fallbacks: false
          require_parameters: true
          quantizations: ["fp4"]
        usage: {include: true}
        reasoning: {enabled: true}

  - model_name: kimi-k3
    litellm_params:
      model: openai/moonshotai/kimi-k3
      api_base: https://openrouter.ai/api/v1
      api_key: os.environ/OPENROUTER_API_KEY
      additional_drop_params: ["reasoning_effort"]
      temperature: 1.0
      top_p: 0.95
      extra_body:
        provider:
          # "fireworks" or "fireworks/us": scripts/probe_openrouter.py check 1
          # decides whether the router honours the variant slug. Edit here and
          # in costs.PRICE_BOOK together, before the first paid run.
          order: ["fireworks"]
          allow_fallbacks: false
          require_parameters: true
        usage: {include: true}
        reasoning: {enabled: true}

litellm_settings:
  callbacks:
    - bakeoff.proxy_callback.instance
    - bakeoff.litellm_patches.instance
  use_chat_completions_url_for_anthropic_messages: true
  additional_drop_params:
    - reasoning_effort
    - context_management
    - output_config
  drop_params: false          # surface unsupported params instead of hiding them
  set_verbose: false

router_settings:
  # Single-deployment groups by design; a cooldown has nothing to fail over
  # to and would hide the auth failure behind "No deployments available".
  disable_cooldowns: true

general_settings:
  num_retries: 3
  request_timeout: 900
```

- [ ] **Step 4: Run tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: everything PASS except `test_every_configured_model_name_can_be_priced[openrouter]`, which FAILS with `UnknownModelError: kimi-k2-6` until Task 4. That is the intended order; proceed.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/config/litellm_config_openrouter.yaml bakeoff/tests/test_config.py
git commit -m "feat: the openrouter proxy config, two Kimi arms pinned to US upstreams

Same openai/ provider class as the mantle candidates so the wire callback
and the resolved-params capture keep firing. Provider pinned with no
fallback, require_parameters on, reasoning_effort dropped in favour of
extra_body.reasoning, usage.include for the provider's own cost.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Price book

**Files:**
- Modify: `bakeoff/src/bakeoff/costs.py:78` (`PRICING_BASIS`), after `:170` (`PRICE_BOOK.update(...)`)
- Test: `bakeoff/tests/test_costs.py`

**Interfaces:**
- Produces: `PRICE_BOOK["kimi-k2-6"]`, `PRICE_BOOK["kimi-k3"]`; `PRICING_BASIS` ending in `+openrouter-2026-09-08`.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_costs.py`:

```python
def test_kimi_k2_6_prices_at_coreweave_rates_with_a_real_cache_read_discount():
    """Spec §5. $0.65 / $3.41 per 1M, cache read $0.15 -> 0.2308x. Unlike the
    bedrock candidates, this route publishes a cache-read rate, so the
    multiplier is a price and a cache hit is worth money, not only latency."""
    assert cost_usd("kimi-k2-6", TokenUsage(input=1_000_000)) == pytest.approx(0.65)
    assert cost_usd("kimi-k2-6", TokenUsage(output=1_000_000)) == pytest.approx(3.41)
    assert cost_usd("kimi-k2-6", TokenUsage(cache_read=1_000_000)) == pytest.approx(0.15, rel=1e-3)


def test_kimi_k3_prices_at_fireworks_rates():
    assert cost_usd("kimi-k3", TokenUsage(input=1_000_000)) == pytest.approx(3.00)
    assert cost_usd("kimi-k3", TokenUsage(output=1_000_000)) == pytest.approx(15.00)
    assert cost_usd("kimi-k3", TokenUsage(cache_read=1_000_000)) == pytest.approx(0.30, rel=1e-3)


def test_an_openrouter_cache_write_bills_as_plain_input():
    """No endpoint lists input_cache_write, so 1.0 is a PRICE: a written
    prefix costs what an unwritten one costs. The 1h tier does not exist on
    this route and takes the same rate so the total stays
    prompt_tokens x input_per_1m when no read occurred."""
    for name, rate in (("kimi-k2-6", 0.65), ("kimi-k3", 3.00)):
        assert cost_usd(name, TokenUsage(cache_write=1_000_000)) == pytest.approx(rate)
        assert cost_usd(name, TokenUsage(cache_write=1_000_000, cache_write_1h=1_000_000)) == pytest.approx(rate)


def test_reasoning_tokens_bill_at_the_output_rate_on_the_openrouter_arms():
    assert cost_usd("kimi-k3", TokenUsage(reasoning=1_000_000)) == pytest.approx(15.00)


def test_the_openrouter_arms_have_no_runtime_alias():
    """The -runtime aliases name the bedrock Converse transport. An
    openrouter arm has no second transport, and an alias would be a price
    for a deployment that cannot exist."""
    assert "kimi-k2-6-runtime" not in PRICE_BOOK
    assert "kimi-k3-runtime" not in PRICE_BOOK


def test_the_pricing_basis_names_the_openrouter_book():
    from bakeoff.costs import PRICING_BASIS
    assert PRICING_BASIS.endswith("+openrouter-2026-09-08")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_costs.py -v -k "kimi_k or openrouter"`
Expected: FAIL with `UnknownModelError`.

- [ ] **Step 3: Add the entries**

Change `bakeoff/src/bakeoff/costs.py:78` to:

```python
PRICING_BASIS = "sonnet-list-2026-08-11+bedrock-2026-08-05+candidate-nocache-2026-08-23+openrouter-2026-09-08"
```

Directly **after** the `PRICE_BOOK.update({... f"{name}-runtime" ...})` block (so no `-runtime` alias is minted for these), add:

```python
# The openrouter arms (spec 2026-09-08 §5). Rates are the ENDPOINT's, not the
# model's: OpenRouter serves one model from many upstreams at different prices,
# and the config pins each arm to one of them. Measured 2026-09-08 from
# GET /api/v1/models/moonshotai/<id>/endpoints.
#
# cache_read is a real discount here, unlike the bedrock candidates: both
# endpoints publish an input_cache_read rate. cache_write is 1.0 because no
# endpoint lists input_cache_write -- a written prefix bills as plain input,
# so 1.0 is a PRICE, not a placeholder, and the same argument as the bedrock
# candidates' 1.0 applies. There is no 1h tier on this route; 1.0 keeps a
# run with no cache read at exactly prompt_tokens x input_per_1m.
#
# Added AFTER the -runtime aliasing above on purpose: an openrouter arm has no
# second transport, and an alias would price a deployment that cannot exist.
PRICE_BOOK.update(
    {
        # coreweave/fp4: $0.65 in, $3.41 out, $0.15 cache read per 1M.
        "kimi-k2-6": ModelPricing(
            input_per_1m=0.65, output_per_1m=3.41,
            cache_read_multiplier=0.15 / 0.65, cache_write_multiplier=1.0,
            cache_write_1h_multiplier=1.0,
        ),
        # fireworks: $3.00 in, $15.00 out, $0.30 cache read per 1M. If
        # probe_openrouter.py check 1 selects fireworks/us, this row becomes
        # 3.30 / 16.50 / 0.10 BEFORE the first paid run, never after.
        "kimi-k3": ModelPricing(
            input_per_1m=3.00, output_per_1m=15.00,
            cache_read_multiplier=0.10, cache_write_multiplier=1.0,
            cache_write_1h_multiplier=1.0,
        ),
    }
)
```

- [ ] **Step 4: Run tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_costs.py tests/test_config.py tests/test_usage_accounting.py -v`
Expected: PASS, including `test_every_configured_model_name_can_be_priced[openrouter]` from Task 3.

- [ ] **Step 5: Commit (own hunks only; these two files carry pre-existing edits)**

```bash
git add -p bakeoff/src/bakeoff/costs.py bakeoff/tests/test_costs.py
git commit -m "feat: price kimi-k2-6 and kimi-k3 at their pinned openrouter endpoints

Endpoint rates, not model rates, with a real cache-read multiplier and a
1.0 cache-write price. No -runtime alias: the arms have one transport.
PRICING_BASIS moves so records under the two books stay distinguishable.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Split the `map_openai_params` wrapper; manifest carries the provider

**Files:**
- Modify: `bakeoff/src/bakeoff/litellm_patches.py:185-196` (ids), `:500-636` (`_apply_openai_param_pins`), `:638-653` (`_report`)
- Modify: `bakeoff/src/bakeoff/proxy_callback.py:532-573` (`write_manifest`, `read_manifest`)
- Modify: `bakeoff/src/bakeoff/runner.py:1390-1392` (3-tuple)
- Test: `bakeoff/tests/test_litellm_patches.py`, `bakeoff/tests/test_proxy_callback.py`

**Interfaces:**
- Consumes: `PROVIDER_ENV` semantics from Task 1 (the string `"BAKEOFF_PROVIDER"`; **do not import `bakeoff.proxy` from `litellm_patches`**, define the literal locally to keep the module import-light).
- Produces: `OPENAI_RESOLVED_PARAMS_CAPTURE = "openai_resolved_params_capture"`; `write_manifest(patches, litellm_version, wire_dir, provider_route: str = "")`; `read_manifest(wire_dir) -> tuple[list[str], str, str]` (patches, litellm version, provider_route). Task 8 reads the third element.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_litellm_patches.py`:

```python
import os


def _map(model="moonshotai/kimi-k2.6"):
    import litellm

    return litellm.OpenAIConfig().map_openai_params(
        non_default_params={"max_tokens": 16, "reasoning_effort": "medium"},
        optional_params={},
        model=model,
        drop_params=False,
    )


def test_under_openrouter_the_two_mantle_rewrites_are_off_and_capture_stays_on(monkeypatch):
    """Spec §3. The rename sends a parameter no OpenRouter endpoint lists
    (zero eligible providers under require_parameters) and the pin fights
    extra_body.reasoning. But record_resolved_params lived inside the same
    wrapper, so switching the wrapper off wholesale nulled `resolved` on
    every call. The capture is its own id and is never gated."""
    from bakeoff.proxy_callback import open_resolved_capture, resolved_params

    monkeypatch.setenv("BAKEOFF_PROVIDER", "openrouter")
    applied = litellm_patches.apply()
    assert litellm_patches.OPENAI_RESOLVED_PARAMS_CAPTURE in applied
    assert litellm_patches.MAX_COMPLETION_TOKENS_RENAME not in applied
    assert litellm_patches.REASONING_EFFORT_PINNED_NONE not in applied

    open_resolved_capture("call-openrouter")
    mapped = _map()
    assert mapped.get("max_tokens") == 16 and "max_completion_tokens" not in mapped
    assert mapped.get("reasoning_effort") == "medium"
    captured = resolved_params("call-openrouter")
    assert captured is not None and captured["max_tokens"] == 16
    open_resolved_capture(None)


def test_under_bedrock_all_six_ids_apply_and_capture_records_the_rewritten_params(monkeypatch):
    from bakeoff.proxy_callback import open_resolved_capture, resolved_params

    monkeypatch.setenv("BAKEOFF_PROVIDER", "bedrock")
    applied = litellm_patches.apply()
    assert set(applied) >= {
        litellm_patches.MAX_COMPLETION_TOKENS_RENAME,
        litellm_patches.REASONING_EFFORT_PINNED_NONE,
        litellm_patches.OPENAI_RESOLVED_PARAMS_CAPTURE,
    }
    open_resolved_capture("call-bedrock")
    mapped = _map(model="google.gemma-4-31b")
    assert mapped.get("max_completion_tokens") == 16 and "max_tokens" not in mapped
    assert mapped.get("reasoning_effort") == "none"
    # Capture runs AFTER the rewrites: `resolved` is what went out.
    assert resolved_params("call-bedrock")["max_completion_tokens"] == 16
    open_resolved_capture(None)


def test_an_unset_provider_means_bedrock(monkeypatch):
    """An invocation path that never sets BAKEOFF_PROVIDER behaves exactly as
    it did before the seam existed."""
    monkeypatch.delenv("BAKEOFF_PROVIDER", raising=False)
    assert litellm_patches.MAX_COMPLETION_TOKENS_RENAME in litellm_patches.apply()
```

Append to `bakeoff/tests/test_proxy_callback.py`:

```python
def test_the_manifest_round_trips_the_provider_route(tmp_path):
    from bakeoff.proxy_callback import read_manifest, write_manifest

    write_manifest(["a", "b"], "1.95.0", tmp_path, provider_route="openrouter")
    assert read_manifest(tmp_path) == (["a", "b"], "1.95.0", "openrouter")


def test_an_old_manifest_without_a_route_reads_as_no_claim(tmp_path):
    (tmp_path / "adapter_patches.json").write_text('{"patches": ["a"], "litellm": "1.95.0"}')
    from bakeoff.proxy_callback import read_manifest

    assert read_manifest(tmp_path) == (["a"], "1.95.0", "")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_litellm_patches.py tests/test_proxy_callback.py -v -k "openrouter or bedrock or unset_provider or manifest"`
Expected: FAIL (`AttributeError: OPENAI_RESOLVED_PARAMS_CAPTURE`; `TypeError: write_manifest() got an unexpected keyword argument`).

- [ ] **Step 3: Add the id and the gate to `litellm_patches.py`**

After `REASONING_EFFORT_PINNED_NONE = ...` at `:189` add:

```python
OPENAI_RESOLVED_PARAMS_CAPTURE = "openai_resolved_params_capture"

# The env var the proxy is handed by proxy.proxy_environment. The literal is
# repeated here rather than imported: this module must stay import-light
# (importing it applies the patches), and bakeoff.proxy pulls in docker.
_PROVIDER_ENV = "BAKEOFF_PROVIDER"


def _rewrites_enabled() -> bool:
    """Whether the two mantle-only rewrites apply. Read at call time so a
    test can flip it in-process; in the proxy it never changes.

    Absent means bedrock: an invocation path that never sets the variable
    behaves exactly as it did before the provider seam existed.
    """
    return os.environ.get(_PROVIDER_ENV, "bedrock") != "openrouter"
```

(`os` is already imported at module scope for `_report`; if not, add `import os`.)

- [ ] **Step 4: Rewrite `_apply_openai_param_pins`**

Replace the function body from `import functools` (`:562`) to the end of the function (`:636`) with:

```python
    import functools

    import litellm

    # `in __dict__`, not `hasattr`. OpenAIConfig inherits from BaseConfig,
    # which declares map_openai_params abstractly -- so hasattr stays True
    # after the override is gone, and the wrapper would wrap an abstract stub
    # that returns None. The guard has to ask whether THIS class still defines
    # the method, which is the thing a litellm refactor would move.
    if "map_openai_params" not in vars(litellm.OpenAIConfig):
        raise RuntimeError(
            "litellm.OpenAIConfig no longer defines map_openai_params: the "
            f"litellm pin moved and {MAX_COMPLETION_TOKENS_RENAME} / "
            f"{REASONING_EFFORT_PINNED_NONE} / {OPENAI_RESOLVED_PARAMS_CAPTURE} "
            "would silently stop applying"
        )

    original = litellm.OpenAIConfig.map_openai_params
    if not getattr(original, _WRAPPED_MARKER, False):

        @functools.wraps(original)
        def patched(
            self,
            non_default_params: dict,
            optional_params: dict,
            model: str,
            drop_params: bool,
        ) -> dict:
            # Forwarded BY KEYWORD, and the parameter names are load-bearing:
            # litellm calls this with keywords, so a wrapper whose signature
            # renames them raises TypeError inside the provider call and
            # litellm re-wraps that as APIConnectionError -- a signature bug
            # wearing a provider failure's clothes.
            mapped = original(
                self,
                non_default_params=non_default_params,
                optional_params=optional_params,
                model=model,
                drop_params=drop_params,
            )
            if isinstance(mapped, dict):
                # THE TWO REWRITES, bedrock only (spec 2026-09-08 §3). On
                # OpenRouter the rename names a parameter no endpoint lists,
                # which under require_parameters is zero eligible providers,
                # and the pin would fight the per-arm extra_body.reasoning.
                if _rewrites_enabled():
                    if "max_tokens" in mapped:
                        mapped["max_completion_tokens"] = mapped.pop("max_tokens")
                    # Assigned unconditionally, never set-if-absent: the value
                    # that would otherwise be here is the one derived from
                    # Claude Code's `thinking` block, and it is exactly what
                    # has to lose.
                    mapped[_REASONING_EFFORT] = _REASONING_EFFORT_VALUE
                # THE CAPTURE, every provider, AFTER the rewrites. This is the
                # ONLY place that knows what the provider is getting: measured
                # 2026-08-12, the success callback fires on the outer
                # anthropic_messages call and the nested acompletion fires
                # nothing, so every param this wrapper touches is invisible
                # from where capture runs. It used to sit inside the rewrite
                # block; gating the block off for openrouter would have nulled
                # `resolved` on every call, which is why it is its own id.
                record_resolved_params({"model": model, **mapped})
            return mapped

        setattr(patched, _WRAPPED_MARKER, True)
        litellm.OpenAIConfig.map_openai_params = patched

    # The observable behaviour, not the assignment. A derived reasoning_effort
    # goes in, so under bedrock the probe also proves the pin BEATS one rather
    # than merely filling a gap, and under openrouter it proves the pin LEFT
    # it alone.
    probe = litellm.OpenAIConfig().map_openai_params(
        non_default_params={"max_tokens": 16, _REASONING_EFFORT: "medium"},
        optional_params={},
        model="google.gemma-4-31b",
        drop_params=False,
    )
    applied: list[str] = []
    if _rewrites_enabled():
        if probe.get("max_completion_tokens") != 16 or "max_tokens" in probe:
            raise RuntimeError(
                f"{MAX_COMPLETION_TOKENS_RENAME} did not take: got {probe!r}"
            )
        if probe.get(_REASONING_EFFORT) != _REASONING_EFFORT_VALUE:
            raise RuntimeError(
                f"{REASONING_EFFORT_PINNED_NONE} did not take: got {probe!r}"
            )
        applied += [MAX_COMPLETION_TOKENS_RENAME, REASONING_EFFORT_PINNED_NONE]
    else:
        if probe.get("max_tokens") != 16 or "max_completion_tokens" in probe:
            raise RuntimeError(
                f"openrouter: the max_tokens rename applied anyway: got {probe!r}"
            )
        if probe.get(_REASONING_EFFORT) != "medium":
            raise RuntimeError(
                f"openrouter: the reasoning_effort pin applied anyway: got {probe!r}"
            )
    # Capture is verified by the marker plus the in-process tests: exercising
    # record_resolved_params here would need a call id in THIS context, and a
    # ContextVar set at apply time leaks into every server task copied from
    # it, attributing unkeyed captures to a probe id.
    applied.append(OPENAI_RESOLVED_PARAMS_CAPTURE)
    return applied
```

Update the function's docstring header list (`:511-513`) to three lines:

```
      MAX_COMPLETION_TOKENS_RENAME      the cap, under the spelling that still works   (bedrock only)
      REASONING_EFFORT_PINNED_NONE      thinking off, uniformly and explicitly         (bedrock only)
      OPENAI_RESOLVED_PARAMS_CAPTURE    what went out, filed under the call id         (every provider)
```

- [ ] **Step 5: Manifest carries the provider**

Change `_report` at `bakeoff/src/bakeoff/litellm_patches.py:651-652` to:

```python
    wire_dir = Path(os.environ.get("BAKEOFF_WIRE_DIR", "/eval/wire"))
    provider_route = os.environ.get(_PROVIDER_ENV, "")
    return write_manifest(patches, litellm_version, wire_dir, provider_route=provider_route)
```

Change `write_manifest` in `bakeoff/src/bakeoff/proxy_callback.py:532-556` to:

```python
def write_manifest(
    patches: list[str], litellm_version: str, wire_dir: Path, provider_route: str = ""
) -> Path | None:
    """Proxy side: record what this process did to its own litellm, and which
    provider route it served.

    Written by the proxy about itself, deliberately. Section 6.1's rule is that
    configuration is never reported as observation -- a record must not claim a
    patch was active because a config file asked for one -- and the harness's
    own litellm version says nothing about the container, which pins its own.
    `provider_route` is the proxy's reading of BAKEOFF_PROVIDER, not the
    driver's flag; "" means the proxy made no claim.
    """
    try:
        wire_dir.mkdir(parents=True, exist_ok=True)
        path = wire_dir / MANIFEST_NAME
        path.write_text(
            json.dumps(
                {
                    "patches": sorted(patches),
                    "litellm": litellm_version,
                    "provider_route": provider_route,
                },
                sort_keys=True,
            )
        )
        return path
    except OSError:
        return None
```

Change `read_manifest` to return a 3-tuple:

```python
def read_manifest(wire_dir: Path) -> tuple[list[str], str, str]:
    """Harness side: (patches, litellm version, provider_route), or empties.

    An absent manifest returns empty values. A manifest written before the
    provider seam has no `provider_route` and reads "" -- no claim, not
    "bedrock": the record must not invent a route for a proxy that never
    said which one it served.
    """
    path = Path(wire_dir) / MANIFEST_NAME
    if not path.exists():
        return [], "", ""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return [], "", ""
    return (
        [str(p) for p in (data.get("patches") or [])],
        str(data.get("litellm") or ""),
        str(data.get("provider_route") or ""),
    )
```

Update the one harness caller at `bakeoff/src/bakeoff/runner.py:1390-1392`:

```python
    adapter_patches: list[str] = []
    proxy_litellm = ""
    provider_route = ""
    if proxy_wire_dir is not None:
        adapter_patches, proxy_litellm, provider_route = read_manifest(proxy_wire_dir)
```

`provider_route` is consumed in Task 8; until then it is an unused local, which is fine.

Run `grep -rn "read_manifest(" bakeoff/ --include=*.py` and update every other unpacking site to three values.

- [ ] **Step 6: Run tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_litellm_patches.py tests/test_proxy_callback.py tests/test_runner.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/litellm_patches.py bakeoff/src/bakeoff/proxy_callback.py bakeoff/src/bakeoff/runner.py bakeoff/tests/test_litellm_patches.py bakeoff/tests/test_proxy_callback.py
git commit -m "fix: the resolved-params capture is its own patch, not a side effect of the mantle rewrites

Under BAKEOFF_PROVIDER=openrouter the max_tokens rename and the
reasoning_effort pin are off; the capture that fills resolved and
sampling_source stays on and runs after whichever rewrites are live. The
adapter manifest now carries provider_route.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Classify: `api_credits`, `router_no_endpoint`, one auth signature

**Files:**
- Modify: `bakeoff/src/bakeoff/classify.py:78-92` (signatures), `:116` (add constant), `:236-249` (ladder)
- Test: `bakeoff/tests/test_classify.py`

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_classify.py`:

```python
def test_an_exhausted_openrouter_balance_is_an_infra_exclusion():
    """Spec §6. OpenRouter's 402. Operator's wallet, not the model; a 4xx
    with no branch fell through to `code = None` and the run scored as a
    model that did nothing."""
    exclusion = classify_exclusion(signals(
        api_error_status=402,
        terminal_error_messages=("OpenrouterException - Insufficient credits",),
    ))
    assert exclusion.reason_code == "api_credits"
    assert exclusion.cls is ExclusionClass.INFRA_FAILURE


def test_a_router_that_found_no_eligible_upstream_is_named_as_such():
    """What require_parameters: true or a wrong `order` slug produces: a 404
    whose body says no endpoint matched. Infra, and specifically the pin --
    not the model, and not a generic 404."""
    exclusion = classify_exclusion(signals(
        api_error_status=404,
        terminal_error_messages=("OpenrouterException - No endpoints found that support the requested parameters",),
    ))
    assert exclusion.reason_code == "router_no_endpoint"


def test_a_bare_404_is_still_not_an_infra_failure():
    assert classify_exclusion(signals(
        api_error_status=404,
        terminal_error_messages=("Not Found",),
    )) is None


def test_openrouters_missing_credentials_wording_is_an_auth_failure():
    exclusion = classify_exclusion(signals(
        api_error_status=None,
        terminal_error_messages=("OpenrouterException - No auth credentials found",),
    ))
    assert exclusion.reason_code == "api_auth"
```

(`ExclusionClass` must be imported at the top of the test file if it is not already: `from bakeoff.classify import ExclusionClass`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_classify.py -v -k "openrouter or eligible or bare_404"`
Expected: the first two and the fourth FAIL (`AttributeError: 'NoneType' object has no attribute 'reason_code'`); the third passes already.

- [ ] **Step 3: Implement**

In `AUTH_ERROR_SIGNATURES` at `bakeoff/src/bakeoff/classify.py:78-92`, add after `"authenticationerror",`:

```python
    # OpenRouter's wording for a request with no Authorization header at all
    # (a proxy started without OPENROUTER_API_KEY). Arrives as 401 too, but
    # the router-refusal path can strip the status; see AUTH_ERROR_STATUSES.
    "no auth credentials found",
```

After `NO_DEPLOYMENT_SIGNATURE = "no deployments available"` at `:116` add:

```python
# OpenRouter's 404 body when `provider.order` names an upstream that does not
# serve the model, or when `require_parameters: true` finds no upstream that
# accepts every parameter sent. Infra, and specifically the PIN -- the config's
# fault, not the model's -- so it gets its own reason code rather than falling
# into the generic-4xx `None` a bare 404 still yields.
NO_ENDPOINT_SIGNATURE = "no endpoints found"
```

In the status ladder at `:236-249`, replace:

```python
    if status is not None:
        if status == 429:
            code = "api_throttle"
        elif status == 408:
            code = "api_timeout"
        elif status >= 500:
            code = "api_5xx"
        else:
            code = None
```

with:

```python
    if status is not None:
        if status == 429:
            code = "api_throttle"
        elif status == 408:
            code = "api_timeout"
        elif status == 402:
            # OpenRouter: the account balance is exhausted. The operator's
            # wallet, never the model.
            code = "api_credits"
        elif status == 404 and messages and NO_ENDPOINT_SIGNATURE in messages[-1]:
            code = "router_no_endpoint"
        elif status >= 500:
            code = "api_5xx"
        else:
            code = None
```

If `classify.py` documents the set of `reason_code` values anywhere (grep `"api_throttle"` in `schema.py` and `classify.py` docstrings), add the two new codes there too.

- [ ] **Step 4: Run tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_classify.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/classify.py bakeoff/tests/test_classify.py
git commit -m "feat: classify OpenRouter's 402 and no-endpoint 404 as infra, name its auth wording

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Wire metadata: `upstream_provider`, `native_finish_reason`, `usage_cost`

**Files:**
- Modify: `bakeoff/src/bakeoff/proxy_callback.py` (new helper after `finish_reason`, `:395`; three metadata keys in `_write`, `:465`)
- Modify: `bakeoff/src/bakeoff/wire.py:186-200` (mirror the keys as `None`)
- Test: `bakeoff/tests/test_proxy_callback.py`, `bakeoff/tests/test_wire.py`

**Interfaces:**
- Produces: `proxy_callback.upstream(kwargs: dict, response: dict) -> tuple[str | None, str | None, float | None]`; metadata keys `"upstream_provider"`, `"native_finish_reason"`, `"usage_cost"` on every wire entry from both capture paths. Task 8 reads them.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_proxy_callback.py`:

```python
OPENROUTER_RAW = {
    "id": "gen-123",
    "provider": "CoreWeave",
    "model": "moonshotai/kimi-k2.6",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "native_finish_reason": "tool_calls", "message": {"role": "assistant", "content": ""}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.000123},
}


def test_the_upstream_provider_and_cost_are_read_from_the_raw_provider_body(wire_dir):
    """Spec §4. litellm's ModelResponse is not guaranteed to keep OpenRouter's
    top-level `provider`, so the callback reads kwargs["original_response"]
    -- the raw JSON litellm hands every success callback -- before the dump."""
    kwargs = kwargs_for("run-or")
    kwargs["original_response"] = json.dumps(OPENROUTER_RAW)
    BakeoffProxyCallback().log_success_event(kwargs, {"choices": [{"finish_reason": "tool_calls"}]}, None, None)
    metadata = read_run_entries(wire_dir, "run-or")[0]["metadata"]
    assert metadata["upstream_provider"] == "CoreWeave"
    assert metadata["native_finish_reason"] == "tool_calls"
    assert metadata["usage_cost"] == pytest.approx(0.000123)


def test_the_upstream_fields_fall_back_to_the_response_dump_then_to_none(wire_dir):
    kwargs = kwargs_for("run-dump")
    BakeoffProxyCallback().log_success_event(kwargs, dict(OPENROUTER_RAW), None, None)
    metadata = read_run_entries(wire_dir, "run-dump")[0]["metadata"]
    assert metadata["upstream_provider"] == "CoreWeave"

    kwargs = kwargs_for("run-none")
    BakeoffProxyCallback().log_success_event(kwargs, {"choices": [{"finish_reason": "stop"}]}, None, None)
    metadata = read_run_entries(wire_dir, "run-none")[0]["metadata"]
    assert metadata["upstream_provider"] is None
    assert metadata["native_finish_reason"] is None
    assert metadata["usage_cost"] is None


def test_the_upstream_fields_never_come_from_the_configured_order(wire_dir):
    """The config's `order` is what was asked for. Only the response says
    who answered; absent, the field is None, not the yaml value."""
    kwargs = kwargs_for("run-cfg")
    kwargs["litellm_params"]["extra_body"] = {"provider": {"order": ["coreweave"]}}
    BakeoffProxyCallback().log_success_event(kwargs, {"choices": [{"finish_reason": "stop"}]}, None, None)
    assert read_run_entries(wire_dir, "run-cfg")[0]["metadata"]["upstream_provider"] is None
```

Append to `bakeoff/tests/test_wire.py` (find how that file builds a `WireLogger`/`BakeoffCallback` and an entry; reuse its fixture):

```python
def test_both_capture_paths_write_the_same_metadata_keys(tmp_path):
    """A key on one path only reads as 'this arm did not report one'."""
    from bakeoff.proxy_callback import BakeoffProxyCallback, read_run_entries
    from tests.test_proxy_callback import kwargs_for

    import os
    os.environ["BAKEOFF_WIRE_DIR"] = str(tmp_path / "wire")
    BakeoffProxyCallback().log_success_event(kwargs_for("run-keys"), {"choices": [{"finish_reason": "stop"}]}, None, None)
    proxy_keys = set(read_run_entries(tmp_path / "wire", "run-keys")[0]["metadata"])

    logger, callback = _logger_and_callback(tmp_path)  # whatever helper test_wire.py already uses
    callback.log_success_event(kwargs_for("run-keys"), {"choices": [{"finish_reason": "stop"}]}, None, None)
    inproc_keys = set(logger.entries()[0]["metadata"])

    assert {"upstream_provider", "native_finish_reason", "usage_cost"} <= proxy_keys
    assert proxy_keys - {"resolved_state"} == inproc_keys - {"bedrock_request_id"}
```

Replace `_logger_and_callback(tmp_path)` with the construction `tests/test_wire.py` already uses in its first test (read the file; do not invent a helper).

- [ ] **Step 2: Run to verify failure**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_proxy_callback.py tests/test_wire.py -v -k "upstream or metadata_keys"`
Expected: FAIL with `KeyError: 'upstream_provider'`.

- [ ] **Step 3: Implement the helper**

After `finish_reason` in `bakeoff/src/bakeoff/proxy_callback.py` (after `:395`) add:

```python
def upstream(kwargs: dict, response: dict[str, Any]) -> tuple[str | None, str | None, float | None]:
    """(upstream provider, its native finish reason, its reported cost), or Nones.

    OpenRouter returns top-level `provider`, `choices[].native_finish_reason`
    and -- with `usage: {include: true}` -- `usage.cost` in every response body
    (spec 2026-09-08 §4). litellm's ModelResponse dump is not guaranteed to
    preserve unknown top-level keys, so `kwargs["original_response"]` -- the
    raw provider JSON litellm passes every success callback, as a string --
    is read first and the dump second.

    None, never the config's `provider.order`: that is what was ASKED for,
    and only the response says who answered. Verifying the pin per call is
    the whole point of the field; a fallback to config would verify nothing.
    """
    raw = kwargs.get("original_response")
    sources: list[dict[str, Any]] = []
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                sources.append(parsed)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    elif isinstance(raw, dict):
        sources.append(raw)
    if isinstance(response, dict):
        sources.append(response)

    provider: str | None = None
    native: str | None = None
    cost: float | None = None
    for source in sources:
        if provider is None:
            value = source.get("provider")
            if isinstance(value, str) and value:
                provider = value
        if native is None:
            choices = source.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                value = choices[0].get("native_finish_reason")
                if isinstance(value, str) and value:
                    native = value
        if cost is None:
            usage = source.get("usage")
            if isinstance(usage, dict):
                value = usage.get("cost")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cost = float(value)
    return provider, native, cost
```

In `_write`, inside the `entry = {... "metadata": {...}}` literal (around `:465`), add directly after `"finish_reason": finish_reason(response),`:

```python
                    # Who actually answered, in the upstream's own words, and
                    # what it says it charged. Read from the raw body, never
                    # from the deployment's configured `order`; None when the
                    # route did not say (every bedrock arm).
                    "upstream_provider": upstream_provider,
                    "native_finish_reason": native_finish_reason,
                    "usage_cost": usage_cost,
```

and, before the `with self._lock:` line, compute them once:

```python
        upstream_provider, native_finish_reason, usage_cost = upstream(kwargs, response)
```

- [ ] **Step 4: Mirror in `wire.py`**

In `bakeoff/src/bakeoff/wire.py:186-200`, inside the `metadata={...}` literal, add after `"finish_reason": finish_reason(payload),`:

```python
                # Same keys as proxy_callback._write, None on this path: the
                # harness is the caller, no upstream stands behind it, and a
                # key present on one capture path only would read as "this
                # arm did not report one".
                "upstream_provider": None,
                "native_finish_reason": None,
                "usage_cost": None,
```

- [ ] **Step 5: Run tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_proxy_callback.py tests/test_wire.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/proxy_callback.py bakeoff/src/bakeoff/wire.py bakeoff/tests/test_proxy_callback.py bakeoff/tests/test_wire.py
git commit -m "feat: wire entries carry the upstream provider, its native finish reason and its reported cost

Read from litellm's original_response then the dump, never from the
configured order. Mirrored as None on the in-process path so the two
projections stay key-for-key identical.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Record fields and schema 3.9.0

**Files:**
- Modify: `bakeoff/src/bakeoff/schema.py:257-284` (changelog + version), `:415-436` (`Versions`), `:770-772` (after `terminal_finish_reason`)
- Modify: `bakeoff/src/bakeoff/runner.py` (three helpers after `terminal_finish_reason`, ~`:360`; assembly at `:614-615`, `:696-722`, `:752-756`)
- Test: `bakeoff/tests/test_runner.py`, `bakeoff/tests/test_schema.py`

**Interfaces:**
- Consumes: metadata keys from Task 7; `read_manifest` 3-tuple from Task 5.
- Produces: `runner.upstream_providers(entries) -> list[str]`, `runner.terminal_native_finish_reason(entries) -> str | None`, `runner.provider_cost_usd(entries) -> float | None`; fields `Versions.provider_route: str = ""`, `RunRecord.upstream_providers: list[str]`, `RunRecord.terminal_native_finish_reason: str | None`, `RunRecord.cost_usd_provider: float | None`.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_runner.py` (beside `_entry`):

```python
from bakeoff.runner import provider_cost_usd, terminal_native_finish_reason, upstream_providers


def _or_entry(provider, native="stop", cost=0.001, failed=False):
    return {"metadata": {
        "failed": failed, "finish_reason": "stop",
        "upstream_provider": provider, "native_finish_reason": native, "usage_cost": cost,
    }}


def test_upstream_providers_are_distinct_in_order_seen_and_skip_failures():
    """Spec §4. A list, not a scalar: a second upstream mid-run is the
    fallback allow_fallbacks:false forbids, and a scalar would hide it."""
    entries = [_or_entry("CoreWeave"), _or_entry("CoreWeave"), _or_entry("Fireworks"), _or_entry(None), _or_entry("Baseten", failed=True)]
    assert upstream_providers(entries) == ["CoreWeave", "Fireworks"]
    assert upstream_providers([]) == []


def test_the_terminal_native_finish_reason_is_the_last_returning_calls():
    entries = [_or_entry("CoreWeave", native="tool_calls"), _or_entry("CoreWeave", native="stop"), _or_entry("CoreWeave", native="length", failed=True)]
    assert terminal_native_finish_reason(entries) == "stop"
    assert terminal_native_finish_reason([_entry("stop")]) is None


def test_provider_cost_sums_returning_calls_and_is_none_if_any_lacks_it():
    """None is 'the provider did not say on every call', which is every
    bedrock run. Summing the calls that did say would be a partial figure
    wearing a total's name."""
    assert provider_cost_usd([_or_entry("CoreWeave", cost=0.5), _or_entry("CoreWeave", cost=0.25), _or_entry("x", cost=9, failed=True)]) == pytest.approx(0.75)
    assert provider_cost_usd([_or_entry("CoreWeave", cost=0.5), _or_entry("CoreWeave", cost=None)]) is None
    assert provider_cost_usd([]) is None
```

Append to `bakeoff/tests/test_schema.py`:

```python
def test_schema_3_9_0_adds_the_provider_fields_with_no_claim_defaults():
    from bakeoff.schema import SCHEMA_VERSION, RunRecord, Versions
    import dataclasses

    assert SCHEMA_VERSION == "3.9.0"
    fields = {f.name: f for f in dataclasses.fields(RunRecord)}
    assert fields["terminal_native_finish_reason"].default is None
    assert fields["cost_usd_provider"].default is None
    assert fields["upstream_providers"].default_factory() == []
    assert {f.name: f.default for f in dataclasses.fields(Versions)}["provider_route"] == ""
```

Find the existing record-assembly test in `tests/test_runner.py` that passes wire entries through `assemble_record` (grep `finish_reasons=` or `wire_entries_seen ==`) and add one assertion path for the new fields:

```python
def test_the_record_carries_the_upstream_fields_and_the_manifest_route(tmp_path, monkeypatch):
    # Reuse the fixture the nearest existing wire-entries test uses to build a
    # proxy_wire_dir with entries; then:
    from bakeoff.proxy_callback import write_manifest
    write_manifest(["openai_resolved_params_capture"], "1.95.0", wire_dir, provider_route="openrouter")
    # ... assemble via the same call that test uses, passing proxy_wire_dir=wire_dir ...
    assert record.versions.provider_route == "openrouter"
    assert record.upstream_providers == ["CoreWeave"]
    assert record.terminal_native_finish_reason == "stop"
    assert record.cost_usd_provider == pytest.approx(0.002)
```

Write the entries into `wire_dir / "<run_id>.jsonl"` with metadata from `_or_entry("CoreWeave", cost=0.001)` twice, matching how that neighbouring test writes its entries.

- [ ] **Step 2: Run to verify failure**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_runner.py tests/test_schema.py -v -k "upstream or native or provider_cost or 3_9_0 or manifest_route"`
Expected: FAIL on imports / `SCHEMA_VERSION == "3.8.0"`.

- [ ] **Step 3: Schema**

In `bakeoff/src/bakeoff/schema.py`, after the 3.8.0 changelog paragraph and before `SCHEMA_VERSION = "3.8.0"` (`:284`), add:

```python
# 3.9.0 adds `Versions.provider_route`, `upstream_providers`,
# `terminal_native_finish_reason` and `cost_usd_provider` for the OpenRouter
# route (spec 2026-09-08 §4). All four default to "not observed": "" / [] /
# None. Every earlier record is a bedrock record, but the empty
# `provider_route` on it is NOT to be read as "bedrock" -- the proxy never
# said, and the record does not invent a route for it.
#
# `upstream_providers` is a list because OpenRouter can answer one run from
# two upstreams, which is the fallback `allow_fallbacks: false` is configured
# to forbid; a scalar would hide the one event the field exists to expose.
# `cost_usd_provider` is the provider's own figure summed over returning
# calls, None if any returning call lacked it, beside the book's `cost_usd`;
# the gap is reported, never reconciled.
```

and change the version line to `SCHEMA_VERSION = "3.9.0"`.

In `Versions` (`:415-436`), after `pricing_basis: str = ""` add:

```python
    # Which provider route the proxy reported serving, from the adapter
    # manifest it writes about itself -- never the driver's --provider flag.
    # "" means the proxy made no claim (records before 3.9.0, or a run whose
    # manifest was never written).
    provider_route: str = ""
```

In `RunRecord`, after `terminal_finish_reason: str | None = None` (`:772`) add:

```python
    # Who answered, per OpenRouter's `provider` field, distinct and in the
    # order seen across the run's returning wire entries. Empty on every
    # bedrock run. More than one entry is a finding: the pinned upstream was
    # not the only one that served this run.
    upstream_providers: list[str] = field(default_factory=list)
    # The upstream's own word for how the last returning call stopped, beside
    # `terminal_finish_reason` (the route's word) -- one more mapping the
    # record would otherwise pass through unrecorded.
    terminal_native_finish_reason: str | None = None
    # What the provider said it charged, summed over returning calls. None
    # when any returning call did not say. Beside `cost_usd`, never folded in.
    cost_usd_provider: float | None = None
```

- [ ] **Step 4: Runner helpers and assembly**

After `terminal_finish_reason` in `bakeoff/src/bakeoff/runner.py` (after its closing line, ~`:376`) add:

```python
def upstream_providers(entries: list[dict[str, Any]]) -> list[str]:
    """Distinct `metadata.upstream_provider` values over RETURNING entries, in
    the order first seen. Failed entries are skipped: no upstream answered.
    Two values on a run pinned with allow_fallbacks:false is the finding the
    field exists for (spec 2026-09-08 §4)."""
    seen: list[str] = []
    for entry in entries:
        metadata = entry.get("metadata") or {}
        if metadata.get("failed"):
            continue
        value = metadata.get("upstream_provider")
        if isinstance(value, str) and value and value not in seen:
            seen.append(value)
    return seen


def terminal_native_finish_reason(entries: list[dict[str, Any]]) -> str | None:
    """The upstream's own word for how the LAST returning call stopped, the
    same walk as terminal_finish_reason over a different key."""
    for entry in reversed(entries):
        metadata = entry.get("metadata") or {}
        if metadata.get("failed"):
            continue
        value = metadata.get("native_finish_reason")
        if isinstance(value, str) and value:
            return value
    return None


def provider_cost_usd(entries: list[dict[str, Any]]) -> float | None:
    """What the provider said it charged, summed over returning entries.

    None -- not a partial sum -- when any returning entry lacks the figure,
    and None when nothing returned. A bedrock run is None on every call; a
    partial sum over the calls that happened to report would wear a total's
    name. Beside `cost_usd`, never reconciled into it.
    """
    total = 0.0
    counted = 0
    for entry in entries:
        metadata = entry.get("metadata") or {}
        if metadata.get("failed"):
            continue
        value = metadata.get("usage_cost")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        total += float(value)
        counted += 1
    return total if counted else None
```

At `:614-615`, after `provider_finish = terminal_finish_reason(entries)` add:

```python
    upstreams = upstream_providers(entries)
    native_finish = terminal_native_finish_reason(entries)
    cost_provider = provider_cost_usd(entries)
```

`assemble_record` receives `adapter_patches` and `proxy_litellm` today; find how they reach it (grep `adapter_patches` in `runner.py`) and pass `provider_route` the same way. In the `Versions(...)` literal at `:696-722` add after `litellm_proxy_version=proxy_litellm,`:

```python
            provider_route=provider_route,
```

In the `RunRecord(...)` literal at `:752-756`, after `terminal_finish_reason=provider_finish,` add:

```python
        upstream_providers=upstreams,
        terminal_native_finish_reason=native_finish,
        cost_usd_provider=cost_provider,
```

- [ ] **Step 5: Run the suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q -m "not integration"`
Expected: PASS. If a schema golden/fixture test compares a full record dict, update its expected keys (the failing assertion will name them).

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/schema.py bakeoff/src/bakeoff/runner.py bakeoff/tests/test_runner.py bakeoff/tests/test_schema.py
git commit -m "feat: schema 3.9.0 -- provider_route, upstream_providers, native finish reason, provider cost

All four default to 'not observed'. provider_route comes from the proxy's
manifest, the other three from the wire entries; none from the flag or the
yaml.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: `scripts/probe_openrouter.py`, the six-check gate

**Files:**
- Create: `bakeoff/scripts/probe_openrouter.py`
- Test: `bakeoff/tests/test_probe_openrouter.py` (pure helpers only; the script itself spends money and is not in the unit run)

**Interfaces:**
- Consumes: `Proxy` from `bakeoff.proxy`, `build_proxy_image` from `scripts.run_matrix` (grep its definition; it is called at `run_matrix.py:450`), `read_run_entries` from `bakeoff.proxy_callback`, `load_env_file` from `bakeoff.envfile`.
- Produces: an exit code (0 all pass, 1 `GATE INCOMPLETE`) and `<scratch>/probe_openrouter/<check>.json`.

- [ ] **Step 1: Write the failing tests for the pure helpers**

Create `bakeoff/tests/test_probe_openrouter.py`:

```python
"""The pure parts of the openrouter probe. The script spends money and is
not in the unit run; what is testable offline is how it reads a response and
how it decides a check."""

from scripts.probe_openrouter import (
    cached_tokens,
    check_cache_fires,
    check_provider_pin,
    check_usage_exclusive,
    filler,
)


def test_filler_is_deterministic_and_roughly_the_size_asked_for():
    a, b = filler("seed", 4000), filler("seed", 4000)
    assert a == b
    assert 3000 <= len(a.split()) <= 5000


def test_cached_tokens_reads_the_openai_details_shape_and_is_none_when_absent():
    assert cached_tokens({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 96}}) == 96
    assert cached_tokens({"prompt_tokens": 100}) is None


def test_the_provider_pin_check_wants_the_named_upstream_in_every_response():
    assert check_provider_pin("coreweave", [{"provider": "CoreWeave"}, {"provider": "CoreWeave"}])["pass"] is True
    result = check_provider_pin("coreweave", [{"provider": "CoreWeave"}, {"provider": "Fireworks"}])
    assert result["pass"] is False and "Fireworks" in result["seen"]


def test_the_cache_check_is_a_hit_rate_over_replicates_not_one_success():
    pairs = [({"prompt_tokens": 4000, "cost": 1.0}, {"prompt_tokens": 4000, "prompt_tokens_details": {"cached_tokens": 3968}, "cost": 0.5})] * 4
    pairs.append(({"prompt_tokens": 4000, "cost": 1.0}, {"prompt_tokens": 4000, "cost": 1.0}))
    result = check_cache_fires(pairs)
    assert result["hits"] == 4 and result["replicates"] == 5
    assert result["pass"] is True  # >= 3/5 with cost dropping on each hit


def test_usage_exclusivity_decides_the_subtraction():
    # Anthropic-shaped output_tokens 50 and reasoning 30 against OpenAI
    # completion_tokens 50 (inclusive): output already contains reasoning.
    result = check_usage_exclusive(anthropic={"output_tokens": 50, "reasoning_tokens": 30}, openai={"completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30}})
    assert result["output_is_inclusive_of_reasoning"] is True
    result = check_usage_exclusive(anthropic={"output_tokens": 20, "reasoning_tokens": 30}, openai={"completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30}})
    assert result["output_is_inclusive_of_reasoning"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_probe_openrouter.py -v`
Expected: FAIL, `ModuleNotFoundError: scripts.probe_openrouter`.

- [ ] **Step 3: Write the script**

Create `bakeoff/scripts/probe_openrouter.py`:

```python
#!/usr/bin/env python3
"""The OpenRouter gate: six checks, run before the first paid matrix (spec 2026-09-08 §7).

Spends cents. Its verdicts settle the two values the design left open --
whether `fireworks/us` is a valid `provider.order` slug, and whether litellm's
Anthropic adapter reports `output_tokens` inclusive of reasoning -- and prove,
per arm, that the pin holds, the cache fires, thinking round-trips through the
adapter, and the callback captured what the record depends on.

Two legs, because two different things are under test:

  Leg A  straight at https://openrouter.ai/api/v1  -- the PROVIDER
    1  provider pin        response.provider == the configured order, every call
    2  require_parameters  Claude Code's parameter set does not 404
    3  cache fires         repeated prefix -> cached_tokens > 0 and cost drops,
                           hit rate over 5 replicates, not one success
  Leg B  through the proxy container         -- litellm's ANTHROPIC ADAPTER
    4  thinking round trip turn 2 re-sends thinking + tool_use + tool_result: 200
    5  usage exclusivity   Anthropic output_tokens vs OpenRouter completion_tokens
    6  callback capture    upstream_provider, native_finish_reason, usage_cost,
                           resolved_state == captured, no unattributed.jsonl

Leg B posts from INSIDE the proxy container (`docker exec`), the same trick
Proxy._wait uses: the internal network has no host route.

Any failure prints GATE INCOMPLETE and exits 1 -- the wording verify_logger.py
uses, so a weaker gate does not pass under the same name. Outputs one JSON
per check under $BAKEOFF_PROBE_OUT (default: ./probe_openrouter_out/).

Usage:
    python scripts/probe_openrouter.py                  # both arms, all checks
    python scripts/probe_openrouter.py --arm kimi-k3 --checks 1,3
    python scripts/probe_openrouter.py --k3-order fireworks/us   # try the variant slug
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

BAKEOFF = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BAKEOFF))
sys.path.insert(0, str(BAKEOFF / "src"))

from bakeoff.envfile import load_env_file  # noqa: E402

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
CONFIG = BAKEOFF / "config" / "litellm_config_openrouter.yaml"
OUT = Path(os.environ.get("BAKEOFF_PROBE_OUT", BAKEOFF / "probe_openrouter_out"))

# The parameter set Claude Code's request resolves to on the openai/ provider
# after litellm's adapter and the config's drops (check 2). Verified against
# the wire log's `resolved` on a bedrock-mantle run, 2026-08-12; the openrouter
# arms drop reasoning_effort and add extra_body, so this is what goes out.
TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "Run a shell command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
}


# ------------------------------------------------------------------ pure helpers


def filler(seed: str, approx_tokens: int) -> str:
    """Deterministic prose of roughly `approx_tokens` tokens (~0.75 words each)."""
    rng = random.Random(seed)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
    return " ".join(rng.choice(words) for _ in range(int(approx_tokens * 0.75) + 1000))


def cached_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return details["cached_tokens"]
    return None


def check_provider_pin(expected_slug: str, responses: list[dict[str, Any]]) -> dict[str, Any]:
    """Every response's `provider` must name the pinned upstream. Compared
    case-insensitively on the slug's first path segment: OpenRouter reports
    display names ("CoreWeave") while `order` takes slugs ("coreweave")."""
    want = expected_slug.split("/")[0].lower()
    seen = [str(r.get("provider")) for r in responses]
    ok = bool(seen) and all(s.lower().replace(" ", "") == want for s in seen)
    return {"check": "provider_pin", "expected": expected_slug, "seen": seen, "pass": ok}


def check_cache_fires(pairs: list[tuple[dict[str, Any], dict[str, Any]]], min_hits: int = 3) -> dict[str, Any]:
    """Each pair is (first call usage, second call usage) for one replicate.
    A hit is cached_tokens > 0 on the second call AND a lower reported cost.
    Pass is a hit rate, never one success -- the bedrock lottery finding."""
    hits = 0
    rows = []
    for first, second in pairs:
        cached = cached_tokens(second) or 0
        cheaper = isinstance(second.get("cost"), (int, float)) and isinstance(first.get("cost"), (int, float)) and second["cost"] < first["cost"]
        hit = cached > 0 and cheaper
        hits += hit
        rows.append({"cached_tokens": cached, "first_cost": first.get("cost"), "second_cost": second.get("cost"), "hit": hit})
    return {"check": "cache_fires", "replicates": len(pairs), "hits": hits, "rows": rows, "pass": hits >= min_hits}


def check_usage_exclusive(anthropic: dict[str, Any], openai: dict[str, Any]) -> dict[str, Any]:
    """Does the Anthropic-shaped `output_tokens` the transcript will carry
    already include reasoning? Decides the trajectory.py subtraction (§4)."""
    out = int(anthropic.get("output_tokens") or 0)
    reasoning = int(anthropic.get("reasoning_tokens") or 0)
    completion = int(openai.get("completion_tokens") or 0)
    inclusive = out == completion and reasoning > 0
    return {
        "check": "usage_exclusive",
        "anthropic_output_tokens": out,
        "anthropic_reasoning_tokens": reasoning,
        "openai_completion_tokens": completion,
        "openai_reasoning_tokens": (openai.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "output_is_inclusive_of_reasoning": inclusive,
        "pass": True,  # informational: either answer is a finding, not a failure
    }


# ------------------------------------------------------------------ Leg A


def arms_from_config() -> dict[str, dict[str, Any]]:
    entries = yaml.safe_load(CONFIG.read_text())["model_list"]
    return {e["model_name"]: e["litellm_params"] for e in entries}


def openrouter_body(params: dict[str, Any], messages: list[dict], tools: bool, order: list[str] | None = None) -> dict[str, Any]:
    extra = json.loads(json.dumps(params.get("extra_body") or {}))
    if order is not None:
        extra["provider"]["order"] = order
    body = {
        "model": params["model"].removeprefix("openai/"),
        "messages": messages,
        "max_tokens": 64,
        "temperature": params.get("temperature", 1.0),
        "stop": ["\u0000"],
        **extra,
    }
    if "top_p" in params:
        body["top_p"] = params["top_p"]
    if tools:
        body["tools"] = [TOOL]
        body["tool_choice"] = "auto"
    return body


def post_openrouter(client: httpx.Client, key: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(OPENROUTER, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body, timeout=180.0)
    try:
        data = response.json()
    except json.JSONDecodeError:
        data = {"raw": response.text}
    data["_status"] = response.status_code
    return data


def leg_a(arm: str, params: dict[str, Any], key: str, k3_order: str | None) -> list[dict[str, Any]]:
    results = []
    order = params["extra_body"]["provider"]["order"]
    if arm == "kimi-k3" and k3_order:
        order = [k3_order]
    with httpx.Client() as client:
        # 1 provider pin, 3 calls
        responses = [post_openrouter(client, key, openrouter_body(params, [{"role": "user", "content": "Say ok."}], tools=False, order=order)) for _ in range(3)]
        results.append({**check_provider_pin(order[0], responses), "arm": arm, "statuses": [r["_status"] for r in responses]})
        # 2 require_parameters with Claude Code's parameter set
        r = post_openrouter(client, key, openrouter_body(params, [{"role": "user", "content": "List the files. Use the tool."}], tools=True, order=order))
        results.append({"check": "require_parameters", "arm": arm, "status": r["_status"], "body": r if r["_status"] != 200 else None, "pass": r["_status"] == 200})
        # 3 cache fires, 5 replicates, 3 s apart
        pairs = []
        for i in range(5):
            prefix = filler(f"{arm}-{i}", 4000)
            msgs = [{"role": "system", "content": prefix}, {"role": "user", "content": "Reply with one word."}]
            first = post_openrouter(client, key, openrouter_body(params, msgs, tools=False, order=order))
            time.sleep(3)
            second = post_openrouter(client, key, openrouter_body(params, msgs, tools=False, order=order))
            pairs.append((first.get("usage") or {}, second.get("usage") or {}))
        results.append({**check_cache_fires(pairs), "arm": arm})
    return results


# ------------------------------------------------------------------ Leg B


def exec_post(proxy, path: str, body: dict[str, Any], run_id: str) -> dict[str, Any]:
    """POST from inside the proxy container; the internal network has no host route."""
    payload = base64.b64encode(json.dumps(body).encode()).decode()
    code = (
        "import base64,json,urllib.request,sys\n"
        f"body=base64.b64decode('{payload}')\n"
        f"req=urllib.request.Request('http://127.0.0.1:4000{path}',data=body,method='POST',headers={{'Content-Type':'application/json','anthropic-version':'2023-06-01','X-Bakeoff-Run-Id':'{run_id}','Authorization':'Bearer probe'}})\n"
        "try:\n"
        "  r=urllib.request.urlopen(req,timeout=180); print(json.dumps({'status':r.status,'body':json.loads(r.read())}))\n"
        "except urllib.error.HTTPError as e:\n"
        "  print(json.dumps({'status':e.code,'body':e.read().decode('utf-8','replace')}))\n"
    )
    result = proxy.container.exec_run(["python", "-c", code])
    return json.loads(result.output.decode("utf-8", "replace").strip().splitlines()[-1])


def leg_b(arm: str, proxy, wire_dir: Path) -> list[dict[str, Any]]:
    from bakeoff.proxy_callback import read_run_entries

    results = []
    run_id = f"probe-{arm}-{int(time.time())}"
    anthropic_tool = {"name": "Bash", "description": "Run a shell command", "input_schema": TOOL["function"]["parameters"]}
    turn1 = {"model": arm, "max_tokens": 256, "stream": False, "tools": [anthropic_tool], "thinking": {"type": "adaptive"},
             "messages": [{"role": "user", "content": "Run `ls` with the Bash tool."}]}
    r1 = exec_post(proxy, "/v1/messages", turn1, run_id)
    blocks = r1["body"].get("content", []) if r1["status"] == 200 else []
    tool_use = next((b for b in blocks if b.get("type") == "tool_use"), None)
    # 4 round trip
    if tool_use:
        turn2 = dict(turn1)
        turn2["messages"] = turn1["messages"] + [
            {"role": "assistant", "content": blocks},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use["id"], "content": "README.md\nsrc\n"}]},
        ]
        r2 = exec_post(proxy, "/v1/messages", turn2, run_id)
        text = any(b.get("type") == "text" and b.get("text") for b in r2["body"].get("content", [])) if r2["status"] == 200 else False
        results.append({"check": "thinking_round_trip", "arm": arm, "turn1_status": r1["status"], "turn2_status": r2["status"],
                        "had_thinking_block": any(b.get("type") == "thinking" for b in blocks), "turn2_body": None if r2["status"] == 200 else r2["body"],
                        "pass": r2["status"] == 200 and text})
    else:
        results.append({"check": "thinking_round_trip", "arm": arm, "turn1_status": r1["status"], "turn1_body": r1["body"], "pass": False})
    # 5 + 6 from the wire entries this run produced
    entries = read_run_entries(wire_dir, run_id)
    last = entries[-1] if entries else {}
    meta = last.get("metadata") or {}
    raw = {}
    try:
        raw = json.loads((last.get("response") or {}).get("_raw", "{}")) if isinstance((last.get("response") or {}).get("_raw"), str) else (last.get("response") or {})
    except json.JSONDecodeError:
        pass
    results.append({**check_usage_exclusive(anthropic=r1["body"].get("usage", {}) if r1["status"] == 200 else {}, openai=raw.get("usage") or {}), "arm": arm})
    results.append({
        "check": "callback_capture", "arm": arm, "entries": len(entries),
        "upstream_provider": meta.get("upstream_provider"), "native_finish_reason": meta.get("native_finish_reason"),
        "usage_cost": meta.get("usage_cost"), "resolved_state": meta.get("resolved_state"),
        "unattributed_exists": (wire_dir / "unattributed.jsonl").exists(),
        "pass": bool(entries) and meta.get("upstream_provider") is not None and meta.get("usage_cost") is not None
                and meta.get("resolved_state") == "captured" and not (wire_dir / "unattributed.jsonl").exists(),
    })
    return results


# ------------------------------------------------------------------ driver


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="append", help="kimi-k2-6 / kimi-k3 (default both)")
    parser.add_argument("--checks", default="1,2,3,4,5,6")
    parser.add_argument("--k3-order", help="override kimi-k3 provider.order for check 1, e.g. fireworks/us")
    parser.add_argument("--skip-proxy", action="store_true", help="Leg A only (no Docker)")
    args = parser.parse_args()

    load_env_file(BAKEOFF / ".env")
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("GATE INCOMPLETE: no OPENROUTER_API_KEY")
        return 1
    arms = arms_from_config()
    chosen = args.arm or list(arms)
    checks = {int(c) for c in args.checks.split(",")}
    OUT.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for arm in chosen:
        if checks & {1, 2, 3}:
            results += leg_a(arm, arms[arm], key, args.k3_order)

    if not args.skip_proxy and checks & {4, 5, 6}:
        from bakeoff.proxy import Proxy, proxy_environment
        from scripts.run_matrix import build_proxy_image

        stamp = time.strftime("%Y%m%d-%H%M%S")
        wire_dir = OUT / f"wire-{stamp}"
        build_proxy_image(BAKEOFF)
        with Proxy("litellm_config_openrouter.yaml", wire_dir, proxy_environment("live", "openrouter"), f"probe-{stamp}",
                   image="bakeoff-litellm-proxy:matrix", repo_root=BAKEOFF) as proxy:
            for arm in chosen:
                results += leg_b(arm, proxy, wire_dir)

    failed = 0
    for r in results:
        name = f"{r['arm']}-{r['check']}"
        (OUT / f"{name}.json").write_text(json.dumps(r, indent=2, default=str))
        status = "PASS" if r.get("pass") else "FAIL"
        failed += not r.get("pass")
        print(f"{status:4}  {name}")
    if failed:
        print(f"\nGATE INCOMPLETE: {failed} check(s) failed; see {OUT}")
        return 1
    print(f"\nall checks passed; outputs in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Then verify the two names imported from `scripts.run_matrix` and `bakeoff.proxy` exist with those signatures (`grep -n "^def build_proxy_image" bakeoff/scripts/run_matrix.py`; `Proxy.__init__` at `proxy.py:72`). If `build_proxy_image` lives elsewhere, import it from there. If the wire entry's `response` does not carry the raw OpenRouter body (it carries litellm's dump), check 5's `openai` argument will be `{}` and the result records `openai_completion_tokens: 0`; in that case read `usage` from the dump (`last["response"]["usage"]`) instead, since litellm preserves `usage` on the dump.

- [ ] **Step 4: Run the helper tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_probe_openrouter.py -v`
Expected: PASS. Confirm `httpx` is importable in the venv (`.venv/bin/python -c "import httpx"`); `probe_cache.py` already depends on it.

- [ ] **Step 5: Dry-run Leg A with a real key, one arm, checks 1 and 2 only (spends < $0.05)**

Run: `cd bakeoff && .venv/bin/python scripts/probe_openrouter.py --arm kimi-k2-6 --checks 1,2 --skip-proxy`
Expected: two `PASS` lines. If check 1 fails with `seen` naming another upstream, the slug in `order` is wrong; fix the config before continuing.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/scripts/probe_openrouter.py bakeoff/tests/test_probe_openrouter.py
git commit -m "feat: probe_openrouter.py, the six-check gate before the first paid matrix

Leg A straight at OpenRouter (pin, require_parameters, cache hit rate);
Leg B through the proxy (thinking round trip, usage exclusivity, callback
capture). Any failure is GATE INCOMPLETE, exit 1.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Mutation anchors, docs, backlog

**Files:**
- Modify: `bakeoff/scripts/mutation_check.py` (`MUTATIONS` list)
- Modify: `CLAUDE.md` (Commands section; new gotchas block), `TASKS.md`
- Modify: `.gitignore` if `bakeoff/probe_openrouter_out/` is not covered (check `git status` after Step 5 of Task 9)

- [ ] **Step 1: Add the mutation anchors**

Append to `MUTATIONS` in `bakeoff/scripts/mutation_check.py`:

```python
    (
        # Spec 2026-09-08 §3: the provider gate. Forcing the rewrites on
        # under openrouter sends max_completion_tokens (no endpoint lists it;
        # require_parameters -> zero providers) and pins reasoning_effort
        # against extra_body.reasoning.
        "openrouter: apply the mantle rewrites on every provider",
        "src/bakeoff/litellm_patches.py",
        '    return os.environ.get(_PROVIDER_ENV, "bedrock") != "openrouter"',
        "    return True",
        "tests/test_litellm_patches.py -k openrouter_the_two_mantle_rewrites",
        "not integration",
    ),
    (
        # The capture must run AFTER the rewrites, or `resolved` reports the
        # pre-rename max_tokens with the provenance of an observation.
        "openrouter: capture before the rewrites, reporting what did not go out",
        "src/bakeoff/litellm_patches.py",
        "                record_resolved_params({\"model\": model, **mapped})\n            return mapped",
        "            return mapped",
        "tests/test_litellm_patches.py -k capture_records_the_rewritten",
        "not integration",
    ),
    (
        # Spec §6: a 402 falling through to `code = None` scores the
        # operator's empty wallet as a model that did nothing.
        "classify: drop the 402 branch",
        "src/bakeoff/classify.py",
        '        elif status == 402:\n            # OpenRouter: the account balance is exhausted. The operator\'s\n            # wallet, never the model.\n            code = "api_credits"\n',
        "",
        "tests/test_classify.py -k exhausted_openrouter",
        "not integration",
    ),
    (
        # Spec §4: the field must never fall back to the configured order.
        "callback: report the configured provider.order as the upstream",
        "src/bakeoff/proxy_callback.py",
        "    provider: str | None = None\n    native: str | None = None",
        "    provider: str | None = (((kwargs.get('litellm_params') or {}).get('extra_body') or {}).get('provider') or {}).get('order', [None])[0]\n    native: str | None = None",
        "tests/test_proxy_callback.py -k never_come_from_the_configured_order",
        "not integration",
    ),
```

Each `find` string must match the source byte-for-byte; after adding, run the mutation check **solo** and fix any anchor it reports stale:

Run: `cd bakeoff && .venv/bin/python scripts/mutation_check.py`
Expected: every mutation reports the named test going red, then the tree is restored (`git status` shows no unexpected changes).

- [ ] **Step 2: `CLAUDE.md` commands**

In the Commands section, change the two `run_matrix.py` invocations and the `smoke_test.py` live invocation to show the flag, and add the probe. Replace:

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --mode live --repeats 1
```

with:

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --mode live --repeats 1            # openrouter (default)
```

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --mode live --repeats 1 --provider bedrock
```

Add, before the collection driver paragraph, a new paragraph:

```
The **OpenRouter gate** — run before the first paid matrix on the openrouter provider. Six checks (spec 2026-09-08 §7): provider pin, `require_parameters` viability, cache hit rate, thinking round trip through litellm's adapter, usage exclusivity, callback capture. Spends cents. Exits 1 with `GATE INCOMPLETE` on any failure; its JSON outputs are what `TASKS.md` cites:

```bash
cd bakeoff && .venv/bin/python scripts/probe_openrouter.py
```
```

- [ ] **Step 3: `CLAUDE.md` gotchas block**

Add a new bullet list section after "Config gotchas that have already cost a debugging session", titled `## OpenRouter gotchas`, with these bullets, each amended with what `probe_openrouter.py` measured in Task 9 Step 5 and the full run (date them):

```
- **`--provider openrouter` is the default; bedrock is `--provider bedrock`.** The config is `litellm_config_openrouter.yaml`, the credential is one static `OPENROUTER_API_KEY` in `.env`, and `credential_window` returns no expiry (`openrouter-static`). `StreakTracker` is the only backstop for a revoked or exhausted key.
- **Provider pinning is load-bearing.** OpenRouter load-balances one model across upstreams with different quantizations and tool-call parsers. Every arm carries `provider.order` (one slug), `allow_fallbacks: false`, `require_parameters: true`. The record's `upstream_providers` is measured from the response's `provider` field, never the config; two values on one run is the finding. `fireworks/fast` serves K3 with **no `tools`**.
- **`reasoning_effort` is dropped on every OpenRouter arm, not pinned.** litellm derives one from Claude Code's `thinking: adaptive`; K2.6's endpoints list `reasoning` but not `reasoning_effort`, so under `require_parameters` a derived effort is zero eligible providers. `extra_body.reasoning: {enabled: true}` is the knob and thinking-on is the policy. The two mantle rewrites in `litellm_patches` are off under `BAKEOFF_PROVIDER=openrouter`; the resolved-params capture is its own id (`openai_resolved_params_capture`) and stays on — it used to live inside the rewrite wrapper, and gating that off would have nulled `resolved` on every call.
- **Cache-read has a price here.** CoreWeave K2.6 $0.15/M against $0.65 input; Fireworks K3 $0.30/M against $3.00. No write surcharge. Measured hit rate: <fill from probe check 3>.
- **`output_tokens` is <inclusive|exclusive> of reasoning on this route** (probe check 5, <date>). <If inclusive: trajectory.py subtracts at parse; pinned in test_usage_accounting.py.>
- **OpenRouter's 402 is `api_credits`, its no-endpoint 404 is `router_no_endpoint`.** Both infra. A bare 404 is still nothing.
```

- [ ] **Step 4: `TASKS.md`**

Add under the appropriate priority (P1 unless the backlog's conventions say otherwise) entries for:

- The bedrock corpus (25 records, 7 event logs under `~/.cache/bakeoff`) is not comparable with any openrouter record: new models, new provider, thinking on. Never sum across `Versions.provider_route`.
- Thinking-on policy and its cost estimate (+20% to +60% per run at 1k–3k reasoning tokens/turn against measured 163 output tokens/turn on kimi-k2-5).
- Sonnet 5 reference arm on OpenRouter: deferred; needs `test_no_deployment_targets_the_real_anthropic_api` reconsidered.
- Fireworks publishes no quantization for either Kimi model; recorded, not pinned.
- If probe check 5 reported inclusive: the `trajectory.py` subtraction and its `test_usage_accounting.py` pin, as its own item until landed.

- [ ] **Step 5: Run the whole gate**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q -m "not integration"`
Expected: PASS.

Run: `cd bakeoff && .venv/bin/python scripts/verify_logger.py`
Expected: passes as before (offline, provider-neutral), or `GATE INCOMPLETE` only for the documented no-Docker reason.

Run: `graphify update .` from the repo root (project rule after code changes).

- [ ] **Step 6: Commit (own hunks only for `CLAUDE.md`, `TASKS.md`)**

```bash
git add bakeoff/scripts/mutation_check.py
git add -p CLAUDE.md TASKS.md
git add graphify-out
git commit -m "docs: --provider in the commands, OpenRouter gotchas, mutation anchors for the four new guarantees

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review against the spec

| Spec section | Task |
|---|---|
| §1 provider seam, `proxy_environment`, `credential_window`, `.env.example` | 1, 2 |
| §2 config, `openai/` prefix, pins | 3 |
| §3 wrapper split, ids, `BAKEOFF_PROVIDER`, manifest | 5 |
| §4 `provider_route`, `upstream_provider`/`native_finish_reason`/`usage_cost`, record fields, reasoning exclusivity | 5, 7, 8, (9 check 5 decides the subtraction; landing it is a `TASKS.md` item in 10) |
| §5 price book, `PRICING_BASIS` | 4 |
| §6 classify | 6 |
| §7 probe | 9 |
| §8 tests | each task; spec named `tests/test_proxy.py`, which does not exist — `tests/test_credentials.py` already imports `bakeoff.proxy` and holds those pins instead |
| §9 docs | 10 |

Type consistency checked: `read_manifest` 3-tuple (Task 5) is consumed in Task 8; metadata keys `upstream_provider`/`native_finish_reason`/`usage_cost` (Task 7) are read by name in Task 8's helpers and Task 9's check 6; `EVAL_ARMS_BY_PROVIDER`, `PROVIDERS`, `DEFAULT_PROVIDER` (Task 1) are imported in Task 2; `OPENAI_RESOLVED_PARAMS_CAPTURE` (Task 5) is asserted in Task 5's tests and cited in Task 10's docs.
