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


def _streaming_chunk(tool_call_id: str, tool_name: str = "Bash"):
    """One openai streaming chunk opening a tool call.

    Built from litellm's own types rather than a stub object: the patched code
    reads `chunk.choices[0].delta.tool_calls[0].function`, and a duck-typed
    stand-in would pass a test that the real chunk shape fails.
    """
    from litellm.types.utils import (
        ChatCompletionDeltaToolCall,
        Delta,
        Function,
        ModelResponseStream,
        StreamingChoices,
    )

    return ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0,
                finish_reason=None,
                delta=Delta(
                    tool_calls=[
                        ChatCompletionDeltaToolCall(
                            id=tool_call_id,
                            type="function",
                            index=0,
                            function=Function(name=tool_name, arguments="{}"),
                        )
                    ]
                ),
            )
        ]
    )


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


def test_nothing_between_v1_messages_and_converse_re_sanitizes_a_tool_id():
    """A CHARACTERIZATION test: it asserts that no other layer will save you.

    The patch is process-wide, so it also covers the bedrock/ deployments --
    and there the id does not stop at Claude Code. Models without a native
    Anthropic route on Bedrock fall through to the openai -> Converse bridge,
    which copies the tool id verbatim into ``BedrockToolUseBlock(toolUseId=)``
    and ``BedrockToolResultBlock(toolUseId=)``. Bedrock constrains that to
    ``[a-zA-Z0-9_-]{1,64}``, and enforces it SERVER-side: litellm sanitizes
    tool NAMES on this path (make_valid_bedrock_tool_name) and never ids, so
    a rejection would arrive as a 400 from AWS with nothing failing locally.

    Two claims, kept apart on purpose. That the id reaches ``toolUseId``
    unsanitized is CONFIRMED -- this test, litellm 1.95.0, offline. That
    Bedrock rejects it is NOT: it is inferred from AWS's published pattern,
    and settling it costs money on an arm outside EVAL_ARMS.

    Which is why the constraint is enforced in the config instead, by
    test_config.test_no_bedrock_arm_reaches_converse_with_ids_it_would_reject.
    Direction matters here: if a future litellm sanitizes ids on the Converse
    path, THIS test goes red and that config restriction can be relaxed. It
    therefore carries no mutation anchor -- an anchor would reward reverting
    the passthrough.
    """
    litellm_patches.apply()
    from litellm.litellm_core_utils.prompt_templates.factory import (
        _bedrock_converse_messages_pt,
    )
    from litellm.llms.anthropic.common_utils import (
        sanitize_tool_use_ids_in_anthropic_messages,
    )
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
        LiteLLMAnthropicMessagesAdapter,
    )

    messages = sanitize_tool_use_ids_in_anthropic_messages(
        [
            {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "functions.Read:0",
                        "name": "Read",
                        "input": {"path": "calc.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "functions.Read:0",
                        "content": "x",
                    }
                ],
            },
        ]
    )
    openai_request, _ = LiteLLMAnthropicMessagesAdapter().translate_anthropic_to_openai(
        {"model": "moonshotai.kimi-k2.5", "messages": messages, "max_tokens": 16}
    )
    blocks = _bedrock_converse_messages_pt(
        messages=openai_request["messages"],
        model="moonshotai.kimi-k2.5",
        llm_provider="bedrock",
    )

    seen = {}
    for block in blocks:
        for content in block["content"]:
            for key in ("toolUse", "toolResult"):
                if key in content:
                    seen[key] = content[key]["toolUseId"]

    assert seen == {
        "toolUse": "functions.Read:0",
        "toolResult": "functions.Read:0",
    }, seen


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
        litellm_patches.TOOL_USE_ID_COLLISION_UNIQUIFY,
        litellm_patches.MAX_COMPLETION_TOKENS_RENAME,
        litellm_patches.REASONING_EFFORT_PINNED_NONE,
        litellm_patches.OPENAI_RESOLVED_PARAMS_CAPTURE,
    ]


