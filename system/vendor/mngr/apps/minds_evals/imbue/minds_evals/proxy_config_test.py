from imbue.minds_evals.proxy_config import build_model_list
from imbue.minds_evals.proxy_config import build_proxy_config


def test_build_model_list_routes_every_priced_anthropic_model() -> None:
    entries = build_model_list()

    names = [entry["model_name"] for entry in entries]
    assert "claude-opus-4-8" in names
    assert "claude-haiku-4-5" in names
    # Bare names are what Claude Code asks for; the provider prefix belongs on the routing target.
    assert all("/" not in name for name in names)
    assert all(entry["litellm_params"]["model"].startswith("anthropic/") for entry in entries)


def test_build_model_list_carries_all_four_prices_inline() -> None:
    opus = next(entry for entry in build_model_list() if entry["model_name"] == "claude-opus-4-8")

    params = opus["litellm_params"]
    # Inline pricing keeps cost correct on a litellm whose own table predates a model, and keeps the
    # proxy's cost and the eval's transcript arithmetic on one table.
    assert params["input_cost_per_token"] > 0
    assert params["output_cost_per_token"] > params["input_cost_per_token"]
    assert params["cache_creation_input_token_cost"] > params["input_cost_per_token"]
    assert params["cache_read_input_token_cost"] < params["input_cost_per_token"]


def test_build_model_list_takes_the_upstream_key_from_the_environment() -> None:
    # The key reaches the box as an env var and must never be baked into an uploaded config file.
    assert all(entry["litellm_params"]["api_key"] == "os.environ/ANTHROPIC_API_KEY" for entry in build_model_list())


def test_proxy_config_refuses_unauthenticated_requests() -> None:
    config = build_proxy_config()

    # With no database and no master key, litellm grants internal-user rights to any key unless a
    # custom auth hook is configured -- so its absence would be an open proxy.
    assert config["general_settings"]["custom_auth"].endswith(".user_api_key_auth")
    assert config["litellm_settings"]["callbacks"] == ["box_proxy_hooks.usage_logger"]
    # A retry would bill twice for one logical call and blur the accounting.
    assert config["litellm_settings"]["num_retries"] == 0
