# Live-view v2: full-fidelity, low-latency human browsing

This is the wire + component contract for the browser fleet's human live view. It
REPLACES the legacy CDP `Page.startScreencast` JPEG-slideshow entirely (that code is
deleted, not flagged off). Agents (browser-use over CDP) and the ownership/handoff
state machine are untouched; only the human view + human input change.

Verified on this image (see the P0 spike + reference digs into Selkies/pixelflux and
neko): pixelflux 2.0.0 installs as a prebuilt CPU wheel (libx264 bundled, GStreamer-
free), distro/Fortress Chromium decodes H.264 via WebCodecs, Xvfb `xrandr --fb`
shrinks-from-max, python-xlib XTest injects display-level input, XFIXES watches the
clipboard.

## Components

```
per browser:
  Xvfb :N   (daemon-spawned, one per LiveBrowser, started at 1920x1080 +RANDR)
    └─ Fortress Chromium, headful, DISPLAY=:N, plain window at (0,0)
         ├─ browser-use CDP client   (agent driving — UNCHANGED)
         ├─ Playwright observer CDP  (tabs/nav/window-bounds — UNCHANGED role)
         └─ renders the active tab to the display
  pixelflux ScreenCapture(:N)        (capture+encode, one per browser, on-demand)
  python-xlib XTest connection(:N)   (human input) + XFIXES clipboard watch
  xclip -display :N                  (clipboard data I/O — re-homed from :99)

daemon (browser-service, one asyncio loop — UNCHANGED process model):
  /browsers/<id>/stream  WS (NEW, binary)  = encoded video, one queue per subscriber
  /browsers/<id>/cast    WS (existing)      = control/tabs/handoff/ping/input/resize/clip-notify
  /browsers/<id>/clipboard HTTP (existing)  = clipboard copy/paste data

viewer (assets/index.html — same iframe/pane/session model):
  WebCodecs VideoDecoder-per-stripe -> canvas   (replaces JPEG <img> paint)
  overlays / take-control / returnbar            (NO custom tab bar/navbar --
    the WHOLE Chromium window is streamed, native chrome and all; the user drives
    the native tabs / URL bar / new-tab button directly via XTest)
  focus-capture (focused + immersive) modes
```

## `/stream` WebSocket (NEW) — binary video

- Opens when the pane wants video; its open/close IS the encode-on-demand signal
  (capture starts on first subscriber, stops at zero). Separate socket from `/cast`
  so video never head-of-line-blocks control.
- **Handshake**: the client sends ONE text frame first: `{"h264": true|false}` (from
  `VideoDecoder.isConfigSupported({codec:"avc1.42E01E",...})`). The server starts the
  capture in H.264 mode if true, else JPEG mode. No other client→server traffic on
  this socket (input/control ride `/cast`).
- **Frames**: every subsequent server→client message is BINARY — one pixelflux stripe,
  header included, sent verbatim (`ws.send(memoryview(stripe))`, zero-copy). The daemon
  never parses it. Formats (big-endian, produced natively by pixelflux):
  - H.264 stripe: `[0]=0x04, [1]=frametype (0x01 IDR / 0x00 delta), [2:4]=frame_id u16,
    [4:6]=y_start u16, [6:8]=width u16, [8:10]=height u16, [10:]=Annex-B NALs`
    (SPS/PPS inline on keyframes).
  - JPEG stripe: `[0]=0x03, [1]=pad, [2:4]=frame_id u16, [4:6]=y_start u16, [6:]=JFIF`.
  - Stripes are independent and drawn at `(0, y_start)`. No frame reassembly.
- On a new subscriber the daemon calls `request_idr_frame()` so it gets a keyframe to
  start decoding immediately (no black canvas on a static page).

## Viewer decode (WebCodecs)

Port of Selkies' `example/index.html` decode (MPL-2.0; keep the header on copied code):
- Keep a `Map<y_start, VideoDecoder>` (H.264) or decode JPEG via `ImageDecoder`.
- Per stripe: parse `byte[0]` → 0x04 H.264 / 0x03 JPEG. For H.264, lazily
  `configure({codec, codedWidth:width, codedHeight:height, optimizeForLatency:true})`
  on first stripe for that `y_start`, then feed `new EncodedVideoChunk({type: frametype
  ? "key" : "delta", timestamp: monotonic_us, data: annexb})`. Drop deltas until the
  first key. `output`→ push VideoFrame to a queue.
