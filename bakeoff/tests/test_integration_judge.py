"""The judge against the real model, on the real prompt, for one real call.

Every other test of the judge drives it through the `CompleteFn` seam with a
fake -- which is the right shape for the vote protocol, the parser and the
whitelist, and is structurally incapable of catching the two things that only
exist below the seam:

* **A prompt no model will answer in the asked shape.** `parse_rubric_response`
  is strict on purpose (spec "Testing"): a closed dimension key set, `type(v)
  is int` rather than `isinstance`, and non-empty reasoning. Offline it is only
  ever handed replies this repo wrote, so every one of them is JSON of the
  shape the assertion wanted. Whether the PINNED judge, shown
  `render_rubric_prompt`'s actual text with its `_RUBRIC_RESPONSE_FORMAT`
  block, produces a reply that survives that parser is a fact about the model
  and the prompt together, and no fake can report it. A prompt the model
  answers in prose burns `retries + 1` calls per comparison and then raises
  `MalformedVerdict` over the whole batch.

* **The live client's wiring, end to end.** `test_judge.py` drives
  `live_completion` to the exact point where the real code opens a socket and
  puts a recording stand-in for `litellm.Router` there -- deliberately, because
  a unit test that reached Bedrock would be a paid test in the default run. So
  the model id, the mantle base URL, the path suffix that differs per model
  family on that endpoint, and the bearer token's region scope are all asserted
  offline as VALUES and never once as a call that returned 200.

MARKERS. `pytestmark` carries BOTH `integration` and `judge_live`, module-wide
rather than per test, so a case added later cannot be written without them.
The second marker is what keeps the section 6.6 logger gate offline:
`verify_logger.py` selects `-m "integration and not task_image and not
judge_live"`, and CLAUDE.md pins that gate as offline, no credentials, no
spend. `judge_live` is a SUBSET of `integration` and is never carried alone --
`task_image` next door costs time, this one costs money.

    cd bakeoff && .venv/bin/python -m pytest -v \
        -m "integration and judge_live" tests/test_integration_judge.py

BUDGET. One `judge_rubric` call at `retries=2`, so one billed completion on a
model that answers in the asked shape and at most three if it does not. Output
is capped by `JUDGE_SAMPLING["max_completion_tokens"]`; the input is one
rendered rubric prompt carrying the click task's prompt and its reference diff
twice (candidate and reference are the same text here) -- 6,566 characters
measured 2026-08-18, well inside a single turn. The credential window
is about an hour -- the Identity Center session policy caps it well below
`MANTLE_TOKEN_TTL` -- which is why `live_completion` mints lazily and why a
run of this file that sat behind a long `-m integration` sweep would fail on
`ExpiredToken` rather than on anything it tests.

NO SCORE IS ASSERTED. The submission here is the task's own reference fix
compared against itself, so a healthy judge scores it at or near the top -- and
an assertion that said so would be an assertion about a model version. The
pinned id moves, the anchors are prose, and a 2 that becomes a 1 on
`convention_adherence` is model drift reported as a harness failure, at which
point the honest fix is to delete the assertion. What is asserted instead is
domain validity: the five dimensions, the three flags, the types, the range,
and reasoning a human could re-read.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from bakeoff.judge import (
    JUDGE_MODEL_ID_DEFAULT,
    RUBRIC_DIMENSIONS,
    RUBRIC_FLAGS,
    PayloadInputs,
    judge_rubric,
    live_completion,
)
from bakeoff.similarity import similarity_context
from bakeoff.tasks import load_task

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_DIR = REPO_ROOT / "taskset" / "click-3360-write-usage-empty-args"

#: The env var the harness carries the mantle bearer token under. Written out
#: rather than imported from `scripts.smoke_bedrock`, for the reason
#: `test_judge.py:1430` gives: this name is the contract, and a rename these
#: tests followed automatically would be a rename that broke the proxy with the
#: suite still green.
MANTLE_ENV_NAME = "BAKEOFF_MANTLE_TOKEN"

#: The minting path's package. `live_completion` falls back to
#: `derive_mantle_token(region)` when the env var is unset, and that function
#: imports `provide_token` from here (`smoke_bedrock.py:159`) -- so an absent
#: package means the fallback cannot produce a token no matter what the SSO
#: session looks like.
TOKEN_GENERATOR = "aws_bedrock_token_generator"


def _no_credential_path() -> bool:
    """True when NEITHER source could produce a token, checked without cost.

    `find_spec` rather than an import, and `os.environ` rather than a call:
    this runs at collection, and a skip condition that minted a token would
    start the ~1h credential clock on every collection of the offline suite --
    including the runs where this module is deselected before a test executes.

    The env arm can read False for a token that IS present: `bakeoff/.env` does
    not reach `os.environ` until something runs `load_dotenv`, which here is
    `import litellm` inside `_judge_router`, long after this line. That is why
    the condition is a disjunction and why the message below names the variable
    to set rather than claiming no credential exists.
    """
    return not os.environ.get(MANTLE_ENV_NAME) and (
        importlib.util.find_spec(TOKEN_GENERATOR) is None
    )


# Both markers, module-wide. See the module docstring: `judge_live` is what
# keeps the section 6.6 logger gate offline and unbilled, and a per-test
# decorator is a thing the next case can be written without. The skip rides in
# the same list for the same reason -- a case added below inherits it rather
# than remembering it.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.judge_live,
    pytest.mark.skipif(
        _no_credential_path(),
        reason=(
            f"no mantle credential path for the judge: set {MANTLE_ENV_NAME}, "
            f"or install {TOKEN_GENERATOR} and run `aws sso login` so a "
            "short-term token can be minted"
        ),
    ),
]

#: A truthful abbreviation of what the ladder actually returns for this exact
#: submission: `test_integration_grader.py::test_the_reference_solution_grades_
#: resolved` runs the same diff through `grade_run` in the task's own image and
#: measures these three green. Name and status only, which is the whole of what
#: the judge may see about the gate (`PayloadInputs.checks`) -- a `CheckResult`
#: here would leak `output_path`, `duration_s` and `detail`, and is the leak
#: that class exists to make impossible.
PASSING_CHECKS: tuple[dict[str, str], ...] = (
    {"name": "patch_non_empty", "status": "pass"},
    {"name": "f2p", "status": "pass"},
    {"name": "p2p", "status": "pass"},
)


def test_the_live_judge_returns_a_well_formed_rubric_profile(capsys):
    """One paid rubric call on the reference fix, scored against itself.

    The submission is `task.solution_diff` VERBATIM as the candidate, with the
    same text as the reference anchor -- the self-comparison, which is the
    cheapest input that is nonetheless a real one: a real repository's real
    prompt, the fix that repository actually shipped, and a similarity context
    computed rather than hand-written. `extra_files` goes to `drop_paths` as
    `payload_inputs_from` passes it, so the three overlap figures the model
    reads here are the ones a production call would produce.

    The assertions restate `parse_rubric_response`'s own post-conditions on
    purpose. Offline that parser is the thing under test and these would be
    circular; here the parser is the thing being TRUSTED, and what is under
    test is that a real reply from the pinned model reaches the far side of it
    at all. They also fail loudly if `judge_rubric` is ever changed to hand
    back unvalidated model content, which is the one refactor that would make
    every offline judge test pass while publishing whatever the model said.

    The reasoning is printed rather than only measured. It is what makes a
    verdict auditable by a human -- the rule `parse_rubric_response` refuses an
    empty one for, and what a disputed κ is re-examined against (§4.3) -- and
    an operator spending a live call deserves to read what he paid for.
    """
    task = load_task(TASK_DIR)
    inputs = PayloadInputs(
        task_prompt=task.prompt,
        reference_diff=task.solution_diff,
        candidate_diff=task.solution_diff,
        checks=PASSING_CHECKS,
        similarity=similarity_context(
            task.solution_diff,
            task.solution_diff,
            drop_paths=task.extra_files,
        ),
    )

    # ONE call. `judge_rubric`'s own `retries` re-asks only on a malformed
    # verdict, and a second `judge_rubric` here -- for a determinism check, or
    # to exercise the pairwise path -- would double the bill of a gate an
    # operator runs by hand.
    payload, rendered, result = judge_rubric(
        inputs, live_completion(JUDGE_MODEL_ID_DEFAULT)
    )

    # The prompt reached the model as the payload describes it. Cheap, and it
    # is the difference between "the judge scored something" and "the judge
    # scored THIS submission": a rendered prompt built from a stale payload
    # would still come back with a perfectly well-formed profile.
    assert payload["kind"] == "rubric"
    assert payload["candidate_diff"] == task.solution_diff
    assert task.prompt in rendered

    assert set(result.dimension_scores) == set(RUBRIC_DIMENSIONS), (
        "the model answered a different set of dimensions than the rubric "
        f"asks for: {sorted(result.dimension_scores)}"
    )
    for name, score in result.dimension_scores.items():
        # `type(...) is int`, not `isinstance`: `isinstance(True, int)` is True
        # and `True == 1`, so an isinstance check records a model that answered
        # `true` as an honest partial score.
        assert type(score) is int, (
            f"dimension {name!r} is {score!r}, not an int"
        )
        assert score in (0, 1, 2), f"dimension {name!r} scored {score}"

    assert set(result.flags) == set(RUBRIC_FLAGS), (
        f"the model answered a different set of flags: {sorted(result.flags)}"
    )
    for name, flag in result.flags.items():
        assert type(flag) is bool, f"flag {name!r} is {flag!r}, not a bool"

    assert result.reasoning.strip(), "a verdict with no reasoning is not one"

    with capsys.disabled():
        print(f"\njudge_model_id={JUDGE_MODEL_ID_DEFAULT}")
        print(f"dimension_scores={result.dimension_scores}")
        print(f"flags={result.flags}")
        print(f"reasoning:\n{result.reasoning}")
