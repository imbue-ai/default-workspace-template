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

from tk_command_parsing.parser import CommandSegment, command_basename, parse_command

# The reserved latchkey host an agent POSTs to when asking the user to approve an
# action, and the POST flag that distinguishes filing a request from reading the
# queue. Both are taken from the transcript parser's detector
# (`PERMISSION_REQUEST_HOST` / `is_permission_request_call` in
# system/apps/system_interface/.../harnesses/tool_output.py) so the calls this
# gate governs are the calls that render as a permission card.
_PERMISSION_REQUEST_HOST = "latchkey-self.invalid/permission-requests"
_POST_RE = re.compile(r"-X\s*POST|--request\s*POST", re.IGNORECASE)

# The commands that can actually FILE a request. The gate is deliberately
# narrower than the transcript parser's whole-input match: quotes are gone by the
# time a segment is tokenized, so without this an unrelated command that merely
# quotes the request (a commit message, a grep pattern, a doc snippet) would read
# as a filing and be blocked for chaining or redirecting.
_REQUEST_COMMANDS = ("latchkey", "curl")

_MULTIPLE = "the call files more than one permission request"
_REDIRECT = "its output is redirected (`>`, `>>`, `2>`, `&>`, ...)"
_CHAIN = (
    "it is chained with, piped into, or preceded by another command "
    "(`&&`, `||`, `;`, `|`, `&`, a leading `cd`, or a newline)"
)


def _is_permission_request(segment: CommandSegment) -> bool:
    """True when this one command POSTs to the permission-requests host."""
    if command_basename(segment) not in _REQUEST_COMMANDS:
        return False
    if not any(_PERMISSION_REQUEST_HOST in word for word in segment.words):
        return False
    return _POST_RE.search(" ".join(segment.words)) is not None


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

    requests = [seg for seg in parsed.segments if _is_permission_request(seg)]
    if not requests:
        return None
    if len(requests) > 1:
        return _MULTIPLE
    request = requests[0]
    if request.has_redirect:
        return _REDIRECT
    if request.terminator == "&":
        # Backgrounded: the call returns before the gateway answers, so its echo
        # never lands in this tool result.
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
