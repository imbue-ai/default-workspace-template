"""The terminal's pages on its own origin: the wrapper that frames the pty, and the ``new`` launch path.

The wrapper (desktop-interface plan section 9.2) is what a window of the terminal shows:
``/?session=<name>`` frames ``https://<pty origin>/?arg=...`` for that session, reports its path
and the session's title to the shell, and re-points the frame when the shell asks it to
navigate. ``/new`` allocates a terminal and redirects to its page. Both origins are derived in
the browser from the labels this module reads out of the registry, the way every app page
derives another app's origin.
"""

import html
import json
from pathlib import Path
from typing import Final

from app_instances.blueprint import HTTP_NOT_FOUND, answer_typed_error
from app_instances.errors import AppInstancesError
from app_instances.interfaces import InstanceNudgerInterface
from app_instances.json_store import NEW_ACTION_ID
from app_instances.primitives import InstanceTitle
from app_manifest.primitives import AppName
from app_manifest.registry import SHELL_APP_NAME, read_origin_label
from flask import Blueprint, Response, jsonify, redirect, request
from flask.typing import ResponseReturnValue
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from terminal_app.errors import InvalidTerminalValueError, UnknownSessionPageError
from terminal_app.primitives import (
    SESSION_QUERY_KEY,
    TAB_QUERY_KEY,
    TerminalTabId,
    TmuxSessionName,
    derive_terminal_title,
    pty_path_for_session,
)
from terminal_app.sessions import WORKDIR_PARAM, TmuxSessionSource

BLUEPRINT_NAME: Final[str] = "terminal_pages"
NEW_PATH: Final[str] = "/new"
HEALTH_PATH: Final[str] = "/api/health"
SESSION_API_PATH: Final[str] = "/api/sessions/<name>"

# The page derives two origins: the shell's (``SHELL_APP_NAME``), for the contract module it
# imports, and the pty's, for the frame.
PTY_APP_NAME: Final[AppName] = AppName("terminal-pty")

HTTP_FOUND: Final[int] = 302

# The JSON script element the page reads off itself: the session it frames (or none), the tab
# id the shell put in the URL, and the origin labels it derives the two origins from, as
# ``PageConfig`` dumps them.
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
  // The page is plain HTML served by the terminal app, so the contract module comes from the
  // shell's origin (contracts.md section 7) and the pty's origin is derived the way
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
      frame.src = originFor(page.pty_label) + page.pty_path;
    }
    connection?.location(pathFor(page.name), page.title);
  }

  async function refresh(session) {
    if (session !== current) return;
    const params = new URLSearchParams();
    if (config.tab !== null) params.set("tab", config.tab);
    let response;
    try {
      response = await fetch(`api/sessions/${encodeURIComponent(session)}?${params}`);
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
    const params = new URLSearchParams();
    params.set("session", session);
    if (config.tab !== null) params.set("tab", config.tab);
    history.replaceState(null, "", `/?${params}`);
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

  if (window.parent !== window && config.shell_label !== "") {
    import(`${originFor(config.shell_label)}/_static/app_contract.js`)
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

    name: TmuxSessionName = Field(description="The session, which is the terminal's key")
    title: InstanceTitle = Field(description="What the tab is called")
    pty_path: str = Field(description="The path on the pty origin that attaches to the session")
    pty_label: str = Field(description="The pty's origin label, or \"\" while it is not registered")


class PageConfig(FrozenModel):
    """Everything the wrapper's script reads off the document."""

    session: TmuxSessionName | None = Field(description="The session the page opened on; None for the bare root")
    tab: TerminalTabId | None = Field(description="The tab id the shell put in the URL, or None for none")
    shell_label: str = Field(description="The shell's origin label, or \"\" when none is registered")
    page: SessionPage | None = Field(description="The session's page, when there is a session")


@pure
def _session_name(raw: str) -> TmuxSessionName:
    try:
        return TmuxSessionName(raw)
    except InvalidTerminalValueError as e:
        raise UnknownSessionPageError(f"no terminal has the name {raw!r}") from e


def _tab_id(raw: str) -> TerminalTabId | None:
    if raw == "":
        return None
    try:
        return TerminalTabId(raw)
    except InvalidTerminalValueError as e:
        # A tab id the pty cannot record under is dropped rather than refused: the page still
        # attaches, it just cannot be re-pointed on a tmux session switch.
        logger.debug("Ignored the tab id in the wrapper URL: {}", e)
        return None


@pure
def render_page(config: PageConfig) -> str:
    title = config.page.title if config.page is not None else _EMPTY_TITLE
    # `</` cannot appear inside a script element's text, whatever the JSON quoting says.
    encoded = json.dumps(config.model_dump(mode="json")).replace("</", "<\\/")
    return (
        _PAGE_TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__CONFIG_ID__", _CONFIG_ELEMENT_ID)
        .replace("__CONFIG__", encoded)
    )


def build_pages_blueprint(
    source: TmuxSessionSource,
    nudger: InstanceNudgerInterface,
    registry_path: Path,
) -> Blueprint:
    """The wrapper page, the ``new`` launch path, the per-session JSON the page refreshes from, and the health probe."""
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)

    def session_page(name: TmuxSessionName, tab_id: TerminalTabId | None) -> SessionPage:
        listed = next((instance for instance in source.list_instances() if instance.key == name), None)
        record = source.remembered_record(name)
        return SessionPage(
            name=name,
            title=listed.title if listed is not None else derive_terminal_title(name),
            pty_path=pty_path_for_session(name, tab_id, record.workdir if record is not None else None),
            pty_label=read_origin_label(registry_path, PTY_APP_NAME),
        )

    @blueprint.get("/")
    def wrapper_page() -> ResponseReturnValue:
        raw_session = request.args.get(SESSION_QUERY_KEY, "")
        tab_id = _tab_id(request.args.get(TAB_QUERY_KEY, ""))
        session = _session_name(raw_session) if raw_session != "" else None
        config = PageConfig(
            session=session,
            tab=tab_id,
            shell_label=read_origin_label(registry_path, SHELL_APP_NAME),
            page=session_page(session, tab_id) if session is not None else None,
        )
        response = Response(render_page(config), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get(NEW_PATH)
    def new_terminal() -> ResponseReturnValue:
        workdir = request.args.get(WORKDIR_PARAM, "")
        params = {WORKDIR_PARAM: workdir} if workdir != "" else {}
        created = source.create_instance(NEW_ACTION_ID, params)
        nudger.nudge()
        return redirect(f"/?{SESSION_QUERY_KEY}={created.key}", code=HTTP_FOUND)

    @blueprint.get(SESSION_API_PATH)
    def session_json(name: str) -> ResponseReturnValue:
        page = session_page(_session_name(name), _tab_id(request.args.get(TAB_QUERY_KEY, "")))
        return jsonify(page.model_dump(mode="json"))

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok"})

    @blueprint.errorhandler(UnknownSessionPageError)
    def answer_unknown_session(error: UnknownSessionPageError) -> ResponseReturnValue:
        return jsonify({"detail": str(error)}), HTTP_NOT_FOUND

    blueprint.register_error_handler(AppInstancesError, answer_typed_error)
    return blueprint
