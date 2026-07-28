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
