"""What every harness's session must satisfy, run against every harness the registry knows.

Sessions are built with ``cls.__new__(cls)`` and their fields assigned by hand, so no type
checker can tell whether ``build`` sets a field a class declares: ty reads a declared field as
present either way. agy's session shipped without ``_handed_back_tokens`` for three weeks that
way, and every stop and harness switch of an agy chat raised an AttributeError.

The tests run over ``HARNESS_SPECS`` rather than naming classes, so a harness added later is
covered without anyone remembering to extend them. ``registry_test`` checks the file sessions'
fields; this module adds the sessions that build their own fields (codex, room) and runs the
shared stop path through every file session's own class.
"""

from collections.abc import Callable
from pathlib import Path
from typing import NoReturn
from typing import cast

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.interrupt import InterruptToComposer
from imbue.chat.harnesses.interrupt import IsCompactionInFlight
from imbue.chat.harnesses.interrupt import PressChord
from imbue.chat.harnesses.interrupt import RestartProcess
from imbue.chat.harnesses.interrupt import SettleActivity
from imbue.chat.harnesses.registry import HARNESS_SPECS
from imbue.chat.harnesses.session import AgentHarnessSession
from imbue.chat.harnesses.session import FileHarnessSession
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher

_FILE_HARNESSES = tuple(
    harness for harness, spec in HARNESS_SPECS.items() if issubclass(spec.session_class, FileHarnessSession)
)
_OWN_FIELD_HARNESSES = tuple(harness for harness in HARNESS_SPECS if harness not in _FILE_HARNESSES)


def _unused(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("unused")


def _session(
    harness: HarnessType, state_dir: Path, interrupter: InterruptToComposer | None = None
) -> AgentHarnessSession:
    """The harness's own session class, built the way the agent manager builds it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    deps = SessionDeps(
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
        build_interrupter=_unused if interrupter is None else lambda _agent_info: interrupter,
        build_shoulder_tap=lambda _agent_info: None,
    )
    return HARNESS_SPECS[harness].session_class.build(deps)


def _unset_declared_fields(session: AgentHarnessSession) -> list[str]:
    """The fields the session's classes declare (its bases' included) that its build left unset."""
    declared = {name for klass in type(session).__mro__ for name in klass.__dict__.get("__annotations__", {})}
    return sorted(declared - set(vars(session)))


def test_the_harness_split_covers_every_harness() -> None:
    """agy, the session that broke, is a file session; codex builds its own fields."""
    assert HarnessType.ANTIGRAVITY in _FILE_HARNESSES
    assert HarnessType.CODEX in _OWN_FIELD_HARNESSES
    assert set(_FILE_HARNESSES) | set(_OWN_FIELD_HARNESSES) == set(HARNESS_SPECS)


@pytest.mark.parametrize("harness", _OWN_FIELD_HARNESSES)
def test_a_session_that_builds_its_own_fields_sets_every_one_it_declares(harness: HarnessType, tmp_path: Path) -> None:
    """codex and room list their fields by hand in ``build``, the way agy's override once did."""
    session = _session(harness, tmp_path / f"agent-{harness.value}")
    assert _unset_declared_fields(session) == []


def test_the_field_check_reports_a_field_a_build_left_unset(tmp_path: Path) -> None:
    """Guards the check above against passing vacuously: agy's old bug, reproduced, is reported."""
    session = _session(HarnessType.CLAUDE, tmp_path / "agent-claude")
    assert isinstance(session, FileHarnessSession)
    del session._handed_back_tokens
    assert _unset_declared_fields(session) == ["_handed_back_tokens"]


class _StopReadingTheInFlightBlock(InterruptToComposer):
    """A stop that reads the session's in-flight block and returns it, as the real drains do."""

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "_StopReadingTheInFlightBlock":
        return cls()

    def drain_to_composer(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
        get_in_flight_block: Callable[[], str],
        is_compaction_in_flight: IsCompactionInFlight,
    ) -> str:
        return get_in_flight_block()


@pytest.mark.parametrize("harness", _FILE_HARNESSES)
def test_a_stop_runs_through_every_file_session(harness: HarnessType, tmp_path: Path) -> None:
    """The stop button and every harness switch drain through ``interrupt_to_composer``.

    Run through each file harness's own session class, so a subclass that breaks the shared
    stop path, by a missing field or an override, fails here and not in a user's chat.
    """
    state_dir = tmp_path / f"agent-{harness.value}"
    session = _session(harness, state_dir, _StopReadingTheInFlightBlock())
    block = session.interrupt_to_composer(
        AgentInfo(
            id=state_dir.name,
            name=harness.value,
            state="RUNNING",
            agent_state_dir=state_dir,
            claude_config_dir=state_dir,
        ),
        cast(AgentSessionWatcher, object()),
        lambda: (True, ""),
        lambda: None,
        lambda: True,
        lambda: False,
    )
    assert block == ""