# --- colliding tool-call ids (the 2026-08-11 Gemma defect) -------------------
#
# Gemma returned the id `call_0` on all 30 responses of every run: its ids are
# indexed WITHIN a response, and it emits one call per response. Claude Code
# executed all 30 locally but could not pair duplicates on the way back, so the
# conversation Gemma saw carried 1 tool_use, 1 tool_result and 28 "(no
# content)" turns -- it never observed anything after its first command and
# repeated the same plan to the turn cap.


def test_an_id_seen_for_the_first_time_is_returned_unchanged():
    """The no-op that keeps this from being a per-arm difference. Sonnet,
    Nemotron and Kimi never repeat an id, so the uniquifier must never fire on
    them -- including on Kimi's `functions.Read:0`, whose exact shape the
    passthrough patch above exists to preserve."""
    for raw in ("toolu_bdrk_01ERnhUz5E3Tug8XwDf9RDcG",
                "call_4196d6e081be491194548c6d",
                "functions.Read:0"):
        seen: set[str] = set()
        assert litellm_patches._uniquify_tool_use_id(raw, seen) == raw


def test_a_colliding_id_gets_the_smallest_free_suffix():
    seen = {"call_0"}
    assert litellm_patches._uniquify_tool_use_id("call_0", seen) == "call_0_1"
    assert litellm_patches._uniquify_tool_use_id("call_0", seen) == "call_0_2"
    assert litellm_patches._uniquify_tool_use_id("call_0", seen) == "call_0_3"


def test_the_uniquified_id_survives_the_request_path_sanitizer():
    """Claude Code echoes the rewritten id back in `tool_result.tool_use_id`,
    and `sanitize_tool_use_ids_in_anthropic_messages` runs on every inbound
    request. A suffix outside [a-zA-Z0-9_-] would be rewritten there and the
    pairing this fix restores would break again one layer down."""
    litellm_patches.apply()
    from litellm.llms.anthropic import common_utils

    out = litellm_patches._uniquify_tool_use_id("call_0", {"call_0"})
    assert common_utils.normalize_anthropic_tool_use_id(out) == out


def test_the_streaming_wrapper_rewrites_only_a_collision():
    """The patch has to bite on the streaming path: every real Claude Code call
    streams. It hangs off the stream wrapper rather than the per-chunk
    translation because the per-chunk call runs in the server's own response
    task, where the request ContextVar reads None -- measured, see the module
    docstring."""
    litellm_patches.apply()
    from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (  # noqa: E501
        AnthropicStreamWrapper,
    )

    token = litellm_patches._SEEN_TOOL_USE_IDS.set({"call_0"})
    try:
        wrapper = AnthropicStreamWrapper(completion_stream=iter([]), model="gemma")
    finally:
        litellm_patches._SEEN_TOOL_USE_IDS.reset(token)

    # Captured at construction, in the request's own context.
    assert getattr(wrapper, litellm_patches._SEEN_IDS_ATTR) == {"call_0"}

    collided = wrapper._should_start_new_content_block(
        _streaming_chunk("call_0")
    ), wrapper.current_content_block_start["id"]
    untouched = wrapper._should_start_new_content_block(
        _streaming_chunk("functions.Read:0")
    ), wrapper.current_content_block_start["id"]

    assert collided[1] == "call_0_1"
    assert untouched[1] == "functions.Read:0"


def test_a_stream_that_never_saw_the_request_leaves_ids_alone():
    """Fail closed, not open. If the id set does not reach the stream the patch
    must do nothing rather than invent a suffix -- a guess would rewrite the
    three arms whose ids are already unique."""
    litellm_patches.apply()
    from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (  # noqa: E501
        AnthropicStreamWrapper,
    )

    wrapper = AnthropicStreamWrapper(completion_stream=iter([]), model="gemma")
    assert getattr(wrapper, litellm_patches._SEEN_IDS_ATTR) is None

    wrapper._should_start_new_content_block(_streaming_chunk("call_0"))
    wrapper._should_start_new_content_block(_streaming_chunk("call_0"))
    assert wrapper.current_content_block_start["id"] == "call_0"


