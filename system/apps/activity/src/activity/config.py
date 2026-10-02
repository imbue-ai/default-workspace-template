from pathlib import Path
from typing import Final

from pydantic_settings import BaseSettings

# The workspace's root. Paths the app shares with other programs are absolute, so a preview, which runs from a worktree,
# reads the live workspace's files.
WORKSPACE_ROOT: Final[Path] = Path("/home/user/workspace")


class Config(BaseSettings):
    """The app's settings, read from ``ACTIVITY_*`` environment variables."""

    model_config = {"frozen": False}

    activity_host: str = "127.0.0.1"
    activity_port: int = 8040


def load_config() -> Config:
    return Config()
