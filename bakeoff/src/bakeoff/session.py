"""What a Claude Code session actually loaded. See spec section 5.2.

Extracted from `scripts/smoke_test.py` unchanged, for the reason the whole
extraction exists: section 5.2 calls configuration the highest-risk
contamination source and requires the effective config be dumped and diffed
at session start of EVERY run. While these lived in the smoke script they
ran on the gate's runs and on nothing else.

The check that matters most here is the one that looks least important:
Claude Code does not fail on a `--settings` path that does not exist. It
starts with none of the pinned settings, the agent cannot edit anything,
every arm lands no diff, and a one-line harness omission reads as four
capability findings.
"""

from __future__ import annotations

import json
from pathlib import Path


def effective_config(stream: Path) -> dict:
    """The stream-json init event, or {} if there is not one.

    Read from the agent's STDOUT, not from the session transcript. The
    transcript records messages; the init event carrying the effective
    model, tools, MCP servers and permission mode is emitted only on the
    stream and is nowhere on disk otherwise. Verified against claude
    2.1.220 -- a transcript from a completed run contains queue-operation,
    user, attachment, assistant and last-prompt records, and no init.

    Section 5.2 requires dumping and diffing the effective config at session
    start. This is the only place the harness can see what the session
    LOADED rather than what it was told to load -- and the difference is
    the whole point, since Claude Code does not fail on a --settings path
    that does not exist.
    """
    try:
        lines = stream.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event
    return {}


def config_problems(config: dict) -> list[str]:
    """Section 5.2 checks against the dump.

    An empty dump is a failure, never a pass. Treating "nothing observed" as
    "nothing wrong" would turn the one check that can catch an unloaded
    settings file into a check that always succeeds.
    """
    if not config:
        problems = ["no init event: cannot verify what the session loaded"]
        return problems

    problems = []
    mode = config.get("permissionMode")
    if mode != "bypassPermissions":
        # The tell for a --settings path that does not exist. Without the
        # settings the agent cannot edit anything, so every arm lands no
        # diff and a harness bug reads as four capability findings.
        problems.append(
            f"permissionMode is {mode!r}, not 'bypassPermissions': "
            "the settings file did not load"
        )
    if config.get("mcp_servers"):
        problems.append(
            f"{len(config['mcp_servers'])} MCP server(s) loaded despite "
            "--strict-mcp-config: this arm had tools the others did not"
        )
    if not config.get("tools"):
        problems.append("no tools in the init event: the agent had nothing to call")
    return problems


def config_differences(configs: dict[str, dict]) -> list[str]:
    """Section 5.2's diff half: identical across arms except the model.

    Two arms configured differently are not comparable, and the comparison
    is the entire deliverable.
    """
    compared = ("permissionMode", "mcp_servers", "tools", "slash_commands")
    baseline_arm, baseline = next(iter(configs.items()))
    differences = []
    for arm, config in configs.items():
        for key in compared:
            if config.get(key) != baseline.get(key):
                differences.append(
                    f"{arm}.{key} differs from {baseline_arm}: "
                    f"{config.get(key)!r} vs {baseline.get(key)!r}"
                )
    return differences
