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
#
# 2.1.0 widens `CacheState.warm` from `bool` to `bool | None` and, more to the
# point, starts populating it. Through 2.0.0 `execute_run` never passed a
# `cache_state`, so EVERY record asserted `{"warm": false}` -- not an empty
# field a reader could see, but a false positive claim, on the axis Sonnet's
# 2.9x cost spread turns on.
#
# Minor rather than major, on the 1.1.0 precedent rather than the 2.0.0 one.
# `None` is falsy, so nothing raises the way `sum(cost_usd)` did; and no reader
# can have depended on the old value, because it was never a measurement. What
# a reader DOES need is to tell a 2.0.0 `false` (a default, meaningless) from a
# 2.1.0 `false` (the provider reported no cache read on the first call) -- which
# is exactly why 1.1.0 moved for a change that was otherwise purely additive.
#
# 2.2.0 finishes what 2.1.0 started and adds the two fields a stored cost needs
# to be repriceable.
#
# 2.1.0 stopped `warm: false` being a default, but only for the arm that has a
# cache. `_usage_from` defaults every cache field to 0, so an arm that reports
# no cache accounting at all -- Gemma and Nemotron, across 9 runs -- still had
# every record claiming it started against a cold cache, a claim about a cache
# whose existence is unconfirmed. 2.2.0 narrows `false` again: it now means the
# provider reported cache accounting somewhere in the run and no read on the
# first call. A 2.1.0 `false` cannot be told from a 2.2.0 one without the
# version, which is why this moves.
#
# Additive with it: `TokenUsage.cache_write_5m` / `cache_write_1h` (spec section
# 3.1's ephemeral split, without which a 1h write is unrecoverably priced at the
# 5m rate) and `Versions.pricing_basis` (which price book produced `cost_usd`,
# now that Sonnet is on Anthropic list pricing rather than the Bedrock page).
#
# 3.0.0 is a MAJOR bump: one API call is now one turn.
#
# Claude Code writes one transcript record PER CONTENT BLOCK, and every one of
# them repeats the whole `usage` block. `parse_trajectory` counted each record
# as a turn and summed each copy, so a five-call run was recorded as six or
# seven turns with its tokens and its cost roughly doubled. Measured on stored
# records 2026-08-11: run 7be2933b recorded `cache_write` 83,867 against a true
# 42,171, and $0.3726 against $0.2148.
#
# `turns_used`, `tokens` and `cost_usd` therefore change VALUE for the same
# underlying run. A reader comparing a 3.0.0 cost against a 2.x one sees a ~2x
# step that is not a real change -- the same broken read contract that made
# `cost_usd | None` a major bump rather than a minor one. Stored records keep
# their figures; the version is what tells a reader they are not comparable.
#
# Additive with it, and the reason the collapse can never go silent again:
# `TurnRecord.api_message_id` (the identity the dedupe keys on),
# `RunRecord.assistant_records` (raw transcript records -- the difference from
# `turns_used` IS the collapse), `RunRecord.turns_streamed` (the CLI's own
# count, an independent cross-check), and
# `CacheState.seconds_since_prior_run` (TTL reasoning was impossible without it).
#
# 3.1.0 closes the two remaining ways a record could describe total loss and
# still look like a quiet, well-behaved run. Minor, not major: no stored value
# changes meaning, and a 3.0.0 reader ignores the new fields safely -- but a
# reader still needs the version, because in 3.0.0 the ABSENCE of these
# signals was not evidence of anything.
#
#   `trajectory_parse_error` is now non-empty when there was no transcript at
#   all, not only when one failed to parse. Before, a missing transcript sent
#   turns, every token count, tool_calls, destructive_events and cost_usd to
#   zero together and left this field empty -- the one ambiguity the whole
#   design vocabulary exists to eliminate, sitting in the middle of it.
#
#   `RunRecord.wire_entries_seen` / `wire_unattributed` (additive) record how
#   many model calls this record could see and how many the proxy could not
#   attribute. Everything derived from the wire -- `sampling`, both hashes,
#   `bedrock_model_id` -- is empty when a run's calls all land in
#   unattributed.jsonl, and the gate for that lived only in the smoke script,
#   which is to say nowhere during an eval. `wire_unattributed` is
#   `int | None`: None means no wire directory was configured and nobody
#   counted, which is not the same claim as a counted zero.
#
# Also in 3.1.0, and not versioned because they only remove false content:
# `system_prompt_sha`/`tool_schema_sha` are "" rather than the sha256 of the
# four bytes "null" when the field was absent, and `artifacts.wire_log_gz` is
# null rather than a path to a file that was never opened.
#
# 3.2.0 widens the wire request projection to carry `max_completion_tokens`
# beside `max_tokens`, and re-sources `sampling.max_output_tokens` from
# whichever of the two the wire actually held.
#
#   The three candidate arms stopped accepting `max_tokens` on Bedrock's
#   OpenAI-compatible route, so `openai_max_completion_tokens_rename` sends
#   the cap under the other spelling. Without the new key the log would still
#   report `max_tokens`, because the projection falls back to Claude Code's
#   raw request body when the resolved params do not carry the name -- and
#   the raw body carries `max_tokens` on every arm, renamed or not. That is a
#   parameter the wire did not carry, reported at full plausibility.
#
#   Minor, not major: no stored value changes meaning and a 3.1.0 reader
#   ignores the new key safely. But the version has to move, because in 3.1.0
#   an absent `max_completion_tokens` was not a claim about anything, and from
#   3.2.0 on it says the arm sent the cap under the older name.
SCHEMA_VERSION = "3.2.0"


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
    """Raw `message.usage`, projected. `input` EXCLUDES the cache fields.

    Verified against the AWS Bedrock prompt-caching page, 2026-08-11: "the
    `inputTokens` field represents only the non-cached input tokens... total
    input tokens = inputTokens + cacheReadInputTokens + cacheWriteInputTokens."
    `costs.cost_usd` therefore charges all three and adds them, and a change
    that made `input` inclusive would double-charge the cached prefix -- on a
    Sonnet run that is most of the input.

    `cache_write` is the TOTAL written, and the two tier fields are parts of it,
    not additions to it: `cache_write - cache_write_5m - cache_write_1h` is the
    write whose TTL the provider did not report. That remainder is real and
    common -- LiteLLM's Converse-to-Anthropic translation emits only the flat
    field and drops Bedrock's `cacheDetails` -- so the tiers must not be summed
    as if they covered the whole. Untiered is an absent observation, not 5m.
    """

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # Spec section 3.1. A 1h write bills at 2x base against 5m's 1.25x (AWS
    # Bedrock prompt-caching page, 2026-08-11), so a log that keeps only the
    # total cannot be repriced -- and Claude Code's Bedrock TTL is a hardcoded
    # 5m today (claude-code#32671), which is exactly the kind of default that
    # changes under an upgrade with nothing in the record to notice it.
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            reasoning=self.reasoning + other.reasoning,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cache_write_5m=self.cache_write_5m + other.cache_write_5m,
            cache_write_1h=self.cache_write_1h + other.cache_write_1h,
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
    # Which price book produced `cost_usd`. The log is append-only, so records
    # written under an earlier book keep their figures forever; without this a
    # reader summing across a book change gets a number that is not a price of
    # anything and cannot tell. Empty on records written before 2.2.0, and on
    # runs whose cost is None.
    pricing_basis: str = ""


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
    """One API call. Not one transcript record -- see `api_message_id`."""

    turn: int
    tokens: TokenUsage
    # None when this turn's tokens could not be priced. `tokens` above stays
    # complete either way, so the turn is repriceable offline.
    cost_usd: float | None
    inference_ms: int
    tool_exec_ms: int
    stop_reason: str | None = None
    bedrock_request_id: str | None = None
    # `message.id` -- what makes one API call identifiable across the several
    # transcript records Claude Code splits it into. Every one of those records
    # repeats the full `usage`, so before 3.0.0 a call's tokens were summed once
    # per content block. `None` on a transcript that carries no id, where each
    # record is treated as its own turn because merging on absence would
    # collapse genuinely distinct calls.
    api_message_id: str | None = None


