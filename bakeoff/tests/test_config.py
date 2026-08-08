"""Checks on the LiteLLM proxy config that need no credentials.

Routing, auth, and endpoint reachability cannot be verified here -- that is
Phase 0c's smoke test (Task 12). What is checkable offline is that the file
parses and that its model names line up with the price book, which is the
one config error that would otherwise surface as an UnknownModelError
partway through a paid run.
"""

from pathlib import Path

# Hard import, not importorskip: a skipped config check reads as green while
# verifying nothing, which is the failure mode this suite keeps running into.
import yaml

from bakeoff.costs import cost_usd
from bakeoff.schema import TokenUsage

CONFIG = Path(__file__).resolve().parents[1] / "config" / "litellm_config.yaml"


def _model_list():
    return yaml.safe_load(CONFIG.read_text())["model_list"]


def test_config_parses_and_lists_every_arm():
    names = {entry["model_name"] for entry in _model_list()}
    assert {"claude-sonnet-5", "gemma-4-31b", "nemotron-3-super-120b", "kimi-k2-5"} <= names


def test_every_configured_model_name_can_be_priced():
    """A name in the config that the price book does not know raises
    UnknownModelError mid-run, after the tokens are already spent."""
    for entry in _model_list():
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
    """
    for entry in _model_list():
        if entry["model_name"].startswith("claude-sonnet-5"):
            continue
        params = entry["litellm_params"]
        assert params["temperature"] == 1.0, entry["model_name"]
        assert params["top_p"] == 0.95, entry["model_name"]


def test_the_production_config_registers_the_proxy_side_wire_callback():
    """Spec section 6.2 makes wire logging mandatory, and the proxy is the
    only process that makes the calls.

    Without this entry a real run reads an empty wire directory and comes
    back with empty sampling, empty prompt and tool hashes, no resolved
    model id, zero errored calls and no API error status -- silently, on a
    record that otherwise looks complete. That shipped once already
    (Task 11, defect 15); the fault-injection config carries the callback
    and the production config did not.
    """
    settings = yaml.safe_load(CONFIG.read_text())["litellm_settings"]
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


def test_no_deployment_targets_the_real_anthropic_api():
    """The assumption that makes the tool-id patch safe, written down.

    `^[a-zA-Z0-9_-]+$` on tool_use ids is enforced by Anthropic's own API.
    Disabling the sanitizer is only sound because nothing here calls it --
    every arm goes to Bedrock. Point an arm at api.anthropic.com and the patch
    stops being a bug fix and starts being a bug.
    """
    for path in sorted(CONFIG.parent.glob("litellm*.yaml")):
        for entry in yaml.safe_load(path.read_text())["model_list"]:
            base = str(entry["litellm_params"].get("api_base", ""))
            assert "api.anthropic.com" not in base, f"{path.name}: {entry['model_name']}"


def test_the_registered_callback_path_resolves_to_a_dispatchable_instance():
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

    paths = yaml.safe_load(CONFIG.read_text())["litellm_settings"]["callbacks"]
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


def test_bedrock_runtime_arms_carry_no_api_key():
    """SigV4 is the whole point of these deployments. An api_key on one takes
    the bearer branch above and never signs -- the same failure as the
    ambient variable, but per-arm and even easier to miss."""
    for entry in _model_list():
        params = entry["litellm_params"]
        if params["model"].startswith("bedrock/"):
            assert "api_key" not in params, entry["model_name"]


def test_gemma_has_no_bedrock_runtime_entry():
    """Gemma 4 31B is served only on bedrock-mantle -- it supports neither
    Converse nor Invoke. A bedrock/ route for it could never resolve, and
    the failure would read as model weakness rather than adapter error
    (spec section 6.4)."""
    for entry in _model_list():
        if entry["model_name"].startswith("gemma-4-31b"):
            assert not entry["litellm_params"]["model"].startswith("bedrock/")
