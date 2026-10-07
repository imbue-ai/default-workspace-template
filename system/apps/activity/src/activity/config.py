from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """The app's settings, read from ``ACTIVITY_*`` environment variables."""

    model_config = {"frozen": False}

    activity_host: str = "127.0.0.1"
    activity_port: int = 8040


def load_config() -> Config:
    return Config()
