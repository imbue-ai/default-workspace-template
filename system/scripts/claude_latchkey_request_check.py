#!/usr/bin/env python3
"""Decide whether a Bash command files a latchkey permission request badly.

Takes the command as its single positional argument (passed by
claude_latchkey_request_standalone.sh). Exits 0 to allow; exits 2 with a guiding
stderr message to BLOCK. See the wrapper for the why.

The command structure (which segments POST to the permission-requests host,
whether one is chained or redirected) comes from the shared `tk_command_parsing`
parser -- despite the name it is a general shell-command splitter, tokenizing
with `shlex` so quoting, escapes, comments, env-var prefixes, and operators are
interpreted the way a shell would. A rationale string that happens to mention
`&&` or `>` therefore stays inside one token and never trips the checks.

This hook runs under a bare `python3` with no virtualenv (see the wrapper), so
it puts the parser lib's source directory on `sys.path` explicitly rather than
relying on an installed package; the lib is stdlib-only for the same reason.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

from tk_command_parsing.parser import CommandSegment, parse_command

# The reserved latchkey host an agent POSTs to when asking the user to approve an
# action, and the POST method flag that distinguishes filing a request from
# reading the queue. Both come from the transcript parser's detector
# (`PERMISSION_REQUEST_HOST` / `is_permission_request_call` in
# system/apps/system_interface/.../harnesses/tool_output.py), so a call this gate
# blocks is a call that renders as a permission card.
_PERMISSION_REQUEST_HOST = "latchkey-self.invalid/permission-requests"
_METHOD_FLAGS = ("-X", "--request")
_POST_FLAG_RE = re.compile(r"(?:-X|--request)=?POST", re.IGNORECASE)

# curl's own ways of writing the response body to a file instead of echoing it on
# stdout. They take the gateway's echoed object out of the tool result exactly as
# `> file` does, so the gate reads them as a redirect. `-O` / `--remote-name` take
# no value; `-o` / `--output` take the next token or a joined one.
_OUTPUT_FLAGS = ("-o", "--output", "-O", "--remote-name")
_OUTPUT_FLAG_RE = re.compile(r"-o.+|--output=.+")

_MULTIPLE = "the call files more than one permission request"
_REDIRECT = (
    "its output is redirected, written to a file by curl itself, or its input "
    "replaced (`>`, `>>`, `2>`, `&>`, `-o`, `-O`, `<`, a heredoc)"
)
_CHAIN = (
    "it is chained with, piped into, or preceded by another command "
    "(`&&`, `||`, `;`, `|`, `&`, a leading `cd`, or a newline)"
)


def _is_argument(word: str) -> bool:
    """True when `word` is a single shell argument rather than a quoted run of
    prose. The lexer has already stripped the quotes, so whitespace inside a
    token is what is left of them."""
    return not any(char.isspace() for char in word)


def _is_request_url(word: str) -> bool:
    """True when `word` is the permission-requests URL itself."""
    return _PERMISSION_REQUEST_HOST in word and _is_argument(word)


def _sets_post_method(words: tuple[str, ...]) -> bool:
    """True when `words` carry curl's POST flag, joined (`-XPOST`,
    `--request=POST`) or separated (`-X POST`)."""
    for i, word in enumerate(words):
        if _POST_FLAG_RE.fullmatch(word):
            return True
        if word in _METHOD_FLAGS and i + 1 < len(words):
            if words[i + 1].upper() == "POST":
                return True
    return False


def _writes_body_to_file(words: tuple[str, ...]) -> bool:
    """True when `words` carry one of curl's write-the-body-to-a-file flags,
    separated (`-o out.json`), joined (`-oout.json`, `--output=out.json`), or
    valueless (`-O`)."""
    return any(
        word in _OUTPUT_FLAGS or _OUTPUT_FLAG_RE.fullmatch(word) is not None
        for word in words
    )


def _request_count(segment: CommandSegment) -> int:
    """How many permission requests this one command files.

    A command has to FILE a request to count, not merely quote one: the host
    must be passed as its own argument (the URL curl receives) and the method as
    its own flag token. A commit message, grep pattern, or doc snippet that
    spells out the canonical request keeps both inside a single token, along
    with the prose around them. The transcript parser's looser whole-input match
    cannot tell those apart; this gate must, because it blocks.

    Counted per URL rather than per command, because curl performs the request
    once for each URL it is given (and `--next` lets each carry its own body),
    so one invocation can file several.
    """
    if not _sets_post_method(segment.words):
        return 0
    return sum(1 for word in segment.words if _is_request_url(word))


def classify(cmd: str) -> str | None:
    """Return the violation reason if `cmd` files a permission request badly.

    Returns None when the command is allowed: either it files no permission
    request at all (including a GET of the queue, or a command that merely
    mentions the host inside a quoted string), or it files exactly one as the
    whole tool call with its output untouched.
    """
    parsed = parse_command(cmd)
    if parsed is None:
        return None

    filings = [(seg, _request_count(seg)) for seg in parsed.segments]
    requests = [seg for seg, count in filings if count > 0]
    if not requests:
        return None
    if sum(count for _, count in filings) > 1:
        return _MULTIPLE
    request = requests[0]
    if request.has_redirect or _writes_body_to_file(request.words):
        return _REDIRECT
    if any(seg.terminator == "&" for seg in parsed.segments):
        # Backgrounded: the call returns before the gateway answers, so its echo
        # never lands in this tool result. Read across every segment, because
        # grouping (`( <request> ) &`) puts the `&` on the empty segment after
        # `)` rather than on the request's own -- and an `&` that separates a
        # real second command is caught by the word-bearing count below anyway.
        return _CHAIN
    # A trailing separator (`cmd;`) leaves an empty tail segment, which is not a
    # second command; anything with words is.
    if len([seg for seg in parsed.segments if seg.words]) > 1:
        # A control operator split the stream, so another command runs alongside
        # the request -- consuming its output (`| jq`) or displacing it.
        return _CHAIN
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    command = args[1] if len(args) > 1 else ""
    violation = classify(command)
    if violation is None:
        return 0

    sys.stderr.write(
        "Blocked: file ONE latchkey permission request per tool call, as the only "
        "command in it -- " + violation + ".\n\n"
        "The chat renders each request as a card the user acts on, and builds it from "
        "that single tool call: what to show comes from the command, and the button "
        "that opens the approval dialog comes from the request object the gateway "
        "echoes on stdout. A second request in the same call is never shown (the user "
        "cannot answer a request they cannot see), and redirecting or piping the "
        "output away leaves the card with no button.\n\n"
        "Re-run with just the one request, output untouched:\n"
        "  latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \\\n"
        "    -H 'Content-Type: application/json' \\\n"
        '    -d \'{"agent_id": "\'"$MNGR_AGENT_ID"\'", ...}\'\n\n'
        "Then wait for the system message carrying the user's verdict before filing "
        "the next one.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
