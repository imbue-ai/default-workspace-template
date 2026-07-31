Workspace service panels now open at an unguessable per-service origin. Each
registered service is assigned a persistent random hostname label
(`<name>-<rand>`, e.g. `terminal-x7k9q2w1`), and the system interface builds
every panel's iframe origin from that label instead of the bare service name --
locally `http://<label>.host-<hex>.localhost:8421/` and `https://<label>.<domain>`
when shared. Saved layouts stay portable: they still persist the service name and
re-derive the URL from that name's current label at render time.
