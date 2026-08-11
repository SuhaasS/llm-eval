"""The proxy-side adapter interventions, and the containment that keeps them there.

Neither defect was theorised; both were measured on live runs, and both were
being scored as model weakness until they were found.

Kimi K2.5 emits ``functions.Read:0``, LiteLLM rewrote it to
``functions_Read_0`` on the response path, Claude Code echoed the mangled form
back, and Kimi stopped driving the tool loop -- 0/22 tool calls mangled versus
42/42 with the colon intact.

Gemma 4 31B failed 9/9 with ``JSON-RPC error -32602: Job registration failed``
because two of Claude Code's tool schemas carry ``propertyNames``, which its
Bedrock serving engine rejects.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Hard import, not importorskip: a skipped adapter check reads as green while
# verifying nothing, which is the failure mode this suite keeps running into.
from bakeoff import litellm_patches  # noqa: E402


def test_a_kimi_id_survives_the_patch_unchanged():
    litellm_patches.apply()
    from litellm.llms.anthropic import common_utils

    assert common_utils.normalize_anthropic_tool_use_id("functions.Read:0") == (
        "functions.Read:0"
    )


def test_the_module_that_bound_the_symbol_by_from_import_is_patched_too():
    """The half-fix that looks complete.

    ``adapters.transformation`` does ``from ...common_utils import
    normalize_anthropic_tool_use_id`` at import time, so it holds its own
    reference. Patching only ``common_utils`` leaves the response path -- where
    the mangling actually happens -- untouched, and the bug survives a change
    that appears to have fixed it.
    """
    litellm_patches.apply()
    from litellm.llms.anthropic.experimental_pass_through.adapters import (
        transformation,
    )

    assert transformation.normalize_anthropic_tool_use_id("functions.Read:0") == (
        "functions.Read:0"
    )


def test_the_request_path_is_covered_transitively():
    """``sanitize_tool_use_ids_in_anthropic_messages`` runs on every inbound
    /v1/messages request. Fixing only the response path would move the id
    mismatch from Claude Code to Kimi rather than remove it: Claude Code would
    echo ``functions.Read:0`` back and this would rewrite it again."""
    litellm_patches.apply()
    from litellm.llms.anthropic.common_utils import (
        sanitize_tool_use_ids_in_anthropic_messages,
    )

    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "functions.Read:0", "name": "Read", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "functions.Read:0", "content": "x"}
            ],
        },
    ]
    out = sanitize_tool_use_ids_in_anthropic_messages(messages)
    assert out[0]["content"][0]["id"] == "functions.Read:0"
    assert out[1]["content"][0]["tool_use_id"] == "functions.Read:0"


def test_the_gemini_thought_signature_split_is_preserved():
    """The patched function has a second, unrelated job. No Gemini arm exists
    today, which is exactly why dropping this would go unnoticed until one
    did."""
    litellm_patches.apply()
    from litellm.llms.anthropic import common_utils

    assert common_utils.normalize_anthropic_tool_use_id(
        "functions.Read:0__thought__sig"
    ) == "functions.Read:0"


def test_apply_is_idempotent():
    first = litellm_patches.apply()
    second = litellm_patches.apply()
    assert first == second == [
        litellm_patches.TOOL_USE_ID_PASSTHROUGH,
        litellm_patches.TOOL_SCHEMA_PROPERTY_NAMES_STRIP,
    ]


# --- propertyNames strip (the 2026-08-10 Gemma defect) -----------------------
#
# Gemma 4 31B failed 9/9 live runs with `JSON-RPC error -32602: Job
# registration failed`. A 15-rung request ladder against live mantle isolated
# it to one JSON-Schema keyword: `propertyNames`, carried by exactly two of the
# 24 tools Claude Code 2.1.220 declares. Stripping it took the arm from FAIL to
# PASS with Nemotron and Kimi unchanged.

# The real shape, from TaskCreate's `metadata` property.
TASK_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "metadata": {
            "type": "object",
            "propertyNames": {"type": "string"},
            "additionalProperties": {"type": "string"},
        },
        "prompt": {"type": "string"},
    },
    "required": ["prompt"],
}


def test_property_names_is_stripped_at_every_depth():
    """Object-level, nested under a property, and inside a list.

    Gemma rejected both the object-level and the nested form, so a strip that
    only reached the top level would leave TaskCreate and TaskUpdate failing --
    which is every real occurrence.
    """
    payload = {
        "propertyNames": {"type": "string"},
        "properties": {"metadata": {"propertyNames": {"type": "string"}}},
        "anyOf": [{"propertyNames": {"type": "string"}}],
    }
    stripped = litellm_patches._strip_property_names(payload)

    assert stripped == {"properties": {"metadata": {}}, "anyOf": [{}]}


def test_property_names_strip_leaves_the_keywords_gemma_accepts():
    """Only the measured keyword goes.

    exclusiveMinimum, maxItems, anyOf, const, format and pattern were each
    probed individually against live Gemma on 2026-08-10 and each PASSED.
    Stripping more than was measured would silently change every arm's tool
    contract to work around a fault that does not exist.
    """
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "number", "exclusiveMinimum": 0},
            "xs": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "status": {"anyOf": [{"type": "string", "const": "deleted"}]},
            "url": {"type": "string", "format": "uri"},
            "id": {"type": "string", "pattern": "^wf_[a-z0-9-]{6,}$"},
        },
        "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
    }

    assert litellm_patches._strip_property_names(schema) == schema


def test_the_strip_does_not_mutate_the_caller_s_schema():
    """The hook hands its result to litellm, but the caller's list is Claude
    Code's own request object -- mutating it in place would make the wire log's
    tool_schema_sha depend on whether capture ran before or after the hook."""
    original = {"properties": {"metadata": {"propertyNames": {"type": "string"}}}}
    snapshot = json.loads(json.dumps(original))

    litellm_patches._strip_property_names(original)

    assert original == snapshot


def test_the_hook_strips_property_names_from_a_real_tool():
    hook = litellm_patches.BakeoffAdapterPatches.async_pre_request_hook
    kwargs = {
        "tools": [
            {
                "name": "TaskCreate",
                "description": "Create a task.",
                "input_schema": TASK_CREATE_SCHEMA,
            }
        ],
        "stream": True,
    }

    out = asyncio.run(hook(litellm_patches.instance, "gemma-4-31b", [], kwargs))

    schema = out["tools"][0]["input_schema"]
    assert "propertyNames" not in schema["properties"]["metadata"]
    assert schema["properties"]["metadata"]["additionalProperties"] == {
        "type": "string"
    }
    # Everything else the hook was handed comes back untouched.
    assert out["stream"] is True


def test_the_hook_is_a_noop_without_tools():
    """`anthropic_messages` calls the hook on every request, including the ones
    Claude Code makes with no tool block. Returning None there would drop the
    whole kwargs dict, since handler.py keeps the hook's return value."""
    hook = litellm_patches.BakeoffAdapterPatches.async_pre_request_hook

    for kwargs in ({"stream": True}, {"tools": None, "stream": True}):
        out = asyncio.run(hook(litellm_patches.instance, "gemma-4-31b", [], kwargs))
        assert out == kwargs


