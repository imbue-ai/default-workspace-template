from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """The shell's settings, read from ``SYSTEM_INTERFACE_*`` environment variables."""

    model_config = {"frozen": False}

    system_interface_host: str = "127.0.0.1"
    system_interface_port: int = 8000
    # The name the page titles itself and its home-screen tile with; empty reads it from the workspace's mngr
    # records (``workspace_name.py``).
    system_interface_workspace_name: str = ""


def load_config() -> Config:
    return Config()
