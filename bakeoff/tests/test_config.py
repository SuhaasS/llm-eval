"""Checks on the LiteLLM proxy config that need no credentials.

Routing, auth, and endpoint reachability cannot be verified here -- that is
Phase 0c's smoke test (Task 12). What is checkable offline is that the file
parses and that its model names line up with the price book, which is the
one config error that would otherwise surface as an UnknownModelError
partway through a paid run.
"""

from pathlib import Path

import pytest

# Hard import, not importorskip: a skipped config check reads as green while
# verifying nothing, which is the failure mode this suite keeps running into.
import yaml

from bakeoff.costs import cost_usd
from bakeoff.proxy import EVAL_ARMS_BY_PROVIDER
from bakeoff.schema import TokenUsage

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
BEDROCK = CONFIG_DIR / "litellm_config.yaml"
OPENROUTER = CONFIG_DIR / "litellm_config_openrouter.yaml"
OFFLINE = CONFIG_DIR / "litellm_smoke_offline.yaml"
CONFIG = BEDROCK  # the bedrock-only pins below read this name
CONFIGS = {"bedrock": BEDROCK, "openrouter": OPENROUTER}
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# bedrock/ models whose tool-call ids are MEASURED to satisfy Bedrock's
# toolUseId pattern `[a-zA-Z0-9_-]{1,64}`. The comment beside each entry is the
# evidence; an entry without one is a guess wearing an allowlist's clothes.
#
# This list exists because bakeoff.litellm_patches removes LiteLLM's tool-id
# sanitizer process-wide, so the character class stopped being enforced
# anywhere between Claude Code and Converse. What used to be a library
# guarantee is now a property of which deployments are configured -- which
# makes it this file's job.
CONVERSE_SAFE_TOOL_ID_MODELS = {
    "bedrock/nvidia.nemotron-super-3-120b",  # call_4196d6e081be491194548c6d
}


def _model_list(path: Path = BEDROCK):
    return yaml.safe_load(path.read_text())["model_list"]


@pytest.mark.parametrize(
    "config, expected",
    [
        (BEDROCK, {"claude-sonnet-5", "gemma-4-31b", "nemotron-3-super-120b", "kimi-k2-5"}),
        (OPENROUTER, {"kimi-k2-6", "kimi-k3"}),
    ],
    ids=["bedrock", "openrouter"],
)
def test_config_parses_and_lists_every_arm(config, expected):
    names = {entry["model_name"] for entry in _model_list(config)}
    assert expected <= names


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_every_configured_model_name_can_be_priced(config):
    """A name in the config that the price book does not know raises
    UnknownModelError mid-run, after the tokens are already spent."""
    for entry in _model_list(config):
        cost_usd(entry["model_name"], TokenUsage(input=1))


def test_sonnet_5_arms_send_no_sampling_parameters():
    """Sonnet 5 returns 400 on any non-default temperature, top_p or top_k.

    Configuring one here would fail every call on the reference arm, and the
    reference arm failing wholesale is the least likely failure for anyone
    to read as a config error. Pinned because a later "make sampling
    uniform across arms" edit is the obvious thing to do and is wrong.
    """
    for entry in _model_list():
        if entry["model_name"].startswith("claude-sonnet-5"):
            params = entry["litellm_params"]
            assert not {"temperature", "top_p", "top_k"} & params.keys()


def test_every_other_arm_pins_its_lab_recommended_sampling():
    """Spec section 5.3: sampling frozen and recorded, never left to a
    provider default that can change under the eval.

    Temperature 0 is the intuitive "fair" choice and is wrong here --
    Moonshot documents multi-minute stalls at 0 for Kimi K2.5, which would
    be measured as latency rather than as a config error.

    Gemma carries no top_p, and that is not an oversight -- see the test
    below. Temperature is still pinned on every arm, because that is the half
    of the policy its route still accepts.
    """
    for entry in _model_list():
        if entry["model_name"].startswith("claude-sonnet-5"):
            continue
        params = entry["litellm_params"]
        assert params["temperature"] == 1.0, entry["model_name"]
        if entry["model_name"].startswith("gemma-4-31b"):
            continue
        assert params["top_p"] == 0.95, entry["model_name"]