def test_the_manifest_records_what_the_proxy_did_to_itself(tmp_path):
    """Provenance must be observed, not configured. `Versions.litellm` is the
    HARNESS process's version and says nothing about the proxy container,
    which pins its own -- so the proxy reports its own version and patch set,
    and the harness reads that back.

    The manifest functions live in bakeoff.proxy_callback, not here: the
    harness has to read them, and importing THIS module applies the patches.
    """
    from bakeoff.proxy_callback import read_manifest, write_manifest

    write_manifest([litellm_patches.TOOL_USE_ID_PASSTHROUGH], "1.95.0", tmp_path)
    patches, version = read_manifest(tmp_path)
    assert patches == [litellm_patches.TOOL_USE_ID_PASSTHROUGH]
    assert version == "1.95.0"


def test_an_absent_manifest_reports_nothing_rather_than_guessing(tmp_path):
    """A proxy that wrote no manifest made no claim. The record must not turn
    that silence into a positive statement about the adapter."""
    from bakeoff.proxy_callback import read_manifest

    assert read_manifest(tmp_path) == ([], "")


def test_importing_the_harness_does_not_patch_litellm():
    """The containment this module's separation exists to provide.

    ``bakeoff.runner`` imports ``bakeoff.proxy_callback`` at module level, so
    hanging the patch off that module would patch litellm in the harness
    process and in every pytest run -- silently changing the behaviour of code
    under test. Run in a subprocess because this test session has already
    applied the patch in-process.
    """
    probe = (
        "import bakeoff.runner, sys;"
        "assert 'bakeoff.litellm_patches' not in sys.modules, 'patch module leaked';"
        "from litellm.llms.anthropic import common_utils as c;"
        "print(c.normalize_anthropic_tool_use_id('functions.Read:0'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    # Unpatched, the harness's own litellm still sanitizes -- which is correct.
    assert result.stdout.strip() == "functions_Read_0"


def test_apply_fails_loudly_if_a_target_module_loses_the_symbol(monkeypatch):
    """A litellm bump that renames or moves the function must break the build,
    not quietly stop applying. A patch that silently becomes a no-op is worse
    than no patch: the record would still say it was active."""
    from litellm.llms.anthropic import common_utils

    monkeypatch.delattr(common_utils, "normalize_anthropic_tool_use_id")
    with pytest.raises(RuntimeError, match="no longer defines"):
        litellm_patches.apply()