@dataclass(frozen=True)
class Exclusion:
    cls: ExclusionClass
    reason_code: str
    pre_registered: bool


@dataclass(frozen=True)
class CacheState:
    """Whether this run started against a cache someone else had already
    warmed, and which run that plausibly was.

    `warm` is read off the FIRST model call's `cache_read`, never off the run
    total. Bedrock's cache warms on turn 2 of a single run, so a run-total
    `cache_read > 0` is true of essentially every Sonnet run and measures
    nothing; turn 1 cannot read what this run itself wrote, so a hit there is
    carryover. Measured on the two 2026-08-11 Sonnet N=3 runs, where turn-1
    `cache_read` separates the expensive run from the cheap ones 2/2:

        (0, 41695) -> $0.547     (30506, 11187) -> $0.216 / $0.176

    Note the warm runs still WRITE 11187 tokens on turn 1 -- a partial prefix
    match. "Warm" is not "no writes".

    `None` means undetermined, not cold. Three ways to get there, and the third
    is why schema 2.2.0 exists:

    - no turn parsed, so nothing was observed at all;
    - turn 1 carried no `usage` block, which projects to all-zero tokens and is
      indistinguishable from a genuine zero once projected;
    - the run reported NO cache accounting anywhere -- no read and no write on
      any turn. Gemma and Nemotron do this on every run, and `false` there would
      assert a cold cache on a model whose cache support is unconfirmed
      (`costs.PRICE_BOOK` carries None multipliers for exactly that reason).
      Through 2.1.0 they all said `false`.

    So `false` now means: the provider accounted for cache somewhere in this
    run, and reported no read on the first call. A reader filtering for cold
    runs must not collect parse failures or arms that never had a cache.

    Never inferred from elapsed time -- Sonnet 5 is absent from the Bedrock
    prompt-caching TTL table, and cross-region inference can force a cache
    write at high demand, so a cold turn 1 need not mean the TTL expired.
    Verified against the AWS Bedrock prompt-caching docs, 2026-08-11.
    """

    warm: bool | None = None
    prior_same_task_run_id: str | None = None
    # Seconds from the prior run's start to this one's. TTL reasoning is
    # impossible without it, and nothing else in the record carries it: the
    # index holds the prior run's `started_at` but no reader of one record can
    # reach it, and `finished_at` is the harness's finish rather than the last
    # model call.
    #
    # It is also what makes the harness's own artifact legible. Measured across
    # six matrices: consecutive repeats start 3-5 seconds apart against a 300
    # second TTL, so "warm" on a repeat says the scheduler ran it seconds after
    # its predecessor, not that a developer would find the cache warm. The
    # number belongs in the record so a reader sees that without the wire log.
    #
    # `None` when there is no prior run, or when its start could not be read.
    seconds_since_prior_run: float | None = None


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


