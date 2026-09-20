"""Unit tests for the shared tool-output rules.

The two tk rules live here rather than in any harness: every harness asks the same two
questions of a command it has already located, so the answers must not be able to differ
between them. The resident error snippet is shared the same way, so its rule (and the
hook-block exception to it) is tested here too.
"""

from imbue.chat.harnesses.events import DisplayKind
from imbue.chat.harnesses.tool_output import classify_tool_call_display
from imbue.chat.harnesses.tool_output import error_snippet
from imbue.chat.harnesses.tool_output import find_permission_request
from imbue.chat.harnesses.tool_output import find_secret_request
from imbue.chat.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.chat.harnesses.tool_output import is_secret_request_call
from imbue.chat.harnesses.tool_output import is_tk_lifecycle_anywhere
from imbue.chat.harnesses.tool_output import stamp_echoed_requests

# the two tk rules, and the asymmetry between them
# Both used to be reimplemented per harness (four copies of the verb set, the parser import
# and the segment walk). These pin the property those copies were free to drift on.


def test_the_hide_rule_is_strict_and_the_truncation_rule_is_broad() -> None:
    """A batched command still does real work, so it must RENDER (not hide) -- but its input
    must survive truncation so the progress view can read the plan out of it. Over-preserving
    input is harmless; over-hiding work silently swallows it."""
    batched = "cd /code && tk start s1"
    assert is_pure_tk_lifecycle_command(batched) is False, "must not hide: it also runs cd"
    assert is_tk_lifecycle_anywhere(batched) is True, "must not truncate: it carries step data"


def test_a_pure_invocation_satisfies_both_rules() -> None:
    command = 'tk create --step "Build the thing"'
    assert is_pure_tk_lifecycle_command(command) is True
    assert is_tk_lifecycle_anywhere(command) is True


def test_uv_run_lifecycle_commands_are_recognized_but_quoted_mentions_are_not() -> None:
    assert is_tk_lifecycle_anywhere('uv run tk create --step "Inspect the messages"')
    assert is_tk_lifecycle_anywhere("cat README.md && uv run tk start wor-step-abc")
    assert not is_tk_lifecycle_anywhere("uv run python -c \"print('tk start wor-step-abc')\"")
    assert not is_tk_lifecycle_anywhere('echo "uv run tk start wor-step-abc"')


def test_a_tk_verb_quoted_inside_another_command_is_neither() -> None:
    """Shell-aware, not a substring match: the shared shlex parser keeps a mention inside a
    quoted argument from being read as a real lifecycle call."""
    command = 'echo "remember to tk close s1"'
    assert is_pure_tk_lifecycle_command(command) is False
    assert is_tk_lifecycle_anywhere(command) is False


# the resident error snippet


def test_error_snippet_keeps_the_first_non_empty_line_of_a_failure() -> None:
    assert error_snippet("\n  Traceback (most recent call last):\n  boom\n") == "Traceback (most recent call last):"


def test_error_snippet_is_empty_for_a_call_a_hook_refused() -> None:
    """A hook block is Claude Code relaying the workspace's own rule back to the agent; the
    collapsed row shows nothing rather than the hook's message in red."""
    blocked = (
        "PreToolUse:Bash hook error: [${MNGR_AGENT_WORK_DIR:-.}/system/scripts/agent_block_pipe_tail_head.sh]: "
        "Do not pipe commands through tail or head."
    )
    assert error_snippet(blocked) == ""
    assert error_snippet("Stop hook error: something") == ""
    assert error_snippet("PreToolUse:mcp__linear__create-issue hook error: [check.sh]: not now") == ""


def test_error_snippet_keeps_an_ordinary_error_that_merely_mentions_a_hook() -> None:
    assert error_snippet("bash: hook error: not a real hook block") == "bash: hook error: not a real hook block"


# the secret request: recognised from the input, lifted from the result

_SECRET_REQUEST_CALL = (
    "python3 .agents/skills/connect-external-service/scripts/request_secret.py "
    "--file svc --var SVC_TOKEN --rationale 'to call the widget API'"
)
_SECRET_ECHO = (
    '{"request_id": "secret-0123456789abcdef0123456789abcdef", "chat_id": "agent-1", "file": "svc", '
    '"variables": ["SVC_TOKEN"], "rationale": "to call the widget API", "status": "pending", '
    '"existing_variables": [], "overwrites": []}'
)
_PERMISSION_ECHO = '{"request_id": "req-1", "payload": {"scope": "slack-api"}, "rationale": "read it"}'


def test_a_secret_request_call_is_recognised_from_its_input_and_renders_as_the_card() -> None:
    assert is_secret_request_call(_SECRET_REQUEST_CALL)
    assert is_secret_request_call("uv run " + _SECRET_REQUEST_CALL.removeprefix("python3 "))
    assert classify_tool_call_display(is_pure_tk=False, raw_input=_SECRET_REQUEST_CALL) is DisplayKind.SECRET_REQUEST
    # A mention that is not the script (another file with a longer name) is not a request.
    assert not is_secret_request_call("cat request_secret.pyc")
    # Naming the script without running it (a Read of its path, a grep for its name) files
    # nothing, so no card: the script's own --file is what marks a filing.
    assert not is_secret_request_call(
        '{"file_path":".agents/skills/connect-external-service/scripts/request_secret.py"}'
    )
    assert not is_secret_request_call('{"pattern":"request_secret.py","path":".agents"}')
    assert is_secret_request_call('{"command":"python3 scripts/request_secret.py \\\n--file svc --var SVC_TOKEN"}')
    assert classify_tool_call_display(is_pure_tk=False, raw_input="ls data/.secrets") is None


def test_the_two_echoed_shapes_are_told_apart_and_stamped_side_by_side() -> None:
    filler = "x" * 3000
    assert find_secret_request(filler + "\n" + _SECRET_ECHO) is not None
    assert find_secret_request(_PERMISSION_ECHO) is None
    assert find_permission_request(_SECRET_ECHO) is None
    event: dict[str, object] = {}
    stamp_echoed_requests(event, _SECRET_ECHO)
    assert event == {"secret_request": {**__import__("json").loads(_SECRET_ECHO)}}
    other: dict[str, object] = {}
    stamp_echoed_requests(other, _PERMISSION_ECHO)
    assert set(other) == {"permission_request"}
    untouched: dict[str, object] = {}
    stamp_echoed_requests(untouched, "plain output with a {brace} and no request")
    assert untouched == {}
