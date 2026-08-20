"""Tests for the one-permission-request-per-call PreToolUse guard.

The guard blocks a latchkey permission request that is batched with another
request, chained with another command, or has its output redirected, so the chat
always sees one request per call with the gateway's echoed object intact. Every
other latchkey call -- including reading the queue -- is left alone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "claude_latchkey_request_check.py"
_spec = importlib.util.spec_from_file_location("claude_latchkey_request_check", _SCRIPT)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

_HOST = "http://latchkey-self.invalid/permission-requests"
_BODY = (
    """'{"agent_id": "a1", "type": "predefined", """
    """"payload": {"scope": "discord-api", "permissions": ["discord-read-all"]}, """
    """"rationale": "read your servers"}'"""
)
# The canonical filing, exactly as the latchkey skill documents it.
_REQUEST = (
    f"latchkey curl -XPOST {_HOST} -H 'Content-Type: application/json' -d {_BODY}"
)


# Commands that must be ALLOWED (classify returns None).
_ALLOWED = [
    _REQUEST,
    _REQUEST.replace("-XPOST", "-X POST"),
    _REQUEST.replace("-XPOST", "--request POST"),
    f"  {_REQUEST}  ",  # surrounding whitespace
    f"{_REQUEST}\n",  # lone trailing newline
    f"{_REQUEST};",  # trailing separator, not a second command
    # A rationale that quotes shell operators: they live inside one token.
    f"""latchkey curl -XPOST {_HOST} -d '{{"rationale": "read A && B > C | D"}}'""",
    # Reading the queue or the available permissions is not a filing: no POST.
    f"latchkey curl {_HOST} | jq .",
    "latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules",
    "latchkey curl http://latchkey-self.invalid/permissions/available/discord",
    # A non-latchkey command that merely mentions the request in a quoted string.
    f"""echo "post -XPOST {_HOST} next" """,
    # ... including when it is chained or redirected: the quotes are gone by the
    # time the segment is tokenized, so only the segment's own command tells a
    # filing from a mention of one.
    f"""git commit -m "document -XPOST {_HOST} usage" && git push""",
    f"""grep -rn "curl -XPOST {_HOST}" system/ > /tmp/hits.txt""",
    "git push origin main",
]

# Commands that must be BLOCKED (classify returns a reason string).
_BLOCKED = [
    f"{_REQUEST} && {_REQUEST}",  # two requests batched into one call
    f"{_REQUEST}\n{_REQUEST}",  # ... via a newline
    f"{_REQUEST} > /tmp/request.json",  # the echoed object never reaches the chat
    f"{_REQUEST} 2>/dev/null",
    f"{_REQUEST} | jq .request_id",  # ... consumed by another command
    f"{_REQUEST} | tee /tmp/request.json",
    f"cd /home/user/workspace && {_REQUEST}",  # a command runs before it
    f"{_REQUEST} && echo done",
    f"{_REQUEST} &",
    # A `#` comment ends at the newline in a real shell, so the request on the
    # next line still runs -- the comment must not hide it.
    f"echo hi # note\n{_REQUEST}",
]


def test_allows_a_lone_untouched_request_and_every_other_call() -> None:
    for cmd in _ALLOWED:
        assert checker.classify(cmd) is None, f"should be allowed: {cmd!r}"


def test_blocks_batched_chained_or_redirected_requests() -> None:
    for cmd in _BLOCKED:
        assert checker.classify(cmd) is not None, f"should be blocked: {cmd!r}"


def test_routes_to_the_right_block_reason() -> None:
    """Each violation kind maps to its distinct reason (not just any block)."""
    assert checker.classify(f"{_REQUEST} && {_REQUEST}") == checker._MULTIPLE
    assert checker.classify(f"{_REQUEST} > /tmp/out.json") == checker._REDIRECT
    assert checker.classify(f"{_REQUEST} | jq .") == checker._CHAIN


def test_main_exit_codes() -> None:
    """main() exits 0 for a lone request, 2 for a redirected one."""
    assert checker.main(["check", _REQUEST]) == 0
    assert checker.main(["check", f"{_REQUEST} > /tmp/out.json"]) == 2
