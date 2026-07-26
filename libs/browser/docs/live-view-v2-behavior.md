# Live-view v2 — behavioral spec (the contract to verify)

The human live view is a **whole-browser stream**: each fleet browser is a headful
Fortress Chromium under its own Xvfb display; pixelflux captures the ENTIRE Chromium
window (native tab strip, toolbar, URL bar included) as striped H.264/JPEG over a
`/stream` WebSocket, decoded in the viewer with WebCodecs. Human input is injected at
the display level via XTest, so the user drives the **native** browser directly. The
agent drives the same Chromium over CDP (browser-use), unchanged. This doc is the
behavior every check verifies against.

## A. Mouse / pointer (human, while they control)
- **Left click** anywhere on the streamed browser lands natively (a link, a native
  tab, the native "+" new-tab button, the native back/forward/reload, the native URL
  bar). Coordinates map canvas-pixel → display-pixel 1:1 (window at 0,0, no crop).
- **Right click** opens the **native** context menu, and menu items are clickable
  (this is the whole point of display-level input — CDP couldn't do it).
- **Drag**: press-move-release with the button held selects text / drags sliders /
  does HTML5 drag-and-drop (separate mousePressed → mouseMoved… → mouseReleased; XTest
  keeps the button state across moves).
- **Middle / right buttons**: button 2 / 3 respectively.
- **Wheel**: vertical scroll = X buttons 4/5, horizontal = 6/7; deltaY>0 scrolls down.
- **Mousemove** is throttled ~12ms client-side; dropped entirely while an agent drives.
- Input is DROPPED whenever `controlOwner === "agent"` (client guard) AND server-side
  the `_input_enabled` gate is clear (belt + suspenders; the gate is authoritative).

## B. Keyboard (human, while they control)
- Keys are forwarded via XTest resolved from the **physical `code`** (KeyA→'a', Digit1
  →'1', Enter→Return, …), so Shift+key produces the shifted char naturally (Shift is
  its own event). Unmapped keysyms get a scratch keycode so any character types.
- **Clipboard shortcuts are NOT forwarded as keys**: Ctrl/Cmd+C/V/X (and legacy
  Ctrl/Shift+Insert, Shift+Delete) fire the browser's copy/paste/cut EVENTS in the
  viewer, which drive the clipboard bridge (below). Forwarding them too would
  double-paste. Everything else passes through.
- **Focus-capture — focused tier**: clicking into the pane focuses the canvas → a
  border glow + a "keys go to the browser" chip; window-level key capture forwards all
  cancelable keys to the remote (except the clipboard combos above). A bare Esc
  releases focus (and is still forwarded so the page sees it).
- **Reserved combos** (Ctrl/Cmd+T/W/N, Ctrl/Cmd+Tab, Cmd+Q) CANNOT be intercepted from
  a normal page → in focused tier they do their host-browser thing and are NOT
  forwarded. To use them on the remote, the user enters **immersive tier**.
- **Immersive tier**: the Fullscreen button calls `requestFullscreen()` +
  `navigator.keyboard.lock([...])` (Chromium only). Then reserved combos ARE captured
  and forwarded; exit on hold-Esc / fullscreenchange unlocks. Degrades gracefully where
  keyboard-lock is unsupported (still fullscreens; reserved combos just don't capture).
- **Native tab management is the primary path** (no custom tab bar): the user clicks
  the native "+", native tabs, native Ctrl+W-close (immersive), the native URL bar — all
  streamed + XTest-driven. Ctrl+T/W/Shift+T are Chromium-native in immersive.

## C. Clipboard (human, while they control) — both directions, text + images
- **Paste IN**: a viewer paste event (Ctrl/Cmd+V, right-click→Paste, Edit menu) sends
  the raw bytes (text or image) over HTTP; the daemon writes the browser's X clipboard
  (`xclip -display :N`) and fires a native Ctrl+V (XTest) into the focused element. A
  "Pasting…" toast for large payloads; server-owned once uploaded (user may navigate
  away). Images paste where the page accepts them.
- **Copy OUT (user-initiated)**: a viewer copy/cut event fires native Ctrl+C/X (XTest),
  reads the X clipboard (`xclip`), returns text or base64 image; the viewer writes the
  user's local clipboard.
- **Copy OUT (remote-initiated)**: a copy that happens INSIDE the page (right-click →
  Copy, an app copy) is detected by a ~500ms poll and pushed `{type:"clipboard"}` to the
  viewer, which writes the local clipboard (caching for the next gesture if denied).
- Per-browser X clipboard (own display) → two open browsers never clobber each other.

## D. Resize
- The viewer reports its pane size (debounced 150ms); the daemon resizes the real
  Chromium WINDOW (`Browser.setWindowBounds`) and moves the pixelflux capture region,
  clamped to [640×480 .. min(1920×1080, framebuffer)]. Canvas backing store follows the
  `resolution` on the control message; decoders reset on a resolution change.
- **Frozen while an agent drives**: a resize arriving while `_input_enabled` is clear is
  dropped (aspect locked during agent control).
- On hand-back to an agent, if the human resized meanwhile, `_wake_agent` tells the
  agent the new resolution and that its cached element indices are void (recompute).

## E. Ownership / agentic-browser-fleet interaction (UNCHANGED contract)
- CLI verbs unchanged: `new`, `ls`, `state`, `open/navigate`, `click`, `input`,
  `select`, `scroll`, `keys`, `screenshot`, `tab`, `acquire`/`release`, `task`, `hold`.
  Direct control is keyless; `task`/`extract` need an Anthropic key.
- **Ownership machine (atomic, single-writer):** one party controls a browser at a time
  — a specific agent or the human. Agents NEVER preempt each other (FIFO wait-queue).
  The human's **take-control ALWAYS wins**, cancels the agent run, and PINS (sticky — no
  idle yield). **Return-to-agents** un-pins and hands to the next waiter.
- **Agent handoff** (e.g. CAPTCHA): agent → human PINNED, requester goes FRONT of the
  resume queue, resumes first on hand-back. Direct-control lease auto-releases after
  idle TTL (agents only; humans are sticky). Claim window revokes an un-claimed grant.
- **Human input (XTest) and agent input (CDP) are mutually exclusive per command**:
  different injection layers, same serialized `_input_enabled` gate under
  `_control_lock`. Human input only lands while the human controls; each agent command
  re-checks ownership (CAS) before it runs, so a `take_control` rejects every *new*
  agent command. BOUNDED EXCEPTION: one agent direct-control action already past its
  CAS check and mid-flight (e.g. a multi-second `navigate`/`type`) completes after the
  human takes control -- so that single action can overlap the human's first inputs.
  A running `task` IS cancelled by `take_control`. New agent work never overlaps.
- **Lifecycle**: `init` (Chromium not up) → `running` → `crashed` (terminal). Drive/
  ownership only once `running`; crash is detected (observer disconnect) and reported;
  crashed name never restored as healthy.
- **Persistence**: per-browser persistent Chromium profile (cookies/logins survive
  stop/restart); manifest records tab URLs + active tab, restored eager-sequentially on
  boot behind the init gate. `_MAX_SESSIONS` cap; serialized launches (OOM guard).
- **Pane model**: `service:browser?session=<name>` refs, the session gate (bare
  `service:browser` rejected), the optimistic pane-pull (`context` → `--layout`).

## F. Streaming lifecycle / robustness
- **Encode on demand**: pixelflux starts on the FIRST `/stream` subscriber, stops at
  zero (unwatched browser = ~0 CPU). New subscriber → keyframe so it starts clean.
- **Pane hidden** (dockview visibility via `minds:panel-visibility`) → viewer closes
  `/stream` → encode pauses; visible again → reopens.
- **Multi-viewer** one browser: all subscribers get the same encoded stripes (fan-out).
- **pixelflux import is lazy** (per capture start): a fresh workspace self-heals once
  deferred-install provides the native libs — no restart, no boot race. Missing lib →
  clear ERROR log + no video (never a crash).
- **Per-browser Xvfb + XTest connection** are torn down on close; display numbers freed.
- **Reconnect**: cast 1013 (not-yet-registered) retries; 1008 (gone) terminal; stream
  socket reconnects on a fixed ~1s timer; decoders reset on reconnect/crash.
- **OOM**: browsers are the most-expendable band; a shed Chromium is dropped from the
  manifest so a respawn restores nothing.

## G. Invariants that MUST NOT regress
Agent browser-use driving, the ownership/handoff/queue state machine, manifest
persistence/restore, the profile-dir naming hack, `_MAX_SESSIONS` + serialized launch,
OOM retagging, the session-gated pane model, headless mode (tests: agent CDP only, no
display/capture/XTest), and CI staying green (headless).