def test_the_hook_collects_the_ids_already_in_the_conversation():
    hook = litellm_patches.BakeoffAdapterPatches.async_pre_request_hook
    messages = [
        {"role": "user", "content": "fix it"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": ""},
                {"type": "tool_use", "id": "call_0", "name": "Bash", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_0", "content": "ok"}
            ],
        },
    ]

    async def seen_after(msgs):
        # Read inside the same coroutine. `asyncio.run` starts a fresh context,
        # so a ContextVar set within it is not visible to the caller -- which
        # is a property of the test harness, not of the request path, where the
        # hook is awaited inside the request's own context.
        await hook(litellm_patches.instance, "gemma-4-31b", msgs, {})
        return litellm_patches._SEEN_TOOL_USE_IDS.get()

    assert asyncio.run(seen_after(messages)) == {"call_0"}
    # A first turn carries none, and must not inherit a previous request's.
    assert asyncio.run(seen_after([])) == set()


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
    patches, version, provider_route = read_manifest(tmp_path)
    assert patches == [litellm_patches.TOOL_USE_ID_PASSTHROUGH]
    assert version == "1.95.0"
    assert provider_route == ""


def test_an_absent_manifest_reports_nothing_rather_than_guessing(tmp_path):
    """A proxy that wrote no manifest made no claim. The record must not turn
    that silence into a positive statement about the adapter."""
    from bakeoff.proxy_callback import read_manifest

    assert read_manifest(tmp_path) == ([], "", "")


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


# --- max_completion_tokens rename (the 2026-08-12 Gemma defect) ---------------
#
# Gemma 4 31B went 0/3 live with every call 400ing before the model saw
# anything: `Unsupported parameter: 'max_tokens' is not supported with this
# model`. Bracketed to a ~14 hour window in which nothing on this side moved.
# The discriminator is the route, and litellm says so itself --
# `mantle_base_segment` puts any model carrying `use_openai_responses_path` on
# `/openai/v1`, which tracks OpenAI's contract, where `max_tokens` is
# deprecated in favour of `max_completion_tokens`.

# Every candidate arm's provider-side model id, as litellm_config.yaml spells
# them. The rename is uniform across all three on purpose: section 5.4 requires
# the arms be identical in everything but the model, and a Gemma-only rename
# would have made transport a per-arm difference and scored it as capability.
CANDIDATE_MODEL_IDS = [
    "google.gemma-4-31b",
    "nvidia.nemotron-super-3-120b",
    "moonshotai.kimi-k2.5",
]


@pytest.mark.parametrize("model", CANDIDATE_MODEL_IDS)
def test_the_cap_reaches_an_openai_arm_as_max_completion_tokens(model):
    """The rename, read off litellm's own param resolution rather than asserted
    against the patch's internals."""
    from litellm.utils import get_optional_params

    resolved = get_optional_params(
        model=model, custom_llm_provider="openai", max_tokens=16384
    )
    assert resolved["max_completion_tokens"] == 16384
    # Not "also present": Bedrock rejects the request outright on seeing it.
    assert "max_tokens" not in resolved


def test_the_cap_is_renamed_not_dropped():
    """`additional_drop_params: ["max_tokens"]` is the tempting one-liner and is
    wrong -- measured, it leaves the body with no cap at all while every other
    arm runs at 16384, which is a config choice scored as a capability
    difference (section 5.4)."""
    from litellm.utils import get_optional_params

    resolved = get_optional_params(
        model="google.gemma-4-31b", custom_llm_provider="openai", max_tokens=16384
    )
    assert 16384 in resolved.values()


