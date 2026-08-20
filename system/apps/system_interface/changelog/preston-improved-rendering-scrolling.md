Rewrote the chat transcript's scroll virtualization so reading history no longer makes the viewport jump.

The transcript reserved scroll space for unloaded history at a fixed 160px per *event*, but the renderer groups a whole turn into one row -- a tool-heavy turn of 50 events renders as a single progress block. Each backfilled page therefore landed thousands of pixels shorter than its reservation in one frame, which unmounted the row the browser was anchored to and teleported the viewport.

Space is now reserved from real measured geometry. Each row's height is measured once after it settles, remembered per conversation and viewport width, and reused: revisiting a conversation is accurate immediately, and ranges this client has never rendered are estimated from what it has actually measured rather than from a fixed constant.

Scrolling the bar deep into a long conversation now lands where the thumb says it should, and spurious jumps -- fired when estimate drift pushed the position calculation past its threshold -- no longer happen.

Those measurements are now kept by the workspace rather than by one browser, in a small file beside the stored tab names and recency. A conversation reopened in another window -- or after a reload, or on another device at the same width -- reserves its space from what was already measured instead of measuring from scratch. The server only stores and returns what a client measured: it never derives a height itself, drops rows that are not measurements rather than failing the write, and keeps only the most recently read transcripts, so the file stays small and a dropped one is simply measured again.
