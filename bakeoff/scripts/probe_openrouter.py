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
    openai_usage = (last.get("response") or {}).get("usage") or {}
    results.append({**check_usage_exclusive(anthropic=r1["body"].get("usage", {}) if r1["status"] == 200 else {}, openai=openai_usage), "arm": arm})
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
        from bakeoff.images import build_proxy_image
        from bakeoff.proxy import Proxy, proxy_environment

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
