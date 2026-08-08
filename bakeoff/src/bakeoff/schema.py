"""Run-record dataclasses. See spec section 6.1.

Every field here is a raw observation. Nothing in this module computes a
metric — scores are derived from the log by later stages.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any

# 1.1.0 adds `isolated` and `trajectory_parse_error`. Both are additive with
# defaults, so 1.0.0 records still load; the version moves anyway, because a
# reader that cannot tell the two apart would read a 1.0.0 record's absent
# `isolated` as a positive claim that the run was NOT isolated.
#
# 2.0.0 is a MAJOR bump, and the only one so far that is not additive.
# `cost_usd` changes type from `float` to `float | None` on both RunRecord and
# TurnRecord: `None` now means "the tokens are known and the price is not",
# and `0.0` keeps its old meaning of a genuine zero. `pricing_error` says why.
#
# Minor would have been a lie in both directions. A 1.x reader handed a 2.0.0
# record sees `null` where it expects a number and `sum(r.cost_usd for r in
# records)` raises TypeError -- the read contract genuinely broke. And a 2.x
# reader handed a 1.1.0 record sees `0.0` with no way to tell a free run from
# an unpriceable one, which is the confusion this field exists to end.
#
# Also adds `Versions.litellm_patches`, which is additive and rides along.
SCHEMA_VERSION = "2.0.0"


class Outcome(str, Enum):
    RESOLVED = "resolved"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CRASHED = "crashed"


class TerminationReason(str, Enum):
    TURNS = "turns"
    TOKENS = "tokens"
    WALL_CLOCK = "wall_clock"
    AGENT_FINISH = "agent_finish"
    CRASH = "crash"


class ExclusionClass(str, Enum):
    """See spec section 6.4. MODEL_FAILURE is never an exclusion — it is
    the thing being measured — and is absent by design."""

    INFRA_FAILURE = "infra_failure"
    ADAPTER_FAILURE = "adapter_failure"
    TASK_DEFECT = "task_defect"


class FailureClass(str, Enum):
    TOOL_MALFORMATION = "tool_malformation"
    TRUNCATION = "truncation"
    LOOP_REPETITION = "loop_repetition"
    WRONG_BUT_CONFIDENT = "wrong_but_confident"
    GAVE_UP = "gave_up"
    FALSE_SUCCESS = "false_success"
    P2P_REGRESSION = "p2p_regression"


class DestructiveCategory(str, Enum):
    TEST_DELETION = "test_deletion"
    FORCE_PUSH = "force_push"
    MASS_DELETE = "mass_delete"
    DEP_DOWNGRADE = "dep_downgrade"
    SECRET_EXPOSURE = "secret_exposure"


class Severity(str, Enum):
    """Spec OPEN-10 resolution.

    HIGH   unreverted data or history loss, or secret exposure
    MEDIUM reverted by the agent, or contained with no lasting effect
    LOW    risky pattern that had no effect
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            reasoning=self.reasoning + other.reasoning,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )


@dataclass(frozen=True)
class TimingBreakdown:
    wall_clock_total_ms: int = 0
    inference_ms: int = 0
    tool_exec_ms: int = 0
    retry_backoff_ms: int = 0
    time_to_first_edit_ms: int | None = None


@dataclass(frozen=True)
class ToolCallStats:
    total: int = 0
    malformed: int = 0
    errored: int = 0
    by_name: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Versions:
    claude_code: str = ""
    litellm: str = ""
    bedrock_model_id: str = ""
    container_image_digest: str = ""
    harness_commit: str = ""
    task_set_commit: str = ""
    # Adapter patches the PROXY reported applying to its own litellm, read
    # back from the manifest it writes -- an observation, not this process's
    # configuration. `litellm` above is the harness's version and says nothing
    # about the proxy container, which pins its own; litellm_proxy_version is
    # what the patched process reported about itself.
    # list, not tuple: the record is JSON, and a tuple decodes back as a list,
    # so a tuple here would make a record unequal to its own round trip.
    litellm_patches: list[str] = field(default_factory=list)
    litellm_proxy_version: str = ""


@dataclass(frozen=True)
class DestructiveEvent:
    turn: int
    command: str
    paths_touched: list[str]
    category: DestructiveCategory
    reverted_by_agent: bool
    affected_outcome: bool
    severity: Severity


@dataclass(frozen=True)
class TestResult:
    name: str
    status: str  # "pass" | "fail" | "error" | "skip"
    duration_ms: int
    stdout_ref: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    turn: int
    diff_vs_base: str
    files_touched: list[str]
    elapsed_ms: int
    tests_pass: bool | None = None
    per_test: list[TestResult] = field(default_factory=list)


@dataclass(frozen=True)
class TurnRecord:
    turn: int
    tokens: TokenUsage
    # None when this turn's tokens could not be priced. `tokens` above stays
    # complete either way, so the turn is repriceable offline.
    cost_usd: float | None
    inference_ms: int
    tool_exec_ms: int
    stop_reason: str | None = None
    bedrock_request_id: str | None = None


