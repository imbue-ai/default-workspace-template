Integrated the agentic browser fleet into the workspace UI.

- The "+" menu now lists the currently-active browsers (from the browser
  daemon's `GET /browsers`) alongside "New browser". Clicking an already-open
  browser focuses its existing pane instead of opening a duplicate; "New browser"
  starts one and opens its pane, surfacing a clear error if the fleet is full.

- The agent-driven layout system can address a specific browser as a pane
  (`service:browser?session=<id>`), so an agent can pull the exact browser it is
  working on into a split-pane next to its chat, and panes for different browsers
  are treated as distinct (focus-if-open, no collisions).

- Updated the frontend to the browser daemon's new fleet endpoints (`/browsers`
  and `/browsers/{id}/cast`) in place of the previous single-session routes.

- "New browser" is no longer gated on an Anthropic API key. Direct control is
  keyless, so a browser can always be started; the old menu item that disabled
  itself and showed a "Browser sessions need an Anthropic API key" dialog was a
  leftover from the delegation model and has been removed.

- Dropped the placeholder "web" example server from the "+" menu (the browser
  fleet is the real web surface), and removed the per-tab Refresh button from
  browser panes -- reloading the pane only reconnects the live view, which read
  as "restart the browser"; the browser viewer has its own in-page Reload button
  for the actual page.
