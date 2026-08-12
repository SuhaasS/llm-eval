"""The offline gate's stub validators, tested directly.

The stub is the only thing standing between a dead adapter patch and a green
gate. `litellm_patches.apply()` can prove a patch FUNCTION works; it cannot
prove litellm still routes through the seam the patch wraps -- that depends on
dispatch internals a version bump can move in silence. The stub answers 400
when a patched-away shape reaches the wire, which is what turns "the patch is
installed" into "the patch is applying".

Which means a validator that cannot fire is worse than no validator: it reads
as coverage and provides none. These tests exist because the gate exercises
each validator only on its happy path -- a green gate proves the good shape
passes, never that the bad shape is caught.

Scoped to the two 2026-08-12 validators. The three older ones
(`validate_no_property_names`, `validate_tool_call_ids`,
`validate_loop_progress`) are exercised only by the gate itself, and extending
this file to cover them is worth doing but is not this change.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _stub():
    """Import the fixture by path.

    `fixtures/` is not a package and is bind-mounted into the proxy container
    rather than installed, so there is no import path to it. Loading by spec
    keeps the fixture exactly as the container sees it -- a copy in `tests/`
    would be a different file that could drift green while the real one rots.
    """
    path = REPO / "fixtures" / "anthropic_stub.py"
    spec = importlib.util.spec_from_file_location("bakeoff_anthropic_stub", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stub = _stub()

TOOLS = [{"type": "function", "function": {"name": "Read", "parameters": {}}}]


# --- validate_max_completion_tokens ------------------------------------------


def test_the_renamed_cap_passes():
    assert stub.validate_max_completion_tokens({"max_completion_tokens": 16384}) is None


def test_the_old_spelling_is_caught():
    """What Bedrock's /openai/v1 route answers 400 to, and what took gemma to
    0/3 on 2026-08-12."""
    problem = stub.validate_max_completion_tokens({"max_tokens": 16384})
    assert problem is not None
    assert "openai_max_completion_tokens_rename" in problem


def test_a_dropped_cap_is_caught_too():
    """`additional_drop_params: ["max_tokens"]` is the tempting one-liner, and
    measured it leaves the body with no cap at all while every other arm runs
    at 16384 -- a config choice that would be scored as capability (5.4). The
    validator has to separate "renamed" from "removed"; both are max_tokens-
    free, and only one of them is the fix."""
    problem = stub.validate_max_completion_tokens({"messages": []})
    assert problem is not None
    assert "uncapped" in problem


# --- validate_reasoning_effort_none ------------------------------------------


def test_tools_with_the_pin_pass():
    assert (
        stub.validate_reasoning_effort_none(
            {"tools": TOOLS, "reasoning_effort": "none"}
        )
        is None
    )


def test_tools_with_no_reasoning_effort_are_caught():
    """The exact shape `additional_drop_params: ["reasoning_effort"]` produced.

    Gemma's route applies a non-none default when the parameter is ABSENT and
    then refuses the tools, so absence is the failure -- not merely a wrong
    value. A validator that only checked for a wrong value would have passed
    the configuration that took the arm to 0/3.
    """
    problem = stub.validate_reasoning_effort_none({"tools": TOOLS})
    assert problem is not None
    assert "openai_reasoning_effort_pinned_none" in problem


def test_tools_with_a_derived_effort_are_caught():
    """The shape a HALF-applied pin produces, and the reason the wrapper
    assigns unconditionally rather than filling a gap. Claude Code sends
    `thinking: {"type": "adaptive"}` on every arm and litellm derives "medium"
    from it; a pin that fails to override the derived value looks configured
    and sends the one thing the route refuses."""
    problem = stub.validate_reasoning_effort_none(
        {"tools": TOOLS, "reasoning_effort": "medium"}
    )
    assert problem is not None
    assert "'medium'" in problem


@pytest.mark.parametrize("body", [{}, {"tools": []}, {"messages": [], "tools": None}])
def test_a_request_without_tools_is_not_policed(body):
    """The real constraint is tools-only. Widening it would fail requests
    Bedrock accepts, and a validator that rejects valid traffic gets deleted
    rather than fixed."""
    assert stub.validate_reasoning_effort_none(body) is None
