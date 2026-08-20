"""The codex judge backend: argv hermeticism, classification, usage, cleanup.

Every test here drives `_run_codex` through a fake. That function is the ONE
place this module starts a process, so faking it is what keeps the suite
offline, unpaid and fast while still exercising the real classification, the
real retry policy and the real accounting -- the three things a wrong verdict
would come from.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bakeoff import codex_judge
from bakeoff.codex_judge import (
    CODEX_RATE_LIMIT_RETRIES,
    CodexAuthFailure,
    CodexCallFailed,
    CodexRateLimited,
    CodexRun,
    CodexUnavailable,
    build_codex_argv,
    classify_failure,
    codex_completion,
    codex_environment,
    codex_model_name,
    codex_sampling,
    failure_message,
    is_codex_judge,
    parse_events,
    usage_from_events,
)
from bakeoff.judge import is_auth_failure, new_usage_totals

PINNED = "codex:gpt-5.2-codex"


def _events(*objects: dict) -> tuple[dict, ...]:
    return parse_events("\n".join(json.dumps(obj) for obj in objects))


def _completed(input_tokens: int = 100, output_tokens: int = 7) -> dict:
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": 0,
        },
    }


def _failed(message: str) -> dict:
    return {"type": "turn.failed", "error": {"message": message}}


@pytest.fixture
def judge_home(tmp_path: Path) -> Path:
    """A home shaped like the real one: auth.json and nothing else."""
    home = tmp_path / "judge-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    return home


@pytest.fixture
def codex_bin(tmp_path: Path) -> Path:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


class _FakeCodex:
    """Records every spawn and answers with a scripted outcome per call."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, argv, prompt, env, timeout_s, output_file):
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes
        self.calls.append(
            {"argv": argv, "prompt": prompt, "env": env, "timeout_s": timeout_s}
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install(monkeypatch, outcomes: list) -> _FakeCodex:
    fake = _FakeCodex(outcomes)
    monkeypatch.setattr(codex_judge, "_run_codex", fake)
    return fake


# --- identity ----------------------------------------------------------------


def test_the_codex_namespace_selects_the_backend_and_survives_onto_the_record():
    assert is_codex_judge(PINNED)
    assert not is_codex_judge("openai.gpt-5.6-sol")
    # Stripped for the wire, kept whole for the record: the namespace is what
    # keeps a codex generation from ever pooling with a mantle one.
    assert codex_model_name(PINNED) == "gpt-5.2-codex"


def test_a_bare_codex_namespace_is_refused_because_the_id_must_be_pinned():
    with pytest.raises(ValueError, match="PINNED"):
        codex_model_name("codex:")
    with pytest.raises(ValueError, match="not a codex judge id"):
        codex_model_name("openai.gpt-5.6-sol")


def test_a_codex_judge_id_passes_the_neutrality_guard_and_a_codex_claude_does_not():
    from bakeoff.judge import NonNeutralJudge, assert_neutral_judge

    assert assert_neutral_judge(PINNED) is None
    with pytest.raises(NonNeutralJudge):
        assert_neutral_judge("codex:claude-sonnet-5")


# --- argv and environment ----------------------------------------------------


def test_the_argv_carries_every_hermetic_flag_and_reads_the_prompt_from_stdin(
    tmp_path: Path,
):
    argv = build_codex_argv(
        "/bin/codex", "gpt-5.2-codex", tmp_path, tmp_path / "last.txt"
    )

    # Each of these suppresses a specific injection; a dropped one is a
    # contamination nothing downstream can see.
    for flag in (
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
    ):
        assert flag in argv, flag
    assert argv[:2] == ["/bin/codex", "exec"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[argv.index("-m") + 1] == "gpt-5.2-codex"
    assert argv[argv.index("--color") + 1] == "never"
    # The prompt goes on stdin: a pairwise prompt carries two full diffs and
    # would hit ARG_MAX as an OSError that says nothing about diffs.
    assert argv[-1] == "-"


def test_the_reasoning_effort_is_sent_only_when_set_and_is_the_whole_of_sampling(
    tmp_path: Path,
):
    plain = build_codex_argv("/bin/codex", "m", tmp_path, tmp_path / "o.txt")
    assert "-c" not in plain
    assert codex_sampling(None) == {}

    with_effort = build_codex_argv(
        "/bin/codex", "m", tmp_path, tmp_path / "o.txt", "high"
    )
    assert 'model_reasoning_effort="high"' in with_effort
    # Never temperature: codex cannot send one, so a block claiming 0.0 would
    # be a false record in the field whose job is to say what went out.
    assert codex_sampling("high") == {"model_reasoning_effort": "high"}
    assert "temperature" not in json.dumps(codex_sampling("high"))


def test_an_ambient_openai_api_key_never_reaches_the_subprocess():
    env = codex_environment("/judge/home", {"OPENAI_API_KEY": "sk-personal", "X": "1"})

    assert "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"] == "/judge/home"
    assert env["X"] == "1"


# --- refusals ----------------------------------------------------------------


def test_a_missing_codex_home_refuses_and_names_the_login_command(
    monkeypatch, codex_bin: Path
):
    monkeypatch.delenv(codex_judge.CODEX_HOME_ENV, raising=False)
    complete = codex_completion(PINNED, codex_bin=str(codex_bin))

    with pytest.raises(CodexUnavailable, match="codex login"):
        complete("prompt")


def test_a_home_without_auth_json_refuses_rather_than_judging_anonymously(
    monkeypatch, tmp_path: Path, codex_bin: Path
):
    empty = tmp_path / "empty"
    empty.mkdir()
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(empty)
    )

    with pytest.raises(CodexUnavailable, match="auth.json"):
        complete("prompt")