@pytest.mark.parametrize(
    "model,provider",
    [
        ("anthropic.claude-sonnet-5", "anthropic"),
        ("us.anthropic.claude-sonnet-5", "bedrock"),
    ],
)
def test_the_max_completion_rename_leaves_both_sonnet_arms_alone(model, provider):
    """Sonnet is exempt STRUCTURALLY, not because its parameters happen to be
    clean. `anthropic/` resolves to AnthropicConfig and `bedrock/` to
    AmazonConverseConfig, neither reachable from the openai provider branch --
    and on a native Anthropic body `max_tokens` is correct and required."""
    from litellm.utils import get_optional_params

    resolved = get_optional_params(
        model=model, custom_llm_provider=provider, max_tokens=16384
    )
    assert "max_completion_tokens" not in resolved


def test_the_max_completion_rename_does_not_double_apply_to_o_series():
    """o-series and gpt-5 already rename, in configs OpenAIConfig branches to
    BEFORE delegating. The patch post-processes the returned dict, so it is a
    no-op there rather than a second rename -- and this is the test that would
    catch a wrapper written to rename on the way IN."""
    from litellm.utils import get_optional_params

    for model in ("o3", "gpt-5"):
        resolved = get_optional_params(
            model=model, custom_llm_provider="openai", max_tokens=16384
        )
        assert resolved["max_completion_tokens"] == 16384
        assert "max_tokens" not in resolved


def test_the_max_completion_rename_forwards_by_keyword():
    """litellm calls `map_openai_params` with keyword arguments, so a wrapper
    whose signature renames them raises TypeError inside the provider call --
    which litellm re-wraps as APIConnectionError, a signature bug wearing a
    provider failure's clothes. Caught exactly once, while writing this patch."""
    import litellm

    resolved = litellm.OpenAIConfig().map_openai_params(
        non_default_params={"max_tokens": 16},
        optional_params={},
        model="google.gemma-4-31b",
        drop_params=False,
    )
    assert resolved == {"max_completion_tokens": 16, "reasoning_effort": "none"}


def test_max_completion_rename_fails_loudly_if_the_entry_point_moves(monkeypatch):
    """The wrapper hangs off `litellm.OpenAIConfig.map_openai_params`. A litellm
    bump that moves it must break the build rather than leave the proxy quietly
    sending the spelling Bedrock rejects."""
    import litellm

    monkeypatch.delattr(litellm.OpenAIConfig, "map_openai_params")
    with pytest.raises(RuntimeError, match="no longer defines"):
        litellm_patches.apply()


# --- reasoning_effort pinned to "none" (the third 2026-08-12 Gemma wall) ------
#
# max_tokens was the first of three rejections on the same route; its 400 fired
# before the others could be seen. The third is the one that matters for an
# agentic eval: gemma refuses `tools` unless reasoning_effort is explicitly
# "none", and the route applies a non-none default when the parameter is
# absent -- so dropping it, which is what additional_drop_params did, is what
# kept the escape out of reach.
#
#   Function tools with reasoning_effort are not supported for
#   google.gemma-4-31b in /v1/chat/completions. To use function tools, use
#   /v1/responses or set reasoning_effort to 'none'.


@pytest.mark.parametrize("model", CANDIDATE_MODEL_IDS)
def test_every_candidate_sends_reasoning_effort_none(model):
    """Uniform across the three candidates, which is the section 5.4 point.

    Sonnet returned zero reasoning tokens across 856 stored calls and all three
    candidates had reasoning_effort dropped, so the whole Phase 0c corpus is
    thinking-off. Pinning "none" makes explicit what every arm was already
    doing implicitly, rather than turning thinking on for one arm and scoring
    the difference as capability.
    """
    from litellm.utils import get_optional_params

    resolved = get_optional_params(
        model=model, custom_llm_provider="openai", max_tokens=16384
    )
    assert resolved["reasoning_effort"] == "none"


def test_the_pin_beats_a_value_derived_from_claude_codes_thinking_block():
    """The whole reason this is a patch and not a config line.

    Claude Code sends `thinking: {"type": "adaptive"}` on every arm, and
    litellm's translate_anthropic_thinking_to_reasoning_effort derives an
    effort from it. Measured 2026-08-12: a deployment carrying
    `allowed_openai_params: ["reasoning_effort"]` AND `reasoning_effort:
    "none"` still put "medium" on the wire -- the derived value wins over the
    deployment default. Only something downstream of the param mapping can pin
    it, which is why the wrapper assigns unconditionally rather than
    setdefault.
    """
    import litellm

    resolved = litellm.OpenAIConfig().map_openai_params(
        non_default_params={"reasoning_effort": "medium"},
        optional_params={},
        model="google.gemma-4-31b",
        drop_params=False,
    )
    assert resolved["reasoning_effort"] == "none"


