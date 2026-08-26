"""Regex patterns that flag a "you can't reach the model with these credentials"
failure in an agent's transcript text.

Shared by every harness (claude, pi, codex): the patterns are matched against
whatever failure string that harness surfaces, so a harness only has to hand its
error text to :func:`is_auth_error_text`.

Sourced from the official Claude Code errors reference
(code.claude.com/docs/en/errors), the Anthropic API errors reference, and
surface forms captured live. Kept in a dedicated module so the list can be
extended without touching parser logic.

Scope note: this is the *credentials/quota* family, deliberately broader than
literal 401s. Exhausted credit, an exceeded proxy budget, and an account whose
third-party usage has run out are not authentication failures in the HTTP sense,
but they share the only recovery the user has -- point the conversation at a
different provider -- and the frontend gives the whole family that one subtext.
Ordinary request failures (400/429/5xx) stay with :mod:`error_patterns`.
"""

from __future__ import annotations

import re

_PATTERN_SOURCES: tuple[str, ...] = (
    r"Not logged in\s*[\u00b7\u2022\-]\s*Please run /login",
    r"Invalid API key",
    r"OAuth token (?:has been revoked|has expired|does not meet scope requirements?)",
    r'"type"\s*:\s*"authentication_error"',
    r"API Error:\s*401\b",
    # pi surfaces a bare "<status> {json}" with no "API Error:" prefix; anchored so a
    # 401 merely mentioned mid-message is not read as a failure (same rule as
    # error_patterns._BARE_STATUS_RE).
    r"^\s*401\b",
    r"Invalid authentication credentials",
    r"Credit balance is too low",
    r"organization has been disabled",
    # Anthropic's third-party-usage exhaustion, captured live from pi:
    # "Third-party apps now draw from your extra usage, not your plan limits.
    #  Add more at claude.ai/settings/usage and keep going."
    # It arrives as a 400 invalid_request_error, so error_patterns would file it as an
    # ordinary bad request -- but the user's only move is to add usage or switch
    # provider, which is exactly this family's subtext.
    r"claude\.ai/settings/usage",
    r"draw from your extra usage",
    # LiteLLM proxy rejections (the Imbue sign-in mode routes claude through
    # a LiteLLM proxy with a per-key rolling budget). Budget exhaustion is
    # not strictly an auth failure, but the recovery is the same one this
    # family offers (wait for the daily reset, or point the conversation at
    # another provider), so it is flagged the same way. Sourced from litellm's
    # proxy error strings; tighten against real captured transcripts if they misfire.
    r"Budget has been exceeded",
    r"ExceededBudget",
    r"Authentication Error, Invalid proxy server token passed",
)

AUTH_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(source, re.IGNORECASE) for source in _PATTERN_SOURCES
)


def is_auth_error_text(text: str) -> bool:
    """Return True if any known credentials/quota failure pattern appears in `text`."""
    if not text:
        return False
    for pattern in AUTH_ERROR_PATTERNS:
        if pattern.search(text):
            return True
    return False
