"""The terminal's pages on its own origin: the wrapper that frames the pty, and the ``new`` launch path.

The wrapper (desktop-interface plan section 9.2) is what a window of the terminal shows:
``/?session=<name>`` frames ``https://<pty origin>/?arg=...`` for that session, reports its path
and the session's title to the shell, and re-points the frame when the shell asks it to
navigate. ``POST /new`` allocates a terminal and answers the path of its page (a POST launch path,
docs/system/blueprint/post-launch-paths/). The pty's origin is derived
in the browser from the label this module reads out of the registry, the way every app page
derives another app's origin; the contract module the page speaks to the shell with is served
from this origin (``APP_CONTRACT_ROUTE``, the shell's build output).
"""

import html
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.registry import APP_CONTRACT_ROUTE, read_origin_label
from flask import Blueprint, Response, jsonify, request, send_file
from flask.typing import ResponseReturnValue
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field
from workspace_layout.windows import parse_window_closed_hint, window_query_value

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
# Where the shell posts a closed window of the terminal (the manifest's ``window_closed_path``): a sweep runs at once.
WINDOW_CLOSED_PATH: Final[str] = "/api/window-closed"
HTTP_NO_CONTENT: Final[int] = 204
SESSION_API_PATH: Final[str] = "/api/sessions/<name>"

# The one parameter the ``new`` launch path takes (system/apps/terminal/app.toml); the shell posts it in a JSON
# object beside its own envelope fields, which are ignored here.
WORKDIR_PARAM: Final[str] = "workdir"

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_NOT_FOUND: Final[int] = 404
HTTP_INTERNAL_ERROR: Final[int] = 500

# The origin the page frames: the pty's.
PTY_APP_NAME: Final[AppName] = AppName("terminal-pty")

# The JSON script element the page reads off itself: the session it frames (or none) and the
# origin label it derives the pty's origin from, as ``PageConfig`` dumps them.
_CONFIG_ELEMENT_ID: Final[str] = "terminal-config"

# The template's placeholders, filled in one pass so that a title or a config carrying a
# placeholder's text is not itself filled.
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"__(TITLE|CONFIG_ID|CONFIG|CONTRACT_PATH)__"
)

