"""What the shell document and the chat document share: the per-request HTML assembly.

Both are the vite build's HTML with meta tags injected per request (the base path, the
workspace hostname, the primary agent id, and for the chat document the chat's own ids), so
the frontend can read its identity off the page rather than guess it from the URL.
"""

import html
import json
import os
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from flask import Response

# Stamped on every document response so a caller can tell the real app from
# the "not built" placeholder, which is otherwise an identical HTTP 200 HTML
# response. The reveal flow's frontend health check reads it.
FRONTEND_BUILT_HEADER: Final[str] = "X-Frontend-Built"

BASE_PATH_META_NAME: Final[str] = "system-interface-base-path"
HOSTNAME_META_NAME: Final[str] = "system-interface-hostname"
PRIMARY_AGENT_ID_META_NAME: Final[str] = "system-interface-agent-id"
# The chat document's own identity: which chat it shows, and which subagent session of it
# when it is a subagent view.
CHAT_AGENT_ID_META_NAME: Final[str] = "system-interface-chat-agent-id"
CHAT_SESSION_ID_META_NAME: Final[str] = "system-interface-chat-session-id"


def html_response(html_content: str, status_code: int = 200) -> Response:
    """Build an uncacheable HTML response for a document.

    A document is assembled per request (base path, hostname, agent ids, and the
    configured plugin script tags are injected into it), so it is never a
    cacheable artifact to begin with. It is also the *only* thing standing
    between a reload and a stale UI: the built assets it links are
    content-hashed, so a freshly-fetched document always names the current
    bundle, and a cached one always names the old one.

    That matters because a page cannot drop its own HTTP cache -- the
    ``location.reload(true)`` form is a Firefox-only extension -- so
    ``reloadInterface`` (see ``frontend/src/reload.ts``) can only reload and
    trust the response to be fresh. ``no-store`` is what makes that trust
    well-founded, including for viewers reaching the workspace through a
    shared tunnel, where an intermediary is free to cache anything we do not
    mark otherwise.
    """
    response = Response(html_content, status=status_code, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


def document_response(html_content: str, *, is_frontend_built: bool) -> Response:
    """Return a document response, stamped with whether it is the real app.

    Both the app and the not-built placeholder are HTTP 200 HTML, so nothing
    downstream can tell them apart from the status line alone. The header says
    which one this is, so a health check does not have to pattern-match markup
    that is free to change.
    """
    response = html_response(html_content)
    response.headers[FRONTEND_BUILT_HEADER] = "true" if is_frontend_built else "false"
    return response


def inject_meta_tag(html_content: str, name: str, content: str) -> str:
    meta_tag = f'<meta name="{name}" content="{html.escape(content, quote=True)}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def inject_base_path_meta_tag(html_content: str, root_path: str) -> str:
    return inject_meta_tag(html_content, BASE_PATH_META_NAME, root_path)


def read_host_name() -> str:
    """Read the host name from $MNGR_HOST_DIR/data.json, falling back to socket.gethostname()."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if host_dir:
        data_path = Path(host_dir) / "data.json"
        if data_path.exists():
            try:
                data = json.loads(data_path.read_text())
                name = data.get("host_name")
                if name:
                    return str(name)
            except (json.JSONDecodeError, OSError):
                pass
    return socket.gethostname()


def inject_hostname_meta_tag(html_content: str) -> str:
    return inject_meta_tag(html_content, HOSTNAME_META_NAME, read_host_name())


def inject_plugin_script_tags(html_content: str, plugin_basenames: Sequence[str], root_path: str) -> str:
    script_tags = "\n".join(f'<script src="{root_path}/plugins/{basename}"></script>' for basename in plugin_basenames)
    return html_content.replace("</body>", f"{script_tags}\n</body>")


def inject_primary_agent_id_meta_tag(html_content: str) -> str:
    """Inject the primary agent ID as a meta tag for the frontend."""
    return inject_meta_tag(html_content, PRIMARY_AGENT_ID_META_NAME, os.environ.get("MNGR_AGENT_ID", ""))


def inject_chat_identity_meta_tags(html_content: str, chat_agent_id: str, session_id: str) -> str:
    """Name the chat (and, for a subagent view, the session) the chat document shows."""
    with_agent = inject_meta_tag(html_content, CHAT_AGENT_ID_META_NAME, chat_agent_id)
    return inject_meta_tag(with_agent, CHAT_SESSION_ID_META_NAME, session_id)
