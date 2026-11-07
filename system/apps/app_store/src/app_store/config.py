from pathlib import Path
from typing import Final

from pydantic_settings import BaseSettings

from getting_started.config import DEFAULT_TEMPLATE_CATALOG_URL

# The store's data, under the app's own folder (contracts.md section 17): its install records, and one folder per
# installed app holding that app's environment, its own data, and its generated manifest and icon.
DEFAULT_DATA_DIRECTORY: Final[Path] = Path("data/.apps/app-store")
# What the store keeps about this machine and can rebuild: the template catalog's last good copy and the cached
# template sizes.
DEFAULT_STATE_DIRECTORY: Final[Path] = Path("data/.state/app-store")


class Config(BaseSettings):
    """The app's settings, read from ``APP_STORE_*`` environment variables."""

    model_config = {"frozen": False}

    app_store_host: str = "127.0.0.1"
    app_store_port: int = 8060
    app_store_data_dir: Path = DEFAULT_DATA_DIRECTORY
    app_store_state_dir: Path = DEFAULT_STATE_DIRECTORY
    # Where the template catalog is fetched from; the same document Getting Started shows. Empty hides the
    # Templates section.
    app_store_template_catalog_url: str = DEFAULT_TEMPLATE_CATALOG_URL


def load_config() -> Config:
    return Config()
