"""Failure and exclusion classification. See spec section 6.4.

Exclusion is the one mechanism by which results can be massaged, so every
reason code is pre-registered here — in code, before any run executes.
Adding a code later is a visible diff, not a judgement call at analysis time.

Model failures are NEVER excluded. They are the measurement.

Grading is offline by design (spec section 5.5), so at harness time the test
outcome is genuinely unknown rather than negative. `tests_passed` is
tri-state for that reason: the classes that need the oracle (FALSE_SUCCESS,
and the WRONG_BUT_CONFIDENT catch-all) only fire on an explicit result,
while the classes readable from the transcript alone — malformation,
truncation, loops, repetition, giving up early — classify immediately.
Treating "not graded yet" as "the tests failed" would stamp FALSE_SUCCESS,
an accusation of dishonesty, onto every well-behaved run, permanently,
since the event log has no update API.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bakeoff.schema import (
    Exclusion,
    ExclusionClass,
    FailureClass,
    Outcome,
    TerminationReason,
)

MALFORMATION_EXCLUSION_THRESHOLD = 0.30
MALFORMATION_FAILURE_THRESHOLD = 0.20
LOOP_DISTINCT_RATIO = 0.30
LOOP_MIN_TURNS = 10
GAVE_UP_MAX_TURNS = 3

PRE_REGISTERED_REASONS: frozenset[str] = frozenset(
    {
        "container_crashed",
        "api_5xx",
        "api_auth",
        "api_throttle",
        "api_timeout",
        "router_no_deployment",
        "tool_translation_failure",
        "task_defect_flaky_test",
        "task_defect_bad_base_sha",
    }
)

# Substrings identifying a credential failure in a provider error message,
# matched against the lowercased message.
#
# This list exists because the status code is not sufficient and, on the arm
# that matters most, is actively misleading. Measured against litellm 1.95.0:
# `_map_bedrock_exception` recognises auth from the literal strings "Unable to
# locate credentials" and "The security token included in the request is
# invalid". An EXPIRED token says *expired*, matches neither, and that
# function's status ladder (500/401/400/404/408/422/429/503) has no 403 branch
# and no else -- so it falls through to exception_type's generic
# APIConnectionError, which reports status 500. Classified on status alone, an
# operator's lapsed SSO session is `api_5xx` forever, and the event log has no
# update API. It is also the only credential failure litellm retries:
# _should_retry(500) is True, _should_retry(401) and (403) are False.
AUTH_ERROR_SIGNATURES: tuple[str, ...] = (
    "expiredtoken",
    "security token included in the request is expired",
    "security token included in the request is invalid",
    "unrecognizedclient",
    "invalidclienttokenid",
    "invalidsignature",
    "signaturedoesnotmatch",
    "unable to locate credentials",
    "accessdenied",
    "token has expired",
)

# litellm's RouterErrors.no_deployments_available, which reaches the harness as
# a plain ValueError with NO status_code -- so a run that ends on one records
# `failed: True` and `status_code: None` and would otherwise be unclassifiable.
#
# It is not a provider rate limit. Measured: one 401 cools the deployment down
# for 5 s via the `_should_retry(...) is False` branch, which is the one branch
# of four that is NOT guarded by `is_single_deployment_model_group` -- and every
# group in litellm_config.yaml is single-deployment. So in this configuration
# the cooldown fires on essentially nothing except an auth failure, and this
# string is what the auth failure gets replaced by. Checked AFTER auth for
# exactly that reason.
NO_DEPLOYMENT_SIGNATURE = "no deployments available"


@dataclass(frozen=True)
class RunSignals:
    outcome: Outcome
    terminated_by: TerminationReason
    tool_calls_total: int
    tool_calls_malformed: int
    truncation_events: int
    distinct_turn_hashes: int
    turns_used: int
    agent_claimed_success: bool
    # None means "not graded yet", which is the harness's normal state --
    # not a failing test result.
    tests_passed: bool | None = None
    p2p_regressions: list[str] = field(default_factory=list)
    container_crashed: bool = False
    api_error_status: int | None = None
    # The messages of the failure this run ENDED on -- the trailing block of
    # failed calls, not the last one and not every failure.
    #
    # A tuple because the last entry lies. An auth failure cools the deployment
    # down (see NO_DEPLOYMENT_SIGNATURE), so the entries after it are statusless
    # router refusals and `entries[-1]` says "No deployments available" rather
    # than what actually happened -- one logical failure, several callback
    # invocations. Empty when the last call succeeded, which is what keeps a
    # recovered error, and a successful completion's own text, from ever
    # reaching the signature match.
    terminal_error_messages: tuple[str, ...] = ()

    @property
    def malformation_rate(self) -> float:
        if self.tool_calls_total == 0:
            return 0.0
        return self.tool_calls_malformed / self.tool_calls_total


def classify_failure(signals: RunSignals) -> FailureClass | None:
    if signals.outcome == Outcome.RESOLVED:
        return None

    # Observable from the transcript alone; no oracle needed.
    if signals.p2p_regressions:
        return FailureClass.P2P_REGRESSION
    if signals.malformation_rate >= MALFORMATION_FAILURE_THRESHOLD:
        return FailureClass.TOOL_MALFORMATION
    if signals.truncation_events > 0:
        return FailureClass.TRUNCATION

    # Needs the test oracle: only an explicit failure supports the claim.
    if signals.agent_claimed_success and signals.tests_passed is False:
        return FailureClass.FALSE_SUCCESS

    if (
        signals.turns_used >= LOOP_MIN_TURNS
        and signals.distinct_turn_hashes / signals.turns_used < LOOP_DISTINCT_RATIO
    ):
        return FailureClass.LOOP_REPETITION
    if signals.turns_used <= GAVE_UP_MAX_TURNS:
        return FailureClass.GAVE_UP

    if signals.tests_passed is None:
        # Nothing structural went wrong and the oracle has not run. Saying
        # anything here would be a guess written into an immutable record;
        # the offline grader re-derives this from the stored run.
        return None

    return FailureClass.WRONG_BUT_CONFIDENT


def classify_exclusion(signals: RunSignals) -> Exclusion | None:
    if signals.container_crashed:
        return Exclusion(
            cls=ExclusionClass.INFRA_FAILURE,
            reason_code="container_crashed",
            pre_registered=True,
        )

    status = signals.api_error_status
    messages = [m.lower() for m in signals.terminal_error_messages if m]

    # Two named halves rather than one condition, so mutation_check can revert
    # each independently: they answer different questions, and exactly one of
    # them covers the bedrock expired-token case.
    is_auth_status = status in (401, 403)
    is_auth_message = any(
        signature in message
        for message in messages
        for signature in AUTH_ERROR_SIGNATURES
    )
    # BEFORE the status ladder, deliberately. The expired-token case arrives as
    # status 500 and would otherwise be filed as an AWS server error.
    if is_auth_status or is_auth_message:
        return Exclusion(
            cls=ExclusionClass.INFRA_FAILURE,
            reason_code="api_auth",
            pre_registered=True,
        )

    # AFTER auth, and only on the LAST message: a router refusal caused by an
    # auth failure is an auth failure, and the branch above has already claimed
    # it. What is left here is the router declining to dispatch for some other
    # reason -- infra either way, and it carries no status to classify by.
    if messages and NO_DEPLOYMENT_SIGNATURE in messages[-1]:
        return Exclusion(
            cls=ExclusionClass.INFRA_FAILURE,
            reason_code="router_no_deployment",
            pre_registered=True,
        )

    if status is not None:
        if status == 429:
            code = "api_throttle"
        elif status == 408:
            code = "api_timeout"
        elif status >= 500:
            code = "api_5xx"
        else:
            code = None
        if code:
            return Exclusion(
                cls=ExclusionClass.INFRA_FAILURE,
                reason_code=code,
                pre_registered=True,
            )

    if signals.malformation_rate >= MALFORMATION_EXCLUSION_THRESHOLD:
        return Exclusion(
            cls=ExclusionClass.ADAPTER_FAILURE,
            reason_code="tool_translation_failure",
            pre_registered=True,
        )

    return None
