"""The terminal's pages on its own origin: the wrapper that frames the pty, and the ``new`` launch path.

The wrapper (desktop-interface plan section 9.2) is what a window of the terminal shows:
``/?session=<name>`` frames ``https://<pty origin>/?arg=...`` for that session, reports its path
and the session's title to the shell, and re-points the frame when the shell asks it to
navigate. ``/new`` allocates a terminal and redirects to its page. The pty's origin is derived
in the browser from the label this module reads out of the registry, the way every app page
derives another app's origin; the contract module the page speaks to the shell with is served
from this origin (``/_static/app_contract.js``, the shell's build output), since a module import
carries no cookie and the forwarder refuses it across origins.
"""

import html
import json
from pathlib import Path
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.registry import APP_CONTRACT_ROUTE, read_origin_label
from flask import Blueprint, Response, jsonify, redirect, request, send_file
from flask.typing import ResponseReturnValue
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from terminal_app.errors import (
    InvalidTerminalValueError,
    TerminalAppError,
    UnknownSessionPageError,
)
from terminal_app.primitives import (
    SESSION_QUERY_KEY,
    TerminalTitle,
    TmuxSessionName,
    Workdir,
    derive_terminal_title,
    pty_path_for_session,
    session_page_path,
)
from terminal_app.sessions import TmuxSessionSource

BLUEPRINT_NAME: Final[str] = "terminal_pages"
NEW_PATH: Final[str] = "/new"
HEALTH_PATH: Final[str] = "/api/health"
SESSION_API_PATH: Final[str] = "/api/sessions/<name>"

# The one parameter the ``new`` launch path takes (system/apps/terminal/app.toml).
WORKDIR_PARAM: Final[str] = "workdir"

HTTP_FOUND: Final[int] = 302
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_NOT_FOUND: Final[int] = 404
HTTP_INTERNAL_ERROR: Final[int] = 500

# The origin the page frames: the pty's.
PTY_APP_NAME: Final[AppName] = AppName("terminal-pty")

# The JSON script element the page reads off itself: the session it frames (or none) and the
# origin label it derives the pty's origin from, as ``PageConfig`` dumps them.
_CONFIG_ELEMENT_ID: Final[str] = "terminal-config"

_PAGE_TEMPLATE: Final[str] = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  html, body { height: 100%; margin: 0; background: #000; color: #ddd; font-family: system-ui, sans-serif; }
  iframe { display: block; width: 100%; height: 100%; border: 0; }
  #empty { display: flex; height: 100%; align-items: center; justify-content: center; font-size: 14px; color: #888; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<iframe id="pty" title="Terminal" hidden sandbox="allow-scripts allow-same-origin allow-forms allow-popups" allow="clipboard-read; clipboard-write"></iframe>
<div id="empty" hidden></div>
<script type="application/json" id="__CONFIG_ID__">__CONFIG__</script>
<script type="module">
  // The page is plain HTML served by the terminal app: the contract module comes from this
  // same origin (contracts.md section 7) and the pty's origin is derived the way
  // system/libs/workspace_ui/src/origin.ts derives every app's: the app's label prefixed onto
  // the workspace coordinate of this page's own host.
  const config = JSON.parse(document.getElementById("__CONFIG_ID__").textContent);
  const COORDINATE_LABEL = /^(?:(?:host|agent)-[a-f0-9]+|[a-f0-9]{32})$/i;
  const RETRY_MS = 2000;
  const frame = document.getElementById("pty");
  const empty = document.getElementById("empty");
  let current = config.session;
  let connection = null;

  function coordinate(host) {
    const labels = host.split(".");
    const index = labels.findIndex((label) => COORDINATE_LABEL.test(label));
    return index < 0 ? host : labels.slice(index).join(".");
  }

  function originFor(label) {
    return `${location.protocol}//${label}.${coordinate(location.host)}`;
  }

  function pathFor(session) {
    const params = new URLSearchParams();
    params.set("session", session);
    return `/?${params}`;
  }

  function showEmpty(message) {
    frame.hidden = true;
    empty.textContent = message;
    empty.hidden = false;
  }

  function show(page) {
    current = page.name;
    document.title = page.title;
    if (page.pty_label === "") {
      showEmpty("The terminal is starting...");
      setTimeout(() => void refresh(page.name), RETRY_MS);
    } else {
      empty.hidden = true;
      frame.hidden = false;
      // A navigate to the session already framed re-points at the same target: assigning the
      // same src again would reload the frame and drop the live ttyd connection.
      const src = originFor(page.pty_label) + page.pty_path;
      if (frame.src !== src) frame.src = src;
    }
    connection?.location(pathFor(page.name), page.title);
  }

  async function refresh(session) {
    if (session !== current) return;
    let response;
    try {
      response = await fetch(`api/sessions/${encodeURIComponent(session)}`);
    } catch (error) {
      // The app is unreachable for the moment (a restart, a dropped connection): keep asking,
      // as the page does while it waits for the pty to register.
      console.warn("[terminal] could not refresh the session page", error);
      setTimeout(() => void refresh(session), RETRY_MS);
      return;
    }
    if (response.status === 404) {
      showEmpty("There is no terminal named " + session + ".");
      return;
    }
    if (!response.ok) {
      // A gateway answering for an app that is restarting behind it, or the app itself failing:
      // neither says the terminal is gone, so keep asking as an unreachable app is kept asked.
      console.warn("[terminal] the session page answered " + response.status);
      setTimeout(() => void refresh(session), RETRY_MS);
      return;
    }
    show(await response.json());
  }

  function navigate(path) {
    const session = new URL(path, location.origin).searchParams.get("session");
    if (session === null || session === "") {
      current = null;
      document.title = "Terminal";
      showEmpty("Open a terminal from the launcher.");
      return;
    }
    history.replaceState(null, "", pathFor(session));
    current = session;
    void refresh(session);
  }

  function focusPty() {
    frame.contentWindow?.postMessage({ type: "ttyd-focus" }, "*");
  }

  // The shell grants focus to the frame it created, which is this page: pass it on to ttyd.
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const data = event.data;
    if (data !== null && typeof data === "object" && data.type === "ttyd-focus") focusPty();
  });
  window.addEventListener("focus", () => connection?.focused());

  if (config.session === null) {
    showEmpty("Open a terminal from the launcher.");
  } else {
    show(config.page);
  }

  if (window.parent !== window) {
    import("__CONTRACT_PATH__")
      .then(({ connectToShell }) => {
        connection = connectToShell({
          capabilities: { navigation: true },
          onNavigate: navigate,
          onShown: focusPty,
        });
        if (current !== null) connection.location(pathFor(current), document.title);
      })
      .catch((error) => console.warn("[terminal] could not load the shell contract", error));
  }
