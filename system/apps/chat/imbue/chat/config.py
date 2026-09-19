from functools import cached_property
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings

# The chat app's own port (contracts.md section 2, the chat row's app URL).
DEFAULT_CHAT_PORT: Final[int] = 8010
# Where the chat persists the user's things (contracts.md section 17), relative to the repo
# root every supervised program runs from.
DEFAULT_CHAT_DATA_DIR: Final[Path] = Path("data/.apps/chat")


class DuplicateStaticBasenameError(ValueError):
    pass


class Config(BaseSettings):
    """The chat app's settings, read from ``CHAT_*`` environment variables."""

    model_config = {"frozen": False}

    chat_javascript_plugins: list[str] | None = None
    chat_static_paths: list[str] | None = None
    chat_host: str = "127.0.0.1"
    chat_port: int = DEFAULT_CHAT_PORT
    # A secondary chat (a preview) points this at a scratch copy so its writes never land
    # in the live chat's data.
    chat_data_dir: Path = Field(default=DEFAULT_CHAT_DATA_DIR)

    @field_validator("chat_javascript_plugins", "chat_static_paths", mode="before")
    @classmethod
    def split_comma_separated(cls, value: object) -> list[str] | None:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return None

    @cached_property
    def javascript_plugin_basenames(self) -> list[str]:
        if not self.chat_javascript_plugins:
            return []
        return [Path(plugin_path).name for plugin_path in self.chat_javascript_plugins]

    @cached_property
    def static_file_basename_to_path(self) -> dict[str, str]:
        all_paths = [
            *(self.chat_javascript_plugins or []),
            *(self.chat_static_paths or []),
        ]
        if not all_paths:
            return {}
        result: dict[str, str] = {}
        for file_path in all_paths:
            basename = Path(file_path).name
            if basename in result:
                raise DuplicateStaticBasenameError(
                    f"Duplicate basename '{basename}': '{result[basename]}' and '{file_path}'"
                )
            result[basename] = file_path
        return result


def load_config() -> Config:
    return Config()
