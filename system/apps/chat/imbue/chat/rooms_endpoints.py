"""The room and mention routes (the multiplayer spec, sections 7.3 and 7.4), and what a mention does.

A room's page is the chat page: it reads the room through the chat routes (``/events``, ``/stream``, ``/message``,
``/rename``, ``/destroy``), which ``server.py`` hands to the functions here when the id names a room. The routes
of this module are the ones beside them: the room list and its verbs, the message log for scripts and visiting
agents (``system/scripts/chat_room.py``), the loopback post an agent answers a room with, and the handle table and
pending mentions the composer and the chat list read.
"""

import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from typing import Any
from typing import Final
from uuid import uuid4

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from flask import current_app
from flask import request
from flask.ctx import AppContext
from loguru import logger
from pydantic import Field
from workspace_layout.identity import RequestIdentity
from workspace_layout.identity import participant_key_of
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import ParticipantKind
from workspace_layout.primitives import WORKSPACE_AGENT_PARTICIPANT_KEY
from workspace_layout.roster import RosterParticipant

from imbue.chat.authors import author_prefix
from imbue.chat.authors import new_author_annotation
from imbue.chat.authors import prefix_safe_name
from imbue.chat.authors import prefix_safe_text
from imbue.chat.identity import request_identity
from imbue.chat.identity import request_participant
from imbue.chat.mention_parser import MentionTable
from imbue.chat.mention_parser import MentionTarget
from imbue.chat.mention_parser import find_mentions
from imbue.chat.mention_parser import handle_table
from imbue.chat.mentions import MentionActions
from imbue.chat.mentions import PendingMentionsResponse
from imbue.chat.models import OkResponse
from imbue.chat.models import ProvisionalChatPhase
from imbue.chat.models import SendMessageRequest
from imbue.chat.models import SendMessageResponse
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import parse_chat_ref
from imbue.chat.request_helpers import json_response
from imbue.chat.request_helpers import require_loopback
from imbue.chat.rooms import BlankRoomTitleError
from imbue.chat.rooms import RoomRecord
from imbue.chat.rooms import RoomSnapshot
from imbue.chat.rooms import new_room_message
from imbue.chat.rooms import room_message_as_page_event
from imbue.chat.state import ChatAppState
from imbue.chat.state import ChatAppStateError
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

ROOMS_ROUTE: Final[str] = "/api/rooms"
LOUNGE_ROUTE: Final[str] = "/api/rooms/lounge"
MENTIONS_ROUTE: Final[str] = "/api/mentions"
PENDING_MENTIONS_ROUTE: Final[str] = "/api/mentions/pending"

_AGENT_FALLBACK_NAME: Final[str] = "Agent"

# Who a mention is acted on for: a person gets the pending mark and, for the owner, the desktop app's feed.
_PERSON_KINDS: Final[frozenset[ParticipantKind]] = frozenset({ParticipantKind.PERSON, ParticipantKind.INVITED})

# The send a room's mention goes through: the app, the target, the text as delivered, and the message id; the
# refusal's detail, or None once accepted.
MentionSend = Callable[[ChatAppState, ChatId, str, str], str | None]


class MentionDeliverer(MutableModel, ABC):
    """How a room's mention reaches an agent chat."""

    @abstractmethod
    def deliver(self, chat_id: ChatId, delivered: str, message_id: str) -> None:
        """Send ``delivered`` to the chat as the message ``message_id``."""


