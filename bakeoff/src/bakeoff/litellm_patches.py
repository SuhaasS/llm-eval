"""Adapter interventions applied to LiteLLM inside the PROXY process only.

Spec section 6.4 exists to tell an adapter failure apart from a model failure.
This module removes two adapter failures that were being measured as model
weakness. Both were total, both were deterministic, and both had one-line
causes -- which is the base rate to weigh before reading the next arm's failure
as capability.

  anthropic_tool_use_id_passthrough        Kimi K2.5, below
  anthropic_tool_schema_property_names_strip   Gemma 4 31B, in the hook on
                                           BakeoffAdapterPatches

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

KNOWN EXPOSURE, recorded rather than fixed: on the ``bedrock/`` Converse route
the id becomes Bedrock's ``toolUseId``, which carries its own character
constraint. Harmless for the arms actually run (``claude-sonnet-5-runtime``
emits compliant ids), but it makes ``kimi-k2-5-runtime`` -- configured, and not
in EVAL_ARMS -- newly unsafe. See TASKS.md.

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

# Stable identifiers recorded on every run record. Changing one changes what
# the log claims was done to the adapter, so these are names, not descriptions.
TOOL_USE_ID_PASSTHROUGH = "anthropic_tool_use_id_passthrough"
TOOL_SCHEMA_PROPERTY_NAMES_STRIP = "anthropic_tool_schema_property_names_strip"
TOOL_USE_ID_COLLISION_UNIQUIFY = "anthropic_tool_use_id_collision_uniquify"

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
    return write_manifest(patches, litellm_version, wire_dir)


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
