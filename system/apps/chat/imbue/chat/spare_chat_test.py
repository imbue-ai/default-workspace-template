from imbue.chat.primitives import ChatId
from imbue.chat.spare_chat import SpareChatAgent
from imbue.chat.spare_chat import SpareChatPhase
from imbue.chat.spare_chat import SpareChatTerms
from imbue.chat.spare_chat import pooled_spares


def _spare(chat_id: str, phase: SpareChatPhase) -> SpareChatAgent:
    return SpareChatAgent(
        chat_id=ChatId(chat_id),
        display_name="Chat 4",
        terms=SpareChatTerms(account_id="acct-1", project_label="", is_fast=True),
        phase=phase,
    )


def test_the_pool_counts_spares_being_created_or_waiting() -> None:
    creating = _spare("agent-creating-9120", SpareChatPhase.CREATING)
    ready = _spare("agent-ready-9120", SpareChatPhase.READY)
    claimed = _spare("agent-claimed-9120", SpareChatPhase.CLAIMED)
    reserved = _spare("agent-reserved-9120", SpareChatPhase.RESERVED)
    released = _spare("agent-released-9120", SpareChatPhase.RELEASED)
    discarding = _spare("agent-discarding-9120", SpareChatPhase.DISCARDING)

    assert pooled_spares((creating, ready, claimed, reserved, released, discarding)) == (creating, ready)
    assert pooled_spares(()) == ()


def test_a_reserved_spare_stays_reserved_once_up_and_goes_back_to_the_pool_as_it_stands() -> None:
    booting = _spare("agent-reserved-4416", SpareChatPhase.RESERVED)
    booted = booting.as_ready(ready_at=12.5)

    assert (booted.phase, booted.ready_at) == (SpareChatPhase.RESERVED, 12.5)
    assert _spare("agent-creating-4416", SpareChatPhase.CREATING).as_ready(ready_at=3.0).phase is SpareChatPhase.READY
    assert booting.as_unreserved().phase is SpareChatPhase.CREATING
    assert booted.as_unreserved() == booted.with_phase(SpareChatPhase.READY)


def test_a_spare_is_starting_until_its_creation_thread_has_settled_it() -> None:
    starting_phases = (SpareChatPhase.CREATING, SpareChatPhase.CLAIMED, SpareChatPhase.RELEASED)
    for phase in starting_phases:
        assert _spare("agent-starting-5823", phase).is_starting()
    for phase in (SpareChatPhase.READY, SpareChatPhase.DISCARDING):
        assert not _spare("agent-settled-5823", phase).is_starting()
    reserved = _spare("agent-reserved-5823", SpareChatPhase.RESERVED)
    assert reserved.is_starting()
    assert not reserved.as_ready(ready_at=1.0).is_starting()