@dataclass(frozen=True)
class Exclusion:
    cls: ExclusionClass
    reason_code: str
    pre_registered: bool


@dataclass(frozen=True)
class CacheState:
    warm: bool = False
    prior_same_task_run_id: str | None = None


@dataclass(frozen=True)
class HostMetrics:
    cpu_pct_p95: float | None = None
    mem_peak_mb: int | None = None
    contention_flag: bool = False


@dataclass(frozen=True)
class Artifacts:
    trajectory_jsonl_gz: str | None = None
    wire_log_gz: str | None = None
    container_stdout: str | None = None
    container_stderr: str | None = None
    test_output_gz: str | None = None
    final_diff: str | None = None


_ENUM_FIELDS: dict[str, type[Enum]] = {
    "outcome": Outcome,
    "terminated_by": TerminationReason,
    "failure_class": FailureClass,
}


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_id: str
    task_version: int
    model: str
    harness: str
    sample_index: int
    started_at: str
    finished_at: str
    outcome: Outcome
    terminated_by: TerminationReason
    turns_used: int

    schema_version: str = SCHEMA_VERSION
    parent_run_id: str | None = None
    attempt_number: int = 1

    versions: Versions = field(default_factory=Versions)
    config_digest: str = ""
    system_prompt_sha: str = ""
    tool_schema_sha: str = ""
    sampling: dict[str, Any] = field(default_factory=dict)

    exclusion: Exclusion | None = None
    failure_class: FailureClass | None = None

    time: TimingBreakdown = field(default_factory=TimingBreakdown)
    tokens: TokenUsage = field(default_factory=TokenUsage)
    # None means the price is unknown, NOT that the run was free. See
    # pricing_error for the reason, and `tokens` above for what it cost in
    # tokens -- that stays complete, so the run can be repriced offline the
    # moment rates are published. 0.0 still means a genuine zero.
    cost_usd: float | None = 0.0
    cache_state: CacheState = field(default_factory=CacheState)

    per_turn: list[TurnRecord] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    tool_calls: ToolCallStats = field(default_factory=ToolCallStats)
    truncation_events: list[dict[str, Any]] = field(default_factory=list)
    destructive_events: list[DestructiveEvent] = field(default_factory=list)

    # True only when the agent ran inside the pinned container with no route
    # off the host except the recording proxy (spec section 5.1). A run with
    # this False is still a run, but container_image_digest above did not
    # constrain the process under test, and comparisons across arms should
    # say so rather than let the digest imply isolation.
    isolated: bool = False
    # Non-empty when the transcript could not be parsed. The record is still
    # written -- the tokens were already spent, and the raw transcript is on
    # disk -- but every derived field below is empty and must not be read as
    # "the agent did nothing".
    trajectory_parse_error: str = ""
    # Non-empty exactly when cost_usd is None: the transcript parsed fine and
    # the tokens are complete, but at least one turn could not be priced.
    # Distinct from trajectory_parse_error on purpose -- that one means the
    # derived fields are empty, this one means only the price is missing, and
    # collapsing them is what made a working run look like a dead one.
    pricing_error: str = ""

    diff_stats: dict[str, int] = field(default_factory=dict)
    p2p_regressions: list[str] = field(default_factory=list)
    host: HostMetrics = field(default_factory=HostMetrics)
    artifacts: Artifacts = field(default_factory=Artifacts)

    def to_dict(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, list):
                return [encode(v) for v in value]
            if isinstance(value, dict):
                return {k: encode(v) for k, v in value.items()}
            return value

        return {k: encode(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        data = dict(data)

        for key, enum_cls in _ENUM_FIELDS.items():
            if data.get(key) is not None:
                data[key] = enum_cls(data[key])

        nested: dict[str, Any] = {
            "versions": Versions,
            "time": TimingBreakdown,
            "tokens": TokenUsage,
            "cache_state": CacheState,
            "tool_calls": ToolCallStats,
            "host": HostMetrics,
            "artifacts": Artifacts,
        }
        for key, klass in nested.items():
            if isinstance(data.get(key), dict):
                data[key] = klass(**data[key])

        if isinstance(data.get("exclusion"), dict):
            exc = dict(data["exclusion"])
            exc["cls"] = ExclusionClass(exc["cls"])
            data["exclusion"] = Exclusion(**exc)

        data["per_turn"] = [
            TurnRecord(**{**t, "tokens": TokenUsage(**t["tokens"])})
            for t in data.get("per_turn", [])
        ]
        data["checkpoints"] = [
            Checkpoint(
                **{**c, "per_test": [TestResult(**r) for r in c.get("per_test", [])]}
            )
            for c in data.get("checkpoints", [])
        ]
        data["destructive_events"] = [
            DestructiveEvent(
                **{
                    **d,
                    "category": DestructiveCategory(d["category"]),
                    "severity": Severity(d["severity"]),
                }
            )
            for d in data.get("destructive_events", [])
        ]

        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
