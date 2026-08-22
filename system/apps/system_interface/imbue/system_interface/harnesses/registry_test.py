"""Unit tests for the harness registry's declared popups."""

from imbue.system_interface.harnesses.registry import HARNESS_SPECS
from imbue.system_interface.harnesses.registry import HarnessPopup
from imbue.system_interface.harnesses.registry import HarnessType
from imbue.system_interface.harnesses.registry import PopupAction
from imbue.system_interface.harnesses.registry import PopupTrigger


def _notice_popups(harness: HarnessType) -> list[HarnessPopup]:
    """Every can't-send-from-chat notice a harness declares for the composer."""
    return [
        popup
        for popup in HARNESS_SPECS[harness].popups
        if popup.trigger is PopupTrigger.COMPOSER_COMMAND and popup.action is PopupAction.NOTICE
    ]


def test_every_harness_declines_the_model_bar_commands_with_the_picker_notice() -> None:
    # The model bar owns /model, /effort and /fast on every harness, so typing one into
    # the composer is declined everywhere -- and with its OWN body, because the reason is
    # not the usual "it takes over the terminal" (they send fine; that is the problem).
    for harness in HARNESS_SPECS:
        bodies = {
            command: popup.notice_body
            for popup in _notice_popups(harness)
            for command in popup.commands
            if command in ("/model", "/effort", "/fast")
        }
        assert set(bodies) == {"/model", "/effort", "/fast"}, harness
        for command, body in bodies.items():
            assert body is not None, f"{harness} {command} falls back to the terminal notice"
            assert "model picker" in body, f"{harness} {command}: {body!r}"


def test_the_model_bar_commands_are_not_also_in_a_harness_declined_tuple() -> None:
    # They live in their own popup so the distinct rationale survives: the per-harness
    # tuples are measured-against-a-live-agent lists of commands that break the terminal,
    # and a future re-measure would find these three send fine and drop them.
    for harness in HARNESS_SPECS:
        for popup in _notice_popups(harness):
            if popup.notice_body is not None:
                continue
            overlap = set(popup.commands) & {"/model", "/effort", "/fast"}
            assert overlap == set(), f"{harness}: {overlap} duplicated in the terminal-notice tuple"
