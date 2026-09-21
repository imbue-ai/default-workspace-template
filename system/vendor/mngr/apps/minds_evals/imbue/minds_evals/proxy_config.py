"""The litellm config the eval's in-box proxy runs from.

Built host-side and uploaded, so the box runs no code generation. Every claude model is routable
through one pattern entry, exactly as the deployed proxy (``apps/modal_litellm``) routes them, and
prices are left to litellm's own map inside the box rather than written inline: four flat per-token
numbers cannot express what that map holds beside them -- the fast-mode premium, the 1-hour
cache-write rate, the regional uplift -- and a model becomes routable and priced the day the map
carries it, with no entry to add here.

The proxy runs with **no database**: litellm's schema is Postgres-only and a fresh one costs about a
hundred migrations, which is far too much to stand up per trial. Without a database litellm has no
virtual keys and no spend tables, so auth and usage recording are supplied by ``box_proxy_hooks``
instead. That trade is what keeps Modal the only infrastructure this eval depends on.
"""

import json
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure

# Only claude models are routable. The workspace runs Claude Code, which talks the Anthropic Messages
# API, and the proxy holds an Anthropic credential and nothing else -- so the pattern is deliberately
# `claude-*` rather than a bare `*`: a non-claude name has to fail here as an unknown model instead of
# being forwarded to Anthropic and coming back as a confusing upstream error. (An Anthropic model not
# named `claude-...` would need this widened.)
_CLAUDE_MODEL_PATTERN: Final[str] = "claude-*"
_ANTHROPIC_TARGET_PATTERN: Final[str] = "anthropic/claude-*"
HOOKS_MODULE: Final[str] = "box_proxy_hooks"


@pure
def build_model_list() -> list[dict[str, Any]]:
    """The one routable entry: a bare claude name as Claude Code asks for it, forwarded upstream
    provider-qualified so litellm knows whose API to talk and which prices to bill at."""
    return [
        {
            "model_name": _CLAUDE_MODEL_PATTERN,
            "litellm_params": {
                "model": _ANTHROPIC_TARGET_PATTERN,
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        }
    ]


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