_PAGE_TEMPLATE: Final[str] = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  html { height: 100%; }
  body { display: flex; flex-direction: column; height: 100%; height: 100dvh; margin: 0; background: #000; color: #ddd; font-family: system-ui, sans-serif; }
  iframe { display: block; flex: 1 1 0; min-height: 0; width: 100%; border: 0; }
  #empty { display: flex; flex: 1 1 0; align-items: center; justify-content: center; font-size: 14px; color: #888; }
  /* The phone's key strip: the keys a soft keyboard lacks, under the terminal and so above the keyboard. */
  #keys { display: none; flex: none; gap: 6px; padding: 6px; background: #111; border-top: 1px solid #262626; touch-action: manipulation; user-select: none; -webkit-user-select: none; -webkit-tap-highlight-color: transparent; }
  #keys button { flex: 1 1 0; min-width: 0; height: 36px; padding: 0; border: 1px solid #333; border-radius: 6px; background: #1c1c1c; color: #ddd; font: 13px system-ui, sans-serif; }
  #keys button[aria-pressed="true"] { background: #ddd; border-color: #ddd; color: #000; }
  /* A finger on a phone-sized page, by the shell's phone rule: a mouse-driven desktop window that small never gets it. */
  @media (pointer: coarse) and (max-width: 500px) and (max-height: 1000px), (pointer: coarse) and (max-height: 500px) and (max-width: 1000px) { #keys { display: flex; } }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<iframe id="pty" title="Terminal" hidden sandbox="allow-scripts allow-same-origin allow-forms allow-popups" allow="clipboard-read; clipboard-write"></iframe>
<div id="empty" hidden></div>
<div id="keys" role="toolbar" aria-label="Terminal keys" hidden>
  <button type="button" data-key="Escape">Esc</button>
  <button type="button" data-key="Tab">Tab</button>
  <button type="button" data-ctrl aria-pressed="false">Ctrl</button>
  <button type="button" data-key="ArrowLeft" aria-label="Left">&larr;</button>
  <button type="button" data-key="ArrowUp" aria-label="Up">&uarr;</button>
  <button type="button" data-key="ArrowDown" aria-label="Down">&darr;</button>
  <button type="button" data-key="ArrowRight" aria-label="Right">&rarr;</button>
</div>
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
  const keys = document.getElementById("keys");
  const ctrlKey = keys.querySelector("[data-ctrl]");
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
    keys.hidden = true;
    empty.textContent = message;
    empty.hidden = false;
  }

  // ttyd fits its grid once at mount, with the DOM renderer's whole-pixel cell width, then
  // switches to its WebGL renderer, whose narrower cell leaves that column count short of the
  // pane; it only refits on a resize, and only listens for one once its socket is open. A
  // one-pixel nudge of the frame after it loads is such a resize; the second covers a slow
  // socket.
  const NUDGE_DELAYS_MS = [500, 2000];
  let nudgeTimers = [];
  function nudgeFrameSize() {
    frame.style.marginBottom = "1px";
    // Lay the shrunk frame out now: restored before the next layout, the frame never changes size.
    void frame.offsetHeight;
    requestAnimationFrame(() => { frame.style.marginBottom = ""; });
  }
  function scheduleRefitNudges() {
    nudgeTimers.forEach(clearTimeout);
    nudgeTimers = NUDGE_DELAYS_MS.map((delay) => setTimeout(nudgeFrameSize, delay));
  }
  frame.addEventListener("load", scheduleRefitNudges);

  // A soft keyboard opening or closing resizes the visual viewport; once it settles, nudge the
  // frame so ttyd refits its grid to whatever the page now gives it.
  const VIEWPORT_SETTLE_MS = 150;
  let viewportNudgeTimer = null;
  window.visualViewport?.addEventListener("resize", () => {
    clearTimeout(viewportNudgeTimer);
    viewportNudgeTimer = setTimeout(nudgeFrameSize, VIEWPORT_SETTLE_MS);
  });

  function pointFrameAt(src) {
    if (frame.src !== src) frame.src = src;
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
      keys.hidden = false;
      // A navigate to the session already framed re-points at the same target: assigning the
      // same src again would reload the frame and drop the live ttyd connection.
      pointFrameAt(originFor(page.pty_label) + page.pty_path);
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

  function postToPty(message) {
    frame.contentWindow?.postMessage(message, "*");
  }

  function focusPty() {
    postToPty({ type: "ttyd-focus" });
  }

  // The strip's keys go to the script dwt adds to ttyd's page (terminal_app/pty_page.py). Ctrl
  // is one-shot: it rides on the next strip key, or the pty page applies it to the next key
  // typed and posts back that it was used.
  let isCtrlArmed = false;
  function setCtrlArmed(armed) {
    isCtrlArmed = armed;
    ctrlKey.setAttribute("aria-pressed", String(armed));
  }
  function pressKey(button) {
    if (button === ctrlKey) {
      setCtrlArmed(!isCtrlArmed);
      postToPty({ type: "terminal:ctrl", armed: isCtrlArmed });
    } else {
      postToPty({ type: "terminal:key", key: button.dataset.key, ctrl: isCtrlArmed });
      setCtrlArmed(false);
    }
  }
  // A key acts on press and never takes focus, so the terminal keeps it and the soft keyboard
  // stays up; a press anywhere else on the strip just hands focus back to the terminal.
  for (const type of ["touchstart", "mousedown"]) {
    keys.addEventListener(type, (event) => event.preventDefault(), { passive: false });
  }
  keys.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    if (event.button !== 0) return;
    const button = event.target.closest("button");
    if (button !== null) pressKey(button);
    focusPty();
  });
  frame.addEventListener("load", () => {
    setCtrlArmed(false);
    if (palette !== undefined) postToPty({ type: "terminal:theme", theme: palette });
  });

  // The workspace's theme reaches the terminal as a palette (docs/system/blueprint/workspace-themes/, section
  // 4.1). The terminal takes no other part in themes ([theming] mode = "none"), so the theme's bundle is loaded
  // into a blank frame of this origin that nobody sees, never into this page: the --term-* tokens are read there
  // once it has loaded and handed to the pty page's xterm, and the theme's rules style nothing here. The standard
  // theme sets none, which leaves xterm its own colors.
  const PALETTE_TOKENS = {
    background: "--term-background",
    foreground: "--term-foreground",
    cursor: "--term-cursor",
    selectionBackground: "--term-selection",
  };
  const ANSI_NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
  ANSI_NAMES.forEach((name, index) => {
    PALETTE_TOKENS[name] = `--term-ansi-${index}`;
    PALETTE_TOKENS[`bright${name[0].toUpperCase()}${name.slice(1)}`] = `--term-ansi-${index + 8}`;
  });
  let palette = undefined;
  let paletteFrame = null;
  // The theme and revision worn or loading, so a repeated shell:theme reloads nothing.
  let wornTheme = null;

  function readPalette(probe) {
    const style = probe.contentWindow.getComputedStyle(probe.contentDocument.documentElement);
    const read = {};
    for (const [key, token] of Object.entries(PALETTE_TOKENS)) {
      const value = style.getPropertyValue(token).trim();
      if (value !== "") read[key] = value;
    }
    return Object.keys(read).length === 0 ? null : read;
  }

  function wearTheme(theme, revision) {
    if (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(theme)) return;
    const key = `${theme}@${revision}`;
    if (key === wornTheme) return;
    wornTheme = key;
    paletteFrame?.remove();
    paletteFrame = null;
    if (theme === "standard") {
      palette = null;
      postToPty({ type: "terminal:theme", theme: null });
      return;
    }
    // Laid out (not display: none), so every engine resolves the frame's styles, but with no size and unseen.
    const probe = document.createElement("iframe");
    probe.setAttribute("aria-hidden", "true");
    probe.tabIndex = -1;
    probe.style.cssText = "position: absolute; width: 0; height: 0; border: 0; visibility: hidden;";
    document.body.appendChild(probe);
    paletteFrame = probe;
    const probeDocument = probe.contentDocument;
    probeDocument.documentElement.dataset.uiTheme = theme;
    const link = probeDocument.createElement("link");
    link.rel = "stylesheet";
    const bundlePath = `/_static/themes/${theme}/theme.css` + (revision ? `?v=${encodeURIComponent(revision)}` : "");
    link.href = new URL(bundlePath, location.href).href;
    link.addEventListener("load", () => {
      if (paletteFrame !== probe) return;
      palette = readPalette(probe);
      postToPty({ type: "terminal:theme", theme: palette });
    });
    // A theme that cannot be loaded (gone, or unavailable) leaves xterm its own colors, not the last theme's.
    link.addEventListener("error", () => {
      if (paletteFrame !== probe) return;
      probe.remove();
      paletteFrame = null;
      wornTheme = null;
      palette = null;
      postToPty({ type: "terminal:theme", theme: null });
    });
    probeDocument.head.appendChild(link);
  }

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (data === null || typeof data !== "object") return;
    // The shell grants focus to the frame it created, which is this page: pass it on to ttyd.
    if (event.source === window.parent && data.type === "ttyd-focus") focusPty();
    if (event.source === frame.contentWindow && data.type === "terminal:ctrl") setCtrlArmed(data.armed === true);
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
          capabilities: { navigation: true, closeChord: false },
          onNavigate: navigate,
          onShown: focusPty,
          onTheme: wearTheme,
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

    name: TmuxSessionName = Field(
        description="The session, which is the terminal's name"
    )
    title: TerminalTitle = Field(description="What the window is called")
    pty_path: str = Field(
        description="The path on the pty origin that attaches to the session"
    )
    pty_label: str = Field(
        description='The pty\'s origin label, or "" while it is not registered'
    )


