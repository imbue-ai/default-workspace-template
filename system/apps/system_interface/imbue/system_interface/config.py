from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """The shell's settings, read from ``SYSTEM_INTERFACE_*`` environment variables."""

    model_config = {"frozen": False}

    system_interface_host: str = "127.0.0.1"
    system_interface_port: int = 8000


def load_config() -> Config:
    return Config()
