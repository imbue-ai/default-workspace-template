"""Tests for the standalone-tk PreToolUse guard.

The guard blocks a `tk start`/`tk close` that is not run as the only command in
a Bash tool call (no leading `cd`, chaining, or output redirection), so the
progress view always sees the transition's output and position. It also blocks a
`tk create --step` whose output is redirected or that passes more than one
`--step` (which silently makes just one step) -- while still allowing a batch of
separate, well-formed creates. Non-tk commands that merely mention a tk verb are
left alone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "claude_tk_standalone_check.py"
_spec = importlib.util.spec_from_file_location("claude_tk_standalone_check", _SCRIPT)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


# Commands that must be ALLOWED (classify returns None).
_ALLOWED = [
    'tk close cod-step-x "summary"',
    "tk start cod-step-x",
    'tk close cod-step-x "did A; then B > C && D"',  # operators only inside the summary
    'tk super close cod-step-x "s"',
    'S1=$(tk create --step "plan a thing")',  # single create, output captured into a var
    'tk create --step "plan a"\ntk create --step "plan b"',  # the canonical batched-create form
    'tk create --step "plan a"; tk create --step "plan b"',  # batched with `;` instead of a newline
    'tk create --step "just one"',  # a lone create with one step
    'tk create --step="joined one"',  # the joined --step=value form, single step
    'git commit -m "tk close the bug"',  # tk close only inside the commit message
    "echo 'run tk start later'",
    "git push origin main",
    'vendor/tk/ticket close cod-step-x "s"',  # path-prefixed, still standalone
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
]

# Commands that must be BLOCKED (classify returns a reason string).
_BLOCKED = [
    'cd /mngr/code && tk close cod-step-x "s"',
    'cd /mngr/code\ntk close cod-step-x "s"',  # the real emaildigest form
    "tk start cod-step-x >/dev/null 2>&1",
    "tk start cod-step-x 2>err",
    'cd /mngr/code; tk start cod-step-vl83 >/dev/null 2>&1; sed -n "1,5p" f',
    'tk close a "x" && tk start b',
    'tk close cod-step-x "s" | tee log',
    "tk start cod-step-x &",
    "tk start cod-step-x\nfoo",  # a second command after an unquoted newline
    "foo\ntk start cod-step-x",  # a command before it via an unquoted newline
    # A trailing `#` comment ends at the newline in a real shell, so a command
    # on the next line still runs -- the comment must not swallow it.
    "tk start cod-step-x # note\nrm -rf /tmp/x",  # second command hidden behind a comment
    "echo hi # note\ntk start cod-step-x",  # tk start runs after a commented first line
    # `tk create` shapes the progress view cannot read.
    'tk create --step "First" --step "Second"',  # two steps in one create -> only the last is made
    'tk create --step "First" --step "Second" --step "Third"',  # three collapse to one
    'tk create --step "one" > /tmp/x.txt',  # redirect hides the `Created` line
    'tk create --step "one" > /tmp/x.txt 2>&1',  # redirect + stderr merge
    'tk create --step "one" > /tmp/x.txt 2>&1; cat /tmp/x.txt',  # the `cat` does not rescue the create segment
    'tk create --step "a"\ntk create --step "b" --step "c"',  # one good create, one doubled -> blocked
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
    assert checker.classify("tk start cod-step-x >/dev/null") == checker._REDIRECT
    assert checker.classify('tk close a "s" && echo hi') == checker._CHAIN
    assert (
        checker.classify('tk create --step "a" --step "b"') == checker._CREATE_MULTISTEP
    )
    assert checker.classify('tk create --step "a" > /tmp/x') == checker._CREATE_REDIRECT


def test_main_exit_codes() -> None:
    """main() exits 0 for a clean close/create, 2 for the broken shapes."""
    assert checker.main(["check", 'tk close cod-step-x "done"']) == 0
    assert checker.main(["check", "tk close cod-step-x >/dev/null"]) == 2
    assert checker.main(["check", 'tk create --step "plan"']) == 0
    assert checker.main(["check", 'tk create --step "a" --step "b"']) == 2
