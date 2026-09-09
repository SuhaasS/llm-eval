"""Minimal .env loader, shared by the bedrock preflight and the proxy.

Lives here rather than in scripts/smoke_bedrock.py because `proxy.py` has to
read `.env` for the openrouter key WITHOUT importing that script: its
module-level imports pull in boto3, and a provider path that needs no AWS
credentials must not resolve any (spec §1, §8).

Does not overwrite variables already set in the environment -- an exported
value should win over a stale file. Tolerates `export KEY=value`, which is
the form credentials arrive in when pasted from a console. Skips unfilled
`<placeholder>` values so a template copied verbatim reads as "not set"
rather than as a literal angle-bracketed key.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path) -> list[str]:
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
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
