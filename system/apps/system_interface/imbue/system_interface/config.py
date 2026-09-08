from pydantic_settings import BaseSettings

from imbue.system_interface.template_catalog import DEFAULT_TEMPLATE_CATALOG_URL


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