class PageConfig(FrozenModel):
    """Everything the wrapper's script reads off the document."""

    session: TmuxSessionName | None = Field(
        description="The session the page opened on; None for the bare root"
    )
    page: SessionPage | None = Field(
        description="The session's page, when there is a session"
    )


@pure
@pure
def closed_window_terminal(hint_body: object) -> TmuxSessionName | None:
    """The terminal a closed window showed, from the ``path`` of the shell's hint.

    None for a path with no session (the root, or one naming something that is not a session name), or a body of
    another shape than the shell posts.
    """
    hint = parse_window_closed_hint(hint_body)
    if hint is None:
        return None
    raw_name = window_query_value(hint.path, SESSION_QUERY_KEY)
    if raw_name is None:
        return None
    try:
        return TmuxSessionName(raw_name)
    except InvalidTerminalValueError:
        return None


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
    values = {
        "TITLE": html.escape(title),
        "CONFIG_ID": _CONFIG_ELEMENT_ID,
        "CONFIG": encoded,
        "CONTRACT_PATH": APP_CONTRACT_ROUTE,
    }
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], _PAGE_TEMPLATE)


def _workdir(raw: object) -> Workdir | None:
    """The ``workdir`` a ``new`` request's body names, or None for the default (absent or ""); a bad one is a 400."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise InvalidTerminalValueError(f"invalid {WORKDIR_PARAM!r}: must be a string")
    try:
        return Workdir(raw)
    except InvalidTerminalValueError as e:
        raise InvalidTerminalValueError(f"invalid {WORKDIR_PARAM!r}: {e}") from e


def _launch_body() -> dict[str, object]:
    """The JSON object a ``new`` launch posts; anything else is a 400."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise InvalidTerminalValueError("the launch body must be a JSON object")
    return body


