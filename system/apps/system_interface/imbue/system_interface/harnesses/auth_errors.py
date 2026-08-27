"""Deciding whether a harness's error text is an authentication failure.

Every harness fails auth differently and says so in a different place, but the ANSWER is the
same everywhere: the account this chat runs on no longer works, and the user has to sign in
again. So the vocabulary is shared and the extraction is per-harness -- `claude/auth_patterns`
predates this and stays where it is, since its list is long, claude-specific, and sourced from
claude's own errors reference.

Measured against the pinned CLIs rather than guessed, because each of these was previously
filled in as `False` with a comment saying the shape was unknown:

* codex writes it to the transcript, not (as its parser's comment claimed) only to
  `logs_2.sqlite`: `task_complete.error.message` carries
  `unexpected status 401 Unauthorized: Incorrect API key provided: ... auth error: 401,
  auth error code: invalid_api_key`.
* pi writes an assistant message with `stopReason: "error"` and `errorMessage` holding the
  provider's raw body: `401 {"type":"error","error":{"type":"authentication_error", ...}}`.
* agy prints `Please sign in to view available models.` from its own commands, and surfaces a
  provider 401 in its error steps.

The patterns are deliberately broad on the http status and the provider's own error type,
because those are the parts that do not change when a provider rewords its prose.
"""

from __future__ import annotations

import re
from typing import Final

# Shared across harnesses: an HTTP 401/403, a provider's structured auth error type, or a CLI
# telling the user in its own words to sign in. Matched case-insensitively.
_SOURCES: Final[tuple[str, ...]] = (
    # Status codes, however the CLI frames them ("401 Unauthorized", "status 401", "API Error: 401").
    r"\b(?:401|403)\b[^\n]{0,40}(?:unauthorized|forbidden|invalid|expired|auth)",
    r"(?:status|error)[^\n]{0,10}\b(?:401|403)\b",
    # The structured error type every Anthropic-shaped and OpenAI-shaped API returns.
    r'"type"\s*:\s*"(?:authentication_error|invalid_request_error)"',
    r'"code"\s*:\s*"(?:invalid_api_key|invalid_token|token_expired)"',
    r"auth error code:\s*\w+",
    # Prose the CLIs use when they know it is the credential.
    r"incorrect api key",
    r"invalid api key",
    r"api key is invalid",
    r"invalid authentication credentials",
    r"please sign in",
    r"not (?:logged|signed) in",
    r"oauth token (?:has been revoked|has expired|is invalid)",
    r"authentication (?:failed|required|error)",
)

AUTH_ERROR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(source, re.IGNORECASE) for source in _SOURCES
)


def is_auth_error_text(text: str) -> bool:
    """Whether `text` is a harness telling us its credential is the problem.

    False for an empty string, and for an error that is merely an error: a rate limit, a
    network blip and a model refusal all fail a turn, but only this one is fixed by signing
    in, and only this one should send the user to the provider chooser.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in AUTH_ERROR_PATTERNS)
