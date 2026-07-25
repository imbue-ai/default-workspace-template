The live browser view is now a real low-latency video stream instead of a JPEG slideshow. Each browser runs headful under its own virtual display, captured and encoded as striped H.264 (with a JPEG fallback) over a dedicated WebSocket and decoded in your browser with WebCodecs -- smoother and more responsive, and it only encodes while someone is actually watching.

Full-fidelity interaction now works: native right-click context menus, native dropdowns and date pickers, and real click-and-drag all behave like a local browser, because your input is injected at the display level rather than into the page. The mouse pointer is rendered into the view.

Because every browser now has its own display, copy/paste no longer leaks between two open browsers, and a copy made inside the remote page (for example via right-click -> Copy) now reaches your local clipboard automatically.

Resizing the pane resizes the real browser window (and the capture) to match, still frozen while an agent is driving and reported back to the agent on hand-back. The old CDP screencast, its JPEG frames, and page-scoped input have been removed -- this stack replaces them entirely.
