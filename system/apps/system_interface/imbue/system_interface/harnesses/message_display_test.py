"""Tests for the backend user-message render classifier.

Ported from the frontend's ``message-classification.test.ts`` -- the decision moved
backend-side, and these cases pin the exact same precedence (explicit detectors beat
``is_meta``; the compaction chip is keyed off its flag, not its text).
"""

from imbue.system_interface.harnesses.events import DisplayKind
from imbue.system_interface.harnesses.message_display import BROWSER_FLEET_TAG
from imbue.system_interface.harnesses.message_display import classify_user_message
from imbue.system_interface.harnesses.message_display import is_non_turn_tail


def test_ordinary_human_prompt_gets_no_decision() -> None:
    assert classify_user_message("please rebase onto main") is None


def test_stop_hook_feedback_is_a_chip() -> None:
    decision = classify_user_message("Stop hook feedback:\nlint failed, fix it")
    assert decision is not None
    assert decision.display is DisplayKind.CHIP
    assert decision.display_label == "Stop hook feedback"


def test_browser_fleet_nudge_is_a_chip_with_the_sentinel_stripped() -> None:
    inner = "Browser foo-1 was handed back to you. Re-run `state foo-1`."
    decision = classify_user_message(f"<{BROWSER_FLEET_TAG}>{inner}</{BROWSER_FLEET_TAG}>")
    assert decision is not None
    assert decision.display is DisplayKind.CHIP
    assert decision.display_label == "Browser fleet"
    assert decision.display_body == inner


def test_bare_task_notification_is_a_chip() -> None:
    decision = classify_user_message("<task-notification>\n<status>completed</status>\n</task-notification>")
    assert decision is not None
    assert decision.display is DisplayKind.CHIP
    assert decision.display_label == "Background task"


def test_task_notification_behind_a_system_preamble_is_a_chip() -> None:
    decision = classify_user_message(
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\nblah\n<task-notification>x</task-notification>"
    )
    assert decision is not None
    assert decision.display is DisplayKind.CHIP
    assert decision.display_label == "Background task"


def test_skill_expansion_lifts_the_skill_name_as_the_label() -> None:
    decision = classify_user_message(
        "Base directory for this skill: /home/.claude/skills/deep-research/\n\n# deep-research"
    )
    assert decision is not None
    assert decision.display is DisplayKind.SKILL_EXPANSION
    assert decision.display_label == "deep-research"


def test_seeded_welcome_is_hidden() -> None:
    decision = classify_user_message("/welcome")
    assert decision is not None
    assert decision.display is DisplayKind.HIDDEN


def test_is_meta_hides_a_framework_message() -> None:
    note = (
        "[Image: original 1800x2800, displayed at 1286x2000. Multiply coordinates by 1.40 to map to original image.]"
    )
    assert classify_user_message(note) is None
    decision = classify_user_message(note, is_meta=True)
    assert decision is not None
    assert decision.display is DisplayKind.HIDDEN


def test_resume_continuation_is_hidden_via_is_meta_not_a_bespoke_matcher() -> None:
    decision = classify_user_message("Continue from where you left off.", is_meta=True)
    assert decision is not None
    assert decision.display is DisplayKind.HIDDEN
    assert classify_user_message("Continue from where you left off.") is None


def test_explicit_detector_wins_over_is_meta() -> None:
    """Stop-hook feedback is is_meta in the transcript yet deliberately surfaces as a chip."""
    decision = classify_user_message("Stop hook feedback:\nlint failed", is_meta=True)
    assert decision is not None
    assert decision.display is DisplayKind.CHIP
    assert decision.display_label == "Stop hook feedback"


def test_a_human_message_mentioning_a_marker_is_not_misread() -> None:
    assert classify_user_message("what does Stop hook feedback: mean?") is None
    assert classify_user_message("tell me about <task-notification> handling") is None
    assert classify_user_message("can you grant me access to slack?") is None
    assert classify_user_message("Your permission request looks good") is None


def test_model_bar_commands_are_hidden() -> None:
    for command in (
        "/model opus[1m]",
        "/model sonnet",
        "/effort xhigh",
        "/effort medium",
        "/fast on",
        "/fast off",
        "/fast",
    ):
        decision = classify_user_message(command)
        assert decision is not None, command
        assert decision.display is DisplayKind.HIDDEN, command


def test_model_bar_stdout_confirmations_are_hidden() -> None:
    for line in (
        "<local-command-stdout>Set model to Opus 4.8 (1M context)</local-command-stdout>",
        "<local-command-stdout>Set effort level to xhigh (saved as your default)</local-command-stdout>",
        "<local-command-stdout>Fast mode ON</local-command-stdout>",
    ):
        decision = classify_user_message(line)
        assert decision is not None, line
        assert decision.display is DisplayKind.HIDDEN, line


def test_look_alike_commands_are_untouched() -> None:
    assert classify_user_message("/models") is None
    assert classify_user_message("model the data for me") is None
    # Prose that merely mentions the wrapper is not the wrapper: the match is anchored.
    assert classify_user_message("the agent printed <local-command-stdout> at me") is None