</script>
</body>
</html>
"""

_EMPTY_TITLE: Final[str] = "Terminal"


class SessionPage(FrozenModel):
    """What the wrapper needs to frame one session; ``model_dump`` is what the page and its API read."""

    name: TmuxSessionName = Field(description="The session, which is the terminal's name")
    title: TerminalTitle = Field(description="What the window is called")
    pty_path: str = Field(description="The path on the pty origin that attaches to the session")
    pty_label: str = Field(description="The pty's origin label, or \"\" while it is not registered")


class PageConfig(FrozenModel):
    """Everything the wrapper's script reads off the document."""

    session: TmuxSessionName | None = Field(description="The session the page opened on; None for the bare root")
    page: SessionPage | None = Field(description="The session's page, when there is a session")


@pure
def _session_name(raw: str) -> TmuxSessionName:
    try:
        return TmuxSessionName(raw)
    except InvalidTerminalValueError as e:
        raise UnknownSessionPageError(f"no terminal has the name {raw!r}") from e


@pure
def render_page(config: PageConfig) -> str:
    title = config.page.title if config.page is not None else _EMPTY_TITLE
    # `</` cannot appear inside a script element's text, whatever the JSON quoting says.
    encoded = json.dumps(config.model_dump(mode="json")).replace("</", "<\\/")
    return (
        _PAGE_TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__CONFIG_ID__", _CONFIG_ELEMENT_ID)
        .replace("__CONFIG__", encoded)
        .replace("__CONTRACT_PATH__", APP_CONTRACT_ROUTE)
    )


def _workdir(raw: str) -> Workdir | None:
    """The ``workdir`` a ``new`` request names, or None for the default; a bad one is a 400."""
    if raw == "":
        return None
    try:
        return Workdir(raw)
    except InvalidTerminalValueError as e:
        raise InvalidTerminalValueError(f"invalid {WORKDIR_PARAM!r}: {e}") from e


def build_pages_blueprint(source: TmuxSessionSource, registry_path: Path, contract_path: Path) -> Blueprint:
    """The wrapper page, the ``new`` launch path, the per-session JSON the page refreshes from, the health probe, and
    the app contract module at ``contract_path`` (the shell's build output, served from this origin)."""
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)

    def session_page(name: TmuxSessionName) -> SessionPage:
        listed = next((terminal for terminal in source.list_terminals() if terminal.name == name), None)
        record = source.remembered_record(name)
        return SessionPage(
            name=name,
            title=listed.title if listed is not None else derive_terminal_title(name),
            pty_path=pty_path_for_session(name, record.workdir if record is not None else None),
            pty_label=read_origin_label(registry_path, PTY_APP_NAME),
        )

    @blueprint.get("/")
    def wrapper_page() -> ResponseReturnValue:
        raw_session = request.args.get(SESSION_QUERY_KEY, "")
        session = _session_name(raw_session) if raw_session != "" else None
        config = PageConfig(session=session, page=session_page(session) if session is not None else None)
        response = Response(render_page(config), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get(NEW_PATH)
    def new_terminal() -> ResponseReturnValue:
        created = source.create_terminal(_workdir(request.args.get(WORKDIR_PARAM, "")))
        return redirect(session_page_path(created.name), code=HTTP_FOUND)

    @blueprint.get(SESSION_API_PATH)
    def session_json(name: str) -> ResponseReturnValue:
        return jsonify(session_page(_session_name(name)).model_dump(mode="json"))

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok"})

    @blueprint.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        if not contract_path.is_file():
            return (
                jsonify({"detail": f"the workspace shell's frontend is not built: {contract_path} is missing"}),
                HTTP_NOT_FOUND,
            )
        # Flask resolves a relative path against the app's own directory, not the cwd the path names.
        return send_file(contract_path.absolute(), mimetype="text/javascript")

    @blueprint.errorhandler(UnknownSessionPageError)
    def answer_unknown_session(error: UnknownSessionPageError) -> ResponseReturnValue:
        return jsonify({"detail": str(error)}), HTTP_NOT_FOUND

    @blueprint.errorhandler(InvalidTerminalValueError)
    def answer_invalid_value(error: InvalidTerminalValueError) -> ResponseReturnValue:
        return jsonify({"detail": str(error)}), HTTP_BAD_REQUEST

    @blueprint.errorhandler(TerminalAppError)
    def answer_terminal_error(error: TerminalAppError) -> ResponseReturnValue:
        # The app's own failures (tmux refusing, the store unreadable) answer a detail body rather than Flask's bare 500.
        logger.opt(exception=error).error("Failed to serve a terminal page request")
        return jsonify({"detail": str(error)}), HTTP_INTERNAL_ERROR

    return blueprint