- One `requestAnimationFrame` render loop: `ctx.drawImage(frame, 0, y_start); frame.close()`.
- The canvas is sized to the stripe width and the total display height (from `/cast`
  `control.resolution`). CSS `object-fit: contain` letterboxes it in the pane.

## `/cast` WebSocket (existing) — control + input, video removed

Unchanged messages: `control` (carries lifecycle + `resolution:[w,h]`), `tabs`,
`handoff_request`, `launch_failed`, `initializing`, `ping`, and inbound
`take_control`/`return_to_agents`/`tab`/`navigate`/`back`/`forward`/`reload`.

Changed:
- The `{type:"frame"}` JPEG message is DELETED (video is on `/stream`).
- Inbound `{type:"mouse"}` / `{type:"key"}` now carry **display coordinates** and are
  injected via XTest (python-xlib), not CDP `Input.dispatch*`. Coords: the viewer maps
  canvas px → display px 1:1 (the frame IS the whole window at (0,0), no crop). Key
  events carry the browser `key`/`code`; the daemon maps to an X keysym (physical-code
  table + Unicode rule).
- `{type:"resize"}`: still `_input_enabled`-gated (frozen while an agent drives). Now
  resizes the Chromium WINDOW (CDP `Browser.setWindowBounds`) and moves the pixelflux
  capture region; `_render_w/h` stay the source of truth; resolution-on-resume nudge
  preserved.
- NEW server→client `{type:"clipboard", mime, data}`: pushed by a ~500ms clipboard poll
  when an app inside the remote page copies (e.g. right-click → Copy). The viewer writes
  the user's local clipboard (`navigator.clipboard`), caching for the next gesture if denied.
- Human tab/navigation is done on the NATIVE browser chrome (streamed + XTest-clicked),
  so the viewer sends no tab/nav messages; the agent's own tab control (`act_tab` → CDP)
  is unchanged.

## Display / whole-browser / resize

- One Xvfb `:N` per browser at 1920x1080 (`_RENDER_MAX`), `+extension RANDR`. Chromium
  DISPLAY=:N by mutating `os.environ["DISPLAY"]` around the serialized `session.start()`
  (browser-use's subprocess inherits `os.environ`; the start is already under
  `_startup_lock`, so it's race-free). Restored after.
- Whole-browser stream: the WHOLE Chromium window (native tab strip, toolbar, URL bar)
  is captured (region = the window at (0,0), no crop); the user drives the native chrome
  directly via XTest. The daemon only records the window id (for resize).
- Resize-to-pane: shrink the window (never grow the framebuffer past its initial max —
  Xvfb can't) via `Browser.setWindowBounds` + `update_capture_region`. Frozen during
  agent control (existing `_apply_resize` gate).

## Focus-capture (viewer)

- Dockview visibility/active is threaded into the iframe via a host→iframe postMessage
  channel (`minds:panel-visibility`); `IframePanel` gains `allow="fullscreen"`.
- Focused tier (click into the video): border-glow + "keys go to browser" chip; window-
  level key capture, preventDefault everything cancelable, forward to `/cast`.
- Immersive tier (button): `requestFullscreen()` + `navigator.keyboard.lock()` (Chromium
  only) to capture Ctrl+T/W/Tab; exit on hold-Esc.
- Encode pauses when the pane is hidden (stream socket closed on `isVisible:false`).

## What is deleted (the legacy human-view path)

`session.py`: `_on_screencast_frame`, `_ack_and_send`, `_capture_one_frame`,
`_stop_screencast`, the `Page.startScreencast`/`Emulation.setDeviceMetricsOverride`
blocks in `_set_active_page`, `_SCREENCAST_*` constants, `_latest_frame`/`_send_in_flight`,
and CDP `Input.dispatch*` for HUMAN mouse/key in `_dispatch_input`. `index.html`:
`drawFrame`, the `<img>`/base64 paint, `scaled()` CDP-coord mapping. The `/cast`
`{type:"frame"}` producer/consumer. Agent CDP control (`act_*`, browser-use) is NOT
touched.