def build_pages_blueprint(
    source: TmuxSessionSource,
    registry_path: Path,
    contract_path: Path,
    # Called for every closed window the shell posts with the terminal the window's path named (None when it
    # named none); the sweeper's ``request_sweep``.
    on_window_closed: Callable[[TmuxSessionName | None], None],
) -> Blueprint:
    """The wrapper page, the ``new`` launch path (a POST answering the new session's page path), the per-session JSON
    the page refreshes from, the health probe, and
    the app contract module at ``contract_path`` (the shell's build output, served from this origin)."""
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)

    def session_page(name: TmuxSessionName) -> SessionPage:
        listed = next(
            (terminal for terminal in source.list_terminals() if terminal.name == name),
            None,
        )
        record = source.remembered_record(name)
        return SessionPage(
            name=name,
            title=listed.title if listed is not None else derive_terminal_title(name),
            pty_path=pty_path_for_session(
                name, record.workdir if record is not None else None
            ),
            pty_label=read_origin_label(registry_path, PTY_APP_NAME),
        )

    @blueprint.get("/")
    def wrapper_page() -> ResponseReturnValue:
        raw_session = request.args.get(SESSION_QUERY_KEY, "")
        session = _session_name(raw_session) if raw_session != "" else None
        config = PageConfig(
            session=session, page=session_page(session) if session is not None else None
        )
        response = Response(render_page(config), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post(NEW_PATH)
    def new_terminal() -> ResponseReturnValue:
        created = source.create_terminal(_workdir(_launch_body().get(WORKDIR_PARAM)))
        return jsonify({"path": session_page_path(created.name)})

    @blueprint.get(SESSION_API_PATH)
    def session_json(name: str) -> ResponseReturnValue:
        return jsonify(session_page(_session_name(name)).model_dump(mode="json"))

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok"})

    @blueprint.post(WINDOW_CLOSED_PATH)
    def window_closed() -> ResponseReturnValue:
        # The body says which window closed, which is proof a window showed its terminal; what is shown now
        # is still read from the shell.
        on_window_closed(closed_window_terminal(request.get_json(silent=True)))
        return "", HTTP_NO_CONTENT

    @blueprint.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        if not contract_path.is_file():
            return (
                jsonify(
                    {
                        "detail": f"the workspace shell's frontend is not built: {contract_path} is missing"
                    }
                ),
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