def test_gemma_sends_no_top_p_because_its_route_refuses_one():
    """Measured 2026-08-12, not chosen: the /openai/v1 route began validating
    this model under OpenAI's reasoning-model contract, which rejects any
    non-default top_p --

        Unsupported parameter: 'top_p' is not supported with this model.

    while accepting temperature 1.0 and rejecting 0.6. So the lab-recommended
    temperature survives and the lab-recommended top_p cannot.

    Pinned for the same reason as the Sonnet test above: "make sampling
    uniform across arms" is the obvious edit and would re-break the arm. It is
    also why nemotron and kimi are NOT levelled down to match -- section 5.3's
    constant across arms is the policy, lab-recommended unless the route
    refuses it, not the number.
    """
    for entry in _model_list():
        if entry["model_name"].startswith("gemma-4-31b"):
            assert "top_p" not in entry["litellm_params"], entry["model_name"]


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_the_production_config_registers_the_proxy_side_wire_callback(config):
    """Spec section 6.2 makes wire logging mandatory, and the proxy is the
    only process that makes the calls.

    Without this entry a real run reads an empty wire directory and comes
    back with empty sampling, empty prompt and tool hashes, no resolved
    model id, zero errored calls and no API error status -- silently, on a
    record that otherwise looks complete. That shipped once already
    (Task 11, defect 15); the fault-injection config carries the callback
    and the production config did not.
    """
    settings = yaml.safe_load(config.read_text())["litellm_settings"]
    assert "bakeoff.proxy_callback.instance" in settings.get("callbacks", [])


def test_every_proxy_config_registers_both_callbacks():
    """Three configs start a proxy, and a patch missing from one of them means
    the offline gate and the fault-injection suite certify an UNPATCHED
    topology under the same name as the patched one -- the weaker check
    wearing the stronger check's label.

    Applies to the wire callback too: that one shipped absent from the
    production config once already (Task 11, defect 15).
    """
    for path in sorted(CONFIG.parent.glob("litellm*.yaml")):
        callbacks = yaml.safe_load(path.read_text())["litellm_settings"]["callbacks"]
        assert "bakeoff.proxy_callback.instance" in callbacks, path.name
        assert "bakeoff.litellm_patches.instance" in callbacks, path.name


def test_the_router_does_not_cool_down_a_single_deployment_group():
    """Measured, litellm 1.95.0: `_should_cooldown_deployment` has four
    branches, and the `_should_retry(status) is False` one is NOT guarded by
    `is_single_deployment_model_group`. _should_retry(401) and (403) are both
    False, so ONE auth failure cools the deployment down for
    DEFAULT_COOLDOWN_TIME_SECONDS (5).

    Every model group here is single-deployment -- the model_names are distinct
    by design so LiteLLM cannot load-balance and randomise transport per call --
    so there is nothing to fail over to and the cooldown buys nothing. What it
    costs is the diagnosability of every auth failure: `num_retries: 3` retries
    into the cooldown and gets RouterRateLimitError, a plain ValueError with no
    status_code, so the run's last wire entry says "No deployments available" at
    status None instead of naming the credential problem that caused it.

    Checked across every proxy config, for the reason
    test_every_proxy_config_registers_both_callbacks gives: a setting missing
    from one of them means the offline gate certifies a different router policy
    from the one the paid run uses, under the same name.
    """
    for path in sorted(CONFIG.parent.glob("litellm*.yaml")):
        config = yaml.safe_load(path.read_text())
        names = [d["model_name"] for d in config["model_list"]]
        assert len(names) == len(set(names)), f"{path.name}: would load-balance"
        assert config["router_settings"]["disable_cooldowns"] is True, path.name


def test_no_deployment_targets_the_real_anthropic_api():
    """The assumption that makes the tool-id patch safe, written down.

    `^[a-zA-Z0-9_-]+$` on tool_use ids is enforced by Anthropic's own API, and
    the patch hands ids through unsanitized. That is only sound because no
    deployment here is Anthropic's API: the bedrock arms answer through
    bedrock, and the openrouter arms through the `openai/` provider class,
    which never constructs an Anthropic `tool_use` id for an upstream to
    reject -- the id crosses as the model emitted it. Point an arm at
    api.anthropic.com and the patch stops being a bug fix and starts being a
    bug, on every arm at once, since the patch is global.
    """
    for path in sorted(CONFIG.parent.glob("litellm*.yaml")):
        for entry in yaml.safe_load(path.read_text())["model_list"]:
            base = str(entry["litellm_params"].get("api_base", ""))
            assert "api.anthropic.com" not in base, f"{path.name}: {entry['model_name']}"


