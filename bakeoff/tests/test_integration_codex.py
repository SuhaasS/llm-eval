"""One real, PAID call through the Codex CLI seat.

MARKERS. `pytestmark` carries BOTH `integration` and `codex_live`, module-wide
and for the same reason `test_integration_judge.py` carries its pair: the
section 6.6 logger gate selects `-m "integration and not task_image and not
judge_live and not codex_live"`, and CLAUDE.md pins that gate as offline, no
credentials, no spend. `codex_live` is a SUBSET of `integration` and is never
carried alone -- a test marked only `codex_live` would be swept into the
DEFAULT unit run by `addopts = "-m 'not integration'"` and would spend money
in the suite an operator runs on every commit.

`codex_live` is separate from `judge_live` because the two are unblocked by
different acts. `judge_live` needs an `aws sso` session; this needs
`BAKEOFF_CODEX_HOME` pointing at a home logged in to the attested Pindrop
seat. An operator who has one usually has not got the other.

    BAKEOFF_CODEX_HOME=~/.codex-bakeoff-judge \\
        pytest -m "integration and codex_live" tests/test_integration_codex.py

WHAT THIS BUYS that the offline suite cannot. Every unit test in
`test_codex_judge.py` drives a fake `_run_codex`, so all of them would stay
green against a flag set the CLI has stopped accepting, a login that has
lapsed, a model name the seat does not serve, or an agent layer that answers
in prose the strict parser refuses. This is the one test that puts the real
argv in front of the real binary -- which is exactly the class of failure that
otherwise shows up nine hours into a paid pass.
"""

from __future__ import annotations

import json
import os

import pytest

from bakeoff.codex_judge import (
    CODEX_HOME_ENV,
    codex_completion,
    codex_harness,
)
from bakeoff.judge import new_usage_totals, parse_pairwise_response

pytestmark = [
    pytest.mark.integration,
    pytest.mark.codex_live,
]

#: Overridable, because the pinned production id is a rollout decision and
#: this test should not have to move when it lands.
CODEX_JUDGE = os.environ.get("BAKEOFF_CODEX_JUDGE", "codex:gpt-5.2-codex")

#: Deliberately NOT a real judge prompt. What is under test is the transport
#: and the reply shape, and a real payload would put two diffs of somebody's
#: source through a paid call for no additional signal.
SMOKE_PROMPT = """\
Reply with ONE JSON object and nothing else, and no other text:

{"verdict": "TIE", "reasoning": "this is a transport smoke test"}
"""


@pytest.fixture(autouse=True)
def _require_judge_home():
    if not os.environ.get(CODEX_HOME_ENV):
        pytest.skip(
            f"{CODEX_HOME_ENV} is unset: this test needs a judge home holding "
            "the attested seat's auth.json"
        )


def test_a_real_codex_call_returns_a_reply_the_strict_parser_accepts():
    """The whole seam, end to end: hermetic argv, real seat, real parse.

    Parsed with the harness's own `parse_pairwise_response` rather than with
    `json.loads`, because what has to hold is not "codex replied" but "codex's
    reply survives the parser a verdict goes through" -- and the agent layer
    wrapping the prompt is the thing most likely to break the second while
    leaving the first true.
    """
    usage = new_usage_totals()
    complete = codex_completion(CODEX_JUDGE, usage_totals=usage)

    reply = complete(SMOKE_PROMPT)

    assert parse_pairwise_response(reply) == "tie"
    assert usage["calls"] == 1
    # If this trips, `_usage_from_events` has stopped matching the CLI's
    # `turn.completed` block and every token figure the pass reports is an
    # under-count that says so only through this counter.
    assert usage["calls_without_usage"] == 0
    assert usage["prompt_tokens"] > 0


def test_the_harness_block_names_a_real_version_and_the_attested_seat():
    """`judge_harness` is the only identity the un-hashed Codex wrapper has.

    A version that could not be read stores as `"unknown"`, which is honest
    and useless: this asserts the path that produces a real one, so a record
    written by a live pass carries something a later reader can act on.
    """
    harness = codex_harness(CODEX_JUDGE)

    assert harness["backend"] == "codex-cli"
    assert harness["codex_cli_version"].startswith("codex")
    assert harness["auth_mode"] == "chatgpt"
    assert harness["sandbox"] == "read-only"
    # Never derived from a token: auth.json proves a session, not whose seat.
    assert harness["auth_seat"], "BAKEOFF_CODEX_SEAT is the operator's attestation"
    json.dumps(harness)  # it has to survive the record's serialization
