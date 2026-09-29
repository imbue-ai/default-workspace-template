from pathlib import Path

from imbue.chat.primitives import ChatId
from imbue.chat.spare_chat import SpareChatAgent
from imbue.chat.spare_chat import SpareChatPhase
from imbue.chat.spare_chat import SpareChatStore
from imbue.chat.spare_chat import SpareChatTerms
from imbue.chat.spare_chat import current_spare
from imbue.chat.spare_chat import spares_after_restart


def _spare(chat_id: str, phase: SpareChatPhase) -> SpareChatAgent:
    return SpareChatAgent(
        chat_id=ChatId(chat_id),
        display_name="Chat 4",
        terms=SpareChatTerms(account_id="acct-1", project_label="", is_fast=True),
        phase=phase,
    )


def test_the_spares_file_reads_back_what_was_written(tmp_path: Path) -> None:
    store = SpareChatStore(path=tmp_path / "chat" / "spare_chat.json")
    spares = (_spare("agent-ready-7731", SpareChatPhase.READY), _spare("agent-gone-7731", SpareChatPhase.DISCARDING))

    store.write(spares)

    assert SpareChatStore(path=store.path).read() == spares


def test_a_missing_or_unreadable_spares_file_reads_as_no_spares(tmp_path: Path) -> None:
    path = tmp_path / "spare_chat.json"
    assert SpareChatStore(path=path).read() == ()

    path.write_text("not json")
    assert SpareChatStore(path=path).read() == ()

    path.write_text('{"spares": [{"chat_id": "agent-1"}]}')
    assert SpareChatStore(path=path).read() == ()


def test_a_restart_discards_a_spare_whose_create_had_not_finished() -> None:
    spares = (
        _spare("agent-creating-5512", SpareChatPhase.CREATING),
        _spare("agent-ready-5512", SpareChatPhase.READY),
        _spare("agent-discarding-5512", SpareChatPhase.DISCARDING),
    )

    assert [spare.phase for spare in spares_after_restart(spares)] == [
        SpareChatPhase.DISCARDING,
        SpareChatPhase.READY,
        SpareChatPhase.DISCARDING,
    ]


def test_the_current_spare_is_the_one_not_being_discarded() -> None:
    discarding = _spare("agent-discarding-9120", SpareChatPhase.DISCARDING)
    creating = _spare("agent-creating-9120", SpareChatPhase.CREATING)

    assert current_spare((discarding, creating)) == creating
    assert current_spare((discarding,)) is None
    assert current_spare(()) is None
