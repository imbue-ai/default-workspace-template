"""Picks the shell document or the chat document for each request, by path.

The chat app has its own origin in the browser (the registered ``chat`` row's label), but the
process cannot route on it: the desktop client's forwarder replaces the ``Host`` header with
the backend's before handing a request over, and every loopback caller (the evals bridge,
the update probes, ``curl``) has no label to send. So the dispatcher looks only at the path:
the chat pages, the instances API, and the API routes only the chat app serves go to it, and
everything else goes to the shell. Each origin's browser context still sees only its own
document, because the shell frames ``/<agent-id>`` at the chat origin and never at its own.

CLEANUP: delete this module in phase 10 of the workspace app model, when the chat app runs
as its own process at its own port and the shell serves alone.
"""

import re
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any
from typing import Final

from flask import Flask

from imbue.imbue_common.pure import pure

WsgiApplication = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]

# A chat page: an agent id, or ``<agent-id>.<session-id>`` for a subagent view. mngr mints
# ``agent-<32 hex>``; the wider alphabet is the instance-key rule's, so a test fixture's id
# (``agent-test-123``) is a chat page too.
CHAT_DOCUMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^/agent-[A-Za-z0-9_-]{1,120}(?:\.[A-Za-z0-9._-]+)?$")

# Path prefixes the chat app alone serves.
_CHAT_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/_instances",
    "/api/agents/create-chat",
    "/api/harnesses",
    "/api/uploads",
    "/api/claude-auth",
    "/api/accounts",
    "/api/lanes",
    "/api/latchkey",
    "/api/proto-agents",
)

# The per-agent verbs the shell keeps until phase 7 replaces them with the instances API;
# every other ``/api/agents/<id>/...`` route is the chat app's.
_SHELL_AGENT_VERBS: Final[frozenset[str]] = frozenset({"destroy", "start", "stop"})
_AGENT_ROUTE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^/api/agents/([^/]+)/([^/]+)")


@pure
def is_chat_path(path: str) -> bool:
    """Whether the chat document serves ``path`` (the shell serves everything else)."""
    if CHAT_DOCUMENT_PATTERN.fullmatch(path):
        return True
    for prefix in _CHAT_PATH_PREFIXES:
        if path == prefix or path.startswith(f"{prefix}/"):
            return True
    agent_route = _AGENT_ROUTE_PATTERN.match(path)
    if agent_route is None:
        return False
    return agent_route.group(2) not in _SHELL_AGENT_VERBS


class PathDispatchingFlask(Flask):
    """The shell's Flask app, handing the chat document's paths to the chat app installed on it.

    A subclass rather than WSGI middleware so the app callers hold (tests, the threaded
    server, ``app_context``) stays a plain Flask app; until ``chat_application`` is installed
    every path is the shell's.
    """

    chat_application: WsgiApplication | None = None

    def wsgi_app(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "") or "/"
        if self.chat_application is not None and is_chat_path(path):
            return self.chat_application(environ, start_response)
        return super().wsgi_app(environ, start_response)
