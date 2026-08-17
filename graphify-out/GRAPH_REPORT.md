# Graph Report - /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval  (2026-08-16)

## Corpus Check
- 76 files · ~216,762 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2055 nodes · 3923 edges · 136 communities (101 shown, 35 thin omitted)
- Extraction: 76% EXTRACTED · 24% INFERRED · 0% AMBIGUOUS · INFERRED: 953 edges (avg confidence: 0.74)
- Token cost: 539,239 input · 0 output

## Community Hubs (Navigation)
- Bedrock Credential Preflight
- Failure Classification
- Wire Log Reading
- Task Loader Tests
- Runner Record Tests
- Reference Diff Partition
- In-Process Wire Capture
- Record Assembly
- Host Resource Sampling
- Pruned Mirror Cache
- Proxy Config Checks
- Fault Injection Gate
- Trajectory Parse Tests
- Append-Only Event Log
- Proxy Wire Callback
- Container Isolation Tests
- Phase 0c Smoke Test
- Per-Turn Checkpoints
- Record Finalize Guards
- Token Pricing
- LiteLLM Adapter Patches
- Matrix Scheduling And Resume
- Infra Problem Gating
- Docker Container Lifecycle
- Start State Materialization
- Smoke Criteria Tests
- Run Orchestration
- Claude Code Command Build
- Record Dataclasses
- Runner Entry Point
- Task Manifest Loading
- Dry Run And Fixtures
- Agent Subprocess Runner
- Runner Result Capture
- Preflight Discrimination Gate
- Collection Driver
- Wire-Derived Fields
- Plan Tasks And Defects
- Record Schema Enums
- Destructive And Secret Scanners
- Trajectory
- Test Claude Runner
- Test Matrix
- Test Verify Logger
- Bakeoff Eval Design
- Anthropic Stub
- Wire
- Test Fault Injection
- Images
- Test Anthropic Stub
- Model Bakeoff Plan
- Litellm Config
- Preflight
- Claude Runner
- Test Images
- Anthropic Stub
- Test Proxy Callback
- Harvesting
- Claude
- Claude
- Claude Runner
- Test Usage Accounting
- Test Container
- Test Smoke Fixture
- Test Smoke Test
- Bakeoff Eval Design
- Bakeoff Harness Logging
- Litellm Config
- Test Smoke Test
- Session
- Test Fault Injection
- Test Preflight
- Test Smoke Test
- Bakeoff Harness Logging
- Real Task Path
- Preflight
- Test Claude Runner
- Eventlog
- Test Smoke Test
- Test Litellm Patches
- Claude
- Claude
- Claude
- Todo
- Test Proxy Callback
- Claude
- Tasks
- Bakeoff Eval Design
- Test Wire
- Harvesting
- Test Litellm Patches
- Litellm Config
- Claude
- Harvesting
- Claude
- Calc
- Verify Logger
- Claude Runner
- Claude
- Claude
- Claude
- Pruned Mirror Handoff
- Anthropic Stub
- Claude
- Test Container
- Test Container
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Litellm Patches
- Test Preflight
- Test Tasks
- Pruned Mirror Handoff
- Bakeoff Eval Design
- Model Bakeoff Plan
- Pyproject

## God Nodes (most connected - your core abstractions)
1. `assemble_record()` - 82 edges
2. `EventLog` - 70 edges
3. `TaskSpec` - 53 edges
4. `load_task()` - 49 edges
5. `materialize()` - 45 edges
6. `TaskError` - 42 edges
7. `HostSampler` - 39 edges
8. `execute_run()` - 39 edges
9. `_fake_run()` - 39 edges
10. `_write_task()` - 39 edges

## Surprising Connections (you probably didn't know these)
- `assemble_record()` --shares_data_with--> `eventlog.last_run_id_for`  [INFERRED]
  bakeoff/src/bakeoff/runner.py → tasks/todo.md
- `The Sampler May Not Cost the Run (stop() is load-bearing)` --rationale_for--> `HostSampler`  [EXTRACTED]
  CLAUDE.md → bakeoff/src/bakeoff/container.py
- `input Excludes Cache Tokens on Every Route` --rationale_for--> `cost_usd()`  [EXTRACTED]
  CLAUDE.md → bakeoff/src/bakeoff/costs.py
- `LiteLLM Callbacks Must Be a CustomLogger Instance` --rationale_for--> `_request()`  [EXTRACTED]
  CLAUDE.md → bakeoff/src/bakeoff/proxy_callback.py
- `The Proxy Must Run as a Container on the Internal Network` --rationale_for--> `execute_run()`  [EXTRACTED]
  README.md → bakeoff/src/bakeoff/runner.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Wire Capture and Per-Run Attribution Flow** — claude_three_process_topology, claude_run_id_attribution, bakeoff_src_bakeoff_proxy_callback_request, bakeoff_src_bakeoff_wire_wirelogger, claude_unattributed_jsonl, tasks_todo_wire_capture_was_dead_in_the_real_topology [EXTRACTED 1.00]
- **The Record-Honesty Invariants** — claude_a_run_always_produces_a_record, claude_absence_is_recorded_never_implied, claude_a_null_says_which_kind_of_null_it_is, claude_configuration_is_never_reported_as_observation, claude_a_failure_says_why_in_the_record, claude_silence_is_the_enemy [EXTRACTED 1.00]
- **The Pruned-Mirror Cache Subsystem** — bakeoff_src_bakeoff_tasks_ensure_pruned_mirror, bakeoff_src_bakeoff_tasks_build_pruned_mirror, bakeoff_src_bakeoff_tasks_verify_pruned, bakeoff_src_bakeoff_tasks_pack_fingerprint, bakeoff_src_bakeoff_tasks_commits_outside, handoff_fingerprint_is_a_change_detector, claude_run_tree_holds_no_object_outside_base_sha [EXTRACTED 1.00]
- **Wire-capture callback registration is identical in all three proxy configs** — bakeoff_config_litellm_config_callbacks, bakeoff_config_litellm_fault_injection_sentinel_exceptions, bakeoff_config_litellm_smoke_offline_config_parity, bakeoff_src_bakeoff_proxy_callback_instance, bakeoff_src_bakeoff_litellm_patches_instance [EXTRACTED 1.00]
- **The four bedrock-mantle deployments and the naming/pricing constraint they share** — bakeoff_config_litellm_config_claude_sonnet_5, bakeoff_config_litellm_config_gemma_4_31b, bakeoff_config_litellm_config_nemotron_3_super_120b, bakeoff_config_litellm_config_kimi_k2_5, bakeoff_config_litellm_config_distinct_model_name, bakeoff_src_bakeoff_costs_price_book [EXTRACTED 1.00]
- **The three-layer task acceptance gate and its worked example** — bakeoff_taskset_harvesting_layer_1, bakeoff_taskset_harvesting_layer_2, bakeoff_taskset_harvesting_layer_3, bakeoff_src_bakeoff_tasks, bakeoff_src_bakeoff_preflight, bakeoff_taskset_click_3360_write_usage_empty_args_task_provenance [EXTRACTED 1.00]
- **The wire-capture blind spot: mandatory logging that silently captured nothing** — docs_superpowers_specs_2026_08_03_llm_bakeoff_eval_design_wire_level_logging, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_defect15_wire_capture_dead, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_defect16_no_wire_callback, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_defect19_mock_skips_streaming, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_callback_isinstance_contract, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_internal_network_topology [EXTRACTED 1.00]
- **Harness defects that read as model capability** — docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_defect17_settings_unmounted, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_defect18_root_image, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_false_success_correction, docs_superpowers_plans_2026_08_12_gate_1_real_task_path_root_cause_a, docs_superpowers_plans_2026_08_12_gate_1_real_task_path_preflight, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_macos_bind_mount_hazard [EXTRACTED 1.00]
- **Controls keeping cost comparable across arms** — docs_model_bakeoff_plan_cost_per_completed_workflow, docs_superpowers_specs_2026_08_03_llm_bakeoff_eval_design_prompt_cache_confound, docs_superpowers_specs_2026_08_03_llm_bakeoff_eval_design_cost_baseline_open_item, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_task2_cost_calculation, docs_superpowers_plans_2026_08_04_bakeoff_harness_logging_sonnet_tokenizer_asymmetry [INFERRED 0.85]

