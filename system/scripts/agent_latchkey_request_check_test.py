"""Tests for the one-permission-request-per-call PreToolUse guard.

The guard blocks a latchkey permission request that is batched with another
request, chained with another command, has its output redirected, or is filed by
a backgrounded tool call, so the chat always sees one request per call with the
gateway's echoed object intact. Every other latchkey call -- including reading
the queue -- is left alone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "agent_latchkey_request_check.py"
_spec = importlib.util.spec_from_file_location("agent_latchkey_request_check", _SCRIPT)
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
# ... and the way every skill actually lays it out: one flag per line, joined by
# backslash continuations. A distinct case, not a reformatting: the escaped
# newline survives the newline-to-`;` pass and shlex emits each continuation as
# its own `"\n"` word token inside the segment, so the words the gate classifies
# are not the ones the single-line form produces.
_MULTILINE_REQUEST = (
    f"latchkey curl -XPOST {_HOST} \\\n"
    "  -H 'Content-Type: application/json' \\\n"
    f"  -d {_BODY}"
)


# Commands that must be ALLOWED (classify returns None).
_ALLOWED = [
    _REQUEST,
    _MULTILINE_REQUEST,
    _REQUEST.replace("-XPOST", "-X POST"),
    _REQUEST.replace("-XPOST", "--request POST"),
    f"  {_REQUEST}  ",  # surrounding whitespace
    f"{_REQUEST}\n",  # lone trailing newline
    f"{_REQUEST};",  # trailing separator, not a second command
    f"( {_REQUEST} )",  # a plain subshell still writes the echo to the result
    # A rationale that quotes shell operators: they live inside one token.
    f"""latchkey curl -XPOST {_HOST} -d '{{"rationale": "read A && B > C | D"}}'""",
    # Reading the queue or the available permissions is not a filing: no POST.
    f"latchkey curl {_HOST} | jq .",
    "latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules",
    "latchkey curl http://latchkey-self.invalid/permissions/available/discord",
    # A non-latchkey command that merely mentions the request in a quoted string.
    f"""echo "post -XPOST {_HOST} next" """,
    # ... including when it is chained or redirected. The lexer strips the quotes,
    # so what marks these as mentions is that the host and the flag arrive inside
    # one token, wrapped in the prose around them, rather than as arguments.
    f"""git commit -m "document -XPOST {_HOST} usage" && git push""",
    f"""grep -rn "curl -XPOST {_HOST}" system/ > /tmp/hits.txt""",
    "git push origin main",
    # Ordinary curl flags are not mistaken for the write-to-a-file ones. `-XPOST`
    # is the case that matters: it is a single-dash letter cluster containing an
    # `O`, and only "the flag is the LAST letter" keeps it out.
    _REQUEST.replace("-XPOST", "-XPOST -sSL"),
    _REQUEST.replace("-XPOST", "-XPOST --oauth2-bearer tok"),
]

# Commands that must be BLOCKED (classify returns a reason string).
_BLOCKED = [
    f"{_REQUEST} && {_REQUEST}",  # two requests batched into one call
    f"{_REQUEST}\n{_REQUEST}",  # ... via a newline
    # ... in every spelling of the method flag the transcript parser's
    # case-insensitive regex reads as a filing, so nothing it cards escapes here.
    (f"{_REQUEST} && {_REQUEST}").replace("-XPOST", "-x POST"),
    (f"{_REQUEST} && {_REQUEST}").replace("-XPOST", "--REQUEST=POST"),
    # ... and inside ONE curl, which runs once per URL it is handed.
    f"{_REQUEST} {_HOST}",
    f"{_REQUEST} --next -XPOST {_HOST} -d {_BODY}",
    f"{_REQUEST} > /tmp/request.json",  # the echoed object never reaches the chat
    f"{_REQUEST} 2>/dev/null",
    # curl writes the body to a file itself, which takes the echoed object away
    # just as `> file` does -- separated, joined, valueless, and bundled with
    # other short flags (`curl -so out.json` is the everyday spelling).
    f"{_REQUEST} -o /tmp/request.json",
    f"{_REQUEST} --output /tmp/request.json",
    f"{_REQUEST} --output=/tmp/request.json",
    f"{_REQUEST} -o/tmp/request.json",
    f"{_REQUEST} -O",
    f"{_REQUEST} -so /tmp/request.json",
    f"{_REQUEST} -fsSLo /tmp/request.json",
    f"{_REQUEST} -sO",
    # An input redirect is blocked too: the parser records only *that* a segment
    # is redirected, and a heredoc body re-enters the parse as further commands.
    f"{_REQUEST} -d @- <<EOF\n{{}}\nEOF",
    f"{_REQUEST} | jq .request_id",  # ... consumed by another command
    f"{_REQUEST} | tee /tmp/request.json",
    f"cd /home/user/workspace && {_REQUEST}",  # a command runs before it
    f"{_REQUEST} && echo done",
    f"{_REQUEST} &",
    # A `#` comment ends at the newline in a real shell, so the request on the
    # next line still runs -- the comment must not hide it.
    f"echo hi # note\n{_REQUEST}",
    # A wrapper in front of the request does not change what reaches the chat:
    # the echoed object is still piped away, still backgrounded, still doubled.
    f"timeout 30 {_REQUEST} | jq .request_id",
    f"nohup {_REQUEST} &",
    f"timeout 30 {_REQUEST} && timeout 30 {_REQUEST}",
    # Grouping does not either -- both bash forms, though only `(` and `)` are
    # operators to the lexer.
    f"( {_REQUEST} ) > /tmp/request.json",
    f"{{ {_REQUEST} ; }} | jq .request_id",
    f"( {_REQUEST} ) &",  # the `&` lands on the segment after `)`, not the request
    # The continuations do not hide a violation either: the gate reads through
    # them to the second request and to the pipe.
    f"{_MULTILINE_REQUEST} && {_MULTILINE_REQUEST}",
    f"{_MULTILINE_REQUEST} | jq .request_id",
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
    assert checker.classify(f"{_REQUEST} {_HOST}") == checker._MULTIPLE
    assert checker.classify(f"{_REQUEST} > /tmp/out.json") == checker._REDIRECT
    assert checker.classify(f"{_REQUEST} -o /tmp/out.json") == checker._REDIRECT
    assert checker.classify(f"{_REQUEST} -so /tmp/out.json") == checker._REDIRECT
    assert checker.classify(f"{_REQUEST} | jq .") == checker._CHAIN


def test_blocks_a_request_filed_by_a_backgrounded_call() -> None:
    """A backgrounded call keeps the echo out of its result just as `&` does --
    the result is a shell id -- and the flag is the only such fact the command
    text cannot carry."""
    assert checker.classify(_REQUEST, is_backgrounded=True) == checker._BACKGROUND
    assert (
        checker.classify(_MULTILINE_REQUEST, is_backgrounded=True)
        == checker._BACKGROUND
    )
    # ... and it is only the gate's business when a request is actually filed.
    assert (
        checker.classify("latchkey curl http://example.invalid/x", is_backgrounded=True)
        is None
    )
    assert (
        checker.classify(f"latchkey curl {_HOST} | jq .", is_backgrounded=True) is None
    )


def test_main_exit_codes() -> None:
    """main() exits 0 for a lone request, 2 for a redirected or backgrounded one."""
    assert checker.main(["check", _REQUEST]) == 0
    assert checker.main(["check", f"{_REQUEST} > /tmp/out.json"]) == 2
    assert checker.main(["check", "--backgrounded", _REQUEST]) == 2
    # The flag is not mistaken for the command, and its absence leaves the
    # positional-only call (the form pi's bridge uses) reading as foreground.
    assert checker.main(["check", "--backgrounded"]) == 0
    assert checker.main(["check", _REQUEST]) == 0
