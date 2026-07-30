# Streamed Browser

A full Chromium — its own tab strip, address bar, native menus and dialogs —
streamed into a workspace pane as pixels, instead of the browser app's rebuilt
HTML chrome around a CDP page view.

Open it from the workspace "+" picker (`streamed-browser`), or
`python3 system/scripts/layout.py open streamed-browser`.

## How it works

- **Session** (`session.py`): a private Xvfb display plus one Fortress Chromium
  maximized onto it, created on the first viewer connect and kept alive across
  reconnects (profile at `data/.apps/streamed-browser/profile`).
- **Video** (`videopipe.py`): pixelflux (Selkies' Rust engine) captures the
  display via shared memory, encodes damage-driven H.264 on the CPU (idle
  screens cost nothing), and the service streams the frames over one WebSocket.
  The viewer acknowledges each frame as it leaves the decoder and the server
  never has more than two frames unacknowledged — bounding in-flight bytes
  across every tunnel hop, so congestion means fewer fresh frames rather than
  seconds of stale backlog. Dropped delta frames recover through a rate-limited
  IDR request.
- **Input** (`xinput.py`): the viewer sends Selkies-grammar messages
  (`kd`/`ku`/`kr`/`m`); injection is XTEST via python-xlib, with the keyboard
  core ported from Selkies (MPL-2.0, provenance in the file header) — keymap
  level resolution, dynamic overlay keycodes for unmapped keysyms, held-state
  cleanup on disconnect.

## v1 limits (deliberate)

- Fixed 1280x800; a pane resize does not resize the display.
- No clipboard sync, audio, touch, or gamepad; no bitrate adaptation (the ack
  channel already carries the timing signal a later version would use).
- One session per workspace.
