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
    """The app's settings, read from ``GETTING_STARTED_*`` environment variables, plus the catalog URL the shell
    used to read."""

    model_config = {"frozen": False}

    getting_started_host: str = "127.0.0.1"
    getting_started_port: int = 8030
    # Where the template catalog is fetched from; empty leaves the page without a templates section. The variable
    # keeps the shell's name so nothing outside the workspace changes when the catalog moved here.
    # CLEANUP: read a ``GETTING_STARTED_TEMPLATE_CATALOG_URL`` instead once the minds side sets that name.
    system_interface_template_catalog_url: str = DEFAULT_TEMPLATE_CATALOG_URL


def load_config() -> Config:
    return Config()