def test_no_bedrock_arm_reaches_converse_with_ids_it_would_reject():
    """The other half of the assumption above, for the transport Anthropic's
    own API is not on.

    Below /v1/messages the bedrock/ route splits in two, and the split is by
    model rather than by config (verified against litellm 1.95.0):

      Claude models   AmazonAnthropicClaudeMessagesConfig -> the Invoke
                      endpoint, carrying a native Anthropic body. No
                      toolUseId is ever constructed, so the tool-id patch
                      cannot reach it. claude-sonnet-5-runtime is exempt
                      structurally, not because its ids happen to be clean.
      everything else the openai -> Converse bridge, where
                      prompt_templates/factory.py copies the tool id verbatim
                      into BedrockToolUseBlock(toolUseId=...) and
                      BedrockToolResultBlock(toolUseId=...). Bedrock
                      constrains that to `[a-zA-Z0-9_-]{1,64}` SERVER-side --
                      litellm sanitizes tool NAMES on this path and never ids,
                      so nothing fails locally.

    With the sanitizer patched out, Kimi's `functions.Read:0` therefore
    arrives at Converse intact (measured; see test_litellm_patches). That is
    why kimi-k2-5-runtime is commented out of the config rather than merely
    left out of EVAL_ARMS -- --models makes any configured deployment one flag
    away from a paid run.

    Resolved through litellm's own routing rather than a hardcoded model list,
    so an upgrade that moves an arm onto the Converse bridge fails here too.
    """
    import litellm
    from litellm.utils import ProviderConfigManager

    for path in sorted(CONFIG.parent.glob("litellm*.yaml")):
        for entry in yaml.safe_load(path.read_text())["model_list"]:
            configured = str(entry["litellm_params"]["model"])
            if not configured.startswith("bedrock/"):
                continue

            model, provider, _, _ = litellm.get_llm_provider(model=configured)
            messages_config = ProviderConfigManager.get_provider_anthropic_messages_config(
                model=model, provider=litellm.LlmProviders(provider)
            )
            if messages_config is not None:
                continue

            assert configured in CONVERSE_SAFE_TOOL_ID_MODELS, (
                f"{path.name}: {entry['model_name']} routes through the Converse "
                "bridge, where its tool-call ids become toolUseId unsanitized. "
                "Measure the ids it returns and add it to "
                "CONVERSE_SAFE_TOOL_ID_MODELS with that evidence, or drop it."
            )


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_the_registered_callback_path_resolves_to_a_dispatchable_instance(config):
    """The trap this config comment has always warned about, asserted.

    get_instance_fn resolves a dotted path with getattr and returns it
    as-is. LiteLLM's success_handler then dispatches on
    isinstance(callback, CustomLogger); a CLASS fails that check and is
    skipped in silence -- no wire log, no error. Pointing at a module-level
    INSTANCE is what makes the config form work at all, so the distinction
    is load-bearing rather than stylistic.
    """
    import importlib

    from litellm.integrations.custom_logger import CustomLogger

    paths = yaml.safe_load(config.read_text())["litellm_settings"]["callbacks"]
    for path in paths:
        module_name, _, attribute = path.rpartition(".")
        resolved = getattr(importlib.import_module(module_name), attribute)

        assert not isinstance(resolved, type), f"{path} is a class; it would be skipped"
        assert isinstance(resolved, CustomLogger), path


def test_no_mantle_arm_reads_the_bearer_variable_litellm_falls_back_to():
    """One proxy serves both transports, and the env var name is what keeps
    them apart.

    LiteLLM's bedrock/ handler (base_aws_llm.get_request_headers, verified
    1.95.0) uses the deployment's api_key when set and otherwise falls back
    to AWS_BEARER_TOKEN_BEDROCK, signing SigV4 only when both are absent. The
    runtime deployments carry no api_key on purpose, so a proxy process
    holding AWS_BEARER_TOKEN_BEDROCK bearer-authenticates all three of them
    and they fail with `bedrock:CallWithBearerToken` -- while the mantle arms
    stay green, which makes it read as a bedrock-runtime problem.

    Renaming the reference back is a one-character-looking edit with no local
    symptom, which is why it is pinned here rather than left to the comment.
    """
    for entry in _model_list():
        api_key = entry["litellm_params"].get("api_key", "")
        assert "AWS_BEARER_TOKEN_BEDROCK" not in api_key, entry["model_name"]