class BackgroundMentionDeliverer(MentionDeliverer):
    """Runs the send under the app's context on a thread of its own, since a send can take seconds and the room's
    post must not wait on it; a refusal is logged."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    state: ChatAppState = Field(frozen=True, description="The app the send runs in")
    send: MentionSend = Field(frozen=True, description="The ordinary send path")

    def deliver(self, chat_id: ChatId, delivered: str, message_id: str) -> None:
        threading.Thread(
            target=self._run,
            args=(current_app.app_context(), chat_id, delivered, message_id),
            daemon=True,
            name=f"room-mention-{str(chat_id)[-8:]}",
        ).start()

    def _run(self, app_context: AppContext, chat_id: ChatId, delivered: str, message_id: str) -> None:
        with app_context:
            refusal = self.send(self.state, chat_id, delivered, message_id)
        if refusal is not None:
            logger.warning("A room's mention could not reach chat {}: {}", chat_id, refusal)


class CreateRoomRequest(FrozenModel):
    """The body of ``POST /api/rooms``."""

    title: str = Field(default="", max_length=256, description="The room's name; empty takes the first free 'Room N'")


class RenameRoomRequest(FrozenModel):
    """The body of ``POST /api/rooms/<id>/rename``."""

    title: str = Field(min_length=1, max_length=256, description="The room's new name")


class RoomListResponse(FrozenModel):
    """What ``GET /api/rooms`` answers."""

    rooms: tuple[RoomSnapshot, ...] = Field(description="Every room, oldest first")


class RoomPostRequest(FrozenModel):
    """The body of ``POST /api/rooms/<id>/post`` (loopback only): what an agent says into a room."""

    message: str = Field(min_length=1, description="What the agent says")
    author: ParticipantKey = Field(
        default=WORKSPACE_AGENT_PARTICIPANT_KEY,
        description="``workspace-agent`` for the workspace's own agent, or a visiting agent's ``agent:<id>``",
    )


class MentionHandler(MentionActions):
    """Resolves and acts on a message's mentions (section 7.4), over the roster and the chat list of the moment."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    state: ChatAppState = Field(frozen=True, description="The app whose roster, chats, rooms, and mentions these are")
    deliverer: MentionDeliverer = Field(frozen=True, description="How a room's mention reaches an agent chat")

    def table(self) -> tuple[MentionTarget, ...]:
        """The handle table now: the roster, then every chat (agent chats and rooms) by its title."""
        chats: list[tuple[ChatId, str]] = [
            (snapshot.chat_id, snapshot.title) for snapshot in self.state.agent_manager.get_chat_snapshots()
        ]
        chats.extend((record.id, record.title) for record in self.state.rooms.read_all())
        return handle_table(self.state.roster.document().participants, chats)

    def resolve(self, text: str) -> tuple[MentionTarget, ...]:
        return find_mentions(text, self.table())

    def act(
        self,
        chat_id: ChatId,
        chat_title: str,
        message_id: str,
        text: str,
        sender: ParticipantKey,
        sender_name: str,
        targets: tuple[MentionTarget, ...],
        room_id: ChatId | None,
    ) -> None:
        """Act on a message's mentions after the message has landed: a person is marked (and the owner notified),
        and, from a room (``room_id``), the workspace's agent or an agent chat is sent the message. A mention of
        the sender does nothing."""
        is_anyone_marked = False
        for target in targets:
            if target.key is not None and target.key == sender:
                continue
            if target.key is not None and ParticipantKind(target.kind) in _PERSON_KINDS:
                self.state.mentions.record(target.key, chat_id, message_id, sender)
                is_anyone_marked = True
                if self._is_owner(target.key):
                    self.state.agent_manager.notify_owner_mentioned(
                        chat_id, f"{sender_name} mentioned you in {chat_title}", text
                    )
                continue
            if room_id is not None:
                self._ask_agent(target, room_id, chat_title, message_id, text, sender, sender_name)
        if is_anyone_marked:
            self.state.broadcaster.broadcast_mentions_updated()

    def _is_owner(self, key: ParticipantKey) -> bool:
        entry = self.state.roster.participant(key)
        return entry is not None and entry.is_owner

    def _ask_agent(
        self,
        target: MentionTarget,
        room_id: ChatId,
        room_title: str,
        message_id: str,
        text: str,
        sender: ParticipantKey,
        sender_name: str,
    ) -> None:
        """Send the room's message to the chat the mention names: the main chat for ``@agent``, the chat by title
        for one; a room or a chat with no agent is left alone, as is a visiting agent (nothing reaches one)."""
        if target.key == WORKSPACE_AGENT_PARTICIPANT_KEY:
            chat_id = self.state.agent_manager.get_main_chat_id()
            if chat_id is None:
                logger.warning("Room {} mentioned the agent, but the workspace has no main chat", room_id)
                return
        elif (
            target.chat_id is not None and self.state.agent_manager.get_chat_snapshot(str(target.chat_id)) is not None
        ):
            chat_id = target.chat_id
        else:
            return
        delivered = author_prefix(sender_name, prefix_safe_text(room_title, str(room_id))) + text
        # Annotated before delivery, so the chat's turn is stamped with the sender's own key (section 5.2).
        self.state.authors.record(chat_id, new_author_annotation(message_id, sender, sender_name, delivered))
        self.state.rooms.note_agent_asked(room_id, chat_id)
        self.deliverer.deliver(chat_id, delivered, message_id)


def _room_not_found(room_id: str) -> Response:
    return error_response(f"Room '{room_id}' not found", 404)


def _snapshots(state: ChatAppState) -> list[RoomSnapshot]:
    return state.rooms.snapshots()


def broadcast_rooms(state: ChatAppState) -> None:
    state.broadcaster.broadcast_rooms_updated(_snapshots(state))


def delete_room(state: ChatAppState, room_id: ChatId) -> bool:
    """Remove the room and its messages, end its open streams, and push the list; False for no such room. Shared by
    the room route and the chat route."""
    if not state.rooms.delete(room_id):
        return False
    state.event_queues.end_streams(str(room_id))
    broadcast_rooms(state)
    return True


def _handler(state: ChatAppState) -> MentionActions:
    handler = state.mention_handler
    if handler is None:
        raise ChatAppStateError("The mention handler is wired by create_application")
    return handler


def _author_name(state: ChatAppState, key: ParticipantKey, identity: RequestIdentity | None) -> str:
    entry: RosterParticipant | None = state.roster.participant(key)
    if entry is not None:
        return entry.display_name
    if identity is not None:
        return state.roster.display_name_of(key, identity)
    return _AGENT_FALLBACK_NAME if key == WORKSPACE_AGENT_PARTICIPANT_KEY else str(key)


def append_room_message(
    state: ChatAppState,
    room: RoomRecord,
    text: str,
    message_id: str,
    author: ParticipantKey,
    author_name: str,
    is_from_agent: bool,
) -> None:
    """Append a message to the room, push it to the room's open streams, act on its mentions, and push the list,
    which then names the chat a mention asked. A message an agent posted mentions people only (section 11): never
    another agent or chat, so no loop forms."""
    handler = _handler(state)
    targets = handler.resolve(text)
    if is_from_agent:
        targets = tuple(
            target for target in targets if target.key is not None and ParticipantKind(target.kind) in _PERSON_KINDS
        )
    mention_ids = [str(target.key if target.key is not None else target.chat_id) for target in targets]
    message = new_room_message(message_id, author, prefix_safe_name(author_name, author), text, mention_ids)
    state.rooms.append(room.id, message)
    if is_from_agent:
        state.rooms.note_agent_answered(room.id)
    state.event_queues.broadcast(str(room.id), room_message_as_page_event(message))
    handler.act(room.id, room.title, message_id, text, author, message.author_name, targets, room_id=room.id)
    broadcast_rooms(state)


def post_room_message(
    state: ChatAppState, room: RoomRecord, body: SendMessageRequest, identity: RequestIdentity
) -> None:
    """A person's message into a room, from its page or the message route."""
    sender = participant_key_of(identity)
    message_id = body.message_id or uuid4().hex
    append_room_message(state, room, body.message, message_id, sender, _author_name(state, sender, identity), False)


def room_events_response(state: ChatAppState, room: RoomRecord, after: str | None) -> Response:
    """The room's messages as the chat page reads a transcript: every message (rooms are short), or those after
    an event id, with the window's place in the whole log."""
    messages = state.rooms.messages(room.id)
    total = len(messages)
    window = state.rooms.messages(room.id, after) if after else messages
    offset = total - len(window)
    events: list[dict[str, Any]] = [room_message_as_page_event(message) for message in window]
    return json_response({"events": events, "offset": offset, "total": total})


def rename_room_response(state: ChatAppState, room_id: str, title: str, success: FrozenModel) -> Response:
    """Rename the room and push the list, answering ``success``; 400 for a blank title, 404 for no such room. Shared
    by the room route and the chat route, which answer under their own response models."""
    parsed = parse_chat_ref(room_id)
    try:
        renamed = None if parsed is None else state.rooms.rename(parsed, title)
    except BlankRoomTitleError as e:
        return error_response(str(e), 400)
    if renamed is None:
        return _room_not_found(room_id)
    broadcast_rooms(state)
    return model_response(success)


def act_on_chat_mentions(
    state: ChatAppState, chat_id: ChatId, message_id: str, text: str, identity: RequestIdentity
) -> None:
    """What a mention in an agent chat does, once the send was accepted: people are marked (and the owner notified);
    agents and chats are plain words the agent reads like any other (section 7.4). Called by every path a person's
    text enters an agent chat through (the message route, the intake), so it takes the sender's ``identity``
    rather than reading the request's: an intake nobody waits on is delivered on a thread of its own."""
    handler = _handler(state)
    targets = tuple(target for target in handler.resolve(text) if target.key is not None and not target.is_chat)
    if not targets:
        return
    snapshot = state.agent_manager.get_chat_snapshot(str(chat_id))
    chat_title = snapshot.title if snapshot is not None else str(chat_id)
    sender = participant_key_of(identity)
    handler.act(chat_id, chat_title, message_id, text, sender, _author_name(state, sender, identity), targets, None)


def room_of(chat_id: str) -> RoomRecord | None:
    """The room an id names, or None: how every chat route tells a room from an agent chat (by the kind the room
    store answers, never by the id's shape)."""
    parsed = parse_chat_ref(chat_id)
    return None if parsed is None else get_state().rooms.read(parsed)


def _list_rooms() -> Response:
    """``GET /api/rooms``: every room, oldest first."""
    return model_response(RoomListResponse(rooms=tuple(_snapshots(get_state()))))


def _create_room() -> Response:
    """``POST /api/rooms``: make a room, named as asked or the first free "Room N"."""
    state = get_state()
    body = parse_request_body(CreateRoomRequest)
    record = state.rooms.create(body.title, request_participant())
    broadcast_rooms(state)
    return model_response(state.rooms.snapshot(record), 201)


def _lounge() -> Response:
    """``POST /api/rooms/lounge``: the Lounge (section 7.5), made now if it does not exist."""
    state = get_state()
    record, is_created = state.rooms.ensure_lounge(request_participant())
    if is_created:
        broadcast_rooms(state)
    return model_response(state.rooms.snapshot(record))


def _rename_room(room_id: str) -> Response:
    """``POST /api/rooms/<id>/rename``: the room's new title."""
    return rename_room_response(get_state(), room_id, parse_request_body(RenameRoomRequest).title, OkResponse())


def _delete_room(room_id: str) -> Response:
    """``DELETE /api/rooms/<id>``: remove the room and its messages, ending its open streams."""
    parsed = parse_chat_ref(room_id)
    if parsed is None or not delete_room(get_state(), parsed):
        return _room_not_found(room_id)
    return Response(status=204)


def _room_messages(room_id: str) -> Response:
    """``GET /api/rooms/<id>/messages``: the room's messages as page events, all of them or those after ``after``."""
    room = room_of(room_id)
    if room is None:
        return _room_not_found(room_id)
    return room_events_response(get_state(), room, request.args.get("after"))


def _post_room_message_endpoint(room_id: str) -> Response:
    """``POST /api/rooms/<id>/messages``: a person's message into the room, as the chat page's send route takes it."""
    room = room_of(room_id)
    if room is None:
        return _room_not_found(room_id)
    body = parse_request_body(SendMessageRequest)
    post_room_message(get_state(), room, body, request_identity())
    return model_response(SendMessageResponse(status="ok"))


def _agent_post_endpoint(room_id: str) -> Response:
    """``POST /api/rooms/<id>/post`` (loopback only): what ``chat_room.py post`` and a visiting agent use."""
    refusal = require_loopback()
    if refusal is not None:
        return refusal
    room = room_of(room_id)
    if room is None:
        return _room_not_found(room_id)
    body = parse_request_body(RoomPostRequest)
    state = get_state()
    append_room_message(
        state, room, body.message, uuid4().hex, body.author, _author_name(state, body.author, None), True
    )
    return model_response(OkResponse())


def _mentions_table() -> Response:
    """``GET /api/mentions``: the handle table, people first, then agents, then chats by title."""
    return model_response(MentionTable(handles=_handler(get_state()).table()))


def _pending_mentions() -> Response:
    """``GET /api/mentions/pending``: the requester's mentions they have not looked at, oldest first."""
    return model_response(PendingMentionsResponse(mentions=get_state().mentions.pending(request_participant())))


def _make_room(chat_id: str) -> Response:
    """``POST /api/chats/awaiting/<id>/make-room``: turn a chat awaiting its first send into a room under the same
    id, keeping its name (the one conversion there is, S3); 404 for no such awaiting chat, 409 for one that is being
    created or has a seed to keep."""
    state = get_state()
    manager = state.agent_manager
    provisional = manager.get_provisional_chat(chat_id)
    if provisional is None:
        return error_response(f"Chat '{chat_id}' is not awaiting its first send", 404)
    if provisional.phase is not ProvisionalChatPhase.AWAITING_FIRST_SEND or provisional.is_seeded:
        return error_response(f"Chat '{chat_id}' cannot become a room: it is being created or has a conversation", 409)
    shown = manager.get_provisional_chats_as_shown()
    title = next((entry.name for entry in shown if entry.chat_id == provisional.chat_id), provisional.name)
    record = state.rooms.create(title, request_participant(), room_id=provisional.chat_id)
    # Listed as a room before the awaiting chat is dropped, so the page on it never sees it vanish.
    broadcast_rooms(state)
    if not manager.discard_provisional_chat(chat_id):
        # A first send raced in and the chat's create is under way: the id is an agent chat's after all.
        delete_room(state, record.id)
        return error_response(f"Chat '{chat_id}' cannot become a room: it is being created", 409)
    return model_response(state.rooms.snapshot(record), 201)


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", ROOMS_ROUTE): RouteModels(response_model=RoomListResponse),
    ("POST", ROOMS_ROUTE): RouteModels(
        request_model=CreateRoomRequest, response_model=RoomSnapshot, success_status=201
    ),
    ("POST", LOUNGE_ROUTE): RouteModels(response_model=RoomSnapshot),
    ("POST", "/api/rooms/{room_id}/rename"): RouteModels(request_model=RenameRoomRequest, response_model=OkResponse),
    ("DELETE", "/api/rooms/{room_id}"): RouteModels(success_status=204),
    ("GET", "/api/rooms/{room_id}/messages"): SchemalessRoute(
        reason="The room's messages as the chat page reads a transcript: a loose events window"
    ),
    ("POST", "/api/rooms/{room_id}/messages"): RouteModels(
        request_model=SendMessageRequest, response_model=SendMessageResponse
    ),
    ("POST", "/api/rooms/{room_id}/post"): RouteModels(request_model=RoomPostRequest, response_model=OkResponse),
    ("GET", MENTIONS_ROUTE): RouteModels(response_model=MentionTable),
    ("GET", PENDING_MENTIONS_ROUTE): RouteModels(response_model=PendingMentionsResponse),
    ("POST", "/api/chats/awaiting/{chat_id}/make-room"): RouteModels(response_model=RoomSnapshot, success_status=201),
}


def register_routes(application: Flask) -> None:
    application.add_url_rule(ROOMS_ROUTE, view_func=_list_rooms, methods=["GET"])
    application.add_url_rule(ROOMS_ROUTE, view_func=_create_room, methods=["POST"], endpoint="_create_room")
    application.add_url_rule(LOUNGE_ROUTE, view_func=_lounge, methods=["POST"])
    application.add_url_rule("/api/rooms/<room_id>/rename", view_func=_rename_room, methods=["POST"])
    application.add_url_rule("/api/rooms/<room_id>", view_func=_delete_room, methods=["DELETE"])
    application.add_url_rule("/api/rooms/<room_id>/messages", view_func=_room_messages, methods=["GET"])
    application.add_url_rule(
        "/api/rooms/<room_id>/messages",
        view_func=_post_room_message_endpoint,
        methods=["POST"],
        endpoint="_post_room_message_endpoint",
    )
    application.add_url_rule("/api/rooms/<room_id>/post", view_func=_agent_post_endpoint, methods=["POST"])
    application.add_url_rule(MENTIONS_ROUTE, view_func=_mentions_table, methods=["GET"])
    application.add_url_rule(PENDING_MENTIONS_ROUTE, view_func=_pending_mentions, methods=["GET"])
    application.add_url_rule("/api/chats/awaiting/<chat_id>/make-room", view_func=_make_room, methods=["POST"])
