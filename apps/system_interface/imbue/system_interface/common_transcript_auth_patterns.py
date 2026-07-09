"""Per-harness regex patterns that flag an auth failure in assistant text.

Mirrors ``claude_auth_patterns.py``'s role for Claude, but keyed by harness
since each CLI's error text is its own. Patterns are seeded only from text
actually observed from a live failure, not guessed -- a wrong-but-confident
pattern is worse than no detection (it would silently misclassify or miss a
real error), so an empty entry here is an honest "not yet observed", not an
oversight.

codex: seeded from a live `codex exec` run against an expired
`~/.codex/auth.json` refresh token (mngr's codex e2e release test hit this;
see changelog/multi-harness-support.md). Note this only catches an auth
failure that surfaces as assistant-turn text -- codex's own headless
`exec` mode instead completed the turn with `last_agent_message: None` and
no assistant text at all, a distinct failure shape (silent empty
completion) this text-matching approach cannot see. Left as a known gap
rather than papering over it with an unverified heuristic.

antigravity, opencode: no live-confirmed auth-error text yet.
"""

from __future__ import annotations

import re

_CODEX_PATTERN_SOURCES: tuple[str, ...] = (
    r"token_expired",
    r"could not be refreshed",
    r"401 Unauthorized",
    r"Please log out and sign in again",
)

_PATTERNS_BY_HARNESS: dict[str, tuple[re.Pattern[str], ...]] = {
    "codex": tuple(re.compile(source, re.IGNORECASE) for source in _CODEX_PATTERN_SOURCES),
}


def is_auth_error_text(harness: str, text: str) -> bool:
    """Return True if a known auth-error pattern for `harness` appears in `text`."""
    if not text:
        return False
    for pattern in _PATTERNS_BY_HARNESS.get(harness, ()):
        if pattern.search(text):
            return True
    return False
