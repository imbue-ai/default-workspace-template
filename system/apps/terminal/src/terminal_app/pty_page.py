"""The script the terminal adds to ttyd's page on the pty origin, so the wrapper's phone keys reach xterm.

The wrapper page (``pages.py``) is on another origin than ttyd, so its key strip can only ``postMessage`` into the
pty frame. The patched web client the imbue-mngr-ttyd package ships exposes its xterm terminal as ``window.term``;
this script, inserted into that client when it is installed (``dispatch.install_ttyd_web_client``), listens for the
wrapper's messages and feeds xterm:

- ``{type: "terminal:key", key, ctrl}`` sends ``key`` (``Escape``, ``Tab``, or an arrow) as the escape sequence
  xterm itself sends for it, honoring the application cursor mode for arrows and ``ctrl`` as xterm's Ctrl modifier.
- ``{type: "terminal:ctrl", armed}`` arms (or disarms) a one-shot Ctrl for the next key typed on the keyboard.
  Every way xterm turns typing into input (keydown, keypress, the input event, IME composition, and the textarea
  diffing a soft keyboard's keyCode 229 goes through) ends in the core service's ``triggerDataEvent``, so the
  script wraps that one method while Ctrl is armed; the next single character typed is sent as its control
  character, and the script posts ``{type: "terminal:ctrl", armed: false}`` back to the wrapper so its Ctrl key
  releases. ``triggerDataEvent`` is xterm's internal API, not its public one, so a client whose xterm lacks it
  keeps the strip's own keys working and warns that typed keys will not take the Ctrl.

A tap on the terminal (a touch that ends where it started, quickly) focuses xterm from that touch's own handler.
iOS raises the soft keyboard only for a focus made inside a user gesture in the focused element's own frame, so
the wrapper's posted ``ttyd-focus`` (which the patched client already handles) cannot raise it; this can.
"""

from typing import Final

from imbue.imbue_common.pure import pure

# The script's text inside a <script> element: it must never contain a closing script tag.
PTY_PAGE_SCRIPT: Final[str] = r"""
(() => {
  "use strict";
  const ARROW_FINALS = new Map([["ArrowUp", "A"], ["ArrowDown", "B"], ["ArrowRight", "C"], ["ArrowLeft", "D"]]);
  const PLAIN_KEYS = new Map([["Escape", "\x1b"], ["Tab", "\t"]]);
  // xterm's own Ctrl mapping (its keyboard evaluator) for the keys that are not letters.
  const CTRL_CODES = new Map([
    ["@", 0], [" ", 0], ["2", 0], ["[", 27], ["3", 27], ["\\", 28], ["4", 28], ["]", 29], ["5", 29],
    ["^", 30], ["6", 30], ["_", 31], ["7", 31], ["/", 31], ["?", 127], ["8", 127],
  ]);
  // A touch that moves further or lasts longer is a scroll or a selection, not a tap.
  const TAP_SLOP_PX = 10;
  const TAP_MAX_MS = 500;
  const isFramed = window.parent !== window;
  let isCtrlArmed = false;
  let wrappedService = null;
  let touchStart = null;

  function withCtrl(char) {
    const upper = char.toUpperCase();
    if (upper.length === 1 && upper >= "A" && upper <= "Z") return String.fromCharCode(upper.charCodeAt(0) - 64);
    const code = CTRL_CODES.get(char);
    return code === undefined ? char : String.fromCharCode(code);
  }

  function sequenceFor(term, key, isCtrl) {
    const plain = PLAIN_KEYS.get(key);
    if (plain !== undefined) return plain;
    const final = ARROW_FINALS.get(key);
    if (final === undefined) return null;
    if (isCtrl) return "\x1b[1;5" + final;
    return (term.modes.applicationCursorKeysMode ? "\x1bO" : "\x1b[") + final;
  }

  function releaseCtrl() {
    isCtrlArmed = false;
    if (isFramed) window.parent.postMessage({ type: "terminal:ctrl", armed: false }, "*");
  }

  function applyCtrlToTypedKeys(term) {
    const service = term._core?.coreService;
    if (service === undefined || service === wrappedService) return;
    if (typeof service.triggerDataEvent !== "function") {
      console.warn("[terminal] this xterm has no triggerDataEvent: Ctrl applies to the key strip only");
      return;
    }
    const send = service.triggerDataEvent.bind(service);
    service.triggerDataEvent = (data, wasUserInput) => {
      if (isCtrlArmed && wasUserInput === true && typeof data === "string" && [...data].length === 1) {
        releaseCtrl();
        return send(withCtrl(data), wasUserInput);
      }
      return send(data, wasUserInput);
    };
    wrappedService = service;
  }

  window.addEventListener("message", (event) => {
    if (!isFramed || event.source !== window.parent) return;
    const data = event.data;
    const term = window.term;
    if (data === null || typeof data !== "object" || term === undefined) return;
    if (data.type === "terminal:ctrl") {
      isCtrlArmed = data.armed === true;
      if (isCtrlArmed) applyCtrlToTypedKeys(term);
    } else if (data.type === "terminal:key") {
      const sequence = sequenceFor(term, data.key, data.ctrl === true);
      if (sequence === null) return;
      isCtrlArmed = false;
      term.input(sequence, true);
    }
  });

  window.addEventListener("touchstart", (event) => {
    const touch = event.touches[0];
    touchStart = event.touches.length === 1 ? { x: touch.clientX, y: touch.clientY, at: event.timeStamp } : null;
  }, { capture: true, passive: true });

  window.addEventListener("touchend", (event) => {
    const start = touchStart;
    touchStart = null;
    const touch = event.changedTouches[0];
    if (start === null || touch === undefined || event.touches.length !== 0) return;
    const isTap = Math.hypot(touch.clientX - start.x, touch.clientY - start.y) <= TAP_SLOP_PX
      && event.timeStamp - start.at <= TAP_MAX_MS;
    if (isTap) window.term?.focus();
  }, { capture: true, passive: true });
})();
"""

_SCRIPT_TAG: Final[bytes] = f"<script>{PTY_PAGE_SCRIPT}</script>".encode()
_BODY_CLOSE: Final[bytes] = b"</body>"


@pure
def add_pty_page_script(client_html: bytes) -> bytes | None:
    """The web client with the script inserted before its closing body tag, or None when it has none."""
    index = client_html.rfind(_BODY_CLOSE)
    if index < 0:
        return None
    return client_html[:index] + _SCRIPT_TAG + client_html[index:]
