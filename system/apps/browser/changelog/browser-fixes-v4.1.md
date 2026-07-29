Click repaints are no longer sent at JPEG quality 100, and the frame clock is
no longer capped at 24fps.

KasmVNC's "dynamic quality" is a change tracker, not a bandwidth adapter: a
rect that was static and changes ONCE -- exactly a click repaint -- scores zero
and is encoded at the maximum quality. The effective maximum was 9, which maps
to JPEG quality 100 (~10:1), because the CLIENT's default "medium" preset
overwrites the server's settings at connect. The same preset clamped the
server's frame clock to 24fps, adding up to ~42ms of pure scheduling delay to
every interaction.

Sessions now run with `-IgnoreClientSettingsKasm`, which makes the server's
flags authoritative, and set dynamic quality max 7 (JPEG 86 -- a ~2.5x byte cut
that is near-imperceptible on text), frame rate 60, plus the video-mode values
the client preset had been supplying so scrolling behaviour is unchanged. The
viewer URL mirrors the same settings, with `video_quality=10` ("custom") --
load-bearing, since any other preset force-overwrites the fine-grained values
at client startup.

Measured context: a full-viewport repaint was an estimated 150-300KB, which on
a 36ms-RTT link needs 4-5 TCP slow-start flights. Halving the payload removes
round trips from every interaction.

One trade-off worth knowing: raising the frame cap is a latency win but raises
the byte rate for continuously animated content, where 24fps was implicitly
rate-limiting. The quality cut more than offsets it for interactive browsing;
on a bandwidth-starved link this is the first flag to reconsider.

----

The latency readout now uses Citrix's ICA decomposition, and has a
deterministic trigger.

`ICA RTT = ICA Latency + Host Delay + Endpoint Delay` is the industry-standard
shape for this measurement. We report the user-visible total (ICA RTT) and the
network-only component (ICA Latency) directly, and the remainder as one
combined "processing" figure -- splitting server from client would need
cooperation from the VNC server that ours does not provide, and an honest
single number beats a fabricated split. ICA RTT is graded against Citrix's
published bands: great under 180ms, good under 240ms.

The bigger change is *comparability*. Sampling real clicks measures what the
user felt, but the spread is dominated by what they happened to click -- a link
repaints a viewport, a checkbox a few hundred pixels -- so runs cannot be
compared, which is the standard critique of click-to-photon as a benchmark. The
viewer now also asks the daemon, every few seconds, to repaint a fixed-size band
at the top of the page, and times that round trip on its own clock (no clock
sync involved). Same bytes every time, so the series is comparable across runs
and across settings changes. Both series are reported; the probe is what gets
graded, and clicks remain as ground truth.

----

Live-view encoding tightened again, against a measured payload.

A full repaint was measured on a real WAN session: ~1.3 MB for an ordinary
article page, ~175 KB for a nearly blank one. That is far larger than the first
round of tuning assumed, and at those sizes payload -- not the network -- is
what the latency is made of.

Dynamic quality max moves 7 to 6 (JPEG ~79, about 30:1, against quality 9's
JPEG 100 at 10:1), with the video-mode quality cut harder still since motion
hides artifacts a static page would show. Quality 5 is deliberately not used:
it enables 4:2:2 chroma subsampling, which fringes coloured text.

Also measured and NOT acted on: idle egress from the workspace is ~18 KB/s even
on a blank page, but the live view accounts for only ~3 KB/s of it (the stream
crosses loopback twice before leaving, and loopback carries far less than the
external interface). The rest is unrelated workspace traffic, so there is
nothing to fix in the browser stack.

----

The synthetic latency probe is removed; ICA RTT comes from real clicks.

The idea was to fix comparability -- click sampling varies with whatever the
user clicked -- by triggering a fixed-size repaint on a timer. Two attempts
both reported latencies nobody experiences. Timing from the request billed the
CDP injection path (a route no user input takes) and read ~3x the real figure;
timing from the server's acknowledgement instead read 510ms against a
measured 269ms total, i.e. a component larger than the whole, because the pixel
watcher was looking at the wrong region and timing out.

Anything the daemon can trigger reaches the screen by a different, slower path
than a real click, so a synthetic probe measures a pipeline that does not exist
for users. Removed rather than left in reporting a number that looks
authoritative and is not. Click sampling stays: its spread is content-dependent,
but every sample is true.

----

Copy and paste work between the remote browser and your machine, and the pane
scales instead of resizing the framebuffer.

**Clipboard.** KasmVNC has shipped a bidirectional binary clipboard -- text and
`image/png`, both directions -- since 0.9.3, with Xvnc owning the X CLIPBOARD
selection itself. It was off for one reason: the client disables its clipboard
whenever it is framed (its check is literally `window.self !== window.top`, and
the live view is two iframes deep). Enabling it is three query parameters.

