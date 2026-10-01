from pathlib import Path

from pydantic_settings import BaseSettings

from host_backup.config import BACKUP_TOML_PATH
from host_backup.config import RESTIC_ENV_PATH


class Config(BaseSettings):
    """The app's settings, read from ``MEMORIES_*`` environment variables."""

    model_config = {"frozen": False}

    memories_host: str = "127.0.0.1"
    memories_port: int = 8050
    # The notes folder Claude's built-in memory writes to (``autoMemoryDirectory`` in .claude/settings.json).
    memories_notes_dir: Path = Path("data/memories")
    # The backups' retention settings and credentials, read to tell the user how long a deleted note survives in them.
    memories_backup_config_path: Path = BACKUP_TOML_PATH
    memories_restic_env_path: Path = RESTIC_ENV_PATH
    # The record of notes the user deleted or edited, which agent_memory_context.py turns into a notice for chats.
    memories_changes_path: Path = Path("data/.state/memories/user-changes.jsonl")


def load_config() -> Config:
    return Config()