## Communities (136 total, 35 thin omitted)

### Community 0 - "Bedrock Credential Preflight"
Cohesion: 0.05
Nodes (63): check_sigv4(), derive_mantle_token(), live(), load_env_file(), main(), missing_credentials(), normalize_mantle_token(), preflight() (+55 more)

### Community 1 - "Failure Classification"
Cohesion: 0.05
Nodes (67): AUTH_ERROR_SIGNATURES, classify_exclusion(), classify_failure(), Failure and exclusion classification. See spec section 6.4.  Exclusion is the on, RunSignals, _DERIVED_PATHS, The reason this scans the trailing block rather than the last message.      Meas, RouterRateLimitError carries no status_code, so a run that ends on one     has ` (+59 more)

### Community 2 - "Wire Log Reading"
Cohesion: 0.07
Nodes (54): _parse_run_entries(), Path, (entries, unreadable line count) for one run's proxy wire log.      Tolerant of, Entries the proxy recorded for one run, in call order.      Split from `unreadab, Lines of this run's wire log that could not be turned into an entry.      Feeds, Calls the proxy could not attribute to a run. Any non-zero value is a     gate f, read_run_entries(), unattributed_count() (+46 more)

### Community 3 - "Task Loader Tests"
Cohesion: 0.06
Nodes (55): _assert_flock_held(), _borrow_the_future(), _dest_is_a_file(), _manifest(), _marker_is_a_directory(), _pack_a_latin1_ref(), _plant_a_real_keep(), Path (+47 more)

### Community 4 - "Runner Record Tests"
Cohesion: 0.05
Nodes (48): _cache_record(), _cache_trajectory(), _checkpoint(), _deletion(), A transcript whose per-turn cache_read is exactly `reads`.      Shaped like the, Turn 1 cannot read what this run wrote, so a hit there is carryover     from an, The distinction the field exists for, and the one that is easy to get     wrong, The regression guard on the test above it: narrowing `false` must not     swallo (+40 more)

### Community 5 - "Reference Diff Partition"
Cohesion: 0.06
Nodes (48): _chunk_path(), diff_chunks(), _numstat(), Split a git diff into one text per file, in diff order.      Texts only -- paths, Paths git reports for one chunk, from git's own header parser.      `-z` gives r, (source, destination) for one chunk, as git names them.      EXACTLY ONE ENTRY E, Partition the merged PR by path.      Returns (test_diff, solution_diff, test_fi, A task that cannot be loaded, materialized or trusted.      One exception type o (+40 more)

### Community 6 - "In-Process Wire Capture"
Cohesion: 0.07
Nodes (40): BakeoffCallback, CustomLogger, Path, Wire-level request/response capture. See spec section 6.2.  Without the raw comp, LiteLLM callback hook. Registered via litellm.callbacks.      LiteLLM invokes lo, WireLogger, test_malformed_completion_survives_in_wire_log(), proxy_callback and wire.py must not drift apart.      A run's canonical artifact (+32 more)

### Community 7 - "Record Assembly"
Cohesion: 0.05
Nodes (44): assemble_record(), final_api_error_status(), The HTTP status a run ENDED on, or None.      Only the last call counts. `genera, 429 and 5xx are separate pre-registered reasons. Collapsing them     would hide, The chain on the shape today's arms produce: tool_calls through the run,     `st, The whole chain, on the shape a real expiry produces: the verbatim     litellm 1, The sequence a real expiry actually produces, end to end.      One 401 cools the, terminal_error_messages is empty when the last call SUCCEEDED, the same     bar (+36 more)