Two things were needed beyond that. The pane's iframe now carries
`allow="clipboard-read; clipboard-write"`. And because these displays have no
window manager, X stayed in `PointerRoot` focus mode: no window ever received a
`FocusIn`, so Chromium reported `document.hasFocus() == false`, and Blink checks
document focus *before* permissions -- meaning every `navigator.clipboard` call
was rejected outright and a site's own "Copy" button did nothing. A small focus
keeper now holds X input focus on the browser window and puts it back whenever a
popup or dropdown takes it, and the browser is granted clipboard permission over
CDP. Both halves are required; neither substitutes for the other.

**Resize.** The pane now fills by scaling rather than by resizing the server's
framebuffer. Growing the framebuffer would have made a maximised pane cost twice
the pixels at every stage that scales with them -- allocation, change tracking,
full-screen damage, the reference copy, and encoding -- and shipped a full
repaint on every size change. Scaling keeps the framebuffer fixed and is
aspect-preserving (the client takes the smaller of the two axis ratios and
applies it to both), so the view fills the pane without changing what the server
encodes. It also keeps agent screenshots a deterministic size.

----

Three clipboard and resize fixes.

**The pane was cropped and off-centre.** `resize=scale` alone is not enough:
the client sets `clipViewport = resize !== 'off'`, so `scale` turns clipping ON
as well as scaling, and clipping wins on the constrained axis -- showing a 1:1
window into the framebuffer instead of the whole thing scaled down. Passing
`view_clip=0` alongside it turns clipping off, so the client scales the entire
framebuffer to fit the pane, aspect-preserving.

**Pasting into the browser pasted the previous clipboard entry.** The KasmVNC
client only reads the local clipboard when the canvas takes focus, and nothing
polls -- so "copy elsewhere, click the pane, paste" raced: the click started an
async clipboard read while the keystroke was already on its way, and the remote
side pasted whatever had been synced last time. Waiting and pasting again worked
because the read had landed by then. The viewer now pushes the local clipboard
at every moment that reliably precedes a paste (window focus, tab becoming
visible, and pointer-down in the pane), deduplicated on content so repeats cost
nothing. This also covers the one path that produces no local event at all: a
paste from the remote browser's own right-click menu.

**WebSocket messages were unbounded.** No `max_message_size` was set on any of
the three hops, so any peer could send an arbitrarily large frame and each hop
would buffer it whole. Now capped at 16 MiB -- generous for the largest thing
that legitimately crosses this link (a 4K screenshot PNG is around 5 MiB) while
bounding what one frame can cost.

Known ceiling, not yet addressed: copying an image **out** of the remote browser
is capped at 1 MiB by KasmVNC itself. Xvnc declines incremental transfers as a
requestor, and Chromium switches to incremental above exactly 1 MiB, so larger
images are dropped with only an INFO log. Raising that needs our own transfer
path for the oversize case.

----

The pane goes back to `resize=remote`.

Switching it to `resize=scale` was a mistake, and the "cropped and off-centre"
pane was its consequence, not an unrelated bug. `scale` pins the server
framebuffer at the launch geometry and scales client-side, which sounds cheaper
and is not: the encoder works on damaged rectangles *of the framebuffer*, so a
1280x800 framebuffer costs a full 1280x800 of encoding and transmission even
when the pane is 700x450 and the client discards most of those pixels in CSS.
`remote` sizes the framebuffer to the pane, so only visible pixels are ever
encoded -- less work whenever the pane is smaller than the framebuffer, which is
the normal case.

`remote` also gives the behaviour that was wanted in the first place: the
browser viewport *is* the pane, filled and sharp at any size, with no letterbox
and nothing cropped. Its cost is per resize rather than ongoing -- a mode change,
a relayout and a full repaint -- and the client debounces that by 500ms, so
dragging a pane divider produces one resize at the end rather than one per frame.

----

Resize-to-pane, bounded.

The pane now sizes the remote framebuffer to itself, so the browser viewport
*is* the pane -- filled and sharp at any size, never letterboxed or cropped --
and `video_quality=1` bounds what that can cost. Despite the name it is not a
quality setting here: the client never transmits it, so the only thing it
reaches is the resolution maths, where it caps the framebuffer the client asks
for at 1280 wide (aspect preserved) and lets the free client-side CSS scale
cover anything larger. Encoding quality is unchanged.

The cap matters because encode cost is linear in pixel count. Measured, same
page, framebuffer varied: 640x400 costs 187 KB and 5.5 ms of server CPU per full
repaint; 1280x800 costs 750 KB and 17.9 ms; 1920x1200 costs 1649 KB and 36.9 ms
-- about 700 KB and 17 ms per megapixel throughout. Uncapped, a maximised pane
would cost more than twice a 1280x800 one. Capped, the framebuffer tracks the
pane up to 1280 wide, which is cheaper than a fixed 1280x800 for every smaller
pane, and is bounded above it.

Also fixed: the launch geometry was never actually applied. vncserver derives
its `-geometry` argument from `desktop.resolution.{width,height}` whenever those
config keys exist and consults the command-line flag only when they do not --
and the shipped defaults file always defines them, as 1024x768. So every display
has been coming up at 1024x768 while the code asked for 1280x800, silently. The
session now writes the user config that actually controls this.