def test_the_env_template_names_the_variable_the_config_actually_reads():
    """The test above pins the config. This pins the instructions.

    `.env.example` told the operator to paste the mantle key into
    AWS_BEARER_TOKEN_BEDROCK until 2026-08-12 -- so following the setup
    document verbatim produced exactly the failure the config is careful to
    avoid, and produced it on the three runtime arms rather than on the one
    being configured. Found by running the live preflight, which reported
    `ignored unfilled placeholders: AWS_BEARER_TOKEN_BEDROCK` beside
    `MISSING BAKEOFF_MANTLE_TOKEN` and caught it only because the placeholder
    was still unfilled.

    A config invariant that the setup instructions contradict is not pinned.
    """
    template = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    keys = {
        line.split("=", 1)[0].strip()
        for line in template.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert "BAKEOFF_MANTLE_TOKEN" in keys, (
        "the template does not name the variable the mantle arms read"
    )
    assert "AWS_BEARER_TOKEN_BEDROCK" not in keys, (
        "the template sets the variable litellm falls back to; filling it in "
        "bearer-authenticates every bedrock/ arm and fails them with "
        "bedrock:CallWithBearerToken"
    )


def test_bedrock_runtime_arms_carry_no_api_key():
    """SigV4 is the whole point of these deployments. An api_key on one takes
    the bearer branch above and never signs -- the same failure as the
    ambient variable, but per-arm and even easier to miss."""
    for entry in _model_list():
        params = entry["litellm_params"]
        if params["model"].startswith("bedrock/"):
            assert "api_key" not in params, entry["model_name"]


def test_every_candidate_arm_uses_the_openai_provider_prefix():
    """Where the two openai param interventions actually live.

    bakeoff.litellm_patches wraps ``litellm.OpenAIConfig.map_openai_params``,
    which ``get_optional_params`` reaches only for
    ``custom_llm_provider == "openai"``. litellm 1.95.0 also ships a
    first-class ``bedrock_mantle/`` provider that would derive the /openai/v1
    vs /v1 base path by itself -- tempting, and it would silently disable both
    interventions: ``BedrockMantleChatConfig`` inherits from
    ``OpenAILikeChatConfig``, which OVERRIDES ``map_openai_params``, so the
    wrapper would still be installed and never called.

    The failure would be a 400 on gemma and, worse, a thinking-on kimi run
    scored against thinking-off arms. So the constraint is a property of which
    deployments are configured, the same shape as
    test_no_bedrock_arm_reaches_converse_with_ids_it_would_reject.

    Scoped to the three arms smoke_test.EVAL_ARMS actually runs. The
    `-runtime` deployments are the documented alternate transport and are on
    `bedrock/` on purpose -- they reach neither intervention, which is a real
    difference and the reason promoting one into EVAL_ARMS is not a one-line
    change.
    """
    candidates = {"gemma-4-31b", "nemotron-3-super-120b", "kimi-k2-5"}
    seen = set()
    for entry in _model_list():
        if entry["model_name"] not in candidates:
            continue
        seen.add(entry["model_name"])
        model = entry["litellm_params"]["model"]
        assert model.startswith("openai/"), (
            f"{entry['model_name']} is on {model!r}: the max_completion_tokens "
            "rename and the reasoning_effort pin both hang off the openai "
            "provider and would stop applying"
        )
    assert seen == candidates, f"candidate arm missing from config: {candidates - seen}"


def test_gemma_has_no_bedrock_runtime_entry():
    """Gemma 4 31B is served only on bedrock-mantle -- it supports neither
    Converse nor Invoke. A bedrock/ route for it could never resolve, and
    the failure would read as model weakness rather than adapter error
    (spec section 6.4)."""
    for entry in _model_list():
        if entry["model_name"].startswith("gemma-4-31b"):
            assert not entry["litellm_params"]["model"].startswith("bedrock/")


def test_litellm_is_the_version_the_patches_were_verified_against():
    """`litellm_patches` monkeypatches litellm internals, and its own guards
    cannot detect drift: two of them use `hasattr` against names a base class
    declares, so they stay true after the override they check is gone. Every
    behavioural claim in that module is annotated against one version. The pin
    in pyproject.toml is what makes drift impossible rather than undetected;
    this asserts the installed tree actually honours it.

    If this fails, do not just bump the number: re-run the patch probes and
    re-read the annotations first.
    """
    from importlib.metadata import version

    assert version("litellm") == "1.95.0"


def test_pyyaml_is_a_runtime_dependency_not_a_dev_one():
    """`tasks.py` imports yaml on the input path -- every real task goes
    through it. Declared under `dev`, a non-dev install fails at task load
    rather than at import, which is after the environment looks healthy."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]

    assert any(d.startswith("pyyaml") for d in deps)


def _openrouter_arms():
    return _model_list(OPENROUTER)


def test_every_openrouter_arm_targets_openrouter_on_the_openai_provider():
    """Spec §2. `openai/`, not litellm's `openrouter/`: the resolved-params
    capture and the two mantle rewrites hang off OpenAIConfig.map_openai_params,
    which get_optional_params reaches only for custom_llm_provider == "openai".
    A different provider class would leave the wrapper installed and never
    called, and `resolved` would read null on every call."""
    for entry in _openrouter_arms():
        params = entry["litellm_params"]
        assert params["model"].startswith("openai/"), entry["model_name"]
        assert params["api_base"] == OPENROUTER_BASE, entry["model_name"]
        assert params["api_key"] == "os.environ/OPENROUTER_API_KEY", entry["model_name"]


def test_every_openrouter_arm_is_pinned_to_one_upstream_with_no_fallback():
    """Spec §2. OpenRouter load-balances one model across upstreams with
    different quantizations and tool-call parsers. Unpinned, an arm is not one
    model across repeats; with fallbacks, `require_parameters` failing on the
    pinned upstream silently routes to whoever is left."""
    for entry in _openrouter_arms():
        provider = entry["litellm_params"]["extra_body"]["provider"]
        assert isinstance(provider.get("order"), list) and len(provider["order"]) == 1, entry["model_name"]
        assert provider["allow_fallbacks"] is False, entry["model_name"]
        assert provider["require_parameters"] is True, entry["model_name"]


def test_every_openrouter_arm_asks_for_provider_cost_and_drops_the_derived_effort():
    """`usage.include` is what makes `cost_usd_provider` exist. Dropping
    reasoning_effort is what keeps require_parameters from rejecting K2.6,
    whose endpoints list `reasoning` but not `reasoning_effort`; litellm
    derives one from Claude Code's thinking:adaptive on every call."""
    for entry in _openrouter_arms():
        params = entry["litellm_params"]
        assert params["extra_body"]["usage"] == {"include": True}, entry["model_name"]
        assert "reasoning_effort" in params["additional_drop_params"], entry["model_name"]
        assert "reasoning" in params["extra_body"], entry["model_name"]


def test_the_openrouter_config_pins_quantization_where_the_endpoint_declares_one():
    """Crusoe declares bf16 for K2.6 (re-pinned from CoreWeave's fp4 on
    2026-09-11: CoreWeave's cache hit 3/10 then 0/15 probe replicates,
    Crusoe's 5/5); Fireworks declares none for K3, so pinning there would
    match nothing."""
    by_name = {e["model_name"]: e["litellm_params"]["extra_body"]["provider"] for e in _openrouter_arms()}
    assert by_name["kimi-k2-6"]["order"] == ["crusoe"]
    assert by_name["kimi-k2-6"]["quantizations"] == ["bf16"]
    assert by_name["kimi-k3"]["order"][0].startswith("fireworks")
    assert "quantizations" not in by_name["kimi-k3"]


def test_every_eval_arm_has_an_offline_stub():
    """Defect B (2026-09-10): config_name_for is provider-neutral for
    offline on purpose -- the stub answers, nothing is spent, no credential
    is read -- so litellm_smoke_offline.yaml is the ONE config an offline run
    reaches whichever --provider is chosen. An arm in EVAL_ARMS_BY_PROVIDER
    with no entry there means a default (no --models) offline run 4xxs on an
    unknown model group and the run is scored as a model failure
    (`gave_up`) instead of refused as a config mismatch -- exactly what
    scripts.run_matrix.arms_missing_from_config exists to catch, except this
    pin catches it before any run is attempted at all."""
    offline_names = {e["model_name"] for e in _model_list(OFFLINE)}
    for provider, arms in EVAL_ARMS_BY_PROVIDER.items():
        for arm in arms:
            assert arm in offline_names, f"{arm} ({provider}) has no offline stub"
