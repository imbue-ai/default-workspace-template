"""Tests for the standalone-tk PreToolUse guard.

The guard blocks a `tk start`/`tk close` that shares its Bash tool call with
anything but other tk lifecycle commands run in sequence (no leading `cd`, no
real work, no pipe or background `&`, no output redirection), so the progress
view always sees every transition's output and position. `tk create` and
non-tk commands that merely mention a tk verb are left alone, and so is a
start or close that provably targets a regular ticket, since the progress view
renders only steps.
"""

from __future__ import annotations

from workspace_bare_scripts import agent_tk_standalone_check as checker

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
    assert checker.classify('tk close cod-step-a "s" && echo hi') == checker._CHAIN
    assert checker.classify("tk start cod-step-x &") == checker._CHAIN
    assert (
        checker.classify('tk close cod-step-a "s" >/dev/null; tk start cod-step-b')
        == checker._REDIRECT
    )


def test_main_exit_codes() -> None:
    """main() exits 0 for a clean close, 2 for a redirected one."""
    assert checker.main(["check", 'tk close cod-step-x "done"']) == 0
    assert checker.main(["check", "tk close cod-step-x >/dev/null"]) == 2


# The ticket block the crystallize, heal and update skills shipped before they
# split it: real work around a start whose target is a regular ticket.
_HARDEN_TICKET_BLOCK = """mkdir -p data/.tasks/harden/crystallize-$NAME
TICKET_ID=$(tk create "crystallize $NAME" -t task \\
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
echo "$TICKET_ID" > data/.tasks/harden/crystallize-$NAME/ticket_id.txt
"""

# A start or close whose target is provably a regular ticket: ALLOWED however chained.
_REGULAR_TARGET_ALLOWED = [
    _HARDEN_TICKET_BLOCK,
    'mkdir -p d\nID=$(tk create "t" -t bug -d "w")\ntk start "$ID"',  # the heal form
    'ID=$(tk create "t" -t task); tk start "$ID"',
    'ID=$(tk create "t" -t task) && tk start "$ID" && echo done',
    'ID=$(tk create "t"); tk start "${ID}"; echo hi',  # braced reference
    'ID=$(tk create "t"); tk start $ID >/dev/null 2>&1',  # redirected
    'ID=$(tk create "t"); tk start "$ID" | tee log',  # piped
    "cd /x && tk start cod-f1zl",  # literal regular id after a cd
    "tk start cod-f1zl >/dev/null",
    'tk close cod-f1zl "done" && pytest',
    "tk start cod-f1zl &",
    'tk close cod-a1b2 "s"; tk close cod-c3d4 "s"; rm -f x',  # several regular targets
    'L=$(tk create "lease" -t chore); tk start "$L"; ./edit.sh; tk close "$L" "done"',
    # A step create in the same command does not taint a regular capture.
    'tk create --step "plan"; ID=$(tk create "t" -t task); tk start "$ID"; echo hi',
]

# A target that is a step, or that cannot be shown to be a regular ticket: still BLOCKED.
_STEP_OR_UNKNOWN_TARGET_BLOCKED = [
    'cd /x && tk start "$ID"',  # variable never assigned in this command
    "cd /x && tk start $UNSET",
    'S=$(tk create --step "plan"); tk start "$S"; echo hi',  # captured from a step create
    'S=$(tk create "t" --step); tk start "$S"',  # flag after the title
    'ID=$(tk create "t"); ID=cod-step-x; tk start "$ID"; echo hi',  # reassigned after capture
    'ID=$(tk create "t"); ID=$(cat f); tk start "$ID"; echo hi',  # reassigned from another command
    'ID=$(tk create "t" | cut -c1-8); tk start "$ID"; echo hi',  # capture is not tk create alone
    'ID=$(echo x); tk start "$ID"; echo hi',  # captured from something other than tk create
    'ID="$(tk create "t")"; tk start "$ID"; echo hi',  # quoted capture is not recognised
    'tk start "$ID"; ID=$(tk create "t"); echo hi',  # assigned only after the start
    'cd /x; tk close "$(cat data/ticket_id.txt)"',  # command substitution as the target
    "cd /x; tk start cod-$N",  # partial expansion in the id
    "cd /x && tk start",  # no target at all
    'ID=$(tk create "t"); tk start "$ID"; tk start cod-step-b; echo hi',  # one step target among them
    'tk close cod-f1zl "s"; tk close cod-step-a "s" >/dev/null',
    "cd /x && tk start cod-step-f1zl",  # literal step id
]


def test_allows_chained_lifecycle_on_regular_tickets() -> None:
    for cmd in _REGULAR_TARGET_ALLOWED:
        assert checker.classify(cmd) is None, f"should be allowed: {cmd!r}"


def test_blocks_chained_lifecycle_on_step_or_unknown_targets() -> None:
    for cmd in _STEP_OR_UNKNOWN_TARGET_BLOCKED:
        assert checker.classify(cmd) is not None, f"should be blocked: {cmd!r}"


def test_main_allows_the_harden_ticket_block() -> None:
    assert checker.main(["check", _HARDEN_TICKET_BLOCK]) == 0