def test_any_local_command_output_is_hidden_not_just_the_model_bars() -> None:
    # This used to additionally require the text to be one of the model bar's three
    # confirmations, which left every other allowed command (/cost here, plus /clear,
    # /compact, /export, /rewind, /plugin, /version, /tui) rendering raw XML in a bubble.
    for line in (
        "<local-command-stdout>Total cost: $1.23</local-command-stdout>",
        "<local-command-stdout>Set model to Fable 5</local-command-stdout>",
        "<local-command-stderr>something went wrong</local-command-stderr>",
    ):
        decision = classify_user_message(line)
        assert decision is not None, line
        assert decision.display is DisplayKind.HIDDEN, line


def test_bash_blocks_become_chips_rather_than_bubbles() -> None:
    # Bash mode is NOT flagged isMeta, so without a detector these render as a user
    # bubble full of raw XML. They stay visible -- the user asked for that output -- but
    # as a chip, whose body renders in a <pre><code> so it reads as terminal output.
    command = classify_user_message("<bash-input>ls -la</bash-input>")
    assert command is not None
    assert command.display is DisplayKind.CHIP
    assert command.display_label == "Bash"
    assert command.display_body == "ls -la"

    # stdout and stderr arrive together in ONE message, the stderr half usually empty.
    output = classify_user_message("<bash-stdout>test</bash-stdout><bash-stderr></bash-stderr>")
    assert output is not None
    assert output.display is DisplayKind.CHIP
    assert output.display_label == "Output"
    assert output.display_body == "test"

    # Both streams present: joined in order, not styled apart.
    both = classify_user_message("<bash-stdout>ok</bash-stdout><bash-stderr>boom</bash-stderr>")
    assert both is not None
    assert both.display_body == "ok\nboom"

    # An empty result still chips rather than falling through to a raw-XML bubble.
    empty = classify_user_message("<bash-stdout></bash-stdout><bash-stderr></bash-stderr>")
    assert empty is not None
    assert empty.display is DisplayKind.CHIP
    assert empty.display_body == ""


def test_permission_resolutions_carry_the_verdict() -> None:
    cases = {
        "Your permission request for GitHub was granted (this decision has been saved).": "granted",
        "Your read-only file-sharing permission request for '/tmp/x' was denied.": "denied",
        "Your permission request for Slack could not be completed because the user's sign-in flow did not finish.": "error",
        # The workspace handler's phrasing puts 'for' AFTER the verdict, and the accounts
        # handler says 'request to list ...' with no 'permission' at all. Both must still
        # resolve, or they poison the order-based correlation queue and every later
        # verdict lands on the wrong card.
        "Your cross-workspace permission request was granted (minds-workspaces-backups-export) for workspace old-mind.": "granted",
        "Your cross-workspace permission request was denied.": "denied",
        "Your request to list this device's signed-in accounts was granted.": "granted",
        "Your request to list this device's signed-in accounts was denied.": "denied",
    }
    for content, verdict in cases.items():
        decision = classify_user_message(content)
        assert decision is not None, content
        assert decision.display is DisplayKind.PERMISSION_RESOLUTION
        assert decision.resolution == verdict
        assert decision.request_id is None


def test_permission_resolutions_carry_the_request_id_when_present() -> None:
    # format_resolution_notice (mngr repo's latchkey/handlers/messaging.py) appends this
    # exact "(request_id: ...)" suffix after the human-readable message.
    decision = classify_user_message(
        "Your permission request for GitHub was granted with the following permissions: "
        "repo. (request_id: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4)"
    )
    assert decision is not None
    assert decision.display is DisplayKind.PERMISSION_RESOLUTION
    assert decision.resolution == "granted"
    assert decision.request_id == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"


def test_apply_to_stamps_only_present_fields() -> None:
    event: dict[str, object] = {"type": "user_message"}
    decision = classify_user_message("/welcome")
    assert decision is not None
    decision.apply_to(event)
    assert event["display"] == "hidden"
    assert "display_label" not in event
    assert "display_body" not in event
    assert "resolution" not in event


def test_is_non_turn_tail_matches_model_bar_traffic_and_is_meta() -> None:
    """The composer bar's slash commands, their confirmations, and framework injections are
    not turns; a transcript ending on one must not pin the indicator on Thinking."""
    assert is_non_turn_tail("/model sonnet") is True
    assert is_non_turn_tail("/effort xhigh") is True
    assert is_non_turn_tail("/fast on") is True
    assert is_non_turn_tail("<local-command-stdout>Set effort level to xhigh</local-command-stdout>") is True
    assert is_non_turn_tail("resume marker", is_meta=True) is True
    assert is_non_turn_tail("a real question") is False
    # Awaiting-a-reply injections are NOT non-turn: the agent responds to these.
    assert is_non_turn_tail("/welcome") is False
    assert is_non_turn_tail("<task-notification>x</task-notification>") is False


def test_permission_resolution_reads_the_machine_tag_first() -> None:
    """The tagged form needs no phrasing recognition -- any prose works."""
    display = classify_user_message("Whatever minds chose to say. (resolution: denied, request_id: evt-9)")
    assert display is not None
    assert display.display == DisplayKind.PERMISSION_RESOLUTION
    assert display.resolution == "denied"
    assert display.request_id == "evt-9"
