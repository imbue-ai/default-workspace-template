from litellm.router_utils.pattern_match_deployments import PatternMatchRouter

from imbue.minds_evals.proxy_config import build_model_list
from imbue.minds_evals.proxy_config import build_proxy_config


def test_build_model_list_routes_every_claude_model_through_one_pattern() -> None:
    """One entry rather than an enumeration, so a model is routable the day litellm's map carries it.
    The bare name is what Claude Code asks for; the provider prefix belongs on the routing target,
    which is what tells litellm whose API to talk and which prices to bill at."""
    (entry,) = build_model_list()

    assert entry["model_name"] == "claude-*"
    assert entry["litellm_params"]["model"] == "anthropic/claude-*"


def test_build_model_list_leaves_prices_to_litellms_own_map() -> None:
    """Four inline per-token numbers cannot express the fast-mode premium, the 1-hour cache-write rate
    or the regional uplift, so writing them would make the proxy's own per-request figures wrong in
    ways its config could not state. litellm prices from its map instead."""
    (entry,) = build_model_list()

    assert [key for key in entry["litellm_params"] if "cost" in key] == []


def test_build_model_list_routes_a_claude_name_and_refuses_anything_else() -> None:
    """Put to litellm's own pattern router, which is what resolves the entry inside the box: a claude
    name routes, and the name it routes to carries the provider prefix, which is what prices the
    request and what a reader of the proxy's log meets. A non-claude name matches nothing and fails
    here as an unknown model, rather than being forwarded on an Anthropic credential that cannot serve
    it and coming back as a confusing upstream error."""
    router = PatternMatchRouter()
    for entry in build_model_list():
        router.add_pattern(entry["model_name"], dict(entry))

    routed = router.route("claude-opus-5")
    assert routed is not None and len(routed) == 1
    assert routed[0]["litellm_params"]["model"] == "anthropic/claude-opus-5"
    assert router.route("gpt-5-mini") is None


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
