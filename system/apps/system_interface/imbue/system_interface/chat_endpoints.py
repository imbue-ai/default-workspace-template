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


def _make_chat_resolver(chat_native_endpoints: frozenset[str]) -> Callable[[], None]:
    """Build the blueprint's ``before_request``, given the endpoints to leave alone.

    A CHAT-NATIVE route is one whose subject is the conversation rather than the
    process behind it -- switching harness is the first, since the point of it is
    that the backing agent changes. Those views take ``chat_id`` straight through;
    everything else is a twin of an ``/api/agents`` handler and gets the resolved
    ``agent_id`` instead.
    """

    def resolve_chat_to_active_agent() -> None:
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
        if request.endpoint in chat_native_endpoints:
            return
        chat_id = ChatId(str(view_args.pop("chat_id")))
        view_args["agent_id"] = get_state().chat_registry.resolve_active_agent_id(chat_id)

    return resolve_chat_to_active_agent


def register_chat_routes(
    application: Flask,
    rules: Sequence[ChatRouteRule],
    chat_native_rules: Sequence[ChatRouteRule] = (),
) -> None:
    """Register the ``/api/chats`` twins of the given agent-scoped view functions.

    The rules are supplied by ``create_application`` (which owns the view
    functions) rather than imported from ``server`` to keep this module a leaf.
    Blueprint endpoint names are auto-namespaced (``chats.<func>``), so a twin
    registration never collides with the ``/api/agents`` registration of the
    same function.

    ``chat_native_rules`` are the routes that have no ``/api/agents`` twin because
    their subject is the chat itself; they receive ``chat_id`` unresolved.
    """
    blueprint = Blueprint("chats", __name__, url_prefix="/api/chats/<chat_id>")
    # Chat-native routes get their endpoint named from their rule rather than from the
    # view function, so the allowlist the resolver checks is derived from the same
    # string the route is registered under -- there is no second place to keep in sync.
    native_endpoints = frozenset(f"chats.{_endpoint_name(rule)}" for rule, _view_func, _methods in chat_native_rules)
    blueprint.before_request(_make_chat_resolver(native_endpoints))
    for rule, view_func, methods in rules:
        blueprint.add_url_rule(rule, view_func=view_func, methods=list(methods))
    for rule, view_func, methods in chat_native_rules:
        blueprint.add_url_rule(rule, endpoint=_endpoint_name(rule), view_func=view_func, methods=list(methods))
    application.register_blueprint(blueprint)


def _endpoint_name(rule: str) -> str:
    """A Flask endpoint name for a chat-native rule (``/switch-harness`` -> ``switch_harness``)."""
    return rule.strip("/").replace("-", "_").replace("/", "_")
