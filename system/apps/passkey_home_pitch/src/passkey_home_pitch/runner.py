"""Landing page pitch: Passkey Home, own your passkeys.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/passkey-home-pitch/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/passkey-home-pitch/`` but
  honors the ``PASSKEY_HOME_PITCH_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``PASSKEY_HOME_PITCH_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://passkey-home-pitch.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import os
from pathlib import Path

from flask import Flask, Response
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/passkey-home-pitch/`` but is overridable via the ``PASSKEY_HOME_PITCH_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/passkey-home-pitch/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("PASSKEY_HOME_PITCH_DATA_DIR", "data/.apps/passkey-home-pitch"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``PASSKEY_HOME_PITCH_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("PASSKEY_HOME_PITCH_PORT", "8098"))

app = Flask("passkey_home_pitch", static_folder=None)

_PITCH_HTML = (Path(__file__).parent / "assets" / "pitch.html").read_text()

# The location beacon: post the path being viewed one hop up (to the
# workspace shell embedding this page) on each page load, so the shell can
# reopen this app's tab at the same place. Injected into the bundled pitch
# markup, which was authored without it.
_LOCATION_BEACON = (
    '<script>if (window.parent !== window) window.parent.postMessage('
    '{type: "shell:location", path: location.pathname + location.search}, "*");</script>'
)
_PITCH_HTML = _PITCH_HTML.replace("</body>", _LOCATION_BEACON + "</body>")


@app.route("/")
def index() -> Response:
    return Response(_PITCH_HTML, mimetype="text/html")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
