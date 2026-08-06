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
    assert settings.get("callbacks") == "bakeoff.proxy_callback.instance"


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

    path = yaml.safe_load(CONFIG.read_text())["litellm_settings"]["callbacks"]
    module_name, _, attribute = path.rpartition(".")
    resolved = getattr(importlib.import_module(module_name), attribute)

    assert not isinstance(resolved, type), f"{path} is a class; it would be skipped"
    assert isinstance(resolved, CustomLogger)


def test_gemma_has_no_bedrock_runtime_entry():
    """Gemma 4 31B is served only on bedrock-mantle -- it supports neither
    Converse nor Invoke. A bedrock/ route for it could never resolve, and
    the failure would read as model weakness rather than adapter error
    (spec section 6.4)."""
    for entry in _model_list():
        if entry["model_name"].startswith("gemma-4-31b"):
            assert not entry["litellm_params"]["model"].startswith("bedrock/")
