"""Deciding whether a harness's error text is an authentication failure.

Every harness fails auth differently and says so in a different place, but the ANSWER is the
same everywhere: the account this chat runs on no longer works, and the user needs different
credentials. So the vocabulary is shared and only the extraction is per-harness.

claude had its own copy of this list (`claude/auth_patterns.py`, sourced from claude's own
errors reference). It is folded in here: two tables answering one question is how they drift,
and claude's entries are not actually claude-specific -- a credit balance and a proxy budget
are facts about a billing relationship, not about a CLI.

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
    r"oauth token does not meet scope requirements?",
    # From claude's errors reference.
    r"invalid authentication credentials",
    r"organization has been disabled",
    # Exhausted ENTITLEMENT rather than a rejected credential, and deliberately in the same
    # family. Neither is an authentication failure in the HTTP sense -- Anthropic reports
    # exhausted third-party usage as a 400 `invalid_request_error`, codex reports a spent quota
    # as `usage_limit_exceeded` -- but the only way forward for both is different credentials,
    # which is exactly what the subtext offers.
    r"credit balance is too low",
    r"usage_limit_exceeded",
    r"exceeded your current quota",
    # LiteLLM proxy rejections. The Imbue sign-in mode routes claude through a proxy with a
    # per-key rolling budget; exhausting it is the same dead end.
    r"budget has been exceeded",
    r"exceededbudget",
    r"authentication error, invalid proxy server token passed",
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
