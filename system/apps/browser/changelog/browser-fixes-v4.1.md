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
