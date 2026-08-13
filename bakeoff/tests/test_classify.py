import pytest

from bakeoff.classify import (
    PRE_REGISTERED_REASONS,
    RunSignals,
    classify_exclusion,
    classify_failure,
)
from bakeoff.schema import ExclusionClass, FailureClass, Outcome, TerminationReason


def signals(**overrides) -> RunSignals:
    base = dict(
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        tool_calls_total=10,
        tool_calls_malformed=0,
        truncation_events=0,
        distinct_turn_hashes=10,
        turns_used=10,
        agent_claimed_success=False,
        tests_passed=False,
        p2p_regressions=[],
        container_crashed=False,
        api_error_status=None,
        terminal_error_messages=(),
        terminal_error_statuses=(),
    )
    base.update(overrides)
    return RunSignals(**base)


def test_resolved_run_has_no_failure_class():
    assert classify_failure(signals(outcome=Outcome.RESOLVED, tests_passed=True)) is None


def test_high_malformation_rate_classifies_as_tool_malformation():
    result = classify_failure(signals(tool_calls_total=10, tool_calls_malformed=4))
    assert result == FailureClass.TOOL_MALFORMATION


def test_truncation_detected():
    result = classify_failure(
        signals(truncation_events=2, terminated_by=TerminationReason.TOKENS)
    )
    assert result == FailureClass.TRUNCATION


def test_repeated_turns_classify_as_loop():
    result = classify_failure(signals(turns_used=30, distinct_turn_hashes=4))
    assert result == FailureClass.LOOP_REPETITION


def test_claimed_success_with_failing_tests_is_false_success():
    result = classify_failure(signals(agent_claimed_success=True, tests_passed=False))
    assert result == FailureClass.FALSE_SUCCESS


def test_p2p_regression_takes_priority_over_generic_failure():
    result = classify_failure(signals(p2p_regressions=["test_login"]))
    assert result == FailureClass.P2P_REGRESSION


def test_early_stop_with_no_edits_is_gave_up():
    result = classify_failure(signals(turns_used=2, distinct_turn_hashes=2))
    assert result == FailureClass.GAVE_UP


def test_container_crash_is_infra_exclusion():
    exclusion = classify_exclusion(signals(container_crashed=True))
    assert exclusion.cls == ExclusionClass.INFRA_FAILURE
    assert exclusion.pre_registered is True


def test_bedrock_5xx_is_infra_exclusion():
    exclusion = classify_exclusion(signals(api_error_status=503))
    assert exclusion.cls == ExclusionClass.INFRA_FAILURE


def test_throttle_is_infra_exclusion():
    exclusion = classify_exclusion(signals(api_error_status=429))
    assert exclusion.cls == ExclusionClass.INFRA_FAILURE


def test_malformation_is_adapter_exclusion_not_infra():
    """Spec section 6.4: adapter failures are reported BOTH ways and must
    never be collapsed into infra."""
    exclusion = classify_exclusion(
        signals(tool_calls_total=10, tool_calls_malformed=5)
    )
    assert exclusion.cls == ExclusionClass.ADAPTER_FAILURE


def test_ordinary_model_failure_is_never_excluded():
    assert classify_exclusion(signals()) is None


def test_an_expired_token_is_auth_not_a_server_error():
    """Measured against litellm 1.95.0: `_map_bedrock_exception` recognises auth
    from "Unable to locate credentials" and "...token...is invalid" only. An
    EXPIRED token matches neither, and that function's status ladder has no 403
    branch and no else, so it falls through to exception_type's generic
    APIConnectionError -- status 500. Classified on status alone, the single
    most likely credential event on the reference arm is written into an
    append-only log as an AWS outage. It is also the one litellm retries:
    _should_retry(500) is True, _should_retry(401) and (403) are False."""
    exclusion = classify_exclusion(signals(
        api_error_status=500,
        terminal_error_messages=(
            "BedrockException - An error occurred (ExpiredTokenException) when "
            "calling the InvokeModel operation: The security token included in "
            "the request is expired",
        ),
    ))
    assert exclusion is not None
    assert exclusion.reason_code == "api_auth"


