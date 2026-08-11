#!/usr/bin/env python3
"""Mutation check: revert each fix, confirm a test goes red.

A test that passes proves nothing on its own -- it may be asserting
something that was always true. The only evidence a test is load-bearing is
that it FAILS when the behaviour it guards is removed.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")

# (label, file, find, replace, test selector, marker)
MUTATIONS = [
    (
        # The 2026-08-07 defect: cost_usd raising took parse_trajectory down
        # with it, and assemble_record then discarded the whole trajectory.
        # A run that produced the correct diff was recorded as turns=0,
        # tokens=0, cost=0 -- indistinguishable from an arm that died on its
        # first call. Reverting to the bare call restores exactly that.
        "pricing: let a cache-token guard trip take the whole trajectory down",
        "src/bakeoff/trajectory.py",
        "        try:\n            turn_cost: float | None = cost_usd(model, usage)",
        "        if True:\n            turn_cost: float | None = cost_usd(model, usage)",
        "tests/test_trajectory.py -k unpriceable",
        "not integration",
    ),
    (
        # Patching only common_utils is the half-fix that looks complete:
        # adapters.transformation bound the symbol with `from ... import`, so
        # it keeps its own reference and the RESPONSE path -- where the
        # mangling happens -- stays broken. Dropping it from the target list
        # reproduces exactly that.
        "adapter: patch only common_utils, leaving the response path mangling",
        "src/bakeoff/litellm_patches.py",
        "    return [common_utils, transformation]",
        "    return [common_utils]",
        "tests/test_litellm_patches.py -k from_import",
        "not integration",
    ),
    (
        # The 2026-08-10 defect: two of Claude Code's 24 tool schemas carry
        # `propertyNames`, and Gemma's Bedrock engine rejects it with
        # -32602. 9/9 live runs lost to 74 bytes. Reverting the strip to
        # identity puts the keyword back on the wire for every arm.
        "adapter: stop stripping propertyNames, restoring Gemma's -32602",
        "src/bakeoff/litellm_patches.py",
        "    if isinstance(obj, dict):\n        return {\n            key: _strip_property_names(value)",
        "    if True:\n        return obj\n    if isinstance(obj, dict):\n        return {\n            key: _strip_property_names(value)",
        "tests/test_litellm_patches.py -k property_names",
        "not integration",
    ),
    (
        # The 2026-08-11 defect: Gemma returns the same tool-call id on every
        # response, Claude Code cannot pair the duplicates, and the model never
        # sees any tool result after its first. Returning the id unchanged puts
        # the collision back.
        "adapter: stop uniquifying colliding tool-call ids",
        "src/bakeoff/litellm_patches.py",
        "    if raw_id not in seen:\n        seen.add(raw_id)\n        return raw_id",
        "    if True:\n        return raw_id",
        "tests/test_litellm_patches.py -k collision",
        "not integration",
    ),
    (
        "defect 1: stop deriving api_error_status from the wire",
        "src/bakeoff/runner.py",
        "    if api_error_status is None:\n        api_error_status = final_api_error_status(entries)",
        "    pass",
        "tests/test_fault_injection.py -k api_error",
        "not integration",
    ),
    (
        "defect 1b: count ANY failed call, not just the final one",
        "src/bakeoff/runner.py",
        "    metadata = entries[-1].get(\"metadata\") or {}\n    if not metadata.get(\"failed\"):\n        return None",
        "    metadata = next((e.get('metadata') or {} for e in entries if (e.get('metadata') or {}).get('failed')), {})\n    if not metadata.get('failed'):\n        return None",
        "tests/test_fault_injection.py -k retry_recovered",
        "not integration",
    ),
    (
        "defect 2: leave the version block half empty",
        "src/bakeoff/runner.py",
        "            litellm=litellm_version(),\n            harness_commit=harness_commit(),",
        "            litellm=\"\",\n            harness_commit=\"\",",
        "tests/test_fault_injection.py -k version_block",
        "not integration",
    ),
    (
        "defect 2b: drop the -dirty suffix",
        "src/bakeoff/runner.py",
        'return f"{sha}-dirty" if dirty else sha',
        "return sha",
        "tests/test_fault_injection.py -k harness_commit",
        "not integration",
    ),
    (
        "defect 3: discard checkpoints on a mid-run failure",
        "src/bakeoff/runner.py",
        "        if recorder is not None:\n            checkpoints = recorder.captured",
        "        pass",
        "tests/test_fault_injection.py -k 'checkpoint_write_failure or killed_mid_stream'",
        "not integration",
    ),
    (
        "defect 4: stop reading the proxy's wire log",
        "src/bakeoff/runner.py",
        "            if proxy_wire_dir is not None:",
        "            if False:",
        "tests/test_fault_injection.py -k proxy_side_capture",
        "integration",
    ),
    (
        "defect 4b: proxy captures, but drops the status code",
        "src/bakeoff/proxy_callback.py",
        '"status_code": _status_code(kwargs) if failed else None,',
        '"status_code": None,',
        "tests/test_fault_injection.py -k proxy_throttle",
        "integration",
    ),
    (
        "defect 4c: read sampling from top-level kwargs (the obvious place)",
        "src/bakeoff/proxy_callback.py",
        "        return resolved.get(name, body.get(name))",
        "        return resolved.get(name, kwargs.get(name))",
        "tests/test_fault_injection.py -k proxy_side_capture",
        "integration",
    ),
    (
        "phantom index: append to the index before the record is durable",
        "src/bakeoff/eventlog.py",
        "        if path.exists():\n            tmp.unlink(missing_ok=True)\n            raise ImmutabilityError(f\"run {record.run_id} already written\")",
        "        pass",
        "tests/ -k 'twice or index or immut'",
        "not integration",
    ),
    (
        "new: drop the -dirty suffix",
        "src/bakeoff/runner.py",
        'return f"{sha}-dirty" if dirty else sha',
        "return sha",
        "tests/ -k dirty",
        "not integration",
    ),
    (
        "new: stop lowercasing header names",
        "src/bakeoff/proxy_callback.py",
        "return {str(k).lower(): v for k, v in headers.items()}",
        "return dict(headers)",
        "tests/test_proxy_callback.py -k case",
        "not integration",
    ),
    (
        "new: drop calls that arrive with no run_id",
        "src/bakeoff/proxy_callback.py",
        'run_id = _headers(kwargs).get(RUN_ID_HEADER) or UNATTRIBUTED',
        'run_id = _headers(kwargs).get(RUN_ID_HEADER)\n        if run_id is None:\n            return',
        "tests/test_proxy_callback.py -k unattributed",
        "not integration",
    ),
    (
        "new: remove the proxy_server_request header fallback",
        "src/bakeoff/proxy_callback.py",
        '        params.get("proxy_server_request"),\n        kwargs.get("proxy_server_request"),',
        '        kwargs.get("proxy_server_request"),',
        "tests/test_proxy_callback.py -k raw_proxy_request",
        "not integration",
    ),
    (
        "new: drop the concurrency lock",
        "src/bakeoff/proxy_callback.py",
        "        with self._lock:",
        "        if True:",
        "tests/test_proxy_callback.py -k 'concurrent or serialized'",
        "not integration",
    ),
    (
        "new: let a truncated line kill the whole wire log",
        "src/bakeoff/proxy_callback.py",
        "        except json.JSONDecodeError:\n            continue",
        "        except json.JSONDecodeError:\n            raise",
        "tests/test_proxy_callback.py -k partial_final_line",
        "not integration",
    ),
    (
        "new: gate reports PASSED with no docker daemon",
        "scripts/verify_logger.py",
        '        print("\\nGATE INCOMPLETE: could not run " + "; ".join(skipped))',
        '        print("\\nGATE PASSED: logging layer verified.")\n        return 0',
        "tests/test_verify_logger.py -k docker",
        "not integration",
    ),
    (
        "digest: stop excluding the per-run run_id header (config field)",
        "src/bakeoff/claude_runner.py",
        '            "temperature",\n            "custom_headers",',
        '            "temperature",',
        "tests/test_claude_runner.py -k run_id_header",
        "not integration",
    ),
    (
        "digest: stop excluding the run_id header env var",
        "src/bakeoff/claude_runner.py",
        '        # Carries the run_id, so it differs by construction on every run.\n        "ANTHROPIC_CUSTOM_HEADERS",\n',
        '',
        "tests/test_claude_runner.py -k run_id_header",
        "not integration",
    ),
    (
        "attribution: never send the run_id header to the agent",
        "src/bakeoff/claude_runner.py",
        '        {"ANTHROPIC_CUSTOM_HEADERS": config.custom_headers}\n        if config.custom_headers\n        else {}',
        '        {}',
        "tests/test_claude_runner.py -k run_id_header",
        "not integration",
    ),
    (
        "in-process callback: drop the failure status code",
        "src/bakeoff/wire.py",
        '"status_code": self._status_code(kwargs) if failed else None,',
        '"status_code": None,',
        "tests/test_wire.py -k status",
        "not integration",
    ),
]


def run(label, rel, find, replace, selector, marker):
    path = REPO / rel
    original = path.read_text()
    if find not in original:
        # A rotted anchor must FAIL, not skip. A mutation harness that
        # quietly stops mutating reports a clean sweep while testing
        # nothing -- the same false assurance it exists to detect.
        print(f"  STALE ANCHOR  {label}\n                no longer present in {rel}")
        return False
    path.write_text(original.replace(find, replace, 1))
    try:
        result = subprocess.run(
            [PY, "-m", "pytest", *selector.split(" -k ")[0].split(),
             "-k", selector.split(" -k ")[1].strip("'\""),
             "-q", "-m", marker, "--basetemp=" + str(Path.home() / ".cache/bakeoff-mut")],
            cwd=REPO, capture_output=True, text=True, timeout=900,
        )
        # Exit 5 is 'no tests collected' -- that is an empty selector, not
        # a caught mutation. Counting it as a catch is how a mutation
        # harness certifies coverage that does not exist.
        if result.returncode == 5:
            print(f'  NO TESTS  {label}')
            return False
        caught = result.returncode != 0
        tail = [l for l in result.stdout.splitlines() if "passed" in l or "failed" in l]
        print(f"  {'CAUGHT' if caught else 'MISSED'}  {label}")
        print(f"          {tail[-1] if tail else result.stdout.strip()[:80]}")
        return caught
    finally:
        path.write_text(original)


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = []
    for m in MUTATIONS:
        if only and only not in m[0]:
            continue
        results.append(run(*m))
    real = [r for r in results if r is not None]
    print(f"\n{sum(1 for r in real if r)}/{len(real)} mutations caught")
    sys.exit(0 if all(real) else 1)
