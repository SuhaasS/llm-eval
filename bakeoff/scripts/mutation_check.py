#!/usr/bin/env python3
"""Mutation check: revert each fix, confirm a test goes red.

A test that passes proves nothing on its own -- it may be asserting
something that was always true. The only evidence a test is load-bearing is
that it FAILS when the behaviour it guards is removed.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")

# (label, file, find, replace, test selector, marker)
MUTATIONS = [
    (
        # The 2026-08-07 defect: cost_usd raising took parse_trajectory down
        # with it, and assemble_record then discarded the whole trajectory.
        # A run that produced the correct diff was recorded as turns=0,
        # tokens=0, cost=0 -- indistinguishable from an arm that died on its
        # first call. Reverting to the bare call restores exactly that.
        "pricing: let a cache-token guard trip take the whole trajectory down",
        "src/bakeoff/trajectory.py",
        "        try:\n            turn_cost: float | None = cost_usd(model, usage)",
        "        if True:\n            turn_cost: float | None = cost_usd(model, usage)",
        "tests/test_trajectory.py -k unpriceable",
        "not integration",
    ),
    (
        # Patching only common_utils is the half-fix that looks complete:
        # adapters.transformation bound the symbol with `from ... import`, so
        # it keeps its own reference and the RESPONSE path -- where the
        # mangling happens -- stays broken. Dropping it from the target list
        # reproduces exactly that.
        "adapter: patch only common_utils, leaving the response path mangling",
        "src/bakeoff/litellm_patches.py",
        "    return [common_utils, transformation]",
        "    return [common_utils]",
        "tests/test_litellm_patches.py -k from_import",
        "not integration",
    ),
    (
        # The 2026-08-10 defect: two of Claude Code's 24 tool schemas carry
        # `propertyNames`, and Gemma's Bedrock engine rejects it with
        # -32602. 9/9 live runs lost to 74 bytes. Reverting the strip to
        # identity puts the keyword back on the wire for every arm.
        "adapter: stop stripping propertyNames, restoring Gemma's -32602",
        "src/bakeoff/litellm_patches.py",
        "    if isinstance(obj, dict):\n        return {\n            key: _strip_property_names(value)",
        "    if True:\n        return obj\n    if isinstance(obj, dict):\n        return {\n            key: _strip_property_names(value)",
        "tests/test_litellm_patches.py -k property_names",
        "not integration",
    ),
    (
        # The 2026-08-11 defect: Gemma returns the same tool-call id on every
        # response, Claude Code cannot pair the duplicates, and the model never
        # sees any tool result after its first. Returning the id unchanged puts
        # the collision back.
        "adapter: stop uniquifying colliding tool-call ids",
        "src/bakeoff/litellm_patches.py",
        "    if raw_id not in seen:\n        seen.add(raw_id)\n        return raw_id",
        "    if True:\n        return raw_id",
        "tests/test_litellm_patches.py -k collision",
        "not integration",
    ),
    (
        # The first of the 2026-08-12 defects: Bedrock's /openai/v1 route began
        # validating Gemma under OpenAI's reasoning-model contract, where
        # max_tokens is deprecated. Dropping the rename sends the spelling the
        # route answers 400 to, on all three candidate arms.
        "adapter: stop renaming max_tokens, restoring Gemma's 0/3",
        "src/bakeoff/litellm_patches.py",
        '                if "max_tokens" in mapped:\n'
        '                    mapped["max_completion_tokens"] = mapped.pop("max_tokens")',
        '                if False:\n'
        '                    mapped["max_completion_tokens"] = mapped.pop("max_tokens")',
        "tests/test_litellm_patches.py -k max_completion",
        "not integration",
    ),
    (
        # The third 2026-08-12 wall, and the one that survives a green-looking
        # config: the route refuses `tools` unless reasoning_effort is
        # explicitly "none", and ABSENT is not "none". Removing the assignment
        # restores the absence that additional_drop_params produced.
        "adapter: stop pinning reasoning_effort, so tools go out unpinned",
        "src/bakeoff/litellm_patches.py",
        "                mapped[_REASONING_EFFORT] = _REASONING_EFFORT_VALUE",
        "                pass",
        "tests/test_litellm_patches.py -k reasoning_effort or pin",
        "not integration",
    ),
    (
        # Removing the tool-id sanitizer process-wide moved the character-class
        # constraint out of the library and into this config. Uncommenting the
        # deployment puts a model whose ids carry a colon back on the Converse
        # bridge, where they become toolUseId verbatim -- and the failure would
        # be a 400 from AWS mid-run, not anything visible offline.
        "config: put the kimi runtime deployment back on the converse route",
        "config/litellm_config.yaml",
        "  # - model_name: kimi-k2-5-runtime\n"
        "  #   litellm_params:\n"
        "  #     model: bedrock/moonshotai.kimi-k2.5",
        "  - model_name: kimi-k2-5-runtime\n"
        "    litellm_params:\n"
        "      model: bedrock/moonshotai.kimi-k2.5\n"
        "      aws_region_name: us-east-1\n"
        "      temperature: 1.0\n"
        "      top_p: 0.95",
        "tests/test_config.py -k converse",
        "not integration",
    ),
    (
        # The measurement that separates Sonnet's $0.547 run from its $0.176
        # ones. Bedrock's cache warms on turn 2 of a single run, so a run-total
        # cache_read is true of nearly every Sonnet run and separates nothing.
        # Taking the total is the plausible-looking mistake, and it is silent.
        "cache: read warm from the run total instead of the first turn",
        "src/bakeoff/runner.py",
        "    return first.cache_read > 0",
        "    return total.cache_read > 0",
        "tests/test_runner.py -k warm",
        "not integration",
    ),
    (
        # The absence half. A run with no parsed turn observed nothing, and
        # calling that cold refiles a parse failure as a measurement -- the
        # same false claim, one layer down from the one 2.1.0 fixed.
        "cache: call an unmeasured run cold instead of undetermined",
        "src/bakeoff/runner.py",
        "    if not parsed.turns:\n        return None",
        "    if not parsed.turns:\n        return False",
        "tests/test_runner.py -k undetermined",
        "not integration",
    ),
    (
        # 2.1.0 fixed `warm` for the one arm that has a cache. Gemma and
        # Nemotron report no cache accounting at all, so turn-1 cache_read is 0
        # for them on every run and `false` asserted a cold cache on models
        # whose cache support is unconfirmed -- 3 of the 4 arms still lying
        # after the fix that was supposed to end it.
        "cache: call an arm that reports no cache accounting cold",
        "src/bakeoff/runner.py",
        "    if total.cache_read == 0 and total.cache_write == 0:\n        return None",
        "    if False:\n        return None",
        "tests/test_runner.py -k no_cache_at_all",
        "not integration",
    ),
    (
        # An assistant record with no usage block projects to all-zero tokens,
        # byte-identical to a genuine zero. API-error records land on turn 1,
        # which is exactly where the measurement is taken.
        "cache: read a first turn that carried no usage as a genuine zero",
        "src/bakeoff/runner.py",
        "    if first == TokenUsage():\n        return None",
        "    if False:\n        return None",
        "tests/test_runner.py -k first_turn_carried_no_usage",
        "not integration",
    ),
    (
        # A 1h cache write bills at 2.00x base against the 5m tier's 1.25x
        # (AWS Bedrock prompt-caching page, 2026-08-11). Charging every write
        # at the 5m rate understates by 60% and nothing raises -- the tier is
        # the only field that could ever detect it, and until 2.2.0 the log
        # did not keep it.
        "cache: bill a 1h cache write at the 5m rate",
        "src/bakeoff/costs.py",
        "                * price.cache_write_1h_multiplier",
        "                * price.cache_write_multiplier",
        "tests/test_costs.py -k one_hour",
        "not integration",
    ),
    (
        # A run that never reached a model call touched no cache. Naming it as
        # the warmer puts a plausible id beside a `warm` it cannot explain.
        "cache: name a run that made no model call as the warmer",
        "src/bakeoff/eventlog.py",
        '                        if entry.get("turns_used", 1) < 1:\n                            continue',
        "                        if False:\n                            continue",
        "tests/test_eventlog.py -k made_no_model_call",
        "not integration",
    ),
    (
        # The 2026-08-11 review defect. Claude Code writes one transcript
        # record per CONTENT BLOCK and repeats the whole usage block in each,
        # so counting records doubled every token total and every cost in the
        # log -- 83,867 cache_write recorded against a true 42,171.
        "turns: count transcript records instead of API calls",
        "src/bakeoff/trajectory.py",
        "        seen = by_message_id.get(key)",
        "        seen = None",
        "tests/test_trajectory.py -k split_across_content_blocks",
        "not integration",
    ),
    (
        # The other direction, and the more expensive one: merging records that
        # carry no id at all would collapse genuinely distinct calls into one
        # turn and understate a run's cost.
        "turns: merge records that carry no message id",
        "src/bakeoff/trajectory.py",
        '        key = message_id or f"\\0record-{result.assistant_records}"',
        "        key = message_id",
        "tests/test_trajectory.py -k without_a_message_id",
        "not integration",
    ),
    (
        # _usage_from runs outside the pricing guard, so anything it raises
        # aborts the parse and assemble_record discards the whole trajectory --
        # the 2026-08-07 row-of-zeroes defect, reachable again through a
        # cache_creation block the proxy layer can reshape.
        "usage: trust the cache_creation block's shape",
        "src/bakeoff/trajectory.py",
        '    tiers = raw.get("cache_creation")\n    if not isinstance(tiers, dict):\n        tiers = {}',
        '    tiers = raw.get("cache_creation") or {}',
        "tests/test_trajectory.py -k malformed_usage",
        "not integration",
    ),
    (
        # A `null` index line is valid JSON and raises AttributeError on .get;
        # the lookup runs OUTSIDE execute_run's try, so the run produces no
        # record at all -- and the bad line poisons every later run too.
        "index: let a corrupt line escape the prior-run lookup",
        "src/bakeoff/eventlog.py",
        "                    except Exception:  # noqa: BLE001 - see TOTAL BY CONSTRUCTION\n                        continue",
        "                    except json.JSONDecodeError:\n                        continue",
        "tests/test_eventlog.py -k corrupt_index_line",
        "not integration",
    ),
    (
        # A partial pricing failure nulls the run total while per_turn keeps
        # real dollars. Keying the basis on the total left those figures with
        # no price book attached.
        "pricing: blank the price basis whenever the run total is unknown",
        "src/bakeoff/runner.py",
        "                if any(turn.cost_usd is not None for turn in parsed.turns)",
        "                if parsed.total_cost_usd is not None",
        "tests/test_fault_injection.py -k price_basis",
        "not integration",
    ),
    (
        # cache_state was a parameter nobody passed for the whole life of
        # schema 2.0.0, and every record claimed warm: false because of it.
        # Dropping the argument reproduces exactly that, and only a test that
        # goes through execute_run can see it.
        "cache: stop threading the prior-run lookup through execute_run",
        "src/bakeoff/runner.py",
        "        prior_same_task_run_id=prior_run_id,",
        "        prior_same_task_run_id=None,",
        "tests/test_fault_injection.py -k cache_state",
        "not integration",
    ),
    (
        # The last statement of a run was unguarded for the whole life of the
        # module, against a docstring promising nothing may raise past it. A
        # stale .partial or a full disk lost the record after the tokens were
        # spent. Calling write_run bare puts that back.
        "record: let a refused write lose the record",
        "src/bakeoff/runner.py",
        "    _write_or_strand(record, event_log, artifacts_root)",
        "    event_log.write_run(record)",
        "tests/test_fault_injection.py -k strands_the_record",
        "not integration",
    ),
    (
        # The zero-row that reads as a quiet run. No transcript sends turns,
        # tokens, tool calls, destructive events and cost to zero together
        # while trajectory_parse_error stays empty -- the one ambiguity the
        # schema's whole vocabulary exists to eliminate.
        "trajectory: leave a missing transcript indistinguishable from silence",
        "src/bakeoff/runner.py",
        '        parse_error = (\n            f"transcript absent: {trajectory_path}"',
        '        parse_error = (\n            ""\n            if True\n            else f"transcript absent: {trajectory_path}"',
        "tests/test_fault_injection.py -k missing_transcript",
        "not integration",
    ),
    (
        # A run whose header stamping broke writes a well-formed record with
        # empty sampling and empty hashes. The gate for it lived only in
        # smoke_test.py, which does not run during an eval.
        "wire: stop recording the calls the proxy could not attribute",
        "src/bakeoff/runner.py",
        "        wire_unattributed=wire_unattributed,\n        # The measurement",
        "        wire_unattributed=None,\n        # The measurement",
        "tests/test_fault_injection.py -k could_not_attribute or this_runs_lost_calls",
        "not integration",
    ),
    (
        # sha256(b"null") is an ordinary-looking 64-hex digest, so a record
        # that observed no system prompt could not be told from one that
        # observed a real prompt -- and two arms that both sent nothing agreed
        # on a hash.
        "wire: hash an absent system prompt into a real-looking digest",
        "src/bakeoff/runner.py",
        '    if value is None:\n        return ""\n    return hashlib.sha256(',
        "    return hashlib.sha256(",
        "tests/test_fault_injection.py -k digest_of_null",
        "not integration",
    ),
    (
        # The wire log is the harness's authority on what was SENT, and it
        # dropped exactly the per-arm params section 6.4 turns on:
        # reasoning_effort is listed on each candidate deployment and on none
        # of Sonnet's. 856 stored calls cannot answer the question.
        "wire: narrow the request projection back to six keys",
        "src/bakeoff/proxy_callback.py",
        '    "thinking",\n    "reasoning_effort",',
        '    "_thinking_dropped",\n    "_effort_dropped",',
        "tests/test_proxy_callback.py -k dropped_params",
        "not integration",
    ),
    (
        # The image shipped no test runner for the whole of Phase 0c, so the
        # agent could not check its own work and Gemma's 9/9 was read as
        # capability. Removing pythonpath reproduces the other half: the
        # fixture is then red before the fix and red after it.
        "fixture: make the smoke task unverifiable again",
        "fixtures/smoke_task/pytest.ini",
        "pythonpath = .",
        "# pythonpath removed",
        "tests/test_smoke_fixture.py -k fixture",
        "not integration",
    ),
    (
        # A config invariant the setup instructions contradict is not pinned.
        # .env.example told the operator to paste the mantle key into
        # AWS_BEARER_TOKEN_BEDROCK, which bearer-authenticates every bedrock/
        # arm -- the failure the config is careful to avoid, produced by
        # following the document.
        "env: point the setup template back at the fallback variable",
        ".env.example",
        "BAKEOFF_MANTLE_TOKEN=<paste-bedrock-api-key>",
        "AWS_BEARER_TOKEN_BEDROCK=<paste-bedrock-api-key>",
        "tests/test_config.py -k env_template",
        "not integration",
    ),
    (
        "defect 1: stop deriving api_error_status from the wire",
        "src/bakeoff/runner.py",
        "    if api_error_status is None:\n        api_error_status = final_api_error_status(entries)",
        "    pass",
        "tests/test_fault_injection.py -k api_error",
        "not integration",
    ),
    (
        "defect 1b: count ANY failed call, not just the final one",
        "src/bakeoff/runner.py",
        "    metadata = entries[-1].get(\"metadata\") or {}\n    if not metadata.get(\"failed\"):\n        return None",
        "    metadata = next((e.get('metadata') or {} for e in entries if (e.get('metadata') or {}).get('failed')), {})\n    if not metadata.get('failed'):\n        return None",
        "tests/test_fault_injection.py -k retry_recovered",
        "not integration",
    ),
    (
        "defect 2: leave the version block half empty",
        "src/bakeoff/runner.py",
        "            litellm=litellm_version(),\n            harness_commit=harness_commit(),",
        "            litellm=\"\",\n            harness_commit=\"\",",
        "tests/test_fault_injection.py -k version_block",
        "not integration",
    ),
    (
        "defect 2b: drop the -dirty suffix",
        "src/bakeoff/runner.py",
        'return f"{sha}-dirty" if dirty else sha',
        "return sha",
        "tests/test_fault_injection.py -k harness_commit",
        "not integration",
    ),
    (
        "defect 3: discard checkpoints on a mid-run failure",
        "src/bakeoff/runner.py",
        "        if recorder is not None:\n            checkpoints = recorder.captured",
        "        pass",
        "tests/test_fault_injection.py -k 'checkpoint_write_failure or killed_mid_stream'",
        "not integration",
    ),
    (
        "defect 4: stop reading the proxy's wire log",
        "src/bakeoff/runner.py",
        "            if proxy_wire_dir is not None:",
        "            if False:",
        "tests/test_fault_injection.py -k proxy_side_capture",
        "integration",
    ),
    (
        "defect 4b: proxy captures, but drops the status code",
        "src/bakeoff/proxy_callback.py",
        '"status_code": _status_code(kwargs) if failed else None,',
        '"status_code": None,',
        "tests/test_fault_injection.py -k proxy_throttle",
        "integration",
    ),
    (
        "defect 4c: read sampling from top-level kwargs (the obvious place)",
        "src/bakeoff/proxy_callback.py",
        "    return project(\n        body,",
        "    return project(\n        kwargs,",
        "tests/test_fault_injection.py -k proxy_side_capture",
        "integration",
    ),
    (
        "phantom index: append to the index before the record is durable",
        "src/bakeoff/eventlog.py",
        "        if path.exists():\n            tmp.unlink(missing_ok=True)\n            raise ImmutabilityError(f\"run {record.run_id} already written\")",
        "        pass",
        "tests/ -k 'twice or index or immut'",
        "not integration",
    ),
    (
        "new: drop the -dirty suffix",
        "src/bakeoff/runner.py",
        'return f"{sha}-dirty" if dirty else sha',
        "return sha",
        "tests/ -k dirty",
        "not integration",
    ),
    (
        "new: stop lowercasing header names",
        "src/bakeoff/proxy_callback.py",
        "return {str(k).lower(): v for k, v in headers.items()}",
        "return dict(headers)",
        "tests/test_proxy_callback.py -k case",
        "not integration",
    ),
    (
        "new: drop calls that arrive with no run_id",
        "src/bakeoff/proxy_callback.py",
        'run_id = _headers(kwargs).get(RUN_ID_HEADER) or UNATTRIBUTED',
        'run_id = _headers(kwargs).get(RUN_ID_HEADER)\n        if run_id is None:\n            return',
        "tests/test_proxy_callback.py -k unattributed",
        "not integration",
    ),
    (
        "new: remove the proxy_server_request header fallback",
        "src/bakeoff/proxy_callback.py",
        '        params.get("proxy_server_request"),\n        kwargs.get("proxy_server_request"),',
        '        kwargs.get("proxy_server_request"),',
        "tests/test_proxy_callback.py -k raw_proxy_request",
        "not integration",
    ),
    (
        "new: drop the concurrency lock",
        "src/bakeoff/proxy_callback.py",
        "        with self._lock:",
        "        if True:",
        "tests/test_proxy_callback.py -k 'concurrent or serialized'",
        "not integration",
    ),
    (
        "new: let a truncated line kill the whole wire log",
        "src/bakeoff/proxy_callback.py",
        "        except json.JSONDecodeError:\n            continue",
        "        except json.JSONDecodeError:\n            raise",
        "tests/test_proxy_callback.py -k partial_final_line",
        "not integration",
    ),
    (
        "new: gate reports PASSED with no docker daemon",
        "scripts/verify_logger.py",
        '        print("\\nGATE INCOMPLETE: could not run " + "; ".join(skipped))',
        '        print("\\nGATE PASSED: logging layer verified.")\n        return 0',
        "tests/test_verify_logger.py -k docker",
        "not integration",
    ),
    (
        "digest: stop excluding the per-run run_id header (config field)",
        "src/bakeoff/claude_runner.py",
        '            "temperature",\n            "custom_headers",',
        '            "temperature",',
        "tests/test_claude_runner.py -k run_id_header",
        "not integration",
    ),
    (
        "digest: stop excluding the run_id header env var",
        "src/bakeoff/claude_runner.py",
        '        # Carries the run_id, so it differs by construction on every run.\n        "ANTHROPIC_CUSTOM_HEADERS",\n',
        '',
        "tests/test_claude_runner.py -k run_id_header",
        "not integration",
    ),
    (
        "attribution: never send the run_id header to the agent",
        "src/bakeoff/claude_runner.py",
        '        {"ANTHROPIC_CUSTOM_HEADERS": config.custom_headers}\n        if config.custom_headers\n        else {}',
        '        {}',
        "tests/test_claude_runner.py -k run_id_header",
        "not integration",
    ),
    (
        "in-process callback: drop the failure status code",
        "src/bakeoff/wire.py",
        '"status_code": self._status_code(kwargs) if failed else None,',
        '"status_code": None,',
        "tests/test_wire.py -k status",
        "not integration",
    ),
    # --- Gate 0: the capture gaps -------------------------------------------
    #
    # Each of these reverts an observation back to the well-formed zero it used
    # to be. The zeros are why they are here: none of them looked like a
    # failure in a stored record.
    (
        # The whole reason the side channel exists. Capture fires on the outer
        # anthropic_messages call and the nested acompletion fires nothing, so
        # without the hand-off the log reports Claude Code's max_tokens for a
        # call that carried max_completion_tokens.
        "wire: stop handing the resolved params to the capture",
        "src/bakeoff/litellm_patches.py",
        "                record_resolved_params({\"model\": model, **mapped})",
        "                pass",
        "tests/test_litellm_patches.py -k hands_what_it_produced",
        "not integration",
    ),
    (
        # A capture attributed to whatever call reads it next. On a proxy
        # serving several arms that is one arm's configuration recorded against
        # another, at full plausibility.
        "wire: attribute a resolved capture without checking whose call it was",
        "src/bakeoff/proxy_callback.py",
        "        captured = _RESOLVED_BY_CALL.get(call_id)",
        "        captured = next(iter(_RESOLVED_BY_CALL.values()), None)",
        "tests/test_proxy_callback.py -k never_attributed",
        "not integration",
    ),
    (
        # The field that identified the first mechanism as dead. Without it a
        # broken hand-off and an `anthropic/` arm behaving correctly are the
        # same null, and the offline gate stays green over both.
        "wire: stop saying which kind of null a missing resolved is",
        "src/bakeoff/proxy_callback.py",
        '        return "captured" if call_id in _RESOLVED_BY_CALL else "not_recorded"',
        '        return "not_recorded"',
        "tests/test_proxy_callback.py -k which_kind_of_null",
        "not integration",
    ),
    (
        # `response_obj` is None on a failure, so this restores
        # {"raw_completion": "None"} as the entire record of a 400.
        "wire: drop the provider's error body from a failed call",
        "src/bakeoff/proxy_callback.py",
        "    exc = kwargs.get(\"exception\")\n    if exc is None:\n        return None",
        "    exc = kwargs.get(\"exception\")\n    if True:\n        return None",
        "tests/test_proxy_callback.py -k provider_body",
        "not integration",
    ),
    (
        # Restores one post-run clock reading spread across every call in the
        # canonical artifact.
        "wire: re-stamp every replayed entry with the harness's own clock",
        "src/bakeoff/wire.py",
        '            "logged_at": logged_at or now,',
        '            "logged_at": now,',
        "tests/test_wire.py -k replayed",
        "not integration",
    ),
    (
        # A CRASHED row with no cause. Exclusion is the one mechanism by which
        # results can be massaged, so an unattributable crash is an
        # unjustifiable exclusion.
        "crash: record that a run crashed without recording why",
        "src/bakeoff/runner.py",
        '        crash_error = f"{type(exc).__name__}: {exc}"',
        '        crash_error = ""',
        "tests/test_fault_injection.py -k crashed_run_records_the_cause",
        "not integration",
    ),
    (
        # The positive safety claim manufactured by a failure: a scanner that
        # raised left destructive_events empty with no error anywhere.
        "safety: let a failing destructive scan read as a clean run",
        "src/bakeoff/runner.py",
        '                        scanner_error = f"{type(exc).__name__}: {exc}"',
        '                        pass',
        "tests/test_fault_injection.py -k failing_destructive_scan",
        "not integration",
    ),
    (
        # A wire-log name collision recorded as a container crash, on a run
        # whose container never started.
        "wire: put the logger back inside the run body, so a collision is a crash",
        "src/bakeoff/runner.py",
        "    except OSError as exc:\n        wire = None\n        wire_log_error = f\"{type(exc).__name__}: {exc}\"",
        "    except OSError:\n        raise",
        "tests/test_fault_injection.py -k wire_log_collision",
        "not integration",
    ),
    (
        # A non-zero exit that still wrote a transcript, byte-identical to a
        # clean finish.
        "agent: stop recording the exit code",
        "src/bakeoff/runner.py",
        "        agent_exit_code=(\n            exit_code if isinstance(exit_code := getattr(\n                runner_result, \"exit_code\", None\n            ), int) else None\n        ),",
        "        agent_exit_code=None,",
        "tests/test_fault_injection.py -k exit_code_is_recorded",
        "not integration",
    ),
    (
        # Unreadable stdout dropped with no counter, which undercounts
        # turns_streamed -- the count that exists to catch undercounts.
        "stdout: treat an unreadable line as an ordinary non-assistant event",
        "src/bakeoff/claude_runner.py",
        '    except json.JSONDecodeError:\n        return "malformed"',
        '    except json.JSONDecodeError:\n        return "other"',
        "tests/test_claude_runner.py -k unparseable_stdout",
        "not integration",
    ),
    (
        # 40 unreadable transcript lines, byte-identical in the record to none.
        "transcript: stop reporting the lines that could not be parsed",
        "src/bakeoff/runner.py",
        "        transcript_malformed_lines=parsed.malformed_lines,",
        "        transcript_malformed_lines=0,",
        "tests/test_fault_injection.py -k unreadable_transcript",
        "not integration",
    ),
    (
        # The failed-API-call count back under a name that reads as a statement
        # about the agent's tool use.
        "tools: report failed API calls as failed tool calls again",
        "src/bakeoff/runner.py",
        "        errored=0,\n        api_calls_failed=failed_calls,",
        "        errored=failed_calls,\n        api_calls_failed=0,",
        "tests/test_runner.py -k failed_api_calls",
        "not integration",
    ),
    (
        # Two counts collapsed into one, which is how a surplus over the
        # proxy's access log got read as retries when it was double-logging.
        "wire: count callback invocations as if they were logical calls",
        "src/bakeoff/runner.py",
        "    return len(seen) + unkeyed",
        "    return len(entries)",
        "tests/test_runner.py -k double_logged",
        "not integration",
    ),
    (
        # Configuration reported as observation, on the one field runner.py's
        # own rule was written about.
        "isolation: assert section 5.1 from the argument instead of measuring it",
        "src/bakeoff/runner.py",
        "            isolated, isolation_evidence = container.network_isolation()",
        "            isolated, isolation_evidence = bool(network), \"\"",
        "tests/test_fault_injection.py -k routable_network_is_recorded",
        "integration",
    ),
    (
        # Right verdict, wrong evidence: Docker names the no-connectivity
        # configuration `none` and reports Internal false for it.
        "isolation: read the Internal flag alone, calling network_mode=none routable",
        "src/bakeoff/container.py",
        '                if attrs.get("Driver") == "null":',
        '                if False:',
        "tests/test_container.py -k no_network_at_all",
        "integration",
    ),
]


def run(label, rel, find, replace, selector, marker):
    path = REPO / rel
    original = path.read_text()
    if find not in original:
        # A rotted anchor must FAIL, not skip. A mutation harness that
        # quietly stops mutating reports a clean sweep while testing
        # nothing -- the same false assurance it exists to detect.
        print(f"  STALE ANCHOR  {label}\n                no longer present in {rel}")
        return False
    path.write_text(original.replace(find, replace, 1))
    try:
        result = subprocess.run(
            [PY, "-m", "pytest", *selector.split(" -k ")[0].split(),
             "-k", selector.split(" -k ")[1].strip("'\""),
             "-q", "-m", marker, "--basetemp=" + str(Path.home() / ".cache/bakeoff-mut")],
            cwd=REPO, capture_output=True, text=True, timeout=900,
        )
        # Exit 5 is 'no tests collected' -- that is an empty selector, not
        # a caught mutation. Counting it as a catch is how a mutation
        # harness certifies coverage that does not exist.
        if result.returncode == 5:
            print(f'  NO TESTS  {label}')
            return False
        caught = result.returncode != 0
        tail = [l for l in result.stdout.splitlines() if "passed" in l or "failed" in l]
        print(f"  {'CAUGHT' if caught else 'MISSED'}  {label}")
        print(f"          {tail[-1] if tail else result.stdout.strip()[:80]}")
        return caught
    finally:
        path.write_text(original)


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = []
    for m in MUTATIONS:
        if only and only not in m[0]:
            continue
        results.append(run(*m))
    real = [r for r in results if r is not None]
    print(f"\n{sum(1 for r in real if r)}/{len(real)} mutations caught")
    sys.exit(0 if all(real) else 1)
