"""The litellm config the eval's in-box proxy runs from.

Built host-side and uploaded, so the box runs no code generation. The model list is derived from
``mngr_usage``'s price table -- the same table ``usage.py`` prices transcripts with -- so the
proxy's own cost figures and the eval's cannot disagree about prices, and a model added there
becomes routable here without a second edit.

The proxy runs with **no database**: litellm's schema is Postgres-only and a fresh one costs about
a hundred migrations, which is far too much to stand up per trial. Without a database litellm has no
virtual keys and no spend tables, so auth and usage recording are supplied by ``box_proxy_hooks``
instead. That trade is what keeps Modal the only infrastructure this eval depends on.
"""

import json
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_usage.pricing import MODEL_PRICING

# Only Anthropic models are routable: the workspace runs Claude Code, which talks the Anthropic
# Messages API, and the pricing table's keys carry the provider that litellm needs to route.
_ANTHROPIC_PREFIX: Final[str] = "anthropic/"
HOOKS_MODULE: Final[str] = "box_proxy_hooks"


@pure
def build_model_list() -> list[dict[str, Any]]:
    """One routable entry per priced Anthropic model, carrying its prices inline.

    Prices are written inline rather than left to litellm's bundled map, so cost stays correct even
    on a litellm version whose own table predates a model. The trade is that four flat per-token
    numbers cannot express what that map holds beside them -- the fast-mode premium
    (``provider_specific_entry.fast``), the 1-hour cache-write rate, the regional uplift -- so every
    figure the proxy reports is a standard-rate, 5-minute-cache one. The eval's own totals re-apply
    the fast premium from ``mngr_usage`` afterwards; the proxy's per-request ``cost_usd`` does not.
    """
    entries: list[dict[str, Any]] = []
    for pricing_key, prices in MODEL_PRICING.items():
        if not pricing_key.startswith(_ANTHROPIC_PREFIX):
            continue
        model_name = pricing_key[len(_ANTHROPIC_PREFIX) :]
        entries.append(
            {
                "model_name": model_name,
                "litellm_params": {
                    "model": pricing_key,
                    "api_key": "os.environ/ANTHROPIC_API_KEY",
                    "input_cost_per_token": prices.input_cost_per_token,
                    "output_cost_per_token": prices.output_cost_per_token,
                    "cache_creation_input_token_cost": prices.cache_creation_input_token_cost,
                    "cache_read_input_token_cost": prices.cache_read_input_token_cost,
                },
            }
        )
    return entries


@pure
def build_proxy_config() -> dict[str, Any]:
    return {
        "model_list": build_model_list(),
        "general_settings": {
            # No master_key and no database: without a custom auth hook litellm would grant
            # internal-user rights to any key at all.
            "custom_auth": "{}.user_api_key_auth".format(HOOKS_MODULE),
        },
        "litellm_settings": {
            "callbacks": ["{}.usage_logger".format(HOOKS_MODULE)],
            # Claude Code sends parameters litellm's Anthropic path does not always accept; dropping
            # them matches how the deployed proxy is configured.
            "drop_params": True,
            # A retry would bill twice for one logical call and blur the accounting.
            "num_retries": 0,
        },
    }


@pure
def render_proxy_config() -> str:
    """The config as YAML. JSON is valid YAML, so this needs no yaml dependency host-side and stays
    exactly reproducible."""
    return json.dumps(build_proxy_config(), indent=2, sort_keys=True)
