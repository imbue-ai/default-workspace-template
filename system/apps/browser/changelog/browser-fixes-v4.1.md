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
