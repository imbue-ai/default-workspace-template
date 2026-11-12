# App gotchas

Read this when verification surfaces something odd; skim for the symptom that
matches. Most apps just work: the app owns its own browser origin (`app-code.md`,
"The app owns its origin"), on shares too, and the scaffolder's defaults
sidestep the remaining traps.

## "The window is stuck on the loading page"

Symptom: the user opens the app's window and sees the forwarder's
auto-retrying loading page instead of the app.

Root cause: nothing is answering on the port registered for that app name.
One of:

- The backend never came up. Check `supervisorctl status <name>` and
  `/var/log/supervisor/<name>-stderr.log`.
- The backend bound a different host or port than the one registered: a Unix
  socket, say, or a port that does not match the `--url` passed to
  `forward_port.py`.
- The name `forward_port.py` registered (the `name` in the `app.toml` passed as
  `--manifest`, or the `--name` flag of a manifest-less line) does not match
  the app the window points at.

Fix: check that the app binds `127.0.0.1` on the port in
`system/supervisord.conf.d/<name>.conf` and that the registered name is the
window's app, then run the checks in `verify.md`.

## A registration fails loudly at startup

Check the name first: `forward_port.py` refuses one that cannot be a hostname
label. The naming rule is step 2's in SKILL.md.

## Don't hardcode `localhost:<port>` in browser-facing code

Inside the container the app is reached at `http://127.0.0.1:<port>/`, but the
user's *browser* reaches it at the app's own origin. Relative URLs and
root-absolute paths (`/api`, `/socket`) are always correct. An absolute
`http://localhost:<port>/...` baked into HTML, JS, or a `Location` header
points the browser at its own machine and breaks. The same goes for redirects:
emit relative or root-absolute `Location` values (`/login` stays on your
origin), never the backend's localhost address.

## WebSockets

Connect to a relative path and derive the scheme from `location.protocol`, so
that pages served over HTTPS (shared workspaces) use `wss:`. A hardcoded `ws:`
is blocked by browsers as mixed content on HTTPS:

```js
const scheme = location.protocol === "https:" ? "wss:" : "ws:";
new WebSocket(scheme + "//" + location.host + "/socket");
```

## Multiple ports per app

If the app listens on more than one port (rare, but it happens with admin UIs
and metrics endpoints), register each as its own app (`<name>-admin`,
`<name>-metrics`). `forward_port.py` registers one URL per name.

## Port already in use

If the port is bound by something else, the start command fails loudly: the
framework prints an error and exits. Supervisord retries a few times and then
leaves the program in `FATAL`, so the failure shows in
`supervisorctl status <name>` and `/var/log/supervisor/<name>-stderr.log`. Pick
a different port. The scaffolder's own pick avoids this; a port chosen by hand
follows `wrap-existing-server.md`, "Pick a port and bind loopback".