def test_an_auth_failure_hidden_behind_the_routers_cooldown_is_still_auth():
    """The reason this scans the trailing block rather than the last message.

    Measured, litellm 1.95.0: `_should_cooldown_deployment` has four branches
    and the `_should_retry(status) is False` one is NOT guarded by
    `is_single_deployment_model_group` -- so one 401 immediately cools the
    deployment down for DEFAULT_COOLDOWN_TIME_SECONDS (5). Every model group in
    litellm_config.yaml is single-deployment, because CLAUDE.md requires every
    model_name distinct to stop load-balancing. `num_retries: 3` then retries
    into the cooldown and gets RouterRateLimitError -- a plain ValueError with
    NO status_code attribute, so `proxy_callback._status_code` records None.

    Reading entries[-1] therefore sees status None and "No deployments
    available", matches nothing, and returns no exclusion at all. The rule is
    the TRAILING BLOCK of failed calls -- exactly the failure that killed the
    run, however many callback invocations it was split across."""
    exclusion = classify_exclusion(signals(
        api_error_status=None,
        terminal_error_messages=(
            "AuthenticationError: BedrockException Invalid Authentication - "
            "The security token included in the request is invalid",
            "No deployments available for selected model, Try again in 5.0 "
            "seconds. Passed model=claude-sonnet-5-runtime.",
            "No deployments available for selected model, Try again in 5.0 "
            "seconds. Passed model=claude-sonnet-5-runtime.",
        ),
    ))
    assert exclusion is not None
    assert exclusion.reason_code == "api_auth"


def test_a_statusless_router_refusal_is_never_silently_unclassified():
    """RouterRateLimitError carries no status_code, so a run that ends on one
    has `failed: True` and `status_code: None` -- unclassifiable before this,
    and therefore no exclusion at all. Without a preceding auth error the cause
    is the router refusing to dispatch, which is infra either way."""
    exclusion = classify_exclusion(signals(
        api_error_status=None,
        terminal_error_messages=(
            "No deployments available for selected model, Try again in 5.0 seconds.",
        ),
    ))
    assert exclusion is not None
    assert exclusion.reason_code == "router_no_deployment"


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_status_is_excluded_as_infra(status):
    """How the three mantle arms and three of the four bedrock auth shapes
    arrive: a status, and a message the signature list would not catch."""
    exclusion = classify_exclusion(signals(api_error_status=status))
    assert exclusion is not None
    assert exclusion.cls == ExclusionClass.INFRA_FAILURE
    assert exclusion.reason_code == "api_auth"


def test_an_auth_signature_alone_is_enough_without_any_status():
    """The message path must stand on its own. `_status_code` reads
    `exception.status_code` and returns None when the exception carries none --
    which RouterRateLimitError and every connection-level failure do."""
    exclusion = classify_exclusion(
        signals(terminal_error_messages=("Unable to locate credentials",))
    )
    assert exclusion is not None
    assert exclusion.reason_code == "api_auth"


def test_a_genuine_server_error_is_still_api_5xx():
    """The auth check must not swallow the class it was carved out of.
    Collapsing them would hide whether Bedrock was failing or the operator's
    session had lapsed -- different problems with different fixes."""
    exclusion = classify_exclusion(signals(
        api_error_status=503,
        terminal_error_messages=("BedrockException - service unavailable",),
    ))
    assert exclusion.reason_code == "api_5xx"


def test_a_400_is_still_not_an_infra_failure():
    """Section 6.4: a 400 is the adapter or the request, not the platform.
    Excluding it would drop exactly the runs that reveal a broken tool
    translation -- Gemma's max_tokens and reasoning_effort rejections were both
    400s and both were harness defects worth keeping."""
    assert classify_exclusion(signals(api_error_status=400)) is None


def test_an_auth_signature_is_matched_case_insensitively():
    """Providers differ on casing; `ExpiredTokenException` and `expiredtoken`
    are the same event and must not classify differently."""
    assert classify_exclusion(
        signals(terminal_error_messages=("EXPIREDTOKENEXCEPTION",))
    ).reason_code == "api_auth"


def test_a_run_that_died_on_a_throttle_is_still_a_throttle():
    """The trailing block holds the failure that killed the run. No auth
    signature is present, so it falls through to the status ladder."""
    assert classify_exclusion(signals(
        api_error_status=429,
        terminal_error_messages=("RateLimitError: throttlingException",),
    )).reason_code == "api_throttle"


def test_a_crash_still_outranks_an_auth_failure():
    """Ordering. If the container died we do not know what the API did, and
    `container_crashed` is the more specific claim."""
    assert classify_exclusion(
        signals(container_crashed=True, api_error_status=401)
    ).reason_code == "container_crashed"


