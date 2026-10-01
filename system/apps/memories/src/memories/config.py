from pathlib import Path

from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """The app's settings, read from ``MEMORIES_*`` environment variables."""

    model_config = {"frozen": False}

    memories_host: str = "127.0.0.1"
    memories_port: int = 8050
    # The notes folder Claude's built-in memory writes to (``autoMemoryDirectory`` in .claude/settings.json).
    memories_notes_dir: Path = Path("data/memories")
    # Where forgotten notes are moved: outside the notes folder, so no chat finds them.
    memories_forgotten_dir: Path = Path("data/.state/memories/forgotten")


def load_config() -> Config:
    return Config()