### Community 8 - "Host Resource Sampling"
Cohesion: 0.06
Nodes (29): HostSampler, Any, Container CPU and memory for the life of a run, on its own thread.      A held `, Container CPU percent for one frame, or None if it is not a         measurement., Never raises. `Thread.start()` raises RuntimeError when the OS         refuses a, Idempotent, and it MUST NOT RAISE. Called from three places: right         after, Nearest-rank p95, or None on an empty series -- never 0.0., Measured against docker SDK 7.2.0 / daemon 29.5.2: the FIRST frame of a     stat (+21 more)

### Community 9 - "Pruned Mirror Cache"
Cohesion: 0.08
Nodes (38): _ancestors(), _build_pruned_mirror(), _commits_outside(), ensure_mirror(), ensure_pruned_mirror(), _FORBIDDEN_PATHS, _git(), mirror_path() (+30 more)

### Community 10 - "Proxy Config Checks"
Cohesion: 0.07
Nodes (37): _model_list(), Checks on the LiteLLM proxy config that need no credentials.  Routing, auth, and, Spec section 6.2 makes wire logging mandatory, and the proxy is the     only pro, Three configs start a proxy, and a patch missing from one of them means     the, Measured, litellm 1.95.0: `_should_cooldown_deployment` has four     branches, a, The assumption that makes the tool-id patch safe, written down.      `^[a-zA-Z0-, The other half of the assumption above, for the transport Anthropic's     own AP, The trap this config comment has always warned about, asserted.      get_instanc (+29 more)

### Community 11 - "Fault Injection Gate"
Cohesion: 0.10
Nodes (35): _assistant(), Spec section 6.6 gate. Twelve fault cases, each of which must produce a complete, The finding-4 guard.      litellm.callbacks registered in the harness process ob, Section 6.6 case 2, through the whole path.      mock_response "litellm.RateLimi, `destructive_events: []` beside `scanner_error: ""` is a positive spec     secti, The assertion this file's FIRST rule exists for.      Through schema 2.0.0 `cach, A tool_use block whose input is a string rather than an object is the     shape, tool_calls.malformed is 0 on every harness-written record BY DESIGN.      Decidi (+27 more)

### Community 12 - "Trajectory Parse Tests"
Cohesion: 0.07
Nodes (27): _assistant(), Path, `_usage_from` runs outside the pricing guard, so anything it raises     aborts t, cost_usd charges cache_write once and reads the 1h tier out of it, so a     tota, Keyed on message.id globally, not on the previous record, so anything     interl, Spec section 6.6 fault-injection gate: per-turn records must sum     exactly to, Every turn is priced here because claude-sonnet-5 has confirmed cache     pricin, The transcript parsed. Only the price is missing.      Before this split, cost_u (+19 more)

### Community 13 - "Append-Only Event Log"
Cohesion: 0.11
Nodes (30): EventLog, `(run_id, started_at)` of the run that plausibly warmed the cache.          `sta, The most recently written run of this task on this model, or None.          Answ, make_record(), Absent evidence is not evidence of a quiet run. Pre-3.0.0 index lines     carry, execute_run calls this OUTSIDE its try. Anything raised here escapes as     an e, CacheState.seconds_since_prior_run is computed from this. It rides on     the sa, execute_run calls this outside its try, before the container starts.     Raising (+22 more)

### Community 14 - "Proxy Wire Callback"
Cohesion: 0.10
Nodes (27): BakeoffProxyCallback, _error(), _headers(), _iso(), project(), Any, CustomLogger, Wire capture from inside the LiteLLM proxy. See spec section 6.2.  WHY THIS EXIS (+19 more)

### Community 15 - "Container Isolation Tests"
Cohesion: 0.06
Nodes (28): Checkpoints are now taken WHILE the agent works, so staging must not     touch t, A failed snapshot must not be indistinguishable from a clean tree.      git repo, Spec section 5.1's second branch: network off, OR through a recording     proxy., `isolated` was `bool(network)` -- the argument the caller passed, not a     prop, The finding this exists to make. A non-internal network gives the agent     a ro, network_mode=none is trivially unroutable and deliberately NOT isolated:     sec, A registry digest travels between machines; a bare image ID pins the     image c, `any([])` is False, so gating this on frames rather than on peers would     asse (+20 more)

### Community 16 - "Phase 0c Smoke Test"
Cohesion: 0.10
Nodes (32): The smoke task directory is a template, not a git repository, assert_agent_can_verify_its_work(), build_images(), build_smoke_repo(), Expectations, main(), print_cache_tokens(), print_cost() (+24 more)

### Community 17 - "Per-Turn Checkpoints"
Cohesion: 0.10
Nodes (22): CheckpointRecorder, Per-turn diff capture. See spec section 5.5.  The agent run stores diffs only. R, Snapshot this turn if it is due. NEVER raises into the caller.          This run, The FINAL snapshot, and it is allowed to raise.          Deliberately different, SupportsSnapshot, ExplodingContainer, FakeContainer, Containment alone would have swapped a loud wrong record for a quiet     one. A (+14 more)

### Community 18 - "Record Finalize Guards"
Cohesion: 0.06
Nodes (33): _fake_run(), The empty case has to stay empty, or `crash_error` becomes noise that     reader, 0 is the code of a clean exit. A run whose agent never ran must not     claim on, What the code claimed and did not do. `gzip.open(path, "xt")` raising     inside, The assertion this file's first rule exists for, and it earned its place     imm, is honest and distinguishable from a named episode. It is also every     record, The whole point of the phase. A failing step is named, not fatal., Per step, not one try around the block.      A single guard would let a stdout f (+25 more)

### Community 19 - "Token Pricing"
Cohesion: 0.12
Nodes (29): cost_usd(), ModelPricing, Token usage to USD (spec section 8).  TWO BASES, ON PURPOSE, AND THE RECORD SAYS, UnknownModelError, Raw `message.usage`, projected. `input` EXCLUDES the cache fields.      Verified, TokenUsage, 2.00x against the 5m tier's 1.25x, verified against the AWS Bedrock     prompt-c, LiteLLM's Converse bridge drops Bedrock's cacheDetails, so candidate     writes (+21 more)

### Community 20 - "LiteLLM Adapter Patches"
Cohesion: 0.10
Nodes (27): apply(), _apply_collision_uniquify(), _apply_openai_param_pins(), BakeoffAdapterPatches, _passthrough_tool_use_id(), Any, CustomLogger, Path (+19 more)

### Community 21 - "Matrix Scheduling And Resume"
Cohesion: 0.10
Nodes (29): Cell, matrix_order(), plan_resume(), Split the order into already-written and still-to-run, and object.      `version, One record's worth of work., The (task, model, sample) sequence to execute, in order.      ROUND-MAJOR, shuff, make_run_id(), Resume is mandatory rather than convenient: `write_run` opens mode "x"     and ` (+21 more)

### Community 22 - "Infra Problem Gating"
Cohesion: 0.12
Nodes (28): infra_problems(), Any, Reasons this record cannot be interpreted -- never reasons it is bad.      Delib, Scheduling, resume, and what the driver is allowed to gate on.  The ordering cas, The line between this and `smoke_test.run_problems`, which fails a run     that, Each of these makes a row unreadable rather than negative. The     settings-file, ExclusionClass carries infra, adapter and task-defect and has no     MODEL_FAILU, The status-agnostic backstop, and the only check here that does not     depend o (+20 more)

### Community 23 - "Docker Container Lifecycle"
Cohesion: 0.11
Nodes (18): ContainerError, ExecResult, RuntimeError, Docker lifecycle with digest pinning and git state control.  Spec section 5.1: i, Run a command, delivering stdout lines as they arrive.          The agent's turn, Run a command that must succeed, or say so.          git writes failures ("not a, Stage everything, then diff against base. Staging first captures         untrack, Overwrite paths with their base_sha contents. Used to restore         test files (+10 more)

### Community 24 - "Start State Materialization"
Cohesion: 0.09
Nodes (28): materialize(), pruned_mirror_path(), Build the run's start state on disk and return its commit SHA.      `dest` must, Keyed on base_sha as well as the URL.      Two tasks on one repository at differ, `--dissociate` appears on BOTH clones, and only this test covers the     second, Two states that were permanent, both measured, both the failure class the     pr, A pruned mirror that cannot be shown to be pruned must be REBUILT, not     refus, A fixed author, committer, date and message make the setup commit a     pure fun (+20 more)

### Community 25 - "Smoke Criteria Tests"
Cohesion: 0.14
Nodes (25): _live_problems(), Spec Phase 0c -- the parts of the smoke test that are checkable offline.  The sm, Minimal stand-in shaped like the fields the criteria read., A record whose transcript could not be parsed has every derived field     empty., A cache-token guard trip means the price is unknown, not that the run     failed, The one case where an unknown price IS fatal. Tokens are what make a     run rep, Spec section 6.2 makes wire logging mandatory, and an isolated run     with an e, Entries in unattributed.jsonl belong to a run nobody can name. The     per-run f (+17 more)

### Community 26 - "Run Orchestration"
Cohesion: 0.14
Nodes (24): Proxy, `bakeoff.proxy.Proxy` bound to this script's image tag and repo root.      The c, ClaudeCodeConfig, execute_run(), Append the record, and if that fails leave it on disk instead of losing it., Run one sample and write exactly one record.      `network` names an internal Do, TaskSpec, _write_or_strand() (+16 more)

### Community 27 - "Claude Code Command Build"
Cohesion: 0.14
Nodes (25): build_command(), make_config(), The allowlist must not be so tight that `claude` cannot be resolved     or run., Docker merges this with the image's own environment.      Forwarding the host PA, Spec section 5.3 freezes sampling at each model's lab-recommended     setting, a, `custom_headers` carries the run_id, so it differs BY CONSTRUCTION on     every, The other half: excluded from the digest, but present in the     environment. Dr, Spec section 5.2: MCP servers change agent behavior and must not     leak into e (+17 more)

### Community 28 - "Record Dataclasses"
Cohesion: 0.12
Nodes (22): _build(), CacheState, Exclusion, Any, Whether this run started against a cache someone else had already     warmed, an, Construct a nested record class, ignoring fields it does not know.      `RunReco, RunRecord, TestResult (+14 more)

### Community 29 - "Runner Entry Point"
Cohesion: 0.11
Nodes (25): _artifacts_block(), _cache_warm(), _elapsed_seconds(), harness_commit(), litellm_version(), _minimal_record(), Path, Execute one run end to end and assemble its record.  Ordering matters: the recor (+17 more)

### Community 30 - "Task Manifest Loading"
Cohesion: 0.11
Nodes (25): load_task(), load_task_set(), Any, A task on disk: manifest, reference diff, and the start state they define.  Spec, Whether `path` sits under any of `prefixes`.      `PurePosixPath.is_relative_to`, Commit of the task-set repository, `-dirty` when the SET is modified.      Scope, Read and validate one task directory.      Raises TaskError on anything that wou, Every task under `root`, validated, in a stable order.      Duplicate task_ids r (+17 more)

### Community 31 - "Dry Run And Fixtures"
Cohesion: 0.12
Nodes (22): build_agent_image(), build_repo(), main(), Path, sh(), agent_container(), agent_image(), alpine_container() (+14 more)

### Community 32 - "Agent Subprocess Runner"
Cohesion: 0.14
Nodes (20): ClaudeCodeRunner, ContainerBackend, Any, Run the agent inside the pinned container (spec section 5.1).      The container, A count, not a log line. `RunnerResult.turns_streamed` is a floor rather     tha, test_the_runner_reports_how_many_stdout_lines_it_could_not_read(), _config(), Live-capture tests. These need a Docker daemon.  The point of the suite is one c (+12 more)

### Community 33 - "Runner Result Capture"
Cohesion: 0.10
Nodes (19): RunnerResult, FakeRunner, Path, Stands in for ClaudeCodeRunner. Fires `turns` boundaries, then either     return, `except Exception: crashed = True` kept no type, no message, no     traceback, s, It was captured all along and thrown away here, so     `Artifacts.container_stde, A CLI that exited non-zero but still wrote a transcript was     byte-identical i, The stream-json reader treated "not an assistant event" and "not JSON"     alike (+11 more)

### Community 34 - "Preflight Discrimination Gate"
Cohesion: 0.14
Nodes (21): agent_image(), Path, The gate that decides whether a task is a task.  Two halves. The unit half pins, A task built from the smoke fixture, so preflight can be run for real     withou, A reference diff: the oracle half always, the fix half optionally.      `fix_sou, The real eval image. Built here rather than assumed present: the whole     point, Green before the fix means the arm is scored on work it did not do., The other half, and the one that catches a non-editable install: if     the refe (+13 more)

### Community 35 - "Collection Driver"
Cohesion: 0.16
Nodes (18): artifacts_root(), base_claude_version(), main(), Path, Build each task's image, materialize its start state, and preflight.      Return, Per-invocation, like the wire directory beside it.      `run_cell` rmtree'd a ce, resolve_tasks(), run_cell() (+10 more)

### Community 36 - "Wire-Derived Fields"
Cohesion: 0.12
Nodes (20): distinct_wire_calls(), finish_reasons(), Any, Digest of a request field, or "" when the field was not there.      The None gua, The messages of the failure a run ENDED on: every failed call from the     last, The statuses of the same trailing block `terminal_error_messages` reads.      A, Logical provider calls, as opposed to callback invocations.      They are not th, How the provider said generation stopped, counted over WIRE ENTRIES.      Entrie (+12 more)

### Community 37 - "Plan Tasks And Defects"
Cohesion: 0.12
Nodes (20): CLAUDE_CONFIG_DIR, not --settings, detaches user config, Defect 12: api_error_status never populated, Defect 13: version block 4/6 empty on every record, Environment is an allowlist, not a denylist, Internal Docker network with the proxy as a peer, Task 10: Run orchestrator (execute_run), Task 1: Schema and append-only event log, Task 8: Failure and exclusion classification (+12 more)

### Community 38 - "Record Schema Enums"
Cohesion: 0.19
Nodes (16): DestructiveCategory, ExclusionClass, FailureClass, Outcome, Run-record dataclasses. See spec section 6.1.  Every field here is a raw observa, See spec section 6.4. MODEL_FAILURE is never an exclusion — it is     the thing, Spec OPEN-10 resolution.      HIGH   unreverted data or history loss, or secret, Severity (+8 more)

### Community 39 - "Destructive And Secret Scanners"
Cohesion: 0.18
Nodes (17): _is_ignorable(), _paths_from(), Destructive-command and secret scanning. See spec sections 6.1 and 6.2.  Severit, scan_destructive(), scan_secrets(), Characterization test for a known gap, not an endorsement of it.      Revert det, test_detects_dependency_downgrade(), test_detects_force_push() (+9 more)

### Community 40 - "Trajectory"
Cohesion: 0.15
Nodes (18): Tool calls the AGENT made. `api_calls_failed` is the one exception., One API call. Not one transcript record -- see `api_message_id`., ToolCallStats, TurnRecord, _elapsed_ms(), _is_tool_result(), parse_trajectory(), _parse_ts() (+10 more)

### Community 41 - "Test Claude Runner"
Cohesion: 0.13
Nodes (11): BackendResult, HostBackend, Run the agent as a subprocess of the harness.      Retained for tests and for ma, Feeds canned stdout lines through the runner's streaming path., An assistant message announces the tool calls a turn is ABOUT to     make, so at, stdout carries system, user and result events, blank lines, and a     possibly-p, Exercises the actual streaming machinery, not just the replay stub., ReplayBackend (+3 more)

### Community 42 - "Test Matrix"
Cohesion: 0.12
Nodes (15): Consecutive uninterpretable cells, globally and per arm.      Two counters and n, Note one cell's outcome. "" to continue, or why to stop., StreakTracker, Section 5.7 requires round-major interleaving, and that is exactly what     defe, Only a run of unreadable rows means something. An isolated bad cell     among go, A dead proxy or a fully expired session kills every arm at once, and     that mu, The two fields plan_resume reads off a stored record., _Rec (+7 more)

### Community 43 - "Test Verify Logger"
Cohesion: 0.14
Nodes (16): _fake_runner(), Tests for the section 6.6 gate itself.  The gate is the deliverable an operator, A bare `python` resolves to whatever is first on PATH, which on this     machine, tests/conftest.py documents this: the Docker VM on macOS mounts $HOME     but no, test_fault_injection.py lives under tests/ and is already collected.     Naming, subprocess.run stand-in: fails only the command containing `failing`., The load-bearing case.      Container-killed-mid-run, the proxy-side wire log an, Without a daemon the container commands must not be executed at all.     Running (+8 more)

### Community 44 - "Bakeoff Eval Design"
Cohesion: 0.13
Nodes (18): Task 3: Tolerant trajectory parser, Timing correction: inference_ms vs tool_exec_ms must partition the span, Start state is base_sha plus the committed test half, A damaged prune cache rebuilds rather than raises, _commits_outside streams one past its sample, _git decodes pinned utf-8 with replacement, The base_sha guard exists for a NAMED failure, not a vacuous pass, Nine deterministic checks (resolved gate) (+10 more)

### Community 45 - "Anthropic Stub"
Cohesion: 0.13
Nodes (12): Handler, non_streaming(), Claude Code also issues a non-streaming call. Answered plainly so it     is one, Reject an openai-route request that still spells the cap ``max_tokens``.      St, Reject an openai-route request carrying tools without an explicit 'none'.      S, Reject a request whose tool schemas still carry ``propertyNames``.      The 2026, Reject a conversation that is not advancing through the script.      The openai, validate_loop_progress() (+4 more)

### Community 46 - "Wire"
Cohesion: 0.15
Nodes (10): What the CLIENT sent. Claude Code's own body, with no fallback.      Verified ag, _request(), Any, HTTP status of a failed call.          This is the only place the harness ever s, Fold one call into the canonical artifact.          `logged_at` is the ORIGINAL, Internal Docker Network Isolation, Three Processes, Not One, The Wire Log Records the Params That Get Dropped (+2 more)

### Community 47 - "Test Fault Injection"
Cohesion: 0.12
Nodes (10): FakeContainer, Stands in for RunContainer. `fail_snapshot_after` models a disk     filling up m, Not merely "a record exists" -- it must round-trip through the event     log and, A snapshot that raises on turn 2 must not discard turn 1.      Those checkpoints, Measured 2026-08-12, once in seven offline arms.      `maybe_capture` is called, Containment alone would swap a loud wrong record for a quiet one.      The secti, test_a_checkpoint_gap_is_named_rather_than_left_looking_like_an_idle_turn(), test_a_mid_run_snapshot_failure_does_not_crash_a_working_run() (+2 more)

### Community 48 - "Images"
Cohesion: 0.23
Nodes (15): build_base_image(), build_proxy_image(), build_task_image(), image_entrypoint(), image_id(), ImageError, Path, RuntimeError (+7 more)

### Community 49 - "Test Anthropic Stub"
Cohesion: 0.12
Nodes (13): The offline gate's stub validators, tested directly.  The stub is the only thing, The shape a HALF-applied pin produces, and the reason the wrapper     assigns un, The real constraint is tools-only. Widening it would fail requests     Bedrock a, Import the fixture by path.      `fixtures/` is not a package and is bind-mounte, What Bedrock's /openai/v1 route answers 400 to, and what took gemma to     0/3 o, `additional_drop_params: ["max_tokens"]` is the tempting one-liner, and     meas, The exact shape `additional_drop_params: ["reasoning_effort"]` produced.      Ge, _stub() (+5 more)

### Community 50 - "Model Bakeoff Plan"
Cohesion: 0.16
Nodes (16): bedrock-mantle endpoint, Claude Sonnet 5 (incumbent reference), Cost-Efficient LLM Bakeoff for Agentic Coding, Cost per completed workflow, Gemma 4 31B, Kimi K2.5, Self-hosted LiteLLM proxy as the access layer, Nemotron 3 Super 120B (+8 more)

### Community 51 - "Litellm Config"
Cohesion: 0.16
Nodes (15): BAKEOFF_MANTLE_TOKEN env var name, bedrock-mantle transport, bedrock-runtime transport, claude-sonnet-5 deployment (mantle), claude-sonnet-5-runtime deployment, gemma-4-31b deployment (mantle), kimi-k2-5 deployment (mantle), kimi-k2-5-runtime deployment (commented out) (+7 more)

### Community 52 - "Preflight"
Cohesion: 0.19
Nodes (10): failed_node_ids(), preflight(), Path, Test invocations inside the container, always under a timeout.      `RunContaine, The p2p set: whatever the manifest declared, or everything else.          Both b, Every reason this task is not a task. Empty `problems` means GO.      Problems a, Node ids pytest reported as FAILED or ERROR, from `-q` output.      Parsed from, _Runner (+2 more)

### Community 53 - "Claude Runner"
Cohesion: 0.18
Nodes (11): config_digest(), container_env(), _eval_env(), Drive Claude Code as a subprocess under a controlled configuration.  Spec sectio, The variables the harness sets deliberately., Environment for an in-container exec: the eval's own keys only.      The host al, Digest of everything that must be identical across arms.      Deliberately exclu, The digest is stored as proof the arms ran identically, but every     behavior k (+3 more)

### Community 54 - "Test Images"
Cohesion: 0.23
Nodes (12): _lines(), The generated task Dockerfile.  Tested as a string, without a daemon, on purpose, The degenerate manifest -- no apt, no pip, no build -- must still     produce so, The base pins pytest so the reference fixture has a runner; a real     repositor, `pip install -e .` has to resolve against /repo specifically: the bind     mount, A hand-edited copy is how the two structural guarantees come back., test_a_task_with_no_dependencies_is_still_a_valid_image(), test_build_commands_run_against_the_scaffold_repo() (+4 more)

### Community 55 - "Anthropic Stub"
Cohesion: 0.26
Nodes (11): openai_chunks(), _openai_tool_call(), How many tool results have come back so far.      Counted from the conversation, Read, then Write, then stop.      The Read is not decoration: Claude Code refuse, One tool_use block, streamed the way Anthropic streams one.      input_json_delt, Read, then Write, then stop -- the same shape as the Anthropic script.      The, script(), sse() (+3 more)

### Community 56 - "Test Proxy Callback"
Cohesion: 0.20
Nodes (12): open_resolved_capture(), Name the call whose resolved params are about to be produced.      Called from t, File what the provider is actually being sent, under the call it is for.      A, record_resolved_params(), The only place that knows what the provider is being sent.      Measured 2026-08, test_the_param_mapping_hands_what_it_produced_to_the_wire_capture(), What the provider was actually sent, when something could see it.      The chann, The failure this guard exists for is misattribution, not a gap.      A ContextVa (+4 more)

### Community 57 - "Harvesting"
Cohesion: 0.17
Nodes (12): click-3360 f2p test set, click-3360 verbatim issue prompt, click-3360 repo pinning (base_sha / start_sha), base_sha is merge_commit^1, Tests assert behaviour, not internal names, Three silent task-image build failures, Layer 1 - refused by code, Layer 2 - required, and nothing checks it (+4 more)

### Community 58 - "Claude"
Cohesion: 0.20
Nodes (12): A Run Always Produces a Record, Absence is Recorded, Never Implied, The Event Log is Append-Only, Bakeoff Eval Harness, Docstrings Carry the Why and the Failure Mode, The Sampler May Not Cost the Run (stop() is load-bearing), Immutable Event Log (primary deliverable), X-Bakeoff-Run-Id Attribution (+4 more)

### Community 59 - "Claude"
Cohesion: 0.20
Nodes (12): LiteLLM Callbacks Must Be a CustomLogger Instance, Gemma's /openai/v1 Reasoning-Model Contract, litellm_patches Must Never Be Imported From bakeoff/, OpenAIConfig is Not an OpenAIGPTConfig (patch the outer entry), The Resolved-Params Channel is Two Hops (ContextVar then keyed dict), Eight of Eight Model Failures Were Harness Defects, Gemma Runs Without top_p — a §5.3 Divergence to Publish, The Request Carries system Twice, First Copy After the User Message (+4 more)

### Community 60 - "Claude Runner"
Cohesion: 0.20
Nodes (8): Path, Execute one agent run.          on_turn(completed_turns, elapsed_ms) fires at ea, Locate this run's transcript under <config_dir>/projects/.          Globbing rat, A run that produced no transcript must report None.      Selecting the newest *., Claude Code hyphenates dots as well as separators when munging cwd     into a pr, test_missing_config_dir_reports_no_transcript(), test_transcript_found_regardless_of_path_punctuation(), test_transcript_is_never_taken_from_another_run()

### Community 61 - "Test Usage Accounting"
Cohesion: 0.22
Nodes (10): _int(), A token count, or 0 for anything that is not one.      `null` is the case that m, Project `message.usage`. Absent fields are 0 -- see the caller's guard.      Zer, _usage_from(), `input` must exclude the cache tokens, on every route into the record.  `costs.c, Bedrock Converse to openai to anthropic, through litellm's own code.      Both h, The arithmetic the two tests above exist to protect., test_native_anthropic_usage_reports_input_exclusive_of_cache() (+2 more)

### Community 62 - "Test Container"
Cohesion: 0.22
Nodes (9): _AddSequence, _container_with(), A `git add -A` that fails the way the real race does, N times., Checkpoints are captured WHILE the agent edits, and Claude Code's     Write is a, The window is a single rename, so a failure that survives every     attempt is a, Retrying "not a git repository" three times buys nothing and delays     the repo, test_a_lost_race_against_the_agents_atomic_write_is_retried(), test_a_stat_failure_that_is_not_a_race_still_raises() (+1 more)

### Community 63 - "Test Smoke Fixture"
Cohesion: 0.22
Nodes (10): CompletedProcess, Path, _pytest_in(), The smoke fixture has to be red before the fix and green after it.  This is the, A throwaway copy, so a failed assertion cannot leave the real fixture     holdin, `pytest.ini` is load-bearing, not tidiness.      Without `pythonpath = .` pytest, smoke_repo(), test_fixture_fails_with_the_bug_present() (+2 more)

### Community 64 - "Test Smoke Test"
Cohesion: 0.18
Nodes (4): _CapturingContainer, Records the kwargs RunContainer was constructed with., If the seeded bug were ever fixed in the template, every arm would     pass the, test_the_fixture_test_fails_against_the_seeded_bug()

### Community 65 - "Bakeoff Eval Design"
Cohesion: 0.20
Nodes (11): Plan-doc success criteria and thresholds, Sonnet 5 tokenizer asymmetry in uniform token budgets, Temperature is per-model and excluded from config_digest, Budget policy: generous caps, measure actuals (§5.4), Calibration pilot and model-neutral drop rule, Model-neutrality rule, Reference points, not gates (OPEN-8), Frozen sampling parameters (§5.3) (+3 more)

### Community 66 - "Bakeoff Harness Logging"
Cohesion: 0.18
Nodes (11): LiteLLM callbacks must be CustomLogger instances, Defect 15: wire capture registered in the wrong process, Defect 16: production proxy config registered no wire callback, Bakeoff Harness & Logging Implementation Plan, Task 4: Destructive-command and secret scanners, Task 7: Wire-level logging and litellm config, Gate 1 — the real-task input path, OPEN-10 destructive-event severity scale (+3 more)

### Community 67 - "Litellm Config"
Cohesion: 0.27
Nodes (10): additional_drop_params (named one at a time), litellm_settings.callbacks registration (production), mock-5xx deployment (500 -> api_5xx), mock-ok deployment, mock-throttle deployment (429 -> api_throttle), mock_response sentinel exceptions, Offline gate must mirror production settings, mock_response short-circuits the streaming callback (+2 more)

### Community 68 - "Test Smoke Test"
Cohesion: 0.24
Nodes (10): adjacent_repeats(), The (arm, repeat) sequence to execute, in order.      ARM-MAJOR IS THE DEFAULT,, Arms that appear in two consecutive positions of the run order., run_order(), The default, and the ordering every cost figure in TASKS.md came from.     Named, Section 5.8's control: repeats of the same arm must never run     back-to-back,, One arm has nothing to interleave with, so the check must report the     truth r, test_arm_major_ordering_runs_every_arm_back_to_back_with_itself() (+2 more)

### Community 69 - "Session"
Cohesion: 0.20
Nodes (9): config_differences(), config_problems(), What a Claude Code session actually loaded. See spec section 5.2.  Extracted fro, Section 5.2 checks against the dump.      An empty dump is a failure, never a pa, Section 5.2's diff half: identical across arms except the model.      Two arms c, The load-bearing assertion of the whole settings-mount fix.      permissionMode, --strict-mcp-config with an empty server list is a section 5.2     requirement., test_config_check_rejects_a_session_that_did_not_load_the_settings() (+1 more)

### Community 70 - "Test Fault Injection"
Cohesion: 0.27
Nodes (10): _entry(), A truncation splitting a multi-byte character raises UnicodeDecodeError     -- a, `null` is valid JSON and raises AttributeError on `.get`., The 3.6.0 defect's second door.      A WireLogger that cannot open used to skip, Containment alone would ship a lie.      `artifacts.wire_log_gz` is published on, test_a_non_object_wire_line_does_not_cost_the_record(), test_a_replay_failure_does_not_publish_a_truncated_wire_log(), test_a_torn_multibyte_wire_line_does_not_cost_the_record() (+2 more)

### Community 71 - "Test Preflight"
Cohesion: 0.22
Nodes (7): Captures the argv preflight would run, without a container., The honest default. An enumerated copy of a pinned suite goes stale     for no b, Otherwise `tests.p2p` is a manifest key that reads like a measurement     and is, _Recorder, test_a_declared_p2p_set_is_actually_used(), test_an_undeclared_p2p_set_means_everything_except_f2p(), _Tests

### Community 72 - "Test Smoke Test"
Cohesion: 0.20
Nodes (10): captured_mounts(), --settings names a container path. Without a mount it names nothing.      Claude, The dry run and the fault-injection suite pass no settings file --     their sta, The transcript is discovered through the config-dir mount. Replacing     extra_m, A path recorded for a file that is not there would read as evidence     that was, Run execute_run against a fake container, return its extra_mounts., test_an_empty_stream_leaves_no_stdout_artifact(), test_no_settings_mount_is_requested_when_none_is_given() (+2 more)

### Community 73 - "Bakeoff Harness Logging"
Cohesion: 0.20
Nodes (10): Checkpoint off-by-one: report turns completed, not announced, Defect 14: checkpoints discarded on mid-run failure, Defect 17: eval_settings.json was mounted nowhere, Defect 18: the image ran as root, FALSE_SUCCESS correction: tests_passed is None, not False, macOS bind-mount hazard (empty mount reads as clean tree), Task 5: Container lifecycle and git pinning, Task 6: Live checkpoint capture (+2 more)

### Community 74 - "Real Task Path"
Cohesion: 0.24
Nodes (10): Defect 19: mock deployments never exercise the streaming path, Task 11: Fault-injection gate (verify_logger.py), Task 12: End-to-end smoke test (Phase 0c), Matrix driver: round-major ordering, resume, containment, Preflight: the per-task discrimination precondition, Root cause A: no precondition on the harness INPUT, Root cause B: operational knowledge lived in a script, Randomized, interleaved execution order (§5.7) (+2 more)

### Community 75 - "Preflight"
Cohesion: 0.25
Nodes (6): The agent must be able to run the test and see it pass, Phase 0c smoke task fixture, _explain(), PreflightResult, Refuse to spend money on a task that cannot be shown to be a task.  This is the, click-3360 image block (less, pytest 8.3.5, editable install)

### Community 76 - "Test Claude Runner"
Cohesion: 0.22
Nodes (8): build_env(), Environment for a host subprocess: allowlist plus the eval's own keys., The parent environment is not a safe base to start from.      A harness run is i, Spec section 6.2 makes wire logging mandatory, and the wire log only     exists, CLAUDE_CONFIG_DIR must be SET, not stripped.      `--settings` loads *additional, test_env_cannot_silently_bypass_the_proxy(), test_env_does_not_leak_ambient_claude_variables(), test_env_isolates_config_dir_from_operator_setup()

### Community 77 - "Eventlog"
Cohesion: 0.25
Nodes (5): ImmutabilityError, Path, RuntimeError, Append-only, immutable run log. See spec section 6.  There is deliberately no up, Raised on any attempt to overwrite an existing run record.

### Community 78 - "Test Smoke Test"
Cohesion: 0.22
Nodes (9): effective_config(), Path, The stream-json init event, or {} if there is not one.      Read from the agent', The init event is emitted on stdout and nowhere else.      Verified against clau, Spec section 5.2 requires dumping and diffing the effective config at     sessio, An absent dump must read as absent. Defaulting to the values that     were REQUE, test_a_transcript_with_no_init_event_yields_nothing_rather_than_a_guess(), test_effective_config_is_read_from_the_init_event() (+1 more)

### Community 79 - "Test Litellm Patches"
Cohesion: 0.22
Nodes (3): The proxy-side adapter interventions, and the containment that keeps them there., A route that never reached the pre-request hook records nothing rather     than, test_a_mapping_with_no_capture_open_is_a_no_op()

### Community 80 - "Claude"
Cohesion: 0.28
Nodes (9): artifacts.* paths are existence- AND ownership-checked, mock_response Deployments Short-Circuit Before the Streaming Wrapper, Mutation Check (revert each guarantee, confirm a test goes red), run_id is Unique Within a Collection, Not Across One, §6.6 Logger Gate (verify_logger.py), The Mutation Harness Read Its Own Stale Bytecode, A Reused Path Makes a Stale Artifact Read as a Current Measurement, A Parameter Nobody Passes is Invisible to a Unit Test (+1 more)

### Community 81 - "Claude"
Cohesion: 0.29
Nodes (8): force_capture, maybe_capture, Checkpoint Capture Must Never Cost the Run It Observes, One API Call is One Turn (Claude Code writes one record per content block), Checkpoint.turn and turns_used Count Different Things, Reprice and Recount the Archive Under 3.0.0, Checkpoints Are Captured Live, Not Post-Hoc, Every Token and Dollar in the Log Was Roughly Doubled (schema 3.0.0)

### Community 82 - "Claude"
Cohesion: 0.29
Nodes (8): The Agent Image Runs as Non-Root, The Agent Must Be Able to Check Its Own Work, Per-Task Discrimination Preflight (red before, green after), pytest exit 1 vs 2/4/5 (tests failed vs environment broken), Stale .pyc Corrupts the Self-Correction Loop, Reference Agent Image Pins claude 2.1.220 and Fails the Build on Drift, The pyc Caveat, Keyed on container_image_digest Not the Date, The Agent Could Not Run Tests Through All of Phase 0c

### Community 83 - "Todo"
Cohesion: 0.38
Nodes (7): eventlog.last_run_id_for, A Null Says Which Kind of Null It Is, cache_state.warm is Turn-1 cache_read, Never the Run Total, The Inter-Run Cache Carryover is a Harness Artifact, Sonnet's Cost Varies 2.9x on Byte-Identical Work, From Scheduling Alone, cache_state Stops Lying (every record asserted warm: false), The Same Lie, Three Arms Over (Gemma/Nemotron report no cache fields)

### Community 84 - "Test Proxy Callback"
Cohesion: 0.29
Nodes (5): _FakeResponse, _ProviderError, `response_obj` is None on a failure, so a 400 landed as     `{"raw_completion":, test_a_failure_records_the_provider_body_not_the_string_none(), Exception

### Community 85 - "Claude"
Cohesion: 0.29
Nodes (7): bedrock/ is Two Routes, Chosen by Model Id (Invoke vs Converse), input Excludes Cache Tokens on Every Route, BAKEOFF_MANTLE_TOKEN, Never AWS_BEARER_TOKEN_BEDROCK, Sonnet on bedrock/ SigV4, Candidates on the Mantle Passthrough, .env.example Contradicted the Invariant It Was Written For, LiteLLM Mangled Kimi's Tool-Call Ids, Converse Folds Cache Into prompt_tokens and the Adapter Subtracts It Back

### Community 86 - "Tasks"
Cohesion: 0.29
Nodes (7): The Harness Does Not Grade, The Eval Measures; It Does Not Decide, Dataset Construction (~80 tasks, §3.5), The Five Collection Blockers, The Offline Grader (does not exist), Offline tool_use / tool_result Id Pairing Check, The 40-Turn Cap is a Placeholder and Already Binding

### Community 87 - "Bakeoff Eval Design"
Cohesion: 0.33
Nodes (7): First real task: pallets/click #3360 / PR #3434, Task on disk: task.yaml plus reference.diff, Pruned-Mirror Handoff Closeout Plan, Contamination-free private task set, Four-source task join (ticket, PR, transcript, base_sha), Task classes A gold / B mined / C agent-only, Task record schema (§3.7)

### Community 88 - "Test Wire"
Cohesion: 0.33
Nodes (6): finish_reason(), The provider's own word for why generation stopped, or None.      The record alr, The fallback, and not dead code: `stop_reason` is what an     Anthropic-shaped d, A failure's response is `{"error": ...}` -- no stopping decision was     reached, test_a_failed_call_has_no_finish_reason_rather_than_a_default(), test_an_anthropic_shaped_response_still_yields_a_finish_reason()

### Community 89 - "Harvesting"
Cohesion: 0.33
Nodes (6): click-3360 provenance (Tier B), Cutting one - the harvest procedure, Pinned reference.diff cut flags, The reference is the real merged PR, verbatim, Screened repository list, sqlglot base_sha date constraint

### Community 90 - "Test Litellm Patches"
Cohesion: 0.33
Nodes (6): The patch has to bite on the streaming path: every real Claude Code call     str, Fail closed, not open. If the id set does not reach the stream the patch     mus, One openai streaming chunk opening a tool call.      Built from litellm's own ty, _streaming_chunk(), test_a_stream_that_never_saw_the_request_leaves_ids_alone(), test_the_streaming_wrapper_rewrites_only_a_collision()

### Community 91 - "Litellm Config"
Cohesion: 0.40
Nodes (5): router_settings.disable_cooldowns (production), Every model_name must be distinct, general_settings.num_retries: 3, router_settings.disable_cooldowns (fault injection), num_retries: 0 in the fault-injection config

### Community 92 - "Claude"
Cohesion: 0.50
Nodes (5): PRICE_BOOK, model_name Doubles as the PRICE_BOOK Key, Two Price Books (Anthropic list for Sonnet, Bedrock page for candidates), A Pricing Failure Must Not Reach parse_trajectory's Caller, Two of Three Candidate Arms Are Unpriceable When Warm

### Community 93 - "Harvesting"
Cohesion: 0.40
Nodes (5): click-3360 budget placeholders, Calibration pilot before freeze, The model-neutral drop rule, Layer 3 - properties of the set, not of any task, task_version bumps on any edit

### Community 94 - "Claude"
Cohesion: 0.40
Nodes (5): A Callback Invocation is Not a Provider Call, No Join Key Exists Between Transcript and Wire Log, Provider finish_reason vs Translated stop_reason, The Adapter Coerces finish_reason and the Harness Reads It as Intent, Nemotron 3 Super Quits Mid-Plan (narrates the call, never emits it)

### Community 97 - "Claude Runner"
Cohesion: 0.50
Nodes (4): classify_stdout_line(), One of "assistant", "other", "malformed", "blank".      Tolerant on purpose: std, The predicate this replaced answered False to both, so a truncated line     and, test_unparseable_stdout_is_distinguished_from_a_non_assistant_event()

### Community 98 - "Claude"
Cohesion: 0.50
Nodes (4): container._checked_exec, A Failure Says Why, In the Record, Silence is the Enemy, macOS --basetemp Under $HOME (Docker VM mounts $HOME, not /var/folders)

### Community 99 - "Claude"
Cohesion: 0.50
Nodes (4): CLAUDE_CONFIG_DIR, Not --settings, Detaches Operator Config, Env is an Allowlist, Not a Denylist, The Environment Was a Denylist Over os.environ, --settings Adds Settings, It Does Not Replace Them

### Community 100 - "Claude"
Cohesion: 0.67
Nodes (4): Diff paths come from git, never a regex over diff --git, Reference Diff Partition (test half / solution half), start_sha is a Pure Function of the Manifest, The Test Half is Applied at Setup — a Methodology Choice

### Community 101 - "Pruned Mirror Handoff"
Cohesion: 0.50
Nodes (4): _build_pruned_mirror as a seam, not a decomposition, Published mirror keeps mkdtemp's 0700, Mutation anchors constrain how the code may be edited, One flock per repo slug

## Ambiguous Edges - Review These
- `Privacy gate blocking team data collection` → `Storage (§6.5)`  [AMBIGUOUS]
  docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md · relation: conceptually_related_to

## Knowledge Gaps
- **47 isolated node(s):** `bakeoff`, `pytest exit 1 vs 2/4/5 (tests failed vs environment broken)`, `No Join Key Exists Between Transcript and Wire Log`, `mock_response Deployments Short-Circuit Before the Streaming Wrapper`, `container._checked_exec` (+42 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **35 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Privacy gate blocking team data collection` and `Storage (§6.5)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `execute_run()` connect `Run Orchestration` to `In-Process Wire Capture`, `Record Assembly`, `Append-Only Event Log`, `Proxy Wire Callback`, `Phase 0c Smoke Test`, `Per-Turn Checkpoints`, `Record Finalize Guards`, `Matrix Scheduling And Resume`, `Docker Container Lifecycle`, `Record Dataclasses`, `Runner Entry Point`, `Dry Run And Fixtures`, `Agent Subprocess Runner`, `Collection Driver`, `Destructive And Secret Scanners`, `Trajectory`, `Wire`, `Claude Runner`, `Claude`?**
  _High betweenness centrality (0.192) - this node is a cross-community bridge._
- **Why does `run_cell()` connect `Collection Driver` to `Wire Log Reading`, `Reference Diff Partition`, `Test Smoke Test`, `Start State Materialization`, `Run Orchestration`?**
  _High betweenness centrality (0.126) - this node is a cross-community bridge._
- **Why does `TaskSpec` connect `Run Orchestration` to `Failure Classification`, `Runner Record Tests`, `In-Process Wire Capture`, `Record Assembly`, `Host Resource Sampling`, `Fault Injection Gate`, `Append-Only Event Log`, `Phase 0c Smoke Test`, `Per-Turn Checkpoints`, `Token Pricing`, `Matrix Scheduling And Resume`, `Docker Container Lifecycle`, `Smoke Criteria Tests`, `Record Dataclasses`, `Runner Entry Point`, `Dry Run And Fixtures`, `Agent Subprocess Runner`, `Runner Result Capture`, `Collection Driver`, `Record Schema Enums`, `Trajectory`, `Test Matrix`, `Test Fault Injection`, `Test Smoke Test`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Are the 55 inferred relationships involving `assemble_record()` (e.g. with `classify_exclusion()` and `classify_failure()`) actually correct?**
  _`assemble_record()` has 55 INFERRED edges - model-reasoned connections that need verification._
- **Are the 51 inferred relationships involving `EventLog` (e.g. with `main()` and `main()`) actually correct?**
  _`EventLog` has 51 INFERRED edges - model-reasoned connections that need verification._
- **Are the 48 inferred relationships involving `TaskSpec` (e.g. with `main()` and `Expectations`) actually correct?**
  _`TaskSpec` has 48 INFERRED edges - model-reasoned connections that need verification._