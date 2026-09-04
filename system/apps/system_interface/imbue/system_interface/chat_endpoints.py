"""``/api/chats/<chat_id>/...``: the chat-facing twins of the ``/api/agents`` routes.

The canonical product routes address a chat by its stable :class:`ChatId`; the
backend resolves which physical agent currently backs that chat at request
time, so a browser never owns the chat->agent mapping. Today resolution is
identity (a chat's id is its first agent's id and the registry falls back to
identity for unrecorded chats), which makes every twin observably equal to its
``/api/agents`` sibling -- the seam exists so a later backing-agent replacement
changes exactly one resolution point. ``/api/agents/...`` remains registered
unchanged as the physical/internal contract.
"""

from collections.abc import Callable
from collections.abc import Sequence
from typing import Any

from flask import Blueprint
from flask import Flask
from flask import Response
from flask import request

from imbue.system_interface.app_context import get_state
from imbue.system_interface.models import ChatId

# One chat route: the rule suffix under /api/chats/<chat_id>, the view function
# (the SAME object serving the /api/agents twin, taking ``agent_id`` as its
# first view argument), and the HTTP methods.
ChatRouteRule = tuple[str, Callable[..., Response], tuple[str, ...]]


def _resolve_chat_to_active_agent() -> None:
    """Rewrite the request's ``chat_id`` view arg to the chat's active ``agent_id``.

    Runs before every blueprint dispatch (``before_request`` fires ahead of the
    view call, so mutating ``request.view_args`` here changes what the view is
    invoked with). Resolution falls back to identity for an unrecorded chat, so
    an unknown id flows through to the handler's own lookup and answers the
    same 404 its ``/api/agents`` twin would.
    """
    view_args: dict[str, Any] | None = request.view_args
    if view_args is None or "chat_id" not in view_args:
        return
    chat_id = ChatId(str(view_args.pop("chat_id")))
    view_args["agent_id"] = get_state().chat_registry.resolve_active_agent_id(chat_id)


def register_chat_routes(application: Flask, rules: Sequence[ChatRouteRule]) -> None:
    """Register the ``/api/chats`` twins of the given agent-scoped view functions.

    The rules are supplied by ``create_application`` (which owns the view
    functions) rather than imported from ``server`` to keep this module a leaf.
    Blueprint endpoint names are auto-namespaced (``chats.<func>``), so a twin
    registration never collides with the ``/api/agents`` registration of the
    same function.
    """
    blueprint = Blueprint("chats", __name__, url_prefix="/api/chats/<chat_id>")
    blueprint.before_request(_resolve_chat_to_active_agent)
    for rule, view_func, methods in rules:
        blueprint.add_url_rule(rule, view_func=view_func, methods=list(methods))
    application.register_blueprint(blueprint)
