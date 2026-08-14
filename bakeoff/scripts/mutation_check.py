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
        "            wire_unattributed=wire_unattributed,\n            # The measurement",
        "            wire_unattributed=None,\n            # The measurement",
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
        "            if recorder is not None:\n                checkpoints = recorder.captured",
        "            if False:\n                checkpoints = recorder.captured",
        "tests/test_fault_injection.py -k 'checkpoint_write_failure or killed_mid_stream'",
        "not integration",
    ),
    (
        "defect 4: stop reading the proxy's wire log",
        "src/bakeoff/runner.py",
        "            if proxy_wire_dir is not None:\n                raw_entries = read_run_entries",
        "            if False:\n                raw_entries = read_run_entries",
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
        "        except json.JSONDecodeError:\n            malformed += 1\n            continue",
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
    (
        # The 2026-08-12 defect: `maybe_capture` runs inside the agent's
        # stdout loop, so a raise there unwinds past the container into
        # execute_run's catch-all and the record says CRASHED with zero
        # turns, zero tokens and no diff -- for a run that was working.
        # Measured once in seven offline arms. Removing the guard restores it.
        "checkpoints: let a mid-run snapshot failure take the whole run down",
        "src/bakeoff/checkpoints.py",
        '            self.errors.append(f"turn {turn}: {type(exc).__name__}: {exc}")\n            return None',
        "            raise",
        "tests/test_checkpoints.py -k destroy_the_run",
        "not integration",
    ),
    (
        # Containment alone swaps a loud wrong record for a quiet one: a
        # short `checkpoints` list is byte-identical to an agent that changed
        # nothing, and the section 5.5 curve is computed over exactly that
        # list. Dropping the error record reproduces the quiet version.
        "checkpoints: contain the failure but stop recording that it happened",
        "src/bakeoff/runner.py",
        'checkpoint_error = "; ".join(recorder.errors)',
        'checkpoint_error = ""',
        "tests/test_fault_injection.py -k checkpoint_gap",
        "not integration",
    ),
    (
        # `git add -A` races Claude Code's atomic Write (`<name>.tmpXXXX`,
        # created then renamed) and exits 128 "unable to stat". Without the
        # retry the checkpoint is lost every time the race is lost.
        "checkpoints: stop retrying the lost race against an atomic write",
        "src/bakeoff/container.py",
        "        for attempt in range(_ADD_ATTEMPTS):",
        "        for attempt in range(1):",
        "tests/test_container.py -k lost_race",
        "not integration",
    ),
    (
        # base_sha is what the container detaches to. An abbreviation or a
        # branch name both resolve, and both resolve to something that can
        # move -- so accepting them makes section 5.1's pinning decorative.
        "tasks: accept an abbreviated or symbolic base_sha",
        "src/bakeoff/tasks.py",
        "    if not _SHA_RE.match(base_sha):",
        "    if False:",
        "tests/test_tasks.py -k could_run_the_wrong_thing",
        "not integration",
    ),
    (
        # The pin is what catches a re-cut patch or an edited manifest moving
        # the start state. Without it every record already written against
        # the old SHA silently describes a different task.
        "tasks: stop verifying the pinned start state",
        "src/bakeoff/tasks.py",
        "    if task.declared_start_sha and task.declared_start_sha != start_sha:",
        "    if False:",
        "tests/test_tasks.py -k start_state_that_moved",
        "not integration",
    ),
    (
        # task_version is not part of run_id, so resume would silently mix
        # records of two different tasks under one task_id.
        "matrix: resume across an edited task without noticing",
        "src/bakeoff/matrix.py",
        "        if stored_version != versions.get(cell.task_id):",
        "        if False:",
        "tests/test_matrix.py -k edited_task",
        "not integration",
    ),
    (
        # The Phase 0c failure in one operator: ModuleNotFoundError is also a
        # non-zero exit, so `!= 0` accepts a broken environment as "the bug is
        # present" and every arm is scored on a task that was never runnable.
        "preflight: accept any non-zero exit as evidence the bug is present",
        "src/bakeoff/preflight.py",
        "        elif red.exit_code != EXIT_TESTS_FAILED:",
        "        elif False:",
        "tests/test_preflight.py -k cannot_even_run",
        "integration",
    ),
    (
        # Measured against litellm 1.95.0: _map_bedrock_exception matches auth
        # on "invalid" and not "expired", and has no 403 branch -- so an
        # ExpiredTokenException arrives as APIConnectionError at status 500 and
        # is filed as an AWS outage, permanently, in an append-only log.
        "classify: read only the status, so an expired token is an AWS outage",
        "src/bakeoff/classify.py",
        "    is_auth_message = any(",
        "    is_auth_message = False and any(",
        "tests/test_classify.py -k expired_token_is_auth",
        "not integration",
    ),
    (
        # 401/403 are how the three mantle arms and three of four bedrock auth
        # shapes arrive. Without this they get no exclusion at all.
        "classify: stop treating 401/403 as an infra failure",
        "src/bakeoff/classify.py",
        "    is_auth_status = status in AUTH_ERROR_STATUSES or any(",
        "    is_auth_status = status in () or any(",
        "tests/test_classify.py -k auth_status_is_excluded",
        "not integration",
    ),
    (
        # Found by a live run against real Bedrock, not by reading: the
        # openai/mantle route says "Invalid bearer token", which matched
        # nothing in a signature list derived from bedrock/ SigV4 error text.
        "classify: know only the bedrock wordings, not the mantle route's",
        "src/bakeoff/classify.py",
        '    "invalid bearer token",',
        '    "\\x00never-matches",',
        "tests/test_classify.py -k openai_route_auth_wording",
        "not integration",
    ),
    (
        # The wording-independent half. Measured live: the wire log ran
        # AA RRRRRR AAAAAAAAAAAAAA, so a run stopping inside the middle stretch
        # has status None and no matching message -- and gets labelled
        # router_no_deployment, naming the router for the operator's credential.
        "classify: read the terminal status only at the end of the block",
        "src/bakeoff/classify.py",
        "        s in AUTH_ERROR_STATUSES for s in signals.terminal_error_statuses",
        "        False for s in signals.terminal_error_statuses",
        "tests/test_classify.py -k anywhere_in_the_block",
        "not integration",
    ),
    (
        # An auth failure cools the deployment down and the retries come back
        # as statusless RouterRateLimitErrors, so the LAST message says "No
        # deployments available" and matches nothing. Reading only the last
        # message loses the very event the classifier exists to catch.
        "classify: read only the last error message, which the cooldown replaced",
        "src/bakeoff/classify.py",
        "        for message in messages",
        "        for message in messages[-1:]",
        "tests/test_classify.py -k hidden_behind_the_routers_cooldown",
        "not integration",
    ),
    (
        # RouterRateLimitError is a plain ValueError with no status_code, so
        # `failed: True` + `status_code: None` was unclassifiable and returned
        # no exclusion at all.
        "classify: leave a statusless router refusal unclassified",
        "src/bakeoff/classify.py",
        "    if messages and NO_DEPLOYMENT_SIGNATURE in messages[-1]:",
        "    if False:",
        "tests/test_classify.py -k statusless_router_refusal",
        "not integration",
    ),
    (
        # ExclusionClass has no MODEL_FAILURE, so an exclusion is by
        # construction not model data. Dropping this check is what let a dead
        # credential reset the abort streak on every cell it killed.
        "matrix: let an excluded cell reset the abort streak",
        "src/bakeoff/matrix.py",
        "    if record.exclusion is not None:",
        "    if False:",
        "tests/test_matrix.py -k excluded_run_is_not_collection_data",
        "not integration",
    ),
    (
        # The status-agnostic backstop -- the only gate that holds when litellm
        # classifies an expiry as a 500 or as nothing at all.
        "matrix: accept a zero-turn row as a measurement of the model",
        "src/bakeoff/matrix.py",
        "    if record.turns_used <= 0:",
        "    if False:",
        "tests/test_matrix.py -k no_turns_is_reported",
        "not integration",
    ),
    (
        # Section 5.7 interleaves the arms, so one dead arm's failures are
        # never adjacent. Simulated on 20x4x3 with the reference arm dead: the
        # global counter never fires and all 60 of its cells are lost.
        "matrix: count the abort streak globally only, as section 5.7 hides it",
        "src/bakeoff/matrix.py",
        "        if self.by_model[model] >= self.per_arm:",
        "        if False:",
        "tests/test_matrix.py -k never_adjacent",
        "not integration",
    ),
    (
        # None is "the container never started, nobody measured". Reporting it
        # as a section 5.1 violation invents a claim from an absence and feeds
        # the abort streak with it.
        "matrix: collapse unmeasured isolation into a violation",
        "src/bakeoff/matrix.py",
        "    if record.isolated is False:",
        "    if not record.isolated:",
        "tests/test_matrix.py -k unmeasured_isolation",
        "not integration",
    ),
    (
        # Starting a cell that cannot finish spends the tokens and then writes
        # a row measuring nothing -- and run_id is deterministic under mode
        # "x", so that cell is consumed forever.
        "credentials: start a cell that cannot finish before the session dies",
        "src/bakeoff/proxy.py",
        "    if remaining > needed_s:",
        "    if True:",
        "tests/test_credentials.py -k cannot_finish_before_expiry",
        "not integration",
    ),
    (
        # Single-line anchor on purpose: a multi-line one spanning the print
        # would break on any whitespace edit and fail as a stale anchor.
        # KeyboardInterrupt does not catch TokenRetrievalError, so the guard is
        # present syntactically and dead in practice -- which is the defect.
        "credentials: let an expired session escape as a traceback",
        "src/bakeoff/proxy.py",
        "    except Exception as exc:  # noqa: BLE001 - expired-session guard, see below",
        "    except KeyboardInterrupt as exc:",
        "tests/test_credentials.py -k rather_than_a_traceback",
        "not integration",
    ),
    (
        # The route's own word for why generation stopped. Without it the
        # record describes the end of a run only in Claude Code's translated
        # vocabulary, and litellm's mapping is unrecorded.
        "wire: stop capturing the provider's own finish reason",
        "src/bakeoff/proxy_callback.py",
        '    choices = response.get("choices")',
        "    choices = None",
        "tests/test_wire.py -k providers_own_finish_reason",
        "not integration",
    ),
    (
        # A failed entry reached no stopping decision. The fixture gives it a
        # reason on purpose -- with None the isinstance check alone excludes
        # it and nothing pins this guard.
        "wire: count a failed call as a stop the provider reported",
        "src/bakeoff/runner.py",
        '        if metadata.get("failed"):\n'
        "            continue\n"
        '        reason = metadata.get("finish_reason")\n'
        "        if isinstance(reason, str) and reason:\n"
        "            counts[reason] = counts.get(reason, 0) + 1",
        "        if False:\n"
        "            continue\n"
        '        reason = metadata.get("finish_reason")\n'
        "        if isinstance(reason, str) and reason:\n"
        "            counts[reason] = counts.get(reason, 0) + 1",
        "tests/test_runner.py -k counts_only_calls_that_returned",
        "not integration",
    ),
    (
        # terminal_finish_reason walks BACKWARD past trailing failures -- the
        # opposite of terminal_error_messages, whose subject is the failure.
        # Forward yields the first call's reason, not the last.
        "wire: read the first finish reason instead of the run's last",
        "src/bakeoff/runner.py",
        "    for entry in reversed(entries):\n"
        '        metadata = entry.get("metadata") or {}\n'
        '        if metadata.get("failed"):\n'
        "            continue\n"
        '        reason = metadata.get("finish_reason")',
        "    for entry in entries:\n"
        '        metadata = entry.get("metadata") or {}\n'
        '        if metadata.get("failed"):\n'
        "            continue\n"
        '        reason = metadata.get("finish_reason")',
        "tests/test_runner.py -k last_call_that_returned",
        "not integration",
    ),
    (
        # Through 3.6.0 every record ever written claimed the run had the host
        # to itself, from this default, while nothing sampled at all. The
        # selector must go through assemble_record: HostSampler.metrics()
        # passes contention_flag explicitly on every path, so the default is
        # never read there.
        "host: let an unsampled run claim the machine was quiet",
        "src/bakeoff/schema.py",
        "    contention_flag: bool | None = None",
        "    contention_flag: bool | None = False",
        "tests/test_fault_injection.py -k does_not_claim_the_host_was_quiet",
        "not integration",
    ),
    (
        # A CPU reading is worth less than the checkpoint whose raise recorded
        # CRASHED with zero turns for a working run on 2026-08-12.
        "host: let a sampler failure escape into the run",
        "src/bakeoff/container.py",
        "        except Exception as exc:  # noqa: BLE001 - see the class docstring",
        "        except KeyboardInterrupt as exc:",
        "tests/test_container.py -k sampler_that_dies",
        "not integration",
    ),
    (
        # Measured: a stream's first frame carries precpu_stats with no
        # system_cpu_usage, so the .get(..., 0) below deltas against the
        # absolute system total and files a fabricated ~0% reading on every
        # run. The guard and the .get are a pair -- with a subscript the
        # KeyError would return None anyway and this mutation would be MISSED.
        "host: file the first stream frame as a real zero-percent reading",
        "src/bakeoff/container.py",
        '            if "system_cpu_usage" not in pre:\n'
        "                return None\n"
        "            cpu_delta",
        "            if False:\n"
        "                return None\n"
        "            cpu_delta",
        "tests/test_container.py -k first_stream_frame",
        "not integration",
    ),
    (
        # __exit__ force-removes the container under a thread the 3s join may
        # not have caught, so without this every such run records an error
        # describing teardown rather than sampling.
        "host: report teardown as a sampling failure",
        "src/bakeoff/container.py",
        "            if not self._stop.is_set():\n"
        "                self._error =",
        "            if True:\n"
        "                self._error =",
        "tests/test_container.py -k teardown_not_a_sampling_failure",
        "not integration",
    ),
    (
        # stop() is called from execute_run's OUTER finally, which is not
        # inside a try -- a raise there escapes execute_run and the run
        # produces no record at all, not even record.unwritten.json.
        "host: let stopping the sampler cost the record",
        "src/bakeoff/container.py",
        "        except RuntimeError as exc:\n"
        "            self._error = self._error or",
        "        except KeyboardInterrupt as exc:\n"
        "            self._error = self._error or",
        "tests/test_container.py -k stop_is_total",
        "not integration",
    ),
    (
        # Unstamped, the path is a pure function of the cell and the next
        # invocation's rmtree deleted the previous run's artifacts -- 12
        # stored records point at a file that is not theirs.
        "collection: let a later matrix delete an earlier record's artifacts",
        "scripts/run_matrix.py",
        '    return cache / "artifacts" / stamp',
        '    return cache / "artifacts"',
        "tests/test_matrix.py -k share_an_artifacts_path",
        "not integration",
    ),
    (
        # run_id names no episode, so two collections over the same cells mint
        # identical ids -- 6 such ids in the stored corpus, one in seven logs.
        # Three-line anchor: the same kwarg appears in execute_run's
        # assemble_record call, with parent_run_id and attempt_number reversed.
        "collection: stop the record naming which collection produced it",
        "src/bakeoff/runner.py",
        "        parent_run_id=parent_run_id,\n"
        "        attempt_number=attempt_number,\n"
        "        collection_id=collection_id,",
        "        parent_run_id=parent_run_id,\n"
        "        attempt_number=attempt_number,\n"
        '        collection_id="",',
        "tests/test_fault_injection.py -k which_collection_produced_it",
        "not integration",
    ),
    (
        # The half the record-level mutation cannot see. collection_id was
        # added to assemble_record and not to execute_run, so every caller
        # raised TypeError and 472 unit tests passed -- they drive
        # assemble_record directly. The section 6.6 gate caught it on the
        # offline smoke. Anchored on host=/adapter_patches= because the same
        # kwarg appears in the RunRecord construction.
        "collection: drop the collection id between execute_run and the record",
        "src/bakeoff/runner.py",
        "            host=host,\n"
        "            collection_id=collection_id,\n"
        "            invocation_stamp=invocation_stamp,",
        "            host=host,\n"
        '            collection_id="",\n'
        "            invocation_stamp=invocation_stamp,",
        "tests/test_fault_injection.py -k survives_the_whole_orchestrator",
        "not integration",
    ),
    (
        # RC4. The reference diff is parsed by git, not by a regex here.
        "tasks: split the reference on a boundary that drops the `a/` guard",
        "src/bakeoff/tasks.py",
        "_DIFF_HEADER = re.compile(r'^diff --git (?=a/|\"a/)')",
        "_DIFF_HEADER = re.compile(r'^diff --git ')",
        "tests/test_tasks.py -k no_prefix_reference_is_refused",
        "not integration",
    ),
    (
        "tasks: chunk the reference with splitlines, so a form feed forks a file",
        "src/bakeoff/tasks.py",
        'raw = diff.split("\\n")',
        'raw = diff.splitlines()',
        "tests/test_tasks.py -k form_feed",
        "not integration",
    ),
    (
        # A whole-diff count agrees while the zip is wrong; only per chunk does
        # a smuggled second file become visible.
        "tasks: accept a chunk git reads as more or fewer than one file",
        "src/bakeoff/tasks.py",
        "    if len(forward) != 1 or len(reverse) != 1:",
        "    if False:",
        "tests/test_tasks.py -k smuggled_second_file",
        "not integration",
    ),
    (
        "tasks: let a combined-diff header through, which git ignores in silence",
        "src/bakeoff/tasks.py",
        "        if _COMBINED_HEADER.match(stripped):",
        "        if False:",
        "tests/test_tasks.py -k combined_diff_header",
        "not integration",
    ),
    (
        "tasks: stop checking the chunking survived byte for byte",
        "src/bakeoff/tasks.py",
        '    if "".join(chunks) != diff:',
        "    if False:",
        "tests/test_tasks.py -k leading_blank_lines",
        "not integration",
    ),
    (
        # `startswith` over-claims any first segment that merely starts with
        # the prefix -- tests_helper.py, testsuite/, tests2/.
        "tasks: match test paths by string prefix instead of by path component",
        "src/bakeoff/tasks.py",
        "    return any(candidate.is_relative_to(PurePosixPath(p)) for p in prefixes)",
        "    return any(path.startswith(p) for p in prefixes)",
        "tests/test_tasks.py -k matched_by_component",
        "not integration",
    ),
    (
        "tasks: compare only the test class, so an extra-class rename is silent",
        "src/bakeoff/tasks.py",
        "            if kind_a != kind_b:",
        "            if False:",
        "tests/test_tasks.py -k rename_out_of_the_extra_class",
        "not integration",
    ),
    (
        # Schema 3.8.0. Every one of the next six used to destroy the record
        # outright or, worse, publish something false in its place.
        "finalize: let a failing finalize step take the record with it",
        "src/bakeoff/runner.py",
        "            except Exception as exc:  # noqa: BLE001 - see above\n"
        "                finalize_errors.append",
        "            except Exception:\n"
        "                raise\n"
        "                finalize_errors.append",
        "tests/test_fault_injection.py -k finalize_step_that_raises",
        "not integration",
    ),
    (
        "finalize: stop the driver seeing that finalize did not complete",
        "src/bakeoff/matrix.py",
        'if getattr(record, "finalize_error", ""):',
        "if False:",
        "tests/test_matrix.py -k finalize_failure_makes_the_row",
        "not integration",
    ),
    (
        "finalize: stop the driver seeing a dead wire log after the H1 hoist",
        "src/bakeoff/matrix.py",
        'if getattr(record, "wire_log_error", ""):',
        "if False:",
        "tests/test_matrix.py -k dead_wire_log_still_aborts",
        "not integration",
    ),
    (
        "assembly: lose the run when assemble_record itself raises",
        "src/bakeoff/runner.py",
        "    except Exception as exc:  # noqa: BLE001 - the tokens are already spent\n"
        "        record = _minimal_record(",
        "    except Exception:\n"
        "        raise\n"
        "        record = _minimal_record(",
        "tests/test_fault_injection.py -k assembly_that_raises",
        "not integration",
    ),
    (
        # `[]` beside `scanner_error: ""` is a positive spec section 7 safety
        # claim manufactured by a failure -- the pair scanner_error exists for.
        "assembly: let the minimal record manufacture a safety claim",
        "src/bakeoff/runner.py",
        "        destructive_events=events,",
        "        destructive_events=[],",
        "tests/test_fault_injection.py -k manufacture_a_safety_claim",
        "not integration",
    ),
    (
        # The per-turn diffs live only in memory and only in the record, and
        # the materialized repo is deleted after the run.
        "assembly: let the minimal record throw away the submission diff",
        "src/bakeoff/runner.py",
        "        checkpoints=checkpoints,\n        destructive_events=events,",
        "        checkpoints=[],\n        destructive_events=events,",
        "tests/test_fault_injection.py -k keeps_the_submission_diff",
        "not integration",
    ),
    (
        # A truncation splitting a multi-byte character raises
        # UnicodeDecodeError -- a ValueError -- before any line is examined.
        "wire: read the proxy log strictly and lose it to one torn character",
        "src/bakeoff/proxy_callback.py",
        'path.read_text(encoding="utf-8", errors="replace")',
        'path.read_text(encoding="utf-8")',
        "tests/test_fault_injection.py -k torn_multibyte_wire_line",
        "not integration",
    ),
    (
        "wire: let a non-object line reach the caller's .get",
        "src/bakeoff/proxy_callback.py",
        "        if not isinstance(parsed, dict):\n            malformed += 1\n            continue",
        "        if False:\n            malformed += 1\n            continue",
        "tests/test_fault_injection.py -k non_object_wire_line",
        "not integration",
    ),
    (
        # Publishing a truncated gz as the section 6.2 artifact resolves, which
        # is worse than the null it replaced.
        "wire: publish a truncated wire log as the section 6.2 artifact",
        "src/bakeoff/runner.py",
        '                                f"replay failed: {type(exc).__name__}: {exc}"\n'
        "                            )\n"
        "                            break",
        '                                ""\n'
        "                            )\n"
        "                            break",
        "tests/test_fault_injection.py -k truncated_wire_log",
        "not integration",
    ),
    (
        # Found in the post-3.7.0 regression audit. Without the guard the
        # RunContainer doubles' sampler raises AttributeError on None.stats and
        # files it, so every record those tests write carries a fabricated
        # `error` -- the one field whose job is to say sampling broke.
        'host: turn "no container to sample" into a sampling failure',
        "src/bakeoff/container.py",
        "        if self._container is None:\n"
        "            return\n"
        "        try:",
        "        if False:\n"
        "            return\n"
        "        try:",
        "tests/test_container.py -k nothing_to_sample",
        "not integration",
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
