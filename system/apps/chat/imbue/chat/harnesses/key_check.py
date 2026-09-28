"""Asking a provider whether a pasted API key works, before it is saved.

The harnesses' own sign-in probes only see whether a key is present, so a key with a typo in it
used to be saved and then fail the account's first chat turn. Listing the provider's models is the
cheapest request that needs a valid key.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final
from typing import assert_never

import httpx

_CHECK_TIMEOUT_SECONDS: Final = 10.0
_ANTHROPIC_BASE_URL: Final = "https://api.anthropic.com"
_ANTHROPIC_VERSION: Final = "2023-06-01"
_OPENAI_MODELS_URL: Final = "https://api.openai.com/v1/models"
_REJECTING_STATUSES: Final = frozenset({401, 403})


class CheckedProvider(StrEnum):
    """The providers whose keys are checked."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class KeyCheck(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    # The provider could not be asked, or answered something that is not a verdict on the key.
    UNCHECKED = "unchecked"


PROVIDER_DISPLAY: Final[dict[CheckedProvider, str]] = {
    CheckedProvider.ANTHROPIC: "Anthropic",
    CheckedProvider.OPENAI: "OpenAI",
}


def check_key(provider: CheckedProvider, api_key: str, base_url: str | None = None) -> KeyCheck:
    """Whether `provider` accepts `api_key`, asked of `base_url` when the key belongs to a proxy."""
    match provider:
        case CheckedProvider.ANTHROPIC:
            url = f"{(base_url or _ANTHROPIC_BASE_URL).rstrip('/')}/v1/models"
            headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
        case CheckedProvider.OPENAI:
            url = _OPENAI_MODELS_URL
            headers = {"Authorization": f"Bearer {api_key}"}
        case _ as unreachable:
            assert_never(unreachable)
    try:
        response = httpx.get(url, headers=headers, timeout=_CHECK_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return KeyCheck.UNCHECKED
    if response.status_code == 200:
        return KeyCheck.ACCEPTED
    if response.status_code in _REJECTING_STATUSES:
        return KeyCheck.REJECTED
    return KeyCheck.UNCHECKED
