Workspace service panels now open at an unguessable per-service origin. Each
registered service is assigned a persistent random hostname label
(`<name>-<rand>`, e.g. `terminal-x7k9q2w1`), and the system interface builds
every panel's iframe origin from that label instead of the bare service name --
locally `http://<label>.host-<hex>.localhost:8421/` and `https://<label>.<domain>`
when shared. Saved layouts stay portable: they still persist the service name and
re-derive the URL from that name's current label at render time.

Fix a runaway iframe-nesting bug: deriveServiceOrigin now prefixes a service's label onto the workspace host COORDINATE (the ``host-<hex>`` label and everything after it) rather than onto ``window.location.host`` verbatim. Because the shell runs at its own label origin (the bare origin redirects there locally; only ``*.<domain>`` is served on a share), deriving relative to the current host verbatim nested every service under the shell's label -- routing it back to the shell (a dockview inside a dockview). Stripping to the coordinate first keeps every service origin a single label deep.