def test_a_missing_binary_refuses_and_names_the_environment_variable(
    monkeypatch, judge_home: Path
):
    monkeypatch.delenv(codex_judge.CODEX_BIN_ENV, raising=False)
    complete = codex_completion(
        PINNED, codex_bin="/nowhere/codex", codex_home=str(judge_home)
    )

    with pytest.raises(CodexUnavailable, match=codex_judge.CODEX_BIN_ENV):
        complete("prompt")


# --- replies -----------------------------------------------------------------


def test_a_completed_turn_returns_the_last_message_verbatim(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    _install(
        monkeypatch,
        [CodexRun(0, _events(_completed()), "", '{"verdict": "A"}')],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home)
    )

    assert complete("prompt") == '{"verdict": "A"}'


def test_an_empty_last_message_returns_empty_string_for_the_parser_to_refuse(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    # NOT an error here. `""` is what makes the strict parser upstream raise
    # MalformedVerdict and re-ask the identical prompt, which is the right
    # answer to an empty completion and is what the mantle path does too.
    _install(monkeypatch, [CodexRun(0, _events(_completed()), "", "")])
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home)
    )

    assert complete("prompt") == ""


# --- classification ----------------------------------------------------------


def test_the_status_is_read_from_the_failed_turn_event_not_from_prose():
    run = CodexRun(
        1,
        _events(
            _failed(
                "unexpected status 401 Unauthorized: Missing bearer or basic "
                "authentication in header, url: https://api.openai.com/v1/"
                "responses, cf-ray: a2e313b7da326ad3-SJC, request id: req_429"
            )
        ),
        "",
        "",
    )

    failure = classify_failure(failure_message(run))

    # `req_429` in the same message must not be read as a rate limit: the
    # status is anchored on the word "status", not on any three digits.
    assert isinstance(failure, CodexAuthFailure)
    assert failure.status_code == 401


def test_an_auth_failure_answers_the_one_classifier_both_callers_read():
    run = CodexRun(1, _events(_failed("unexpected status 403 Forbidden")), "", "")

    failure = classify_failure(failure_message(run))

    # `judge.is_auth_failure` is the single classifier the backend retry
    # policy and the driver's abort paragraph both ask; it must recognise a
    # codex failure with no edit of its own.
    assert is_auth_failure(failure)


def test_a_rate_limit_is_its_own_class_and_is_never_read_as_auth():
    run = CodexRun(1, _events(_failed("unexpected status 429 Too Many Requests")), "", "")

    failure = classify_failure(failure_message(run))

    assert isinstance(failure, CodexRateLimited)
    # The abort message must say "wait", not "log in again".
    assert not is_auth_failure(failure)


def test_a_failure_with_no_readable_status_is_generic_rather_than_guessed_auth():
    run = CodexRun(1, _events(_failed("the connection went away")), "", "")

    failure = classify_failure(failure_message(run))

    assert isinstance(failure, CodexCallFailed)
    assert not is_auth_failure(failure)


def test_exit_zero_is_success_even_when_no_completion_event_survived():
    """Success is the EXIT CODE, deliberately -- not the presence of an event.

    Requiring `turn.completed` would make the harness depend on a diagnostic
    event's NAME: a codex release that renamed it would turn every successful
    call -- verdict written to the `-o` file, exit 0 -- into a failure, get it
    retried at a second paid spawn, and record it as an error. That is the
    most expensive way this module can be wrong, and it would happen to every
    unit at once.
    """
    run = CodexRun(0, _events({"type": "turn.started"}), "", '{"verdict": "A"}')

    assert failure_message(run) is None


