"""Unit tests for the harness registry's declared popups and capabilities."""

from imbue.chat.harnesses.registry import HARNESS_SPECS
from imbue.chat.harnesses.registry import HarnessPopup
from imbue.chat.harnesses.registry import HarnessType
from imbue.chat.harnesses.registry import PopupAction
from imbue.chat.harnesses.registry import PopupTrigger
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr_antigravity.plugin import register_agent_type as register_antigravity_agent_type
from imbue.mngr_claude.plugin import register_agent_type as register_claude_agent_type
from imbue.mngr_codex.plugin import register_agent_type as register_codex_agent_type
from imbue.mngr_opencode.plugin import register_agent_type as register_opencode_agent_type
from imbue.mngr_pi_coding.plugin import register_agent_type as register_pi_coding_agent_type


def _notice_popups(harness: HarnessType) -> list[HarnessPopup]:
    """Every can't-send-from-chat notice a harness declares for the composer."""
    return [
        popup
        for popup in HARNESS_SPECS[harness].popups
        if popup.trigger is PopupTrigger.COMPOSER_COMMAND and popup.action is PopupAction.NOTICE
    ]


def _can_launch_fast(harness: HarnessType) -> bool:
    """Whether this harness has a fast mode at all, read off the ONE place that declares it:
    the fast-mode turn-limit check. Derived rather than listed, so a harness that gains
    (or loses) fast mode cannot end up declining a /fast it does not have."""
    return any(popup.action is PopupAction.FAST_MODE_LIMIT for popup in HARNESS_SPECS[harness].popups)


def test_every_harness_declines_the_model_bar_commands_with_the_picker_notice() -> None:
    # The model bar owns /model and /effort on every harness, so typing one into the
    # composer is declined everywhere -- and with its OWN body, because the reason is not
    # the usual "it takes over the terminal" (they send fine; that is the problem).
    #
    # /fast is NOT universal: only the fast-capable harnesses declare it. Declining it
    # elsewhere would point the user at a picker control that is not rendered for that
    # harness, which is worse than letting the text through.
    #
    # The seed pseudo-harness is skipped: no agent ever runs on it, so no composer can
    # type a command into it, and it declares no popups at all.
    for harness in HARNESS_SPECS:
        if harness is HarnessType.SEED:
            continue
        bodies = {
            command: popup.notice_body
            for popup in _notice_popups(harness)
            for command in popup.commands
            if command in ("/model", "/effort", "/fast")
        }
        expected = {"/model", "/effort", "/fast"} if _can_launch_fast(harness) else {"/model", "/effort"}
        assert set(bodies) == expected, harness
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


def test_supports_compaction_matches_the_mngr_agent_classes_that_can_compact() -> None:
    # The autocompact sweep only runs `mngr autocompact run` on harnesses that declare
    # supports_compaction, so the flag has to agree with mngr: a harness wrongly left False
    # silently stops being compacted, and one wrongly True costs a full mngr startup per
    # chat per minute that ends in "does not support context compaction". The seed
    # pseudo-harness has no mngr agent type; every other harness must be listed here.
    register_by_harness = {
        HarnessType.CLAUDE: register_claude_agent_type,
        HarnessType.CODEX: register_codex_agent_type,
        HarnessType.PI_CODING: register_pi_coding_agent_type,
        HarnessType.OPENCODE: register_opencode_agent_type,
        HarnessType.ANTIGRAVITY: register_antigravity_agent_type,
    }
    for harness, spec in HARNESS_SPECS.items():
        if harness is HarnessType.SEED:
            continue
        agent_type_name, agent_class, _config_class = register_by_harness[harness]()
        assert agent_type_name == harness.value
        assert agent_class is not None, harness
        assert spec.supports_compaction == issubclass(agent_class, HasCompactionMixin), harness
