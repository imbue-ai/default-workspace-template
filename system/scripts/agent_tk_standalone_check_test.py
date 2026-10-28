"""Tests for the standalone-tk PreToolUse guard.

The guard blocks a `tk start`/`tk close` that shares its Bash tool call with
anything but other tk lifecycle commands run in sequence (no leading `cd`, no
real work, no pipe or background `&`, no output redirection), so the progress
view always sees every transition's output and position. `tk create` and
non-tk commands that merely mention a tk verb are left alone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "agent_tk_standalone_check.py"
_spec = importlib.util.spec_from_file_location("agent_tk_standalone_check", _SCRIPT)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


# Commands that must be ALLOWED (classify returns None).
_ALLOWED = [
    'tk close cod-step-x "summary"',
    "tk start cod-step-x",
    'tk close cod-step-x "did A; then B > C && D"',  # operators only inside the summary
    'tk super close cod-step-x "s"',
    'S1=$(tk create --step "plan a thing")',  # create is exempt
    'tk create --step "plan a"\ntk create --step "plan b"',  # the canonical batched-create form is exempt
    'git commit -m "tk close the bug"',  # tk close only inside the commit message
    "echo 'run tk start later'",
    "git push origin main",
    'system/vendor/tk/ticket close cod-step-x "s"',  # path-prefixed, still standalone
    "tk start $S1",
    # A leading env-var assignment does not suppress or reposition the output,
    # so it is benign -- the shell still prints `Updated ... -> ...` in place.
    'TICKETS_DIR=/x tk close cod-step-x "s"',
    "A=1 B=2 tk start cod-step-x",
    "tk start cod-step-x  # a quick note",  # trailing comment, stripped by the shell
    "tk start cod-step-x  # note; rm -rf /",  # operators live inside the comment
    'tk close cod-step-x "line one\nline two"',  # newline inside the quoted summary
    "  tk start cod-step-x  ",  # surrounding whitespace
    "tk start cod-step-x\n",  # lone trailing newline
    "tk start cod-step-x;",  # a trailing `;` is not a second command
    # A step boundary in one call: close the finished step, start the next.
    'tk close cod-step-a "did a" && tk start cod-step-b',
    'tk close cod-step-a "did a"; tk start cod-step-b',
    'tk close cod-step-a "did a"\ntk start cod-step-b',
    'tk close cod-step-a "did a";\ntk start cod-step-b',  # `;` then a newline
    'tk close cod-step-a "did a" &&\ntk start cod-step-b',  # `&&` then a newline
    'tk close cod-step-a "did a" &&\n\ntk start cod-step-b',  # `&&` then a blank line
    'tk close cod-step-a "did a" && \\\ntk start cod-step-b',  # a line continuation
    'tk create --step "plan a"\n\ntk start cod-step-a',  # a blank line between them
    'tk close cod-step-a "did a" && tk close cod-step-b "did b" && tk start cod-step-c',
    # The plan and its first start in one call.
    'tk create --step "plan a"; tk create --step "plan b"; tk start cod-step-a',
    'tk super close cod-step-a "s"; tk start cod-step-b',
]

# Commands that must be BLOCKED (classify returns a reason string).
_BLOCKED = [
    'cd /home/user/workspace && tk close cod-step-x "s"',
    'cd /home/user/workspace\ntk close cod-step-x "s"',  # the real emaildigest form
    "tk start cod-step-x >/dev/null 2>&1",
    "tk start cod-step-x 2>err",
    'cd /home/user/workspace; tk start cod-step-vl83 >/dev/null 2>&1; sed -n "1,5p" f',
    'tk close cod-step-x "s" | tee log',
    'tk close cod-step-a "x" || tk start cod-step-b',  # `||` runs the start only on failure
    'tk close cod-step-a "x" & tk start cod-step-b',  # the close is backgrounded
    'tk close cod-step-a "x" &\ntk start cod-step-b',  # backgrounded, then a newline
    'tk close cod-step-a "x" ||\ntk start cod-step-b',  # `||` then a newline
    'tk close cod-step-a "x" >/dev/null; tk start cod-step-b',  # the close's output is lost
    "tk start cod-step-x && pytest",  # real work after the start would be hidden with it
    "tk start cod-step-x; sleep 1",
    'tk create --step "plan a"; echo hi; tk start cod-step-a',  # real work between tk commands
    'S1=$(tk create --step "plan a"); tk start "$S1"',  # the capture swallows the Created line
    'S1=$(tk create --step "plan a") && tk start "$S1"',
    "tk start cod-step-x &",
    "tk start cod-step-x\nfoo",  # a second command after an unquoted newline
    "foo\ntk start cod-step-x",  # a command before it via an unquoted newline
    # A trailing `#` comment ends at the newline in a real shell, so a command
    # on the next line still runs -- the comment must not swallow it.
    "tk start cod-step-x # note\nrm -rf /tmp/x",  # second command hidden behind a comment
    "echo hi # note\ntk start cod-step-x",  # tk start runs after a commented first line
]


def test_allows_standalone_and_non_lifecycle_commands() -> None:
    for cmd in _ALLOWED:
        assert checker.classify(cmd) is None, f"should be allowed: {cmd!r}"


def test_blocks_chained_or_redirected_start_close() -> None:
    for cmd in _BLOCKED:
        assert checker.classify(cmd) is not None, f"should be blocked: {cmd!r}"


def test_routes_to_the_right_block_reason() -> None:
    """Each violation kind maps to its distinct reason (not just any block)."""
    assert checker.classify('cd /x && tk close cod-step-x "s"') == checker._BEFORE
    assert (
        checker.classify('S1=$(tk create --step "a"); tk start "$S1"')
        == checker._BEFORE
    )
    assert checker.classify("tk start cod-step-x >/dev/null") == checker._REDIRECT
    assert checker.classify('tk close a "s" && echo hi') == checker._CHAIN
    assert checker.classify("tk start cod-step-x &") == checker._CHAIN
    assert (
        checker.classify('tk close a "s" >/dev/null; tk start b') == checker._REDIRECT
    )


def test_main_exit_codes() -> None:
    """main() exits 0 for a clean close, 2 for a redirected one."""
    assert checker.main(["check", 'tk close cod-step-x "done"']) == 0
    assert checker.main(["check", "tk close cod-step-x >/dev/null"]) == 2
