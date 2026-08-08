"""Adapter patches applied to LiteLLM inside the PROXY process only.

Spec section 6.4 exists to tell an adapter failure apart from a model failure.
This module removes one adapter failure that was being measured as model
weakness.

THE DEFECT. Kimi K2.5 emits tool-call ids of the form ``functions.Read:0``.
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
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

# Stable identifier recorded on every run record. Changing it changes what the
# log claims was done to the adapter, so it is a name, not a description.
TOOL_USE_ID_PASSTHROUGH = "anthropic_tool_use_id_passthrough"

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
    """Install the patches and verify they took. Idempotent.

    Returns the applied patch ids. Raises if a target module no longer holds
    the symbol -- a litellm upgrade that renames or moves it must fail loudly
    here rather than leave the proxy quietly mangling ids again.
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
    return write_manifest(patches, litellm_version, wire_dir)


class BakeoffAdapterPatches(CustomLogger):
    """Applies the patches on construction.

    A CustomLogger subclass on purpose. LiteLLM's ``initialize_callbacks_on_proxy``
    resolves each entry in ``litellm_settings.callbacks`` and its success
    handler dispatches on ``isinstance(cb, CustomLogger)`` -- a duck-typed
    object is skipped in silence. Registering properly means the entry cannot
    rot into a no-op that still looks configured.
    """

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