def test_a_non_zero_exit_with_no_failed_turn_still_reports_the_stderr():
    """The status is gone, so the message has to carry something actionable.
    Classified generically rather than guessed at -- see the auth rule."""
    run = CodexRun(3, _events({"type": "turn.started"}), "boom", "")

    message = failure_message(run)

    assert message is not None and "boom" in message
    assert isinstance(classify_failure(message), CodexCallFailed)


def test_a_completed_turn_on_exit_zero_is_success():
    assert failure_message(CodexRun(0, _events(_completed()), "", "ok")) is None


# --- retry policy ------------------------------------------------------------


def test_an_auth_failure_is_never_retried_because_nothing_here_can_mint(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    fake = _install(
        monkeypatch,
        [
            CodexRun(1, _events(_failed("unexpected status 401 Unauthorized")), "", ""),
            CodexRun(0, _events(_completed()), "", "never reached"),
        ],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home)
    )

    with pytest.raises(CodexAuthFailure):
        complete("prompt")
    assert len(fake.calls) == 1


def test_a_transport_failure_is_retried_exactly_once(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    fake = _install(
        monkeypatch,
        [
            CodexRun(1, _events(_failed("the connection went away")), "", ""),
            CodexRun(0, _events(_completed()), "", "recovered"),
        ],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home)
    )

    assert complete("prompt") == "recovered"
    assert len(fake.calls) == 2


