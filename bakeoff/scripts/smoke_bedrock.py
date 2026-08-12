#!/usr/bin/env python3
"""Phase 0c smoke test (Task 12): does the LiteLLM config actually route to Bedrock?

Two stages, run in order:

  preflight  no network, no credentials. Parses litellm_config.yaml, builds the
             Router, checks every model_name has a PRICE_BOOK entry, and reports
             which credentials each arm still needs. Always safe to run.

  live       one minimal completion per arm through the real Router, with
             BakeoffCallback registered so wire logging is exercised on the same
             path the real run uses. Costs a few cents total.

Each arm is reported independently: a mantle failure and a bedrock-runtime
failure mean different things (spec section 6.4 -- adapter class vs model), and
collapsing them into one pass/fail would lose exactly the distinction Phase 0
exists to make.

Usage:
    python scripts/smoke_bedrock.py                    # preflight only
    python scripts/smoke_bedrock.py --live             # preflight + all arms
    python scripts/smoke_bedrock.py --live --models claude-sonnet-5,gemma-4-31b
    python scripts/smoke_bedrock.py --live --tools     # also test tool translation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config" / "litellm_config.yaml"
ENV_FILE = REPO / ".env"

sys.path.insert(0, str(REPO / "src"))


def scrub_placeholders() -> list[str]:
    """Delete env vars whose value is still an unfilled <placeholder>.

    Necessary because `import litellm` calls load_dotenv(), which reads
    bakeoff/.env with no placeholder filtering. An untouched
    `AWS_PROFILE=<aws-profile-name>` line therefore lands in os.environ, and
    botocore prefers a named profile over static keys -- so a fully populated
    key pair fails with ProfileNotFound. Scrub after litellm is imported.
    """
    removed = []
    for key, value in list(os.environ.items()):
        if value.startswith("<") and value.endswith(">"):
            del os.environ[key]
            removed.append(key)
    return removed


def resolve_aws_paths() -> None:
    """Make project-local AWS_CONFIG_FILE / credentials paths cwd-independent.

    botocore resolves a relative path against the process cwd, so a repo-local
    `AWS_CONFIG_FILE=.aws/config` silently finds nothing when the harness is
    launched from anywhere but bakeoff/. Anchor relative paths to the repo.
    """
    for key in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        value = os.environ.get(key)
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = REPO / path
        os.environ[key] = str(path)
        if not path.exists():
            print(f"aws         {key} points at a missing file: {path}")


def load_env_file(path: Path) -> list[str]:
    """Minimal .env loader. Avoids adding python-dotenv for one script.

    Does not overwrite variables already set in the environment -- an exported
    AWS_PROFILE or SSO session should win over a stale file.
    """
    loaded = []
    if not path.exists():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # Tolerate `export KEY=value` -- credentials get pasted straight out of
        # the AWS SSO console, which hands them over in that form. Without this
        # the key parses as "export AWS_ACCESS_KEY_ID" and the arm reports
        # MISSING while the file plainly contains the value.
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


# ---------------------------------------------------------------- credentials

# The name the harness carries the mantle bearer token under. Deliberately
# NOT AWS_BEARER_TOKEN_BEDROCK: LiteLLM's bedrock/ (SigV4) handler falls back
# to that variable when a deployment has no api_key, so a process holding it
# bearer-authenticates every bedrock-runtime arm and fails them with
#   not authorized to perform: bedrock:CallWithBearerToken
# unless the role carries that permission. config/litellm_config.yaml names
# BAKEOFF_MANTLE_TOKEN explicitly on the mantle deployments, which takes the
# api_key branch; the runtime deployments find nothing and sign. That is what
# lets one proxy serve both transports at once.
MANTLE_ENV = "BAKEOFF_MANTLE_TOKEN"

# The variable LiteLLM itself consults. Never set by the harness; scrubbed
# where an operator's shell or .env may have set it.
LITELLM_BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"

SIGV4_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def normalize_mantle_token() -> bool:
    """Move an operator-supplied bearer token onto the name the config reads.

    A token exported or written to .env under the AWS name is a working
    credential, and the rename would otherwise report the mantle arms as
    MISSING while it sits right there. Adopt it, then let the post-construction
    scrub remove the AWS-named copy so the runtime arms still sign.
    """
    legacy = os.environ.get(LITELLM_BEARER_ENV)
    if legacy and not os.environ.get(MANTLE_ENV):
        os.environ[MANTLE_ENV] = legacy
        return True
    return False


def derive_mantle_token(region: str) -> str | None:
    """Mint a short-term Bedrock bearer token from the ambient SigV4 session.

    The mantle arms authenticate with a bearer token, not SigV4, so an SSO
    session alone does not reach them. A long-term Bedrock API key is an IAM
    *service-specific credential*, which only an IAM user can hold -- an SSO
    assumed role cannot, so the console path is unavailable to this principal.

    The short-term token is derived locally from the current credentials and
    inherits their expiry. Deliberately held in memory only: writing it to
    .env would put a live credential on disk with no expiry tracking, and it
    dies with the session anyway.
    """
    try:
        from aws_bedrock_token_generator import provide_token
    except ImportError:
        print("mantle      aws-bedrock-token-generator not installed")
        return None
    try:
        return provide_token(region=region)
    except Exception as exc:  # noqa: BLE001
        print(f"mantle      token derivation failed: {type(exc).__name__}: {exc}")
        return None


def transport_of(params: dict[str, Any]) -> str:
    return "bedrock-runtime" if params.get("model", "").startswith("bedrock/") else "bedrock-mantle"


def missing_credentials(transport: str) -> list[str]:
    if transport == "bedrock-mantle":
        return [] if os.environ.get(MANTLE_ENV) else [MANTLE_ENV]
    # bedrock-runtime signs with SigV4: static keys, or a profile/SSO session.
    if os.environ.get("AWS_PROFILE"):
        return []
    return [name for name in SIGV4_ENV if not os.environ.get(name)]


# ------------------------------------------------------------------ preflight


def preflight() -> tuple[Any, list[dict[str, Any]], int]:
    import yaml

    from bakeoff.costs import PRICE_BOOK

    problems = 0
    config = yaml.safe_load(CONFIG.read_text())
    entries = config["model_list"]

    print(f"config      {CONFIG.relative_to(REPO.parent)}")
    print(f"arms        {len(entries)}")

    names = [entry["model_name"] for entry in entries]
    if len(names) != len(set(names)):
        # Repeated model_name makes LiteLLM round-robin the group, randomizing
        # transport per call and confounding latency and tool-translation results.
        print("FAIL        duplicate model_name -- LiteLLM would load-balance these")
        problems += 1

    arms = []
    for entry in entries:
        params = entry["litellm_params"]
        name = entry["model_name"]
        transport = transport_of(params)
        priced = name in PRICE_BOOK
        if not priced:
            problems += 1
        arms.append(
            {
                "name": name,
                "transport": transport,
                "model": params["model"],
                "priced": priced,
                "missing": missing_credentials(transport),
            }
        )

    print()
    print(f"{'arm':<30} {'transport':<16} {'priced':<7} credentials")
    print("-" * 78)
    for arm in arms:
        creds = "ok" if not arm["missing"] else "MISSING " + ",".join(arm["missing"])
        priced = "ok" if arm["priced"] else "MISSING"
        print(f"{arm['name']:<30} {arm['transport']:<16} {priced:<7} {creds}")

    # Router construction resolves os.environ/ references and validates params.
    # It does not make a network call, so this stays credential-free.
    from litellm import Router

    try:
        router = Router(model_list=entries, num_retries=0)
        print("\nrouter      constructed ok")
        # The harness carries the token as MANTLE_ENV, which LiteLLM never
        # reads, so nothing here poisons the runtime arms. An operator's shell
        # or .env can still hold LITELLM_BEARER_ENV, and that one LiteLLM does
        # consult -- it would route every bedrock-runtime arm through bearer
        # auth instead of SigV4 and fail them all, while the mantle arms stay
        # green. Scrub it after construction, when the mantle deployments have
        # already captured their api_key.
        if os.environ.pop(LITELLM_BEARER_ENV, None):
            print(f"            ambient {LITELLM_BEARER_ENV} unset "
                  "(keeps bedrock-runtime arms on SigV4)")
    except Exception as exc:  # noqa: BLE001 -- report, do not mask
        print(f"\nrouter      FAILED to construct: {type(exc).__name__}: {exc}")
        return None, arms, problems + 1

    runtime = [a for a in arms if a["transport"] == "bedrock-runtime" and not a["missing"]]
    if runtime:
        problems += check_sigv4(runtime)

    return router, arms, problems


def check_sigv4(runtime_arms: list[dict[str, Any]]) -> int:
    """Validate SigV4 credentials with STS before spending Bedrock calls.

    Bedrock reports expired temporary credentials as
    `Invalid API Key format: Must start with pre-defined prefix` -- a
    bearer-token error on a route that does not use bearer tokens. Read
    literally it sends you looking at the config; the real fault is an expired
    SSO/STS session. One free GetCallerIdentity turns that into the truth.
    """
    try:
        import boto3
    except ImportError:
        print(
            f"boto3       NOT INSTALLED -- {len(runtime_arms)} bedrock-runtime arms "
            "cannot run (`pip install boto3`)"
        )
        for arm in runtime_arms:
            arm["missing"] = ["boto3"]
        return 1

    region = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION") or "us-east-1"
    try:
        identity = boto3.client("sts", region_name=region).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        detail = str(exc)
        hint = ""
        if "ExpiredToken" in detail or "expired" in detail:
            hint = " -- refresh the session (aws sso login / new STS token)"
        print(f"sigv4       FAILED: {name}: {detail[:160]}{hint}")
        for arm in runtime_arms:
            arm["missing"] = ["valid AWS credentials"]
        return 1

    print(f"sigv4       ok -- account {identity['Account']}, {identity['Arn']}")
    return 0


# ----------------------------------------------------------------- live smoke

PROBE = [{"role": "user", "content": "Reply with exactly: OK"}]

PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_status",
        "description": "Return the status of a service.",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
}


def usage_of(response: Any):
    from bakeoff.schema import TokenUsage

    usage = getattr(response, "usage", None) or {}
    get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
    prompt_details = get("prompt_tokens_details", None) or {}
    completion_details = get("completion_tokens_details", None) or {}

    def sub(details, key):
        if isinstance(details, dict):
            return details.get(key) or 0
        return getattr(details, key, 0) or 0

    return TokenUsage(
        input=get("prompt_tokens", 0) or 0,
        output=get("completion_tokens", 0) or 0,
        reasoning=sub(completion_details, "reasoning_tokens"),
        cache_read=sub(prompt_details, "cached_tokens"),
        cache_write=get("cache_creation_input_tokens", 0) or 0,
    )


def live(router, arms, selected: set[str] | None, with_tools: bool) -> int:
    from bakeoff.costs import cost_usd
    from bakeoff.wire import BakeoffCallback, WireLogger

    import litellm

    run_id = f"smoke-{int(time.time())}"
    wire_path = Path(tempfile.mkdtemp(prefix="bakeoff-smoke-")) / f"{run_id}.jsonl.gz"
    wire_logger = WireLogger(wire_path)
    # An INSTANCE, not the dotted-path config form: LiteLLM's success_handler
    # dispatches on isinstance(cb, CustomLogger), and a class object fails that
    # check silently -- no wire log, no error.
    litellm.callbacks = [BakeoffCallback(wire_logger, run_id)]

    print(f"\nlive smoke  run_id={run_id}")
    print(f"wire log    {wire_path}\n")
    print(f"{'arm':<30} {'result':<10} {'ms':>7} {'in':>7} {'out':>7} {'usd':>9}  detail")
    print("-" * 100)

    failures = 0
    for arm in arms:
        name = arm["name"]
        if selected and name not in selected:
            continue
        if arm["missing"]:
            print(f"{name:<30} {'SKIP':<10} {'':>7} {'':>7} {'':>7} {'':>9}  needs {','.join(arm['missing'])}")
            failures += 1
            continue

        # The cap goes out under the spelling the PROXY would send, and this
        # script has to reproduce that by hand. `bakeoff.litellm_patches`
        # renames max_tokens -> max_completion_tokens on every openai/
        # deployment, but it is applied by importing it -- and the harness may
        # never import it (that separation is what keeps the harness process's
        # litellm unpatched), so this in-process Router is unpatched by design.
        #
        # Without this branch the probe would keep sending max_tokens to
        # Bedrock's /openai/v1 route and keep getting the 400 that took Gemma
        # to 0/3 on 2026-08-12 -- after the proxy was fixed. The tool used to
        # bracket that defect would have gone on reproducing it, and read as
        # the fix not taking. See litellm_patches for the measurement; if that
        # rename changes, this line is the other place it lives.
        cap = (
            "max_completion_tokens"
            if arm["model"].startswith("openai/")
            else "max_tokens"
        )
        kwargs: dict[str, Any] = {"model": name, "messages": PROBE, cap: 32}
        if with_tools:
            kwargs["tools"] = [PROBE_TOOL]
            kwargs["messages"] = [
                {"role": "user", "content": "Call get_status for the service 'api'."}
            ]
            if arm["model"].startswith("openai/"):
                # Gemma's route refuses tools outright unless this is
                # explicitly "none" -- and absent is not "none", because the
                # route supplies its own default:
                #   Function tools with reasoning_effort are not supported for
                #   google.gemma-4-31b in /v1/chat/completions.
                #
                # THREE settings, and all three are required. Measured
                # 2026-08-12 by removing each in turn:
                #   reasoning_effort        the value the route demands
                #   allowed_openai_params   litellm, NOT Bedrock, rejects the
                #                           parameter on a non-o-series openai
                #                           model (_check_valid_arg)
                #   additional_drop_params  the DEPLOYMENT lists
                #                           ["reasoning_effort"], and that drop
                #                           strips the value set right here --
                #                           with the first two alone this arm
                #                           still 400s
                #
                # The proxy needs none of this because bakeoff.litellm_patches
                # injects downstream of the drop. This script cannot import
                # that module (it would patch the harness's litellm), so the
                # pin is reproduced by hand and the two must move together.
                kwargs["reasoning_effort"] = "none"
                kwargs["allowed_openai_params"] = ["reasoning_effort"]
                kwargs["additional_drop_params"] = []

        started = time.monotonic()
        try:
            response = router.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001 -- per-arm isolation is the point
            elapsed = int((time.monotonic() - started) * 1000)
            detail = f"{type(exc).__name__}: {str(exc)[:120]}"
            print(f"{name:<30} {'FAIL':<10} {elapsed:>7} {'':>7} {'':>7} {'':>9}  {detail}")
            failures += 1
            continue

        elapsed = int((time.monotonic() - started) * 1000)
        usage = usage_of(response)
        try:
            usd = f"{cost_usd(name, usage):.6f}"
        except Exception as exc:  # noqa: BLE001
            usd = "ERR"
            print(f"{'':<30} {'':10} price book: {type(exc).__name__}: {exc}")
            failures += 1

        message = response.choices[0].message
        text = (message.content or "").strip().replace("\n", " ")[:40]
        calls = getattr(message, "tool_calls", None)
        detail = f"tool_calls={len(calls)}" if calls else f"text={text!r}"
        print(f"{name:<30} {'OK':<10} {elapsed:>7} {usage.input:>7} {usage.output:>7} {usd:>9}  {detail}")

        if with_tools and not calls:
            print(f"{'':<30} {'':10} no tool_call returned -- adapter or model, see wire log")

    wire_logger.close()
    entries = wire_logger.entries()
    print(f"\nwire log    {len(entries)} calls captured")
    flagged = [e for e in entries if e["secret_flags"]]
    if flagged:
        print(f"            {len(flagged)} entries carry secret flags: "
              f"{sorted({f for e in flagged for f in e['secret_flags']})}")
    if not entries:
        print("            FAIL -- callback never fired; wire logging is mandatory (spec 6.2)")
        failures += 1

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make real Bedrock calls")
    parser.add_argument("--models", help="comma-separated model_name subset")
    parser.add_argument("--tools", action="store_true", help="probe tool translation")
    parser.add_argument(
        "--derive-mantle-token",
        action="store_true",
        help="mint a short-term bearer token from the current AWS session "
        f"(in memory only) instead of reading {MANTLE_ENV}",
    )
    args = parser.parse_args()

    # Import litellm before reading .env ourselves: it runs load_dotenv() on
    # import, and we want our own parse (and the placeholder scrub below) to be
    # the last word on what ends up in os.environ.
    import litellm  # noqa: F401

    loaded = load_env_file(ENV_FILE)
    scrubbed = scrub_placeholders()
    if scrubbed:
        print(f"env         ignored unfilled placeholders: {', '.join(sorted(scrubbed))}")
    resolve_aws_paths()
    if normalize_mantle_token():
        print(f"env         adopted {LITELLM_BEARER_ENV} as {MANTLE_ENV}")
    if loaded:
        print(f"env         {ENV_FILE.name}: {', '.join(loaded)}")
    elif ENV_FILE.exists():
        print(f"env         {ENV_FILE.name} present but no values set")
    else:
        print(f"env         no {ENV_FILE.name} (using ambient environment)")

    if args.derive_mantle_token:
        region = os.environ.get("AWS_REGION_NAME") or "us-east-1"
        token = derive_mantle_token(region)
        if token:
            # Set before preflight so the mantle arms report their real
            # credential state, and before Router construction so the config's
            # os.environ/BAKEOFF_MANTLE_TOKEN references resolve.
            os.environ[MANTLE_ENV] = token
            print(f"mantle      derived short-term bearer token (len {len(token)})")

    router, arms, problems = preflight()

    if not args.live:
        print(f"\npreflight   {'PASS' if problems == 0 else f'{problems} problem(s)'}")
        print("            re-run with --live to make real calls")
        return 1 if problems else 0

    if router is None:
        return 1

    selected = set(args.models.split(",")) if args.models else None
    if selected:
        known = {arm["name"] for arm in arms}
        unknown = selected - known
        if unknown:
            print(f"\nunknown model_name: {sorted(unknown)}")
            return 1

    try:
        failures = live(router, arms, selected, args.tools)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    total = problems + failures
    print(f"\nsmoke       {'PASS' if total == 0 else f'{total} failure(s)'}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
