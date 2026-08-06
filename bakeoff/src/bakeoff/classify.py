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
        "api_throttle",
        "api_timeout",
        "tool_translation_failure",
        "task_defect_flaky_test",
        "task_defect_bad_base_sha",
    }
)


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
