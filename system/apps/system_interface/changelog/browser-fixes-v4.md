Opening a browser pane now requires naming the browser, so an agent can no
longer spawn an orphan viewer.

A browser pane must address a specific fleet browser
(`service:browser?session=<name>`). The bare `service:browser` opened a
session-less viewer bound to nothing -- the dead "Open a browser from the +
menu" placeholder -- which looked like a broken browser rather than a misuse of
the ref. The layout broadcast endpoint now rejects a session-less `open`/`split`
with a 400 that names the right form ("A browser pane needs a specific browser
name: use 'service:browser?session=<name>', or the agentic-browser-fleet
'new'/'task' commands, which open the pane for you"), before anything is
broadcast to clients.

The check fires ahead of the existing layout checks, so the caller gets the
guiding message instead of an opaque downstream failure. The fleet's own
pane-pull always carries `?session=<name>`, so it is unaffected, as is every
other ref kind.

----

The service proxy now sends an `Origin` header to backend WebSockets, and reads
them in 64 KiB chunks.

Some backends refuse an upgrade that carries no `Origin`: KasmVNC answers 404
with "request failed websocket checks, missing Sec-WebSocket-Origin header".
`simple_websocket` sends none by default; the proxy now presents the backend's
own address, which is what a same-origin client would send. Backends that ignore
`Origin` (ttyd, the browser fleet's cast socket) are unaffected.

The read buffer moves from `simple_websocket`'s 4 KiB default to 64 KiB, which
is far too small for binary streams like VNC framebuffer updates -- each one cost
many recv syscalls plus reassembly, and this proxy runs inside the workspace,
where gVisor makes every syscall several times more expensive.

Services created at runtime can also now be hidden from the layout listing by
name prefix (`is_hidden_service`), not just by exact name. Each live browser
registers its own `browser-<name>` service for its display; those are addressed
as `service:browser?session=<name>`, so listing them too would show every browser
twice.
