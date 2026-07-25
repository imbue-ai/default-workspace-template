# Live-view v2: full-fidelity, low-latency human browsing for the fleet

Status: PLANNING. No code in this branch yet. Builds on PR #315
(`browser-fleet-improvements`: headful-under-Xvfb, xclip clipboard, fill-the-pane
resize, pane auto-open) — that PR merges first; this plan assumes its tree.

Produced from four parallel deep-mapping passes over (1) the fleet daemon/agent
contract, (2) the browser-use integration, (3) the human-interaction surface and
system_interface embedding, and (4) external research on Neko / Selkies / aiortc,
media connectivity, and packaging. File:line references are to the
`browser-fleet-improvements` tree.

---

## 1. Problem

The human live view is a CDP `Page.startScreencast` JPEG slideshow over a
WebSocket, painted onto a canvas. Structural consequences:

- **Latency/choppiness even on localhost.** No video codec, no real framerate
  control; every frame is a full JPEG encode+ship+decode. CDP itself caps out
  well under 24fps on busy pages. Steel and Browserbase use the same design and
  have the same ceiling — it is built for *watching an agent*, not for a human
  to drive. Anti-throttling flags are already injected by browser-use
  (`CHROME_DEFAULT_ARGS` includes `--disable-background-timer-throttling`,
  `--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`),
  so throttling is not the cause; the transport is.
- **Fidelity gaps.** The screencast captures page content only: native context
  menus open invisibly (right-click "does nothing"), native `<select>`
  dropdowns/date pickers neither render nor accept CDP page-scoped input,
  drag is throttled/synthetic and unreliable.
- **Input round-trip.** Nothing appears until a fresh frame completes
  input -> WS -> CDP -> repaint -> JPEG -> WS -> canvas.

## 2. Requirements (everything committed to in this effort)

- R1 **Full fidelity**: native right-click menus visible AND clickable; native
  dropdowns/pickers work; real click-drag (text selection, sliders, HTML5 DnD);
  cursor rendered.
- R2 **Smooth, low-latency**: real video codec streaming, not a JPEG slideshow;
  feels close to a local browser on localhost; usable over the tunnel.
