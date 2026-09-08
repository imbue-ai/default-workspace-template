from typing import Final

from pydantic_settings import BaseSettings

# Where the shipped catalog lives: the raw file on the template repository.
# CLEANUP: point this at ``main`` (and update ``catalog/README.md``,
# ``system/changelog/mngr-new-tab-page.md``, and
# ``docs/system/blueprint/new-tab-page/plan-new-tab-page.md``, which name the branch too) once
# the new-tab-page work has merged to ``main``.
DEFAULT_TEMPLATE_CATALOG_URL: Final[str] = (
    "https://raw.githubusercontent.com/imbue-ai/default-workspace-template/mngr/new-tab-page/catalog/new-tab-templates.json"
)


class Config(BaseSettings):
    """The shell's settings, read from ``SYSTEM_INTERFACE_*`` environment variables."""

    model_config = {"frozen": False}

    system_interface_host: str = "127.0.0.1"
    system_interface_port: int = 8000
    # Where the New Tab page's template catalog is fetched from; empty leaves the page without
    # a templates section.
    system_interface_template_catalog_url: str = DEFAULT_TEMPLATE_CATALOG_URL


def load_config() -> Config:
    return Config()
