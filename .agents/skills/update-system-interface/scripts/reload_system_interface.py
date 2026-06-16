#!/usr/bin/env python3
"""Tell the running system interface to reload its whole UI in the browser.

The frontend-reveal step of the ``update-system-interface`` flow: after the lead
agent rebuilds the (gitignored) static bundle with ``npm run build``, it runs
this script to make the user's open workspace reload into the new bundle.

It POSTs ``{op: "reload_system_interface"}`` to the loopback-only
``/api/layout/broadcast`` endpoint, which relays a ``layout_op`` WebSocket
message; the dockview shell responds by reloading the top-level page (picking up
the new hashed assets and transitively every child chat iframe). With no browser
connected the broadcast is a harmless no-op.

The op name is deliberately explicit and the script deliberately separate from
``scripts/layout.py``: ``layout.py`` is the general panel-arranging surface
exposed via the ``manage-layout`` skill (its ``refresh`` op reloads a single
inner iframe), whereas a full-UI reload is a privileged step in one lead-only
reveal sequence. Keeping it out of the layout CLI -- under an unmistakable name
-- prevents an agent from confusing the two.

Run via bare ``python3`` (standard library only), like ``forward_port.py``.

Environment:
    MINDS_WORKSPACE_SERVER_URL  Base URL of the workspace server
                                (default http://127.0.0.1:8000).
    MNGR_AGENT_ID               Sent for telemetry (body + X-Mngr-Agent-Id).

Exit codes: 0 on a successful broadcast; 1 if the server could not be reached or
returned an error.
"""

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"
_OP = "reload_system_interface"


def main() -> int:
    base_url = os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL).rstrip("/")
    url = f"{base_url}/api/layout/broadcast"
    agent_id = os.environ.get(ENV_MNGR_AGENT_ID, "")
    body = json.dumps({"op": _OP, "args": {}, "agent_id": agent_id}).encode("utf-8")
    headers = {"Content-Type": "application/json", MNGR_AGENT_ID_HEADER: agent_id}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            status = response.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"error: reload_system_interface rejected (HTTP {e.code}): {detail}\n")
        return 1
    except urllib.error.URLError as e:
        sys.stderr.write(f"error: could not reach workspace server at {url}: {e.reason}\n")
        return 1

    if status != 200:
        sys.stderr.write(f"error: reload_system_interface failed (HTTP {status})\n")
        return 1

    sys.stderr.write(
        "reload_system_interface broadcast sent; any connected browser will reload the whole "
        "interface (no-op if no browser is connected).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