- R3 **Resize**: browser fills the pane (grow and shrink); size FROZEN while an
  agent drives; resolution reported on resume with the re-`state` nudge
  (preserves #315 semantics).
- R4 **Clipboard**: text + images, both directions; trigger-agnostic (Ctrl/Cmd
  keys, right-click menu, Edit menu, any keymap); "Pasting…/Copying…" toast for
  large payloads; paste is server-owned once uploaded (user can navigate away);
  right-click -> Copy in the remote must reach the user's local clipboard.
- R5 **Focus-capture mode**: a visible indicator when the user is interacting
  with the browser pane; while focused, every capturable shortcut routes to the
  remote browser; an immersive (fullscreen) tier captures browser-reserved
  combos too (Ctrl+T, Ctrl+W, Ctrl+Shift+T, Ctrl+N, Ctrl+Tab).
- R6 **Our chrome**: custom tab bar + navbar stay (new/close/switch tab, back/
  forward/reload, URL bar); the underlying browser is chromeless (tabless).
  Ctrl+Shift+T (reopen closed tab) supported via a daemon-side recently-closed
  stack. Ctrl+W on the last tab: close it and auto-open a fresh home tab — the
  browser itself is only retired via the explicit close command/UI.
- R7 **Untouched**: Fortress engine (stealth), browser-use driving, agent CDP
  element-index control, the ownership/handoff/queue state machine, the
  session-gated `service:browser` pane model and auto pane-pull, manifest
  persistence/restore, OOM shedding integration.
- R8 **Efficiency**: encode only while a human watches; pause when the pane is
  hidden; adaptive resolution/framerate; fits 4 GB / 2–4 vCPU boxes with
  `_MAX_SESSIONS=3`.
- R9 **Connectivity**: works both on local docker (HTTP-only port publishing)
  and behind the Cloudflare tunnel (HTTP/WS only) — with no mandatory new
  ports and no mandatory TURN dependency.
- R10 **Per-browser clipboard isolation** (fixes the latent bug where all
  browsers share one X11 CLIPBOARD on `:99`).

## 3. The transport decision (the headline)

**Primary transport: encoded video over the existing WebSocket, decoded with
WebCodecs — not WebRTC. WebRTC becomes an optional later uplift.**

This diverges from the working title of this branch, deliberately. The research
pass found:

1. **The Cloudflare tunnel cannot carry WebRTC media, period.** Public
   hostnames are HTTP/WS only; UDP requires WARP on every client. Remote users
   would need a TURN relay (Cloudflare TURN: TLS on 443, ~1 TB free then
   $0.05/GB) — a hard runtime dependency, extra RTT, and per-GB cost.
2. **Local docker only publishes HTTP ports.** WebRTC would require publishing
   a dedicated identical-numbered UDP/TCP mux port (Pion-style). The natural
   Python library (aiortc) **cannot bind a fixed port at all** — no udpmux
   equivalent — so stock aiortc cannot receive media behind docker port
   publishing without TURN.
3. **The industry moved our way.** Selkies 2.x — the reference open-source
   low-latency Linux remote desktop — *removed* its GStreamer/WebRTC-first
   runtime; its default mode is now **striped software H.264/JPEG over plain
   WebSockets, decoded client-side with WebCodecs** (`pixelflux` capture/encode,
   MPL-2.0, pip-installable). WebRTC is opt-in for the cases that can carry it.
   LinuxServer Webtop 3.0 ships this stack.

WS + WebCodecs gives us the actual goals — video-codec smoothness (x264
"ultrafast/zerolatency", damage/stripe-based encoding), full-display fidelity,
low latency — while riding the **already-proxied HTTP port through the existing
service dispatcher**, which was verified to be a transparent bidirectional
passthrough. Zero new ports, zero TURN, works identically local and tunneled,
stays in Python/asyncio next to the daemon. The legacy JPEG screencast is
retained as an automatic fallback for clients without WebCodecs.

WebRTC remains on the roadmap (Phase 7) for the local-docker case (single
published UDP mux port) and/or Cloudflare TURN remotely, **only if** measured
WS-mode latency proves insufficient. Evidence so far says it won't be needed.

## 4. Target architecture

```
per browser (xN, N = _MAX_SESSIONS):
  Xvfb :N  (or Xorg+dummy — spike-dependent)  ~25–60 MB
    └─ Fortress Chromium, headful, chromeless, DISPLAY=:N
         ├─ browser-use CDP client  (agent driving — unchanged)
         ├─ Playwright observer CDP client (tabs/nav — unchanged)
         └─ paints the ACTIVE tab to the display

daemon (browser-service, unchanged process):
  per WATCHED browser: pixelflux capture+encode task (striped x264 / JPEG)
    frames -> /browsers/<id>/stream WS (binary)  [new socket; open = watched]
  control/tabs/handoff/lifecycle/input/resize -> existing /cast WS (unchanged)
  clipboard -> existing HTTP routes, xclip -display :N
  clipboard-watch (XFIXES) -> push copy-outs over /cast
  human input -> existing gate (_input_enabled) -> XTest injection (xdotool)
  agent input -> CDP ActionHandler (unchanged)

viewer (assets/index.html, same iframe/pane model):
  WebCodecs VideoDecoder -> canvas   (replaces JPEG <img> painting)
  our tab bar / navbar / overlays / take-control — unchanged UI contract
  focus-capture mode + fullscreen immersive tier
```

### Design decisions

- **D1 — Transport**: WS + WebCodecs striped H.264 via `pixelflux`; JPEG
  encoder mode as per-client fallback; legacy CDP screencast retained behind a
  flag during rollout. Video rides a NEW dedicated `/browsers/<id>/stream`
  WebSocket so control messages never contend with frames (head-of-line), and
  so the stream socket's open/close IS the encode-on-demand signal.
- **D2 — One X display per browser.** Daemon-spawned `Xvfb :N` per
  `LiveBrowser` (lifecycle owned like the Chromium it hosts). Fixes clipboard
  cross-talk (R10). The shared `[program:xvfb]` `:99` remains only as a
  fallback/transition display.
- **D3 — Chromeless browser.** Preferred: plain window on a WM-less display
  with the capture region cropped to the web-contents area (constant offsets,
  measurable once via `Page.getLayoutMetrics` vs window bounds). Alternative:
  `--app=` mode — but multi-tab behavior under `--app`/`--kiosk` is
  Chromium-version-dependent and unpinned (new targets may open separate
  windows); the P0 spike decides. Our tab bar stays the tab UI either way
  (tabs are CDP targets, not window chrome).
- **D4 — Human input via XTest** (`xdotool`/python-xlib), display-level, so
  native menus/dropdowns/drag are genuinely clickable. Input messages keep
  riding the existing cast WS and MUST keep routing through
  `handle_cast_message`'s `_input_enabled` gate under `_control_lock` —
  ownership semantics unchanged. Coordinates become display coordinates
  (identical to the video frame — simpler than today's scaled mapping).
  Mousemove throttle drops from 30 ms to ~10–15 ms (XTest is cheap).
  Agents stay on CDP; the two never conflict (different injection layers,
  same serialized gate).
- **D5 — Real-window sizing.** Construct the browser-use session headful with
  `no_viewport=True` (stops browser-use re-applying
  `Emulation.setDeviceMetricsOverride` per tab) and drop the fleet's own
  override + screencast clamp. Size = the X display / Chromium window
  (`--window-size` + `Browser.setWindowBounds`). Element indexing is safe:
  browser-use reads the real viewport via `Page.getLayoutMetrics` and targets
  nodes, not pixels (verified in 0.13.1 source).
- **D6 — Resize-to-pane** via runtime display resize (`xrandr`). Xvfb's RANDR
  resize support is version-sensitive (historically could not grow past the
  initial size) — the P0 spike tests it on our exact image; fallbacks in
  order: Xorg + xf86-video-dummy per display (neko's proven pattern), or
  fixed-max framebuffer + client-side scaling. The #315 semantics carry over
  verbatim: resize only while human/idle controls (`_apply_resize` gate),
  `_render_w/h` remain the source of truth, `_wake_agent` still reports a
  changed resolution on resume.
- **D7 — Clipboard.** Keep the #315 HTTP routes + xclip design, now with
  `-display :N` (R10). Add a daemon-side clipboard WATCHER (XFIXES
  selection-change events, neko/selkies pattern): any remote copy — including
  right-click -> Copy in a native menu — pushes `{type:"clipboard", mime, data}`
  over the cast WS; the viewer writes the user's local clipboard
  (`navigator.clipboard`), caching for the next user gesture if the write is
  denied. Paste-in flow unchanged (event-driven, raw-bytes POST, toast,
  server-owned).
- **D8 — Focus-capture mode (two tiers).**
  - *Focused* (default on click into the video): visible indicator (border
    glow + a "keys go to browser" chip); key listeners move to window-level
    and preventDefault everything cancelable — Ctrl+C/V/X, Ctrl+L, Ctrl+F,
    F-keys, Tab, etc. — forwarding to the remote. Browser-reserved combos
    (Ctrl+T/W/N/Tab, Cmd+Q) CANNOT be intercepted outside fullscreen — that is
    a web-platform limit, not a design choice; the chip links to immersive
    mode and the custom tab bar covers those actions on-screen.
  - *Immersive* (explicit button): `requestFullscreen()` +
    `navigator.keyboard.lock()` (Chromium-only, requires fullscreen + secure
    context) — captures literally everything including Ctrl+W/Ctrl+T.
    Exit via hold-Esc (Chromium's built-in).
  - Focus exits on clicking outside the pane / pane blur / Esc-tap outside a
    page context. Plumbing prerequisite: thread dockview's
    `isVisible`/`isActive` into the iframe via a new host->iframe postMessage
    channel (the chat panels' `createMithrilRenderer` is the copy-pattern —
    today the browser pane's iframe renderer is the only one that threads
    neither), and add `allow="fullscreen"` (+ keep `clipboard-read;
    clipboard-write`) on `IframePanel`.
- **D9 — Tab keyboard semantics** (daemon-side, works in immersive mode and
  from the tab bar): Ctrl+T -> new home tab; Ctrl+W -> close active tab, and if
  it was the last, open a fresh home tab instead of killing the browser;
  Ctrl+Shift+T -> reopen from a per-browser recently-closed URL stack (the
  manifest already tracks tab URLs; keep last ~10 closed).
- **D10 — Encode-on-demand lifecycle**: pipeline starts when `/stream` gains
  its first subscriber, stops at zero; pane hidden (dockview visibility via the
  D8 postMessage channel) closes/pauses the stream socket; adaptive: encode at
  min(pane size, display size), damage/stripe encoding keeps static pages
  nearly free; per-stream CPU watchdog caps degenerate pages.
- **D11 — Control plane unchanged.** The cast WS keeps carrying
  control/tabs/handoff_request/lifecycle/ping and take_control/
  return_to_agents/resize/input — ordered and reliable, which lossy media
  transports cannot guarantee. The viewer's deterministic lifecycle rendering
  (control-before-frames seeding) is preserved.
- **D12 — What #315 code this supersedes** (after rollout): the CDP screencast
  producer (`_set_active_page` screencast sub-block, `_on_screencast_frame`,
  `_ack_and_send`, `_capture_one_frame`), the `frame` message + JPEG canvas
  painting, the device-metrics resize mechanism, and the keepalive *ping* (the
  sweeps in `_keepalive_loop` stay). Everything else in #315 — Xvfb+xclip
  foundation, clipboard routes/UI, session gate, pane auto-open, resize
  semantics, resolution-on-resume — is kept or re-homed, not discarded.

## 5. Preserved contracts (verbatim)

The full inventory lives in the mapping pass; the binding summary:

- CLI surface + exit codes (`0/1/2/3/4/64/69`), env vars, NDJSON task/hold
  streaming where the connection is the lease, direct-command status
  vocabulary (`ok/busy_human/busy_agent/lost_control/stale_index/...`).
- Ownership machine: single-writer `_write_control_locked`, CAS `_transition`,
  human-always-wins `take_control`, `handoff` front-of-queue + pin, wait vs
  resume queues, idle-lease + claim-window sweeps, `_wake_agent` messaging
  (incl. resolution report), lifecycle init/running/crashed, crash abandonment.
- Manifest persistence/restore, profile-dir naming hack
  (`browser-use-user-data-dir-` prefix; anti-`_copy_profile` tripwire tests),
  `_MAX_SESSIONS`, serialized launches, OOM retagging.
- Pane model: `service:browser?session=<name>` refs, the session gate, the
  optimistic pane pull (`context` -> `--layout`), SKILL.md behavioral contract.

## 6. browser-use constraints (verified against 0.13.1 source)

- browser-use launches Fortress as a **raw subprocess** with its own CDP
  client; our Playwright is a second observer-only CDP client. Agent control
  is TCP-to-localhost, DISPLAY-independent.
- `BrowserProfile.env` exists but is NOT applied on local launches — the child
  inherits `os.environ`. Per-browser DISPLAY therefore = mutate
  `os.environ["DISPLAY"]` around `session.start()` (already serialized by
  `_startup_lock`, so race-free), restore after. xclip gets `-display :N`.
- Arbitrary launch-arg passthrough confirmed (`args=[...]` appended verbatim;
  `--window-position=0,0` always injected; anti-throttling flags already in
  `CHROME_DEFAULT_ARGS`).
- `no_viewport=True` (headful) is the supported switch that stops browser-use's
  per-tab device-metrics overrides; the `not (headless and no_viewport)`
  assert is satisfied.
- CI only exercises headless (`BROWSER_HEADLESS=1` in the integration test);
  headful/display behavior needs the P0 spike + a live workspace test.

## 7. Cost budget (3 browsers, 4 GB / 2–4 vCPU box)

| Item | Cost | Notes |
|---|---|---|
| Xvfb per browser | ~25–60 MB RSS | fb is W*H*4 ≈ 8.3 MB at 1080p |
| pixelflux encode, watched | ~0.3–0.6 core @ 1280x800/15fps | x264 ultrafast, stripes; 1080p30 ≈ 1–1.5 cores |
| Unwatched browser | ~0 | no capture, no encode |
| apt/pip deltas | pixelflux via pip; no GStreamer needed | GStreamer closure (~80–150 MB) only if Phase 7 |
| Bandwidth | lower than JPEG slideshow | inter-frame + damage encoding |

Policy: encode only while watched (normally one pane), default 1280x800@15–20,
cap concurrent watched streams, CPU watchdog. Three simultaneously-watched
1080p30 streams would saturate the box — prevented by policy, not hope.

## 8. Risk register

| # | Risk | Mitigation |
|---|---|---|
| 1 | Xvfb runtime resize (RANDR) unreliable on our image | P0 spike; fall back to Xorg+dummy (neko pattern) or fixed-max + client scaling |
| 2 | gVisor quirks: XShm (pixelflux), Xorg-dummy, per-display sockets | P0 spike all three on the real image; ximagesrc/x11grab as capture fallback |
| 3 | Chromeless multi-tab behavior under --app unpinned | P0 spike with real Fortress; crop-capture plan B keeps plain window |
| 4 | WebCodecs H.264 unavailable in a user's browser | server-side JPEG mode per client (automatic), legacy screencast last resort |
| 5 | XTest input vs ownership races | injection stays behind the existing `_input_enabled`/`_control_lock` gate; no new authority |
| 6 | MPL-2.0 housekeeping (pixelflux / vendored selkies files) | vendored files isolated + headers kept; publish modified copies if images are distributed |
| 7 | Keyboard.lock is Chromium-only | focus tier degrades gracefully; tab bar covers reserved combos everywhere |

## 9. Phased build order

- **P0 — Spike (days, on the real gVisor image).** Xvfb vs Xorg-dummy runtime
  resize; pixelflux capture+encode of `:N` (XShm under gVisor); binary frames
  through the service-dispatcher WS proxy; WebCodecs decode in Chrome/Safari/
  Firefox; Fortress under `--app` multi-tab behavior; CPU at 1280x800@15 and
  1080p@30. Go/no-go on D2/D3/D6 choices.
- **P1 — Per-browser displays.** Daemon-spawned Xvfb `:N` per LiveBrowser,
  DISPLAY env around serialized start, xclip `-display`, readiness/teardown,
  supervisord `:99` kept as fallback.
- **P2 — WS video path.** pixelflux task per watched browser; new `/stream`
  WS; WebCodecs canvas in the viewer; encode-on-demand off socket lifecycle;
  legacy screencast behind a flag. Ship: latency/fidelity of VIDEO improves;
  input still CDP at this point.
- **P3 — Display-level input + clipboard watch.** XTest injection through the
  existing gate; drop the mousemove throttle; XFIXES clipboard watcher +
  copy-out push (right-click Copy reaches the user). Native menus/dropdowns/
  drag now fully work.
- **P4 — Chromeless + tab keyboard semantics.** Spike-informed chromeless
  launch (crop or --app); Ctrl+W-last-tab home-tab behavior; recently-closed
  stack + Ctrl+Shift+T; custom bar unchanged.
- **P5 — Resize-to-pane.** xrandr (or dummy-driver) live resize wired to the
  #315 resize semantics (freeze-during-agent, resolution-on-resume).
- **P6 — Focus-capture.** postMessage visibility/active channel; focused tier
  (window-level capture + indicator chip); immersive tier (fullscreen +
  keyboard.lock); `allow="fullscreen"` on IframePanel; encode pause on hidden.
- **P7 — Optional WebRTC uplift.** Only if measured WS latency insufficient:
  local single-UDP-mux-port path (+ mngr provider port-publish work) and/or
  Cloudflare TURN for remote, likely via Selkies' webrtc mode or a Pion
  sidecar (aiortc ruled out for port binding).

Each phase lands behind a flag next to the working legacy path; nothing breaks
the fleet mid-rollout.

## 10. Open questions

1. P0 outcomes gate D2 (Xvfb vs Xorg-dummy), D3 (crop vs --app), D6 strategy.
2. Audio (pcmflux/Opus) — deliberately out of scope for v1; revisit after P3.
3. Multi-viewer same browser: WS fan-out makes this nearly free (each
   subscriber gets the encoded stream) — confirm the stripe encoder supports
   multi-subscriber without re-encode.
4. mngr-side work is required ONLY for Phase 7 (publishing a UDP port on the
   docker/Lima providers); Phases 0–6 need no mngr changes.
