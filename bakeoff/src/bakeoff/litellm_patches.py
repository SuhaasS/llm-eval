"""Adapter interventions applied to LiteLLM inside the PROXY process only.

Spec section 6.4 exists to tell an adapter failure apart from a model failure.
This module removes five adapter failures that were being measured as model
weakness. All were total, all were deterministic, and all had one-line causes
-- which is the base rate to weigh before reading the next arm's failure as
capability.

  anthropic_tool_use_id_passthrough        Kimi K2.5, below
  anthropic_tool_schema_property_names_strip   Gemma 4 31B, in the hook on
                                           BakeoffAdapterPatches
  anthropic_tool_use_id_collision_uniquify Gemma 4 31B again, at the streaming
                                           wrapper: it returns the id `call_0`
                                           on every response, so Claude Code
                                           could pair nothing after the first
                                           call
  openai_max_completion_tokens_rename      Gemma 4 31B a third time, at the
                                           openai provider's param mapping:
                                           its route stopped accepting the
                                           deprecated `max_tokens` spelling
  openai_reasoning_effort_pinned_none      Gemma 4 31B a fourth time, same
                                           seam: its route refuses `tools`
                                           unless reasoning_effort is
                                           explicitly "none", and absent is
                                           not "none"

THE FIRST DEFECT. Kimi K2.5 emits tool-call ids of the form ``functions.Read:0``.
LiteLLM's ``normalize_anthropic_tool_use_id`` rewrites every character outside
``[a-zA-Z0-9_-]`` to an underscore, so the id becomes ``functions_Read_0`` on
the way back to Claude Code. Claude Code stores it and echoes it in the next
request's ``tool_result.tool_use_id``; the bridge sends the mangled form to
Kimi; Kimi stops driving the tool loop and answers in prose, exiting
``subtype: success`` with no edit and no diff.

Measured 2026-08-07 against bedrock-mantle: 0/22 tool calls with the mangled
id, 42/42 with a colon preserved, and with the sanitizer disabled Kimi
completes the smoke task 3/3 (``Read x2 -> Edit -> Bash -> Bash``). The
function's own docstring names this pairing -- it was added for the REVERSE
case, a Kimi-format id reaching a native Anthropic deployment, and is
destructive when applied on the Kimi path itself.

WHY THIS IS NOT A THUMB ON THE SCALE. The sanitizer is a no-op for every other
arm: Sonnet emits ``toolu_bdrk_01EV...`` and Nemotron ``call_3a203f89...``,
both already inside the allowed character class. The patch is unconditional and
process-wide precisely so it cannot become a per-arm difference -- a patch
applied only to the Kimi deployments would make the arms non-identical in
transport, which is the section 6.4 confound this harness works to avoid.

WHY IT IS SAFE HERE. Anthropic's ``^[a-zA-Z0-9_-]+$`` is enforced by the real
Anthropic API, which nothing in this eval calls -- every deployment targets
Bedrock. tests/test_config.py pins that assumption so it fails loudly if an
arm is ever pointed at api.anthropic.com.

WHERE THE CONSTRAINT LIVES INSTEAD. Anthropic's API is not the only enforcer
of that character class, so removing the sanitizer moves the constraint rather
than retiring it. Below /v1/messages the ``bedrock/`` route splits by model,
not by config: Claude models resolve to ``AmazonAnthropicClaudeMessagesConfig``
and go to the Invoke endpoint carrying a native Anthropic body, never
constructing a ``toolUseId`` -- so ``claude-sonnet-5-runtime`` is exempt
STRUCTURALLY, not because its ids happen to be clean. Every other model falls
through to the openai -> Converse bridge, where
``prompt_templates/factory.py`` copies the tool id verbatim into
``BedrockToolUseBlock(toolUseId=)`` / ``BedrockToolResultBlock(toolUseId=)``,
and Bedrock constrains that to ``[a-zA-Z0-9_-]{1,64}``.

Two claims, kept apart because only one of them is measured: that the id
reaches ``toolUseId`` unsanitized is CONFIRMED (litellm 1.95.0, offline, by
tests/test_litellm_patches.py); that Bedrock rejects it is NOT -- it is
inferred from AWS's published pattern, and settling it costs money on an arm
outside EVAL_ARMS.

So the constraint is now a property of WHICH DEPLOYMENTS ARE CONFIGURED, and
tests/test_config.py's
``test_no_bedrock_arm_reaches_converse_with_ids_it_would_reject`` is what
enforces it -- resolving each ``bedrock/`` arm's route through litellm's own
routing and requiring any Converse-bound model to be on an allowlist whose
entries carry measured id shapes. ``kimi-k2-5-runtime`` is commented out of
litellm_config.yaml for exactly this reason; scoping THIS patch by provider
instead was rejected, because re-enabling the sanitizer for bedrock reinstates
the very defect described above and turns a loud 400 into a silent zero-diff
run.

THE FOURTH DEFECT. Gemma 4 31B failed 0/3 live on 2026-08-12 with every call
400ing before the model saw anything: ``Unsupported parameter: 'max_tokens' is
not supported with this model``. Bracketed to a ~14 hour window in which
nothing on this side moved -- same litellm 1.95.0, same claude 2.1.220, same
patches -- and narrowed to the parameter by probing each arm outside Claude
Code: ``max_tokens`` fails on Gemma alone, ``max_completion_tokens`` and
neither both pass, and Nemotron and Kimi accept either.

The discriminator is the ROUTE, not the model, and litellm says so itself.
``bedrock_mantle.common_utils.mantle_base_segment`` selects ``/openai/v1`` for
any model whose price-map entry carries ``use_openai_responses_path`` -- the
gemma-4-* family, gpt-5.x and grok-4.3 -- and ``/v1`` for everything else.
``/openai/v1`` tracks OpenAI's own contract, where ``max_tokens`` is deprecated
in favour of ``max_completion_tokens`` and reasoning models reject it outright.

WHY IT IS APPLIED HERE AND NOT PER ARM. ``max_completion_tokens`` is accepted
on all three candidate arms, so the rename goes in across the board rather than
scoped to Gemma. Section 5.4 requires the arms be identical in everything but
the model; a Gemma-only rename would have made transport a per-arm difference
and scored it as capability. The two names cap the same quantity -- reasoning
tokens are a detail OF ``completion_tokens``, not additional to it -- so this
is a spelling change on the arms that already worked.

``additional_drop_params: ["max_tokens"]`` is the tempting one-liner and is
wrong: measured, it leaves the body with NO cap at all while every other arm
runs at 16384, which is a config choice scored as a capability difference.

THE FIFTH DEFECT, and it was hiding behind the fourth. ``max_tokens`` was the
first of three walls on the same route; the 400 fired before the others could
be seen. Measured by request ladder on 2026-08-12: a non-default ``top_p`` is
rejected outright (handled in litellm_config.yaml, not here, because it is a
sampling decision), ``temperature`` must be 1, and ``tools`` are refused unless
``reasoning_effort`` is explicitly ``"none"``. The route applies a non-none
default when the parameter is absent, so dropping it -- which is what
``additional_drop_params: ["reasoning_effort"]`` did -- is what kept the escape
out of reach. A workaround for one defect was blocking the fix for the next.

AND THE CONFIG COMMENT THAT SENT US THERE WAS WRONG. litellm_config.yaml
recorded the 2026-08-07 ``reasoning_effort`` rejection as Bedrock's. Probed
2026-08-12 with raw httpx, bypassing litellm entirely: Bedrock accepts it on
all three candidate arms, and ``"high"`` measurably changes output (gemma
240 -> 580 completion tokens, kimi 171 -> 717). The rejection is litellm's own
``_check_valid_arg``, because ``OpenAIGPTConfig.get_supported_openai_params``
omits ``reasoning_effort`` for a non-o-series model. A client-side guard was
recorded as a provider constraint, and the drop rested on that for five days.

WHY OpenAIConfig AND NOT OpenAIGPTConfig. ``get_optional_params`` dispatches
``custom_llm_provider == "openai"`` to ``litellm.OpenAIConfig()``, which is NOT
an ``OpenAIGPTConfig`` subclass -- it branches on o-series, gpt-5 and audio
models first and only then delegates to the module-level ``openAIGPTConfig``
instance. Patching the inner one covers today's fall-through and is bypassed
the moment litellm classifies a candidate into one of those branches. This
patch wraps the outer entry and post-processes its result, so it holds whatever
litellm does internally and is a no-op wherever litellm already renamed.

WHAT IT DOES NOT REACH, deliberately recorded because the failure would be
silent. ``OpenAILikeChatConfig`` overrides ``map_openai_params``, and
``BedrockMantleChatConfig`` inherits from it, so a deployment moved to the
``bedrock_mantle/`` provider -- which litellm 1.95.0 ships, and which would
derive the /openai/v1 vs /v1 path by itself -- would not be renamed by this.
tests/test_config.py pins that every candidate deployment uses ``openai/``, so
the constraint is a property of which deployments are configured rather than a
comment nobody reads.

Sonnet 5 is untouched on both transports and is exempt STRUCTURALLY, not
because its parameters happen to be clean: ``anthropic/`` resolves to
``AnthropicConfig`` and ``bedrock/`` to ``AmazonConverseConfig``, neither of
which is reachable from the openai provider branch. On a native Anthropic body
``max_tokens`` is correct and required.

WHY THIS MODULE IS SEPARATE FROM proxy_callback. The harness imports
``bakeoff.proxy_callback`` at module level (``runner.py`` uses
``read_run_entries``), so applying the patch there would silently patch litellm
in the harness process and in every pytest run.

Importing THIS module applies the patch -- ``instance`` is constructed at
module scope, because ``get_instance_fn`` resolves the configured dotted path
with ``getattr`` and needs the attribute to already exist. That is precisely
why nothing in ``bakeoff/`` may import it: the separation, not the import
timing, is what keeps the harness's litellm unpatched.
tests/test_litellm_patches.py pins that no harness module reaches it.

Verified against litellm 1.95.0.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

# The resolved-params side channel. bakeoff.proxy_callback owns it because the
# HARNESS has to read it and importing THIS module applies its patches -- the
# same reason write_manifest lives over there. The dependency runs in this
# direction only, and proxy_callback imports nothing from here.
from bakeoff.proxy_callback import open_resolved_capture, record_resolved_params

# Stable identifiers recorded on every run record. Changing one changes what
# the log claims was done to the adapter, so these are names, not descriptions.
TOOL_USE_ID_PASSTHROUGH = "anthropic_tool_use_id_passthrough"
TOOL_SCHEMA_PROPERTY_NAMES_STRIP = "anthropic_tool_schema_property_names_strip"
TOOL_USE_ID_COLLISION_UNIQUIFY = "anthropic_tool_use_id_collision_uniquify"
MAX_COMPLETION_TOKENS_RENAME = "openai_max_completion_tokens_rename"
REASONING_EFFORT_PINNED_NONE = "openai_reasoning_effort_pinned_none"
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


# The parameter, and the one value section 5.4 allows it to take. A constant
# rather than a literal because the value IS the eval decision: every arm runs
# thinking-off, so no arm's reasoning is a config artefact. Changing it here
# changes what the eval measures, which is why it does not live inline.
_REASONING_EFFORT = "reasoning_effort"
_REASONING_EFFORT_VALUE = "none"

# The tool_use ids already present in the conversation being answered, set by
# the pre-request hook and read on the response path.
#
# A ContextVar and not an attribute, because there is nowhere to hang an
# attribute: `LiteLLMAnthropicMessagesAdapter()` is constructed fresh at each
# call site (transformation.py, streaming_iterator.py), so `self` never
# outlives one translation. The hook is awaited rather than run as a Task, so
# what it sets lands in the enclosing request's context and is visible to the
# provider call and the streaming iterator beneath it.
# The default is None, never a set(). A mutable default on a ContextVar is a
# single object shared by every context that never called set() -- so a missed
# propagation would not fail, it would quietly accumulate ids across every run
# and every arm in the process and still produce unique-looking output. That
# reads as a working patch. None means "no request context reached here", which
# is a state the code can detect and refuse to guess at.
_SEEN_TOOL_USE_IDS: ContextVar[set[str] | None] = ContextVar(
    "bakeoff_seen_tool_use_ids", default=None
)

# The one JSON-Schema keyword Gemma's Bedrock engine will not accept. Measured,
# not guessed: see the second defect in the module docstring. Deliberately a
# single name rather than a denylist -- every other exotic keyword in Claude
# Code's schemas was probed individually and passed.
_REJECTED_SCHEMA_KEYWORD = "propertyNames"

# Anthropic's own separator for Gemini thought signatures. The function being
# patched has a second, unrelated job -- stripping this suffix -- and that job
# is preserved. No Gemini arm exists today, which is exactly why removing it
# silently would go unnoticed until one did.
_THOUGHT_SEPARATOR = "__thought__"


def _passthrough_tool_use_id(raw_id: str) -> str:
    """``normalize_anthropic_tool_use_id`` minus the character-class rewrite.

    Upstream does two things: split off a Gemini thought-signature suffix, then
    ``re.sub(r"[^a-zA-Z0-9_-]", "_", ...)``. Only the second is destructive
    here, so only the second is dropped.
    """
    base_id = (
        raw_id.split(_THOUGHT_SEPARATOR, 1)[0]
        if _THOUGHT_SEPARATOR in raw_id
        else raw_id
    )
    return base_id or "tool_use_id"


def _strip_property_names(obj: Any) -> Any:
    """Return ``obj`` with every ``propertyNames`` key removed, at any depth.

    A copy, never in-place. The object handed to the hook is Claude Code's own
    request, and mutating it would make the wire log's ``tool_schema_sha``
    depend on whether capture ran before or after the hook -- configuration
    leaking into observation, which section 6.2 exists to prevent.

    Recursion is over the whole schema rather than the top level because Gemma
    rejected the nested form too, and nested is where both real occurrences
    live (``properties.metadata.propertyNames`` on TaskCreate and TaskUpdate).
    """
    if isinstance(obj, dict):
        return {
            key: _strip_property_names(value)
            for key, value in obj.items()
            if key != _REJECTED_SCHEMA_KEYWORD
        }
    if isinstance(obj, list):
        return [_strip_property_names(item) for item in obj]
    return obj


def _uniquify_tool_use_id(raw_id: str, seen: set[str]) -> str:
    """Return an id not already used in this conversation, and claim it.

    A no-op unless ``raw_id`` collides -- which is what keeps this from being a
    per-arm difference. Sonnet, Nemotron and Kimi never repeat an id, so this
    never fires on them, and in particular it never touches Kimi's
    ``functions.Read:0``, the exact shape the passthrough patch above exists to
    preserve. Uniquifying unconditionally would rewrite all three.

    The suffix stays inside ``[a-zA-Z0-9_-]`` on purpose: Claude Code echoes
    the rewritten id back in ``tool_result.tool_use_id`` and the inbound
    ``sanitize_tool_use_ids_in_anthropic_messages`` runs over it, so a suffix
    outside that class would be rewritten there and break the pairing this
    restores.
    """
    if raw_id not in seen:
        seen.add(raw_id)
        return raw_id
    suffix = 1
    while f"{raw_id}_{suffix}" in seen:
        suffix += 1
    unique = f"{raw_id}_{suffix}"
    seen.add(unique)
    return unique


def _tool_use_ids_in(messages: Any) -> set[str]:
    """Every tool_use id already present in an anthropic-format conversation.

    Only ``tool_use`` ids, not ``tool_result.tool_use_id``: a result always
    echoes a use, so collecting both would add nothing and would make the set
    depend on whether a turn had been answered yet.
    """
    seen: set[str] = set()
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and isinstance(block.get("id"), str)
            ):
                seen.add(block["id"])
    return seen


def _uniquify_in_place(block: Any, seen: set[str] | None) -> None:
    """Rewrite a ``tool_use`` block's id if it collides with ``seen``.

    ``seen is None`` means the conversation's ids never reached this stream.
    Do nothing then, rather than invent a suffix: without them there is no way
    to know whether this id collides, and a guess would rewrite the three arms
    whose ids are already unique. The offline gate is what proves this branch
    is not the one being taken -- it carries a real collision, so if the set
    goes missing the arm loops and lands no diff.
    """
    if seen is None or not isinstance(block, dict):
        return
    if block.get("type") != "tool_use":
        return
    raw_id = block.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return
    unique = _uniquify_tool_use_id(raw_id, seen)
    if unique != raw_id:
        block["id"] = unique


def _targets() -> list[Any]:
    """Every module holding a reference to the function being replaced.

    ``common_utils`` owns it, but ``adapters.transformation`` bound it with
    ``from ... import`` at import time and therefore holds its own reference --
    rebinding only the owner is a no-op on the response path, which is where
    the mangling happens. Confirmed by experiment: patching one and not the
    other leaves the bug in place.

    The request path needs no separate entry:
    ``sanitize_tool_use_ids_in_anthropic_messages`` reaches the function
    through ``common_utils``'s own module global, so patching the owner covers
    it transitively.
    """
    from litellm.llms.anthropic import common_utils
    from litellm.llms.anthropic.experimental_pass_through.adapters import (
        transformation,
    )

    return [common_utils, transformation]


def apply() -> list[str]:
    """Install the interventions and verify they took. Idempotent.

    Returns their ids, in the order they are applied. Raises if a target module
    no longer holds the symbol -- a litellm upgrade that renames or moves it
    must fail loudly here rather than leave the proxy quietly mangling ids
    again.

    The two interventions are verified differently because they work
    differently. The tool-id passthrough is a monkeypatch, so the check calls
    the patched function and reads the result. The propertyNames strip is a
    pre-request HOOK, and nothing here can prove litellm will call it -- that
    depends on this class being registered in ``litellm_settings.callbacks``
    and on ``anthropic_messages`` still dispatching pre-request hooks. What is
    checked here is only that the strip itself works and the hook exists to
    carry it. The claim that litellm actually calls it is made by the offline
    gate, where a surviving ``propertyNames`` makes the stub answer 400.
    """
    applied: list[str] = []
    for module in _targets():
        if not hasattr(module, "normalize_anthropic_tool_use_id"):
            raise RuntimeError(
                f"{module.__name__} no longer defines "
                "normalize_anthropic_tool_use_id: the litellm pin moved and "
                f"{TOOL_USE_ID_PASSTHROUGH} would silently stop applying"
            )
        module.normalize_anthropic_tool_use_id = _passthrough_tool_use_id

    # Assert the observable behaviour, not the assignment. This is the check
    # that would catch a fourth importer appearing in a future litellm.
    for module in _targets():
        probe = module.normalize_anthropic_tool_use_id("functions.Read:0")
        if probe != "functions.Read:0":
            raise RuntimeError(
                f"{TOOL_USE_ID_PASSTHROUGH} did not take in {module.__name__}: "
                f"got {probe!r}"
            )
    applied.append(TOOL_USE_ID_PASSTHROUGH)

    probe = _strip_property_names(
        {"properties": {"metadata": {_REJECTED_SCHEMA_KEYWORD: {"type": "string"}}}}
    )
    if probe != {"properties": {"metadata": {}}}:
        raise RuntimeError(
            f"{TOOL_SCHEMA_PROPERTY_NAMES_STRIP} did not take: got {probe!r}"
        )
    if not hasattr(BakeoffAdapterPatches, "async_pre_request_hook"):
        raise RuntimeError(
            f"{TOOL_SCHEMA_PROPERTY_NAMES_STRIP} has no hook to run in: "
            "BakeoffAdapterPatches lost async_pre_request_hook"
        )
    applied.append(TOOL_SCHEMA_PROPERTY_NAMES_STRIP)

    applied.append(_apply_collision_uniquify())
    applied.extend(_apply_openai_param_pins())
    return applied


_WRAPPED_MARKER = "_bakeoff_collision_uniquify"

# Where the conversation's id set is captured, and why it is not captured on
# the per-chunk translation itself.
#
# MEASURED 2026-08-11, by probing the offline gate rather than reasoning about
# it. The pre-request hook sets the ContextVar correctly
# (``HOOK set={'functions.Read:0'}``), and the per-chunk translation reads
# ``None`` on every single chunk: the SSE response is iterated by the server's
# own task, whose context was copied before the hook ran. A ContextVar read
# from there is always empty.
#
# ``AnthropicStreamWrapper.__init__`` still sees it
# (``WRAPPER_INIT seen={'functions.Read:0'}``) -- it runs in the handler
# coroutine, right after the provider call. So the set is snapshotted there,
# onto the stream object, and travels with it into the chunks.
#
# This is also why the ContextVar's default is None rather than ``set()``: with
# a shared mutable default the broken version still produced unique-looking
# ids, by accumulating them process-wide across every run and arm, and the gate
# passed. The failure has to be visible to be fixed.
_SEEN_IDS_ATTR = "_bakeoff_seen_tool_use_ids"


def _apply_collision_uniquify() -> str:
    """Patch the streaming response path. Idempotent."""
    import functools

    from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (  # noqa: E501
        AnthropicStreamWrapper,
    )

    for name in ("__init__", "_should_start_new_content_block"):
        if not hasattr(AnthropicStreamWrapper, name):
            raise RuntimeError(
                f"AnthropicStreamWrapper no longer defines {name}: the litellm "
                f"pin moved and {TOOL_USE_ID_COLLISION_UNIQUIFY} would silently "
                "stop applying"
            )

    original_init = AnthropicStreamWrapper.__init__
    if not getattr(original_init, _WRAPPED_MARKER, False):

        @functools.wraps(original_init)
        def patched_init(self, *args, **kwargs):
            # A copy: the stream mutates its own set as it issues ids, and two
            # concurrent streams must not share one.
            seen = _SEEN_TOOL_USE_IDS.get()
            setattr(self, _SEEN_IDS_ATTR, set(seen) if seen is not None else None)
            return original_init(self, *args, **kwargs)

        setattr(patched_init, _WRAPPED_MARKER, True)
        AnthropicStreamWrapper.__init__ = patched_init

    original_should_start = AnthropicStreamWrapper._should_start_new_content_block
    if not getattr(original_should_start, _WRAPPED_MARKER, False):

        @functools.wraps(original_should_start)
        def patched_should_start(self, *args, **kwargs):
            started = original_should_start(self, *args, **kwargs)
            # Only when a NEW block opens: that is the one moment the id is
            # minted, and rewriting it on later deltas would change an id the
            # client has already seen.
            if started:
                _uniquify_in_place(
                    getattr(self, "current_content_block_start", None),
                    getattr(self, _SEEN_IDS_ATTR, None),
                )
            return started

        setattr(patched_should_start, _WRAPPED_MARKER, True)
        AnthropicStreamWrapper._should_start_new_content_block = patched_should_start

    probe: set[str] = {"call_0"}
    if _uniquify_tool_use_id("call_0", probe) != "call_0_1":
        raise RuntimeError(
            f"{TOOL_USE_ID_COLLISION_UNIQUIFY} did not take: a colliding id was "
            "returned unchanged"
        )
    return TOOL_USE_ID_COLLISION_UNIQUIFY


def _apply_openai_param_pins() -> list[str]:
    """Make the openai provider's outgoing params the ones the route accepts.

    Two interventions, one wrapper, because they hang off the same seam:
    ``litellm.OpenAIConfig.map_openai_params``, the entry
    ``get_optional_params`` dispatches ``custom_llm_provider == "openai"`` to.
    They are reported as two ids because they are two guarantees and a reader
    of ``Versions.litellm_patches`` needs to know which one a record was
    written under.

      MAX_COMPLETION_TOKENS_RENAME      the cap, under the spelling that still works   (bedrock only)
      REASONING_EFFORT_PINNED_NONE      thinking off, uniformly and explicitly         (bedrock only)
      OPENAI_RESOLVED_PARAMS_CAPTURE    what went out, filed under the call id         (every provider)

    POST-PROCESSING, not pre. Three separate things depend on it:

      * The same function routes o-series, gpt-5 and audio models to configs
        that already rename, so a check on the RETURNED dict is a no-op there
        rather than a double rename.
      * ``_check_valid_arg`` runs over ``non_default_params`` BEFORE this and
        rejects ``reasoning_effort`` outright for a non-o-series openai model
        (``OpenAIGPTConfig.get_supported_openai_params`` omits it). Injecting
        into the result is what gets past a guard that is litellm's, not AWS's.
      * ``additional_drop_params`` has already run, so the injected value is
        not the one that gets dropped.

    WHY reasoning_effort IS PINNED AND NOT LEFT ALONE. Gemma's route refuses
    ``tools`` unless ``reasoning_effort`` is explicitly ``"none"`` -- absent is
    not good enough, because the route applies a non-none default. Measured
    2026-08-12: ``Function tools with reasoning_effort are not supported for
    google.gemma-4-31b in /v1/chat/completions. To use function tools, use
    /v1/responses or set reasoning_effort to 'none'.``

    WHY CONFIG CANNOT DO IT. Claude Code sends ``thinking: {"type":
    "adaptive"}`` on every arm and litellm's
    ``translate_anthropic_thinking_to_reasoning_effort`` derives a value from
    it. Measured: a deployment carrying ``allowed_openai_params:
    ["reasoning_effort"]`` and ``reasoning_effort: "none"`` still sends
    ``"medium"`` -- the derived value wins over the deployment default. Only
    something downstream of the mapping can pin it.

    WHY none AND NOT A REAL EFFORT. Section 5.4: every arm identical but the
    model. Sonnet returned zero reasoning tokens across 856 stored calls and
    all three candidates had ``reasoning_effort`` dropped, so the whole Phase
    0c corpus is thinking-off. Pinning ``"none"`` makes explicit on all three
    candidates what every arm was already doing implicitly, rather than turning
    thinking on for one arm and calling the difference capability. Turning it
    ON is a defensible eval and a different one -- it needs Gemma on
    ``/v1/responses`` (tools and reasoning are mutually exclusive on
    chat/completions), and it invalidates rather than re-measures the corpus.

    Idempotent. Only ``openai/`` deployments reach it, so Sonnet's two arms are
    untouched. See the module docstring for what this deliberately does not
    reach.
    """
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
                # Off means untouched: no rename, no pin, no restore.
                # OpenAIGPTConfig.get_supported_openai_params never lists
                # reasoning_effort for a non-o-series model (verified against
                # litellm 1.95.0), so `original` above has already dropped it
                # from `mapped` by the time this branch is reached -- and that
                # is exactly what the openrouter arms want: the config also
                # carries reasoning_effort in additional_drop_params, and
                # Kimi K2.6's endpoints do not list the parameter at all, so
                # sending it under require_parameters: true is zero eligible
                # providers, a 404 on every call.
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
    # than merely filling a gap, and under openrouter it proves the wrapper
    # touched nothing -- litellm's own mapping already omits reasoning_effort
    # for a non-o-series model, and that absence is what the openrouter arms
    # want (see the comment on the wrapper above).
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
        if _REASONING_EFFORT in probe:
            raise RuntimeError(
                f"openrouter: the reasoning_effort pin applied anyway: got {probe!r}"
            )
    # Capture is verified by the marker plus the in-process tests: exercising
    # record_resolved_params here would need a call id in THIS context, and a
    # ContextVar set at apply time leaks into every server task copied from
    # it, attributing unkeyed captures to a probe id.
    applied.append(OPENAI_RESOLVED_PARAMS_CAPTURE)
    return applied


def _report(patches: list[str]) -> Path | None:
    """Tell the harness what this process did, via the shared wire directory.

    The writer lives in bakeoff.proxy_callback so the harness can read it
    without importing THIS module -- importing this one applies the patches.
    """
    from bakeoff.proxy_callback import write_manifest

    try:
        from importlib.metadata import version as package_version

        litellm_version = package_version("litellm")
    except Exception:  # noqa: BLE001 - a version lookup must not fail the proxy
        litellm_version = ""
    wire_dir = Path(os.environ.get("BAKEOFF_WIRE_DIR", "/eval/wire"))
    provider_route = os.environ.get(_PROVIDER_ENV, "")
    return write_manifest(patches, litellm_version, wire_dir, provider_route=provider_route)


class BakeoffAdapterPatches(CustomLogger):
    """Applies the patches on construction, and carries the propertyNames strip.

    A CustomLogger subclass on purpose, now for two reasons. LiteLLM's
    ``initialize_callbacks_on_proxy`` resolves each entry in
    ``litellm_settings.callbacks`` and its success handler dispatches on
    ``isinstance(cb, CustomLogger)`` -- a duck-typed object is skipped in
    silence. Registering properly means the entry cannot rot into a no-op that
    still looks configured. And ``async_pre_request_hook`` below is only
    reached because this is a real CustomLogger: ``_execute_pre_request_hooks``
    iterates ``litellm.callbacks`` and skips anything that fails the same
    isinstance check.
    """

    async def async_pre_request_hook(
        self, model: str, messages: list, kwargs: dict
    ) -> dict:
        """Remove ``propertyNames`` from every tool schema, on every arm.

        THE DEFECT. Gemma 4 31B failed 9/9 live runs with ``JSON-RPC error
        -32602: Job registration failed: Engine bad request: Task submission
        failed with status 400 Bad Request: Generation failed``. Measured
        against live bedrock-mantle on 2026-08-10 by a 15-rung request ladder:
        the fault is one JSON-Schema keyword. ``exclusiveMinimum``,
        ``maxItems``, ``anyOf``, ``const``, ``format`` and ``pattern`` each
        pass; ``propertyNames`` fails, object-level and nested alike. Exactly
        two of the 24 tools Claude Code 2.1.220 declares carry it -- TaskCreate
        and TaskUpdate, both on ``properties.metadata`` -- and each fails alone
        against Gemma while passing against Nemotron. 74 bytes, one whole arm.

        WHY THIS IS NOT A THUMB ON THE SCALE. ``propertyNames: {"type":
        "string"}`` is vacuous: JSON object keys are strings by definition, so
        removing it constrains nothing that was constrained before. No arm's
        tool contract changes meaning. The strip is unconditional and applies
        to all four arms for the same section 6.4 reason the tool-id patch is
        process-wide: a per-arm intervention would make the arms non-identical
        in transport, which is the confound this harness works to avoid.

        WHY A HOOK RATHER THAN A PATCH. ``anthropic_messages`` calls
        ``_execute_pre_request_hooks`` before it branches on provider, and
        keeps the ``tools`` this returns. So one hook covers the ``anthropic/``
        Sonnet arm and the three ``openai/`` candidate arms identically.
        Patching the openai->anthropic adapter instead would reach the
        candidates only, and reintroduce the asymmetry.

        Returning ``kwargs`` rather than ``None`` matters: the caller keeps the
        return value, so ``None`` on a request with no tools would discard
        every other parameter.

        Verified against litellm 1.95.0 and claude 2.1.220.
        """
        tools = kwargs.get("tools")
        if tools:
            kwargs["tools"] = _strip_property_names(tools)

        # THE SECOND DEFECT, and the only place the response path can learn
        # about it. Gemma 4 31B returned the tool-call id `call_0` on all 30
        # responses of every run -- its ids are indexed WITHIN a response, and
        # it emits one call per response. Claude Code executed all 30 locally
        # but could not pair duplicates on the way back, so the conversation
        # Gemma saw carried 1 tool_use, 1 tool_result and 28 "(no content)"
        # turns: it never observed anything after its first command, and
        # repeated the same plan until the turn cap. Measured 2026-08-11;
        # Sonnet 5/5, Nemotron 8/8 and Kimi 5/5 ids were all distinct.
        #
        # A fresh set per request, never carried over: the ids are read off
        # THIS conversation, so nothing is remembered between calls and a
        # first turn correctly starts empty.
        _SEEN_TOOL_USE_IDS.set(_tool_use_ids_in(messages))

        # Open the wire log's resolved-params capture for this request. Seeded
        # HERE, in the one place that runs once per request in the request's own
        # context, so the container is the same object in `map_openai_params`
        # below it and in the success callback after it -- measured, three
        # matching identities on one probe. A fresh container per request: two
        # runs served concurrently must not read each other's params, and a
        # stale one is how one arm's config gets attributed to another.
        #
        # Stamped with litellm's own call id, which this hook and the success
        # callback both receive and which is measured to hold the same value at
        # both ends. Without the stamp a capture that outlived its request would
        # be attributed to the next call rather than refused.
        open_resolved_capture(kwargs.get("litellm_call_id"))
        return kwargs

    def __init__(self) -> None:
        super().__init__()
        self.patches = apply()
        self.manifest_path = _report(self.patches)
        print(
            f"[bakeoff] adapter patches applied: {', '.join(self.patches)}"
            f" (manifest: {self.manifest_path or 'NOT WRITTEN'})",
            flush=True,
        )


# The dotted path named in every proxy config. Must be an INSTANCE, not the
# class -- get_instance_fn returns whatever the path resolves to, and a class
# fails the isinstance check and is dropped without a word.
instance = BakeoffAdapterPatches()
