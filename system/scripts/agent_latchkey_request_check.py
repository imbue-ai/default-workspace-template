#!/usr/bin/env python3
"""Decide whether a Bash command files a latchkey permission request badly.

Takes the command as its positional argument, optionally preceded by
``--backgrounded`` when the tool call runs the command in the background (both
passed by agent_latchkey_request_standalone.sh, which reads them out of the
hook payload). Exits 0 to allow; exits 2 with a guiding stderr message to BLOCK.
See the wrapper for the why.

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

from tk_command_parsing.parser import CommandSegment
from tk_command_parsing.parser import parse_command

# The reserved latchkey host an agent POSTs to when asking the user to approve an
# action, and the POST method flag that distinguishes filing a request from reading
# the queue. The host is verbatim `PERMISSION_REQUEST_HOST` from
# system/apps/system_interface/imbue/system_interface/harnesses/tool_output.py, the
# reader this gate exists for (the wrapper names the rest of that seam). The method
# match is a superset of that parser's `-X\s*POST|--request\s*POST`: token-wise, and
# accepting the `=` form, so `--request=POST` -- which curl honors -- is gated too.
_PERMISSION_REQUEST_HOST = "latchkey-self.invalid/permission-requests"
# Lowercased, because the flag is matched case-insensitively -- as the parser's
# regex and the joined form below both are.
_METHOD_FLAGS = ("-x", "--request")
_POST_FLAG_RE = re.compile(r"(?:-X|--request)=?POST", re.IGNORECASE)

# curl's own ways of writing the response body to a file instead of echoing it on
# stdout, which takes the gateway's object out of the tool result exactly as `> file`
# does. Matched with `fullmatch`; `-[A-Za-z]*[oO]` accepts the bundled forms (`-so
# /tmp/x`, `-fsSLo out.json`) by requiring the flag to be the cluster's LAST letter,
# which is what keeps `-XPOST` out -- and why the bundled joined-value form
# (`-soout.json`) is left alone, since no pattern separates it from `-XPOST` without
# modelling which short flags consume a value.
_OUTPUT_FLAG_RE = re.compile(r"-[A-Za-z]*[oO]|-o.+|--output(?:=.+)?|--remote-name")

_MULTIPLE = "the call files more than one permission request"
_REDIRECT = (
    "its output is redirected, written to a file by curl itself, or its input "
    "replaced (`>`, `>>`, `2>`, `&>`, `-o`, `-O`, `<`, a heredoc)"
)
_CHAIN = (
    "it is chained with, piped into, or preceded by another command "
    "(`&&`, `||`, `;`, `|`, `&`, a leading `cd`, or a newline)"
)
_BACKGROUND = (
    "the tool call itself runs in the background (`run_in_background`), so its "
    "result is a shell id rather than the gateway's echo"
)

# The flag the wrapper adds when the hook payload says the tool call is
# backgrounded. It is a property of the CALL, not of the command text, so it
# cannot be read out of the command the way everything else here is.
_BACKGROUNDED_FLAG = "--backgrounded"


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
    `--request=POST`) or separated (`-X POST`). Flag and value are both matched
    case-insensitively, so no spelling the parser's `re.IGNORECASE` regex reads
    as a filing is missed here."""
    for i, word in enumerate(words):
        if _POST_FLAG_RE.fullmatch(word):
            return True
        if word.lower() in _METHOD_FLAGS and i + 1 < len(words):
            if words[i + 1].upper() == "POST":
                return True
    return False


def _writes_body_to_file(words: tuple[str, ...]) -> bool:
    """True when `words` carry one of curl's write-the-body-to-a-file flags,
    separated (`-o out.json`), joined (`-oout.json`, `--output=out.json`),
    valueless (`-O`), or bundled with other short flags (`-so out.json`)."""
    return any(_OUTPUT_FLAG_RE.fullmatch(word) is not None for word in words)


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


def classify(cmd: str, is_backgrounded: bool = False) -> str | None:
    """Return the violation reason if `cmd` files a permission request badly.

    ``is_backgrounded`` is whether the tool call runs `cmd` in the background
    (claude's Bash ``run_in_background``), which sends the output somewhere the
    result cannot carry it -- the one input here that the command text does not
    hold.

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
    if is_backgrounded:
        # Same failure as a trailing `&` below -- the call returns a shell id
        # before the gateway answers, so its echo never lands in this tool
        # result -- reached through the tool's own flag instead of the command.
        return _BACKGROUND
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
    args = (sys.argv if argv is None else argv)[1:]
    is_backgrounded = _BACKGROUNDED_FLAG in args
    positional = [arg for arg in args if arg != _BACKGROUNDED_FLAG]
    command = positional[0] if positional else ""
    violation = classify(command, is_backgrounded=is_backgrounded)
    if violation is None:
        return 0

    sys.stderr.write(
        "Blocked: file ONE latchkey permission request per tool call, as the only "
        "command in it -- " + violation + ".\n\n"
        "The chat renders each request as a card the user acts on, and builds it from "
        "that single tool call: what to show comes from the command, and the button "
        "that opens the approval dialog comes from the request object the gateway "
        "echoes on stdout. A second request in the same call is never shown (the user "
        "cannot answer a request they cannot see), and anything that keeps the echo out "
        "of this call's result -- redirecting or piping it away, or backgrounding the "
        "call so the result is a shell id -- leaves the card with no button.\n\n"
        "Re-run with just the one request, in the foreground, output untouched:\n"
        "  latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \\\n"
        "    -H 'Content-Type: application/json' \\\n"
        '    -d \'{"agent_id": "\'"$MNGR_AGENT_ID"\'", ...}\'\n\n'
        "Filing another request straight after this one is fine -- it just needs a "
        "tool call of its own.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
