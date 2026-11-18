"""What every AI's entry in the tracker provides, in a form the rest of the tracker uses for all of them.

Each AI the tracker can read has its own module under ``ai_usage.providers`` that builds
one ``UsageProvider``: where to ask for usage, where the sign-in key is, and two small
functions for that AI's own reply and sign-in file layouts.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

# Where Studio keeps each signed-in account's folder.
ACCOUNTS_DIR = Path("/home/user/.minds/accounts")


class Limit(FrozenModel):
    """One usage limit in a reading, in the same shape whichever AI reported it."""

    key: str = Field(description="Stable id of the limit")
    label: str = Field(description="What the AI's own usage page calls it")
    percent: float = Field(description="How much is used, 0-100")
    resets_at: str | None = Field(description="When it resets (ISO 8601)")


def no_plan(credentials: dict[str, Any]) -> str | None:
    """For an AI whose sign-in file does not name the plan."""
    return None


def no_extra_headers(credentials: dict[str, Any]) -> dict[str, str]:
    """For an AI whose usage endpoint needs nothing besides the sign-in key."""
    return {}


class UsageProvider(FrozenModel):
    """Where one kind of AI agent reports its usage, and how to read its answer and its sign-in."""

    agent_type: str = Field(
        description="The agent type Studio reports for its chats, e.g. 'claude'"
    )
    label: str = Field(description="What the user calls this AI")
    usage_url: str = Field(description="The endpoint that answers the account's usage")
    usage_page: str = Field(
        description="The page in the AI's own settings that shows the same numbers"
    )
    account_key: Callable[[str | None], str | None] = Field(
        description="Which account a chat's usage belongs to, from the account Studio labels it with; None to skip it"
    )
    credentials_paths: Callable[[str], list[Path]] = Field(
        description="Where that account's sign-in file may be, most likely first"
    )
    token_path: tuple[str, ...] = Field(
        description="The keys leading to the sign-in key inside that file"
    )
    request_headers: Callable[[dict[str, Any]], dict[str, str]] = Field(
        description="Headers the usage endpoint needs besides the sign-in key, from the sign-in file"
    )
    request_body: bytes | None = Field(
        default=None,
        description="Body to send when the usage endpoint expects a POST instead of a GET",
    )
    extra_usage_urls: dict[str, str] = Field(
        default_factory=dict,
        description="More endpoints to ask on each reading, by the key their reply is kept under in the reading",
    )
    parse_limits: Callable[[dict[str, Any]], list[Limit]] = Field(
        description="Turns the usage endpoint's reply into limits"
    )
    read_plan: Callable[[dict[str, Any]], str | None] = Field(
        description="Names the plan from the sign-in file, if it says"
    )

    def credentials(self, account: str) -> dict[str, Any] | None:
        for path in self.credentials_paths(account):
            try:
                text = path.read_text().strip()
            except OSError:
                continue
            if not text:
                continue
            try:
                data: Any = json.loads(text)
            except json.JSONDecodeError:
                data = text
            if isinstance(data, dict):
                return data
            if isinstance(data, str) and self.token_path:
                wrapped: dict[str, Any] = {self.token_path[-1]: data}
                for key in reversed(self.token_path[:-1]):
                    wrapped = {key: wrapped}
                return wrapped
        return None

    def access_token(self, credentials: dict[str, Any]) -> str | None:
        value: Any = credentials
        for key in self.token_path:
            value = value.get(key) if isinstance(value, dict) else None
        return value if isinstance(value, str) and value else None

    def plan(self, account: str) -> str | None:
        credentials = self.credentials(account)
        return self.read_plan(credentials) if credentials is not None else None
