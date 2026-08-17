from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parents[1]
_LOCAL_CONFIG_PATH = _REPO_ROOT / "litellm_proxy" / "config.yaml"


def _load_local_model_list() -> list[dict[str, Any]]:
    """Load model_list from the local-dev litellm_proxy/config.yaml."""
    with _LOCAL_CONFIG_PATH.open("rb") as config_file:
        return yaml.safe_load(config_file)["model_list"]


def test_deployed_and_local_model_lists_match(app_module: ModuleType) -> None:
    """The Modal app and the local-dev litellm config must expose identical models + pricing.

    These are two representations of the same model list for two different
    consumers (the Modal-deployed proxy vs the local `litellm` CLI). Keeping
    them byte-for-byte in agreement here makes silent drift impossible.
    """
    deployed_model_list = app_module.LITELLM_CONFIG["model_list"]
    local_model_list = _load_local_model_list()
    assert deployed_model_list == local_model_list