@pytest.mark.parametrize(
    "model,provider",
    [
        ("anthropic.claude-sonnet-5", "anthropic"),
        ("us.anthropic.claude-sonnet-5", "bedrock"),
    ],
)
def test_the_pin_leaves_both_sonnet_arms_alone(model, provider):
    """Sonnet reaches neither wrapper: `anthropic/` resolves to AnthropicConfig
    and `bedrock/` to AmazonConverseConfig, and only the openai provider branch
    reaches OpenAIConfig. Injecting an openai-shaped reasoning_effort into a
    native Anthropic body would be a per-arm transport difference of exactly
    the kind section 6.4 exists to keep out."""
    from litellm.utils import get_optional_params

    resolved = get_optional_params(
        model=model, custom_llm_provider=provider, max_tokens=16384
    )
    assert "reasoning_effort" not in resolved


def test_the_pin_survives_the_drop_that_used_to_be_the_mechanism():
    """`additional_drop_params: ["reasoning_effort"]` is still in every
    candidate deployment, deliberately: it makes a patch that fails to apply
    fail LOUDLY rather than forward the derived "medium". It must not, however,
    strip the pinned value -- the drop runs over non_default_params, the pin is
    injected into the mapped result, and this asserts that ordering rather than
    trusting it."""
    from litellm.utils import get_optional_params

    resolved = get_optional_params(
        model="google.gemma-4-31b",
        custom_llm_provider="openai",
        max_tokens=16384,
        additional_drop_params=["reasoning_effort"],
    )
    assert resolved["reasoning_effort"] == "none"


# --- the resolved-params hand-off --------------------------------------------


def test_the_param_mapping_hands_what_it_produced_to_the_wire_capture():
    """The only place that knows what the provider is being sent.

    Measured 2026-08-12 against a real streaming provider call: the success
    callback fires on the OUTER anthropic_messages call and the nested
    acompletion fires nothing of its own, so `optional_params` and
    `standard_logging_object.model_parameters` both carry the state BEFORE this
    wrapper -- `max_tokens: 16384`, no `max_completion_tokens`, no
    `reasoning_effort`. Without the hand-off the wire log reports Claude Code's
    request as though it were the wire.

    AFTER the rewrites, not before: the point is what goes out.
    """
    import litellm

    from bakeoff.proxy_callback import open_resolved_capture, resolved_params

    open_resolved_capture("call-under-test")
    litellm.OpenAIConfig().map_openai_params(
        non_default_params={"max_tokens": 16384, "reasoning_effort": "medium"},
        optional_params={},
        model="google.gemma-4-31b",
        drop_params=False,
    )

    captured = resolved_params("call-under-test")
    assert captured is not None, "the mapping recorded nothing"
    assert captured["max_completion_tokens"] == 16384
    assert captured["reasoning_effort"] == "none"
    assert "max_tokens" not in captured
    assert captured["model"] == "google.gemma-4-31b"


def test_a_mapping_with_no_capture_open_is_a_no_op():
    """A route that never reached the pre-request hook records nothing rather
    than half of something. It must not raise either -- this runs inside the
    provider call, and an exception here surfaces as an APIConnectionError two
    layers away."""
    import litellm

    from bakeoff.proxy_callback import resolved_params

    litellm.OpenAIConfig().map_openai_params(
        non_default_params={"max_tokens": 8},
        optional_params={},
        model="google.gemma-4-31b",
        drop_params=False,
    )
    assert resolved_params("some-other-call") is None


# --- the provider seam (spec 2026-09-08 section 3) ---------------------------


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
