"""Unit tests for the harness registry's declared popups and session contracts."""

from pathlib import Path
from typing import NoReturn

from imbue.chat.harnesses.registry import ComposerCompactHandling
from imbue.chat.harnesses.registry import HARNESS_SPECS
from imbue.chat.harnesses.registry import HarnessPopup
from imbue.chat.harnesses.registry import HarnessType
from imbue.chat.harnesses.registry import PopupAction
from imbue.chat.harnesses.registry import PopupTrigger
from imbue.chat.harnesses.session import FileHarnessSession
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps


def _session_deps(harness: HarnessType, state_dir: Path) -> SessionDeps:
    """Deps a build needs to exist and nothing else: a build must do no I/O and touch no harness."""

    def unused(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("a session build must not reach into its harness")

    return SessionDeps(
        harness=harness,
        state_dir=state_dir,
        model_state_path=state_dir / "model_state.json",
        send_to_harness=lambda _text, _is_resent_when_unsure: SendOutcome.OK,
        notify_agents_changed=lambda: None,
        is_harness_starting_up=lambda: False,
        is_tracked=lambda: True,
        on_queue_snapshot=lambda _snapshot: None,
        on_user_turn=lambda _event: None,
        recompute_activity=lambda: None,
        clear_queue_state=lambda: None,
        catalog_options=lambda: (),
        build_interrupter=unused,
        build_shoulder_tap=unused,
    )


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
    # The seed pseudo-harness and the room harness are skipped: no model runs on either (no
    # agent at all on a seed, a reader of its pane on a room), so there is no model bar to
    # point at, and neither declares a popup.
    for harness in HARNESS_SPECS:
        if harness in (HarnessType.SEED, HarnessType.ROOM):
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


def test_only_claude_adds_the_billing_line_to_the_fast_mode_notice() -> None:
    bodies = {
        harness: popup.notice_body
        for harness in HARNESS_SPECS
        for popup in HARNESS_SPECS[harness].popups
        if popup.action is PopupAction.FAST_MODE_LIMIT
    }
    assert bodies == {
        HarnessType.CLAUDE: "Fast mode always uses API billing, not subscription usage.",
        HarnessType.CODEX: None,
    }


def test_only_the_harnesses_mngr_can_compact_support_compaction_and_only_claude_interrupts_one() -> None:
    capabilities = {
        harness: (spec.supports_compaction, spec.can_interrupt_compaction) for harness, spec in HARNESS_SPECS.items()
    }
    assert capabilities == {
        HarnessType.CLAUDE: (True, True),
        HarnessType.CODEX: (True, False),
        HarnessType.PI_CODING: (True, False),
        HarnessType.OPENCODE: (False, False),
        HarnessType.ANTIGRAVITY: (False, False),
        HarnessType.SEED: (False, False),
        HarnessType.ROOM: (False, False),
    }


def test_a_composer_compact_runs_on_every_harness_mngr_can_compact_and_is_declined_on_none() -> None:
    handling = {harness: spec.composer_compact_handling for harness, spec in HARNESS_SPECS.items()}
    assert handling == {
        HarnessType.CLAUDE: ComposerCompactHandling.FORWARDED_TO_HARNESS,
        HarnessType.CODEX: ComposerCompactHandling.REQUESTED_FROM_MNGR,
        HarnessType.PI_CODING: ComposerCompactHandling.REQUESTED_FROM_MNGR,
        HarnessType.OPENCODE: ComposerCompactHandling.SENT_AS_TEXT,
        HarnessType.ANTIGRAVITY: ComposerCompactHandling.SENT_AS_TEXT,
        HarnessType.SEED: ComposerCompactHandling.SENT_AS_TEXT,
        HarnessType.ROOM: ComposerCompactHandling.SENT_AS_TEXT,
    }
    for harness, spec in HARNESS_SPECS.items():
        if spec.composer_compact_handling is not ComposerCompactHandling.SENT_AS_TEXT:
            assert spec.supports_compaction
            assert all("/compact" not in popup.commands for popup in _notice_popups(harness))


def test_every_file_session_build_keeps_every_field_its_base_builds(tmp_path: Path) -> None:
    """A subclass ``build`` that re-lists the base's fields goes stale the moment the base grows one.

    agy's did: it never made the set of sends a stop hands back, and the shared stop/handoff drain
    reads it, so every handoff off an agy chat 500'd in its draining step. Every file session in
    the registry is checked here, so the next one that overrides ``build`` cannot repeat it.
    """
    file_sessions = {
        harness: spec.session_class
        for harness, spec in HARNESS_SPECS.items()
        if issubclass(spec.session_class, FileHarnessSession)
    }
    # The override this guard exists for; if agy ever drops it the guard still covers the rest.
    assert HarnessType.ANTIGRAVITY in file_sessions
    for harness, session_class in file_sessions.items():
        state_dir = tmp_path / "agents" / harness.value
        state_dir.mkdir(parents=True)
        session = session_class.build(_session_deps(harness, state_dir))
        base = FileHarnessSession.build(_session_deps(harness, state_dir))
        assert set(vars(base)) <= set(vars(session)), harness
        # Declared-but-unset is the same drift seen from the other side: a field added to the
        # class body and forgotten by a build that lists fields by hand.
        declared = {name for klass in session_class.__mro__ for name in klass.__dict__.get("__annotations__", {})}
        assert declared <= set(vars(session)), (harness, declared - set(vars(session)))
