from imbue.chat.chat_intakes import PENDING_INTAKE_TTL_SECONDS
from imbue.chat.chat_intakes import PendingIntakeStore
from imbue.chat.chat_intakes import chat_id_selected_by_window_path
from imbue.chat.chat_intakes import intake_path
from imbue.chat.chat_intakes import most_recently_messaged_chat_id
from imbue.chat.models import IntakeRequest
from imbue.chat.models import IntakeTarget
from imbue.chat.primitives import ChatId
from imbue.chat.testing import make_chat_snapshot
from imbue.imbue_common.model_update import to_update


class _Clock:
    """A settable wall clock."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _request(target: IntakeTarget = IntakeTarget.CURRENT_CHAT) -> IntakeRequest:
    return IntakeRequest(message="hello", target=target)


def test_the_window_path_selects_the_chat_the_root_shows_or_the_chat_page_it_is() -> None:
    assert chat_id_selected_by_window_path("/?chat=agent-1") == ChatId("agent-1")
    assert chat_id_selected_by_window_path("/agent-1") == ChatId("agent-1")
    assert chat_id_selected_by_window_path("/agent-1.agent-2.sess-3") == ChatId("agent-1")
    assert chat_id_selected_by_window_path("/agent-1?intake=abc") == ChatId("agent-1")


def test_a_window_path_showing_no_chat_selects_none() -> None:
    assert chat_id_selected_by_window_path("/") is None
    assert chat_id_selected_by_window_path("") is None
    assert chat_id_selected_by_window_path("/?chat=not%20an%20id") is None
    assert chat_id_selected_by_window_path("/api/chats/intake") is None
    assert chat_id_selected_by_window_path("/agent-1/extra") is None


def test_the_most_recently_messaged_chat_wins_and_a_never_messaged_one_counts_as_oldest() -> None:
    older = make_chat_snapshot("agent-1", last_messaged_at=100.0)
    newer = make_chat_snapshot("agent-2", last_messaged_at=200.0)
    never = make_chat_snapshot("agent-3", last_messaged_at=None)
    assert most_recently_messaged_chat_id([older, newer, never]) == ChatId("agent-2")
    assert most_recently_messaged_chat_id([never]) == ChatId("agent-3")
    assert most_recently_messaged_chat_id([]) is None


def test_intake_paths_carry_the_selection_and_the_token() -> None:
    assert intake_path(ChatId("agent-1"), None) == "/?chat=agent-1"
    assert intake_path(ChatId("agent-1"), "tok") == "/?chat=agent-1&intake=tok"
    assert intake_path(None, "tok") == "/?intake=tok"
    assert intake_path(None, None) == "/"


def test_a_pending_intake_is_taken_once() -> None:
    store = PendingIntakeStore(clock=_Clock())
    token = store.mint(_request(), ChatId("agent-1"), needs_pick=False)

    held = store.get(token)
    assert held is not None
    assert held.chat_id == ChatId("agent-1") and held.needs_pick is False and held.request.message == "hello"
    taken = store.take(token)
    assert taken == held
    assert store.get(token) is None
    assert store.take(token) is None


def test_a_discarded_or_unknown_token_answers_nothing() -> None:
    store = PendingIntakeStore(clock=_Clock())
    token = store.mint(_request(IntakeTarget.CHAT_SELECTOR), None, needs_pick=True)
    store.discard(token)
    assert store.get(token) is None
    store.discard("never-minted")
    assert store.get("never-minted") is None


def test_a_pending_intake_expires_unapplied() -> None:
    clock = _Clock()
    store = PendingIntakeStore(clock=clock)
    token = store.mint(_request(), ChatId("agent-1"), needs_pick=False)
    clock.now += PENDING_INTAKE_TTL_SECONDS
    assert store.get(token) is not None
    clock.now += 1.0
    assert store.get(token) is None


def test_the_shells_string_presets_read_as_the_request_fields() -> None:
    request = IntakeRequest.model_validate(
        {"message": "hi", "target": "current_chat", "is_draft": "true", "client_id": "c1", "desktop_id": "home"}
    )
    assert request.target is IntakeTarget.CURRENT_CHAT
    assert request.is_draft is True
    assert request.is_delivery_awaited is False
    assert request.model_copy_update(to_update(request.field_ref().is_draft, False)).is_draft is False
