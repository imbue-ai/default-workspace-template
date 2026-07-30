# Bad-TCP Browser

A **deliberately-naive** pixel-streamed browser, kept as a foil to the optimized
browser fleet so the two can be run side by side. It streams a full Chromium as
damage-driven H.264 (pixelflux stripes) over one WebSocket with display-level
Selkies input — the same base as the fleet — but with **every latency defense
removed**. The point is to show, live, how poorly naive TCP pixel streaming holds
up on a congested or high-latency link.

Open it from the workspace "+" picker (`bad-tcp-browser`), or
`python3 system/scripts/layout.py open bad-tcp-browser`.

## What makes it "bad" (on purpose)

`videopipe.py` has **no flow control**:

- **No per-stripe credit-ack.** The viewer never acknowledges stripes; the server
  never bounds bytes in flight. Stripes are shoved at the socket as fast as they
  encode. On a congested path the kernel + tunnel send buffers fill with
  reliably-delivered-but-ever-later frames and interaction latency climbs without
  bound — exactly what the browser app's credit-ack prevents.
- **No capture-rate adaptation, no RTT-adaptive window, no delay-gated quality.**
  The encoder runs flat out at a fixed fps regardless of what the link can carry.

The only things kept are what make the picture *correct* (not fast): a per-row
newest-wins mailbox as a server-side OOM guard, and sticky-IDR / resync-on-broken-
chain so a dropped delta re-keys instead of painting garbage. Neither bounds
bytes in flight.

## The rest (shared with the fleet's lineage)

- **Session** (`session.py`): a private Xvfb display plus one Fortress Chromium
  maximized onto it, created on the first viewer connect and kept alive across
  reconnects (profile at `data/.apps/bad-tcp-browser/profile`).
- **Input** (`xinput.py`): Selkies-grammar messages (`kd`/`ku`/`kr`/`m`) injected
  via XTEST (python-xlib), keyboard core ported from Selkies (MPL-2.0).
- Audio (jsmpeg) and clipboard (xclip) carry over unchanged; the demo is about the
  video transport.

## Not to be "fixed"

This app exists only to contrast with the optimized browser. Do **not** add flow
control here — that would defeat its entire purpose.