def _build(klass: type, data: dict[str, Any]) -> Any:
    """Construct a nested record class, ignoring fields it does not know.

    `RunRecord.from_dict` has always filtered unknown TOP-LEVEL keys; nested
    classes were built with a bare `klass(**data)`, so a record written by a
    LATER schema raised TypeError out of `EventLog.read_run` -- the whole record
    lost to one added field. Every schema bump since 2.2.0 has added nested
    fields, which is what made the gap reachable.

    Dropping what this version cannot represent is right here and wrong at the
    top level of an analysis: the reader still sees `schema_version`, so it can
    tell it is holding a newer record and decide. Raising instead makes that
    decision for it, permanently, in the direction of losing the data.
    """
    known = {f.name for f in fields(klass)}
    return klass(**{k: v for k, v in data.items() if k in known})


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
    # API calls, one per `message.id`. Before 3.0.0 this counted transcript
    # records, which Claude Code emits one per content block.
    turns_used: int

    schema_version: str = SCHEMA_VERSION
    parent_run_id: str | None = None
    attempt_number: int = 1

    # Two independent counts of the same thing, stored because they disagree in
    # informative ways and a single number hid a 2x token inflation for months.
    #
    # `assistant_records` is the raw transcript record count; the difference
    # from `turns_used` is exactly how many content blocks the calls were split
    # across. `turns_streamed` is what the Claude Code CLI itself counted on
    # stdout -- a different process, a different code path, and the budget
    # `--max-turns` is spent against. Any run where the three cannot be
    # reconciled is a parse to look at, and none of that is visible from a
    # single `turns_used`.
    assistant_records: int = 0
    turns_streamed: int = 0

    versions: Versions = field(default_factory=Versions)
    config_digest: str = ""
    system_prompt_sha: str = ""
    tool_schema_sha: str = ""
    sampling: dict[str, Any] = field(default_factory=dict)

    # How many model calls this record could actually see, and how many it
    # lost. Everything derived from the wire -- sampling, the two hashes
    # above, bedrock_model_id, the API error status -- is empty when
    # `wire_entries_seen` is 0, and until 3.1.0 the record gave a reader no
    # way to tell that from a proxy that simply had nothing to report.
    #
    # `wire_unattributed` counts calls that reached the proxy carrying no
    # usable X-Bakeoff-Run-Id and went to unattributed.jsonl instead. Any
    # value above 0 means this record is describing fewer calls than the run
    # made; the calls themselves are not lost, but nothing joins them back.
    # None means NOT MEASURED -- no proxy wire directory was configured, so
    # there was no unattributed.jsonl to count. Zero is a measurement.
    #
    # The gate for this lived only in scripts/smoke_test.py, which is to say
    # it did not run during the eval. A run whose header stamping broke wrote
    # a well-formed record with empty sampling and empty hashes, which is the
    # exact silent shape proxy_callback was written to prevent.
    wire_entries_seen: int = 0
    wire_unattributed: int | None = None

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
    # Non-empty when the transcript could not be READ -- either it failed to
    # parse, or (since 3.1.0) there was no transcript to parse. Both zero
    # every derived field below, and the reader's question is the same in
    # both cases: is this row a quiet run or is it missing data? Empty here
    # is what licenses reading the zeros as observation.
    #
    # The absent case carries the word "absent" and the path; a parse failure
    # carries the exception type and message. The record is still written in
    # both -- the tokens were already spent, and where a transcript exists it
    # is still on disk, so a parse failure is recoverable offline.
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
                data[key] = _build(klass, data[key])

        if isinstance(data.get("exclusion"), dict):
            exc = dict(data["exclusion"])
            exc["cls"] = ExclusionClass(exc["cls"])
            data["exclusion"] = Exclusion(**exc)

        data["per_turn"] = [
            _build(TurnRecord, {**t, "tokens": _build(TokenUsage, t["tokens"])})
            for t in data.get("per_turn", [])
        ]
        data["checkpoints"] = [
            _build(
                Checkpoint,
                {
                    **c,
                    "per_test": [
                        _build(TestResult, r) for r in c.get("per_test", [])
                    ],
                },
            )
            for c in data.get("checkpoints", [])
        ]
        data["destructive_events"] = [
            _build(
                DestructiveEvent,
                {
                    **d,
                    "category": DestructiveCategory(d["category"]),
                    "severity": Severity(d["severity"]),
                },
            )
            for d in data.get("destructive_events", [])
        ]

        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
