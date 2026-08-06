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


def test_every_reason_code_is_pre_registered():
    """Spec section 6.4: exclusion criteria are written down before the run.
    An ad-hoc reason code would be the mechanism by which results get
    massaged."""
    cases = [
        signals(container_crashed=True),
        signals(api_error_status=503),
        signals(api_error_status=429),
        signals(tool_calls_total=10, tool_calls_malformed=5),
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
