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
    discarding = _spare("agent-discarding-9120", SpareChatPhase.DISCARDING)

    assert pooled_spares((creating, ready, claimed, discarding)) == (creating, ready)
    assert pooled_spares(()) == ()