def test_the_mantle_routes_own_auth_wording_is_recognised():
    """Found by a live run, not by reading: gemma with an invalid bearer token
    says "litellm.AuthenticationError: AuthenticationError: OpenAIException -
    Invalid bearer token", which matched NOTHING in a signature list derived
    entirely from bedrock/ SigV4 error text. It still classified via the 401,
    so the gap only showed when the terminal call carried no status."""
    exclusion = classify_exclusion(signals(
        api_error_status=None,
        terminal_error_messages=(
            "litellm.AuthenticationError: AuthenticationError: "
            "OpenAIException - Invalid bearer token",
        ),
    ))
    assert exclusion is not None
    assert exclusion.reason_code == "api_auth"


@pytest.mark.parametrize(
    "wording",
    ["Invalid bearer token", "Invalid API key provided", "Incorrect API key"],
)
def test_each_openai_route_auth_wording_is_recognised_on_its_own(wording):
    """Each openai/mantle phrase pinned in isolation, without the
    "AuthenticationError" type name that the full live message also carries --
    otherwise one broad signature would cover for every specific one and none
    of them could be shown to matter."""
    exclusion = classify_exclusion(
        signals(api_error_status=None, terminal_error_messages=(wording,))
    )
    assert exclusion is not None
    assert exclusion.reason_code == "api_auth"


def test_an_auth_status_anywhere_in_the_block_beats_the_cooldowns_refusal():
    """The wording-independent half, and the one that would have caught the
    gap above without anyone guessing at strings.

    This is the measured sequence, live against real Bedrock on 2026-08-13 with
    cooldowns enabled: two genuine 401s, then six statusless
    RouterRateLimitErrors from the cooldown they triggered. A run stopping
    inside that middle stretch has status None and, before the signature fix,
    no matching message -- so it was labelled `router_no_deployment`, naming
    the router for the operator's own credential."""
    refusal = "No deployments available for selected model, Try again in 5 seconds."
    exclusion = classify_exclusion(signals(
        api_error_status=None,
        terminal_error_messages=("some auth failure", "some auth failure",
                                 refusal, refusal, refusal),
        terminal_error_statuses=(401, 401, None, None, None),
    ))
    assert exclusion is not None
    assert exclusion.reason_code == "api_auth"


def test_a_refusal_with_no_auth_anywhere_is_still_the_router():
    """The other side: without an auth status or signature in the block, "no
    deployments available" is the router declining to dispatch for some other
    reason, and must not be relabelled as a credential problem."""
    exclusion = classify_exclusion(signals(
        api_error_status=None,
        terminal_error_messages=(
            "No deployments available for selected model, Try again in 5 seconds.",
        ),
        terminal_error_statuses=(None,),
    ))
    assert exclusion.reason_code == "router_no_deployment"


def test_every_reason_code_is_pre_registered():
    """Spec section 6.4: exclusion criteria are written down before the run.
    An ad-hoc reason code would be the mechanism by which results get
    massaged."""
    cases = [
        signals(container_crashed=True),
        signals(api_error_status=503),
        signals(api_error_status=429),
        signals(tool_calls_total=10, tool_calls_malformed=5),
        signals(api_error_status=401),
        signals(api_error_status=403),
        signals(terminal_error_messages=("ExpiredTokenException",)),
        signals(terminal_error_messages=("No deployments available for selected model",)),
    ]
    for case in cases:
        exclusion = classify_exclusion(case)
        assert exclusion.reason_code in PRE_REGISTERED_REASONS


def test_ungraded_run_is_not_accused_of_false_success():
    """tests_passed=None means "not graded yet", not "the tests failed".

    FALSE_SUCCESS asserts the model claimed a success it did not achieve.
    Deriving that from an absent grade would convict every run the oracle
    has not reached, permanently, in a log with no update API.
    """
    result = classify_failure(signals(agent_claimed_success=True, tests_passed=None))
    assert result != FailureClass.FALSE_SUCCESS
    assert result is None


def test_harness_time_signals_leave_the_failure_class_undetermined():
    """Reproduces the orchestrator's own signal construction for a clean run.

    The harness cannot know whether tests passed -- grading is offline by
    design (spec section 5.5) -- and it marks any self-terminating agent as
    having claimed success. Classifying from that alone would label every
    well-behaved run. Structural failures still classify; this one has none,
    so the class stays open for the offline grader.
    """
    clean = signals(
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        agent_claimed_success=True,
        tests_passed=None,
        tool_calls_malformed=0,
        truncation_events=0,
        turns_used=12,
        distinct_turn_hashes=12,
    )
    assert classify_failure(clean) is None
    assert classify_exclusion(clean) is None