def test_a_rate_limit_backs_off_and_gives_up_with_its_own_class(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    limited = CodexRun(
        1, _events(_failed("unexpected status 429 Too Many Requests")), "", ""
    )
    fake = _install(monkeypatch, [limited] * (CODEX_RATE_LIMIT_RETRIES + 1))
    waits: list[float] = []
    complete = codex_completion(
        PINNED,
        codex_bin=str(codex_bin),
        codex_home=str(judge_home),
        sleep=waits.append,
    )

    with pytest.raises(CodexRateLimited):
        complete("prompt")

    assert len(fake.calls) == CODEX_RATE_LIMIT_RETRIES + 1
    assert len(waits) == CODEX_RATE_LIMIT_RETRIES
    # Growing, and jittered so a concurrent pool does not resynchronise every
    # worker onto one wake-up and hit the window together.
    assert waits[-1] > waits[0]


def test_a_rate_limit_that_clears_returns_the_verdict(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    _install(
        monkeypatch,
        [
            CodexRun(1, _events(_failed("unexpected status 429")), "", ""),
            CodexRun(0, _events(_completed()), "", "after the wait"),
        ],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        sleep=lambda _: None,
    )

    assert complete("prompt") == "after the wait"


# --- usage -------------------------------------------------------------------


def test_the_usage_block_maps_onto_the_harness_keys_and_derives_the_total():
    counts = usage_from_events(_events(_completed(input_tokens=14591, output_tokens=5)))

    assert counts == {
        "prompt_tokens": 14591,
        "completion_tokens": 5,
        # DERIVED: codex reports no total, unlike the mantle route whose
        # reported total must never be re-summed.
        "total_tokens": 14596,
    }


def test_an_unreadable_usage_block_is_counted_rather_than_guessed():
    assert usage_from_events(_events({"type": "turn.completed"})) is None
    assert usage_from_events(_events(_failed("nope"))) is None
    # bool is a subclass of int, and True in a token slot would add 1 and read
    # as a measurement.
    assert usage_from_events(
        _events({"type": "turn.completed", "usage": {"input_tokens": True,
                                                     "output_tokens": 3}})
    ) is None


def test_calls_count_before_the_wire_and_missing_usage_is_counted_not_dropped(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    totals = new_usage_totals()
    _install(
        monkeypatch,
        [
            CodexRun(0, _events(_completed(100, 10)), "", "one"),
            CodexRun(0, _events({"type": "turn.completed"}), "", "two"),
        ],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        usage_totals=totals,
    )

    complete("a")
    complete("b")

    assert totals["calls"] == 2
    assert totals["prompt_tokens"] == 100
    assert totals["completion_tokens"] == 10
    assert totals["calls_without_usage"] == 1
    # Nothing is ever minted on this backend.
    assert totals["auth_refreshes"] == 0


def test_a_run_that_never_completed_a_turn_is_not_counted_as_unreadable_usage(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    """`calls_without_usage` means "your token TOTAL is short", and nothing else.

    A 401 has no usage to read rather than usage that could not be read, so
    counting it here would bury the one signal this counter carries under
    every auth failure and every dead endpoint. `calls` already counts the
    attempt.
    """
    totals = new_usage_totals()
    _install(
        monkeypatch,
        [CodexRun(1, _events(_failed("unexpected status 401 Unauthorized")), "", "")],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        usage_totals=totals,
    )

    with pytest.raises(CodexAuthFailure):
        complete("prompt")

    assert totals["calls"] == 1
    assert totals["calls_without_usage"] == 0


def test_the_reasoning_effort_reaches_the_argv_and_not_only_the_record(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    """The record claims what was SENT, so the effort has to actually go out.

    A closure built without it runs at the model's default while every line
    says `model_reasoning_effort: high` -- and the value parses, so nothing
    downstream can tell. This asserts the wire, which is the half a test
    driving an injected seam can never see.
    """
    fake = _install(monkeypatch, [CodexRun(0, _events(_completed()), "", "ok")])
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        reasoning_effort="high",
    )

    complete("prompt")

    (call,) = fake.calls
    assert 'model_reasoning_effort="high"' in call["argv"]


def test_the_first_concurrent_calls_cannot_read_a_half_filled_resolution(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    """Resolution is atomic, not key-by-key.

    Unguarded, a second thread sees a non-empty dict, concludes the work is
    done, and reads `resolved["home"]` one statement before it exists -- one
    paid unit dying on a `KeyError` that says nothing true about the seat or
    the collection, and charging the breaker for it. `--concurrency` submits
    the first prompts of a batch together, so this is the ordinary first wave
    rather than an exotic race.
    """
    run = CodexRun(0, _events(_completed(1, 1)), "", "ok")
    monkeypatch.setattr(codex_judge, "_run_codex", lambda *a, **k: run)
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home)
    )
    errors: list[BaseException] = []

    def hammer():
        try:
            for _ in range(40):
                complete("p")
        except BaseException as exc:  # noqa: BLE001 - the point of the test
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_the_backoff_never_waits_longer_than_the_cap_it_advertises(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    """The cap is applied AFTER the jitter. Capping first lets the multiplier
    carry the wait half again past the ceiling the constant names."""
    limited = CodexRun(1, _events(_failed("unexpected status 429")), "", "")
    _install(monkeypatch, [limited] * (CODEX_RATE_LIMIT_RETRIES + 1))
    waits: list[float] = []
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        sleep=waits.append,
    )

    with pytest.raises(CodexRateLimited):
        complete("prompt")

    assert waits
    assert max(waits) <= codex_judge.CODEX_RATE_LIMIT_CAP_S


def test_a_failed_call_still_counts_because_it_may_have_been_billed(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    totals = new_usage_totals()
    _install(
        monkeypatch,
        [CodexRun(1, _events(_failed("unexpected status 401 Unauthorized")), "", "")],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        usage_totals=totals,
    )

    with pytest.raises(CodexAuthFailure):
        complete("prompt")

    assert totals["calls"] == 1


def test_usage_mutation_is_locked_so_concurrency_cannot_lose_an_increment(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    totals = new_usage_totals()
    run = CodexRun(0, _events(_completed(1, 1)), "", "ok")
    monkeypatch.setattr(
        codex_judge, "_run_codex", lambda *a, **k: run
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home),
        usage_totals=totals,
    )

    threads = [
        threading.Thread(target=lambda: [complete("p") for _ in range(50)])
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert totals["calls"] == 400
    assert totals["prompt_tokens"] == 400
    assert totals["completion_tokens"] == 400


# --- hygiene -----------------------------------------------------------------


def test_the_scratch_directory_is_removed_even_when_the_call_raises(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    seen: list[Path] = []

    def spawn(argv, prompt, env, timeout_s, output_file):
        seen.append(Path(argv[argv.index("-C") + 1]))
        raise CodexCallFailed("died mid-call")

    monkeypatch.setattr(codex_judge, "_run_codex", spawn)
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home)
    )

    with pytest.raises(CodexCallFailed):
        complete("prompt")

    assert seen, "the spawn never happened"
    assert not any(path.exists() for path in seen)


def test_every_call_gets_its_own_scratch_so_no_verdict_can_reach_the_next(
    monkeypatch, judge_home: Path, codex_bin: Path
):
    fake = _install(
        monkeypatch,
        [CodexRun(0, _events(_completed()), "", "ok") for _ in range(3)],
    )
    complete = codex_completion(
        PINNED, codex_bin=str(codex_bin), codex_home=str(judge_home)
    )

    for _ in range(3):
        complete("prompt")

    scratches = {call["argv"][call["argv"].index("-C") + 1] for call in fake.calls}
    assert len(scratches) == 3


def test_an_unparsable_event_line_never_costs_a_paid_verdict():
    # The verdict comes from the -o file; the event stream is diagnostic, so a
    # line in a shape this parser has never seen must not lose the call.
    events = parse_events('not json\n{"type": "turn.completed"}\n\n[1,2]\n')

    assert events == ({"type": "turn.completed"},)
