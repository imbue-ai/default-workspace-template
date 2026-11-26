# App gotchas

Read this when verification surfaces something odd; skim for the symptom that
matches. Most apps just work: the app owns its own browser origin (`code.md`,
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
label. The `build-app` skill states the naming rule.

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

An app has one port, the one the workspace allocates for it (`{port:main}`
in its `app.toml`), and one registered URL, which is where its window
opens. Do not register an extra port as a second app named `<name>-admin`
or `<name>-metrics`: an app's name may not be another app's name followed by
a hyphen and more, because those names belong to the app's own background
programs, so the registration is refused.

- **An extra surface** (an admin page, a metrics endpoint) is a path on the
  app's one port (`/admin`, `/metrics`), served by the same process.
- **A background program** the app needs beside its main one (a worker, a
  poller, a queue consumer) is a `[[services]]` entry in its `app.toml`
  (`role`, `command`, `args`, `env`). It runs as the supervisord program
  `<name>-<role>`, generated like the app's own, and gets no window and no
  port of its own; `{port:main}` in its `args` or `env` is the app's port,
  for a program that calls the app.
- **A wrapped tool that insists on a second listening port** keeps that port
  on loopback and unregistered. Nothing allocates it, so give the tool a
  port well away from the range the workspace hands out (8080 upwards), and
  reach it only from inside the workspace.

## Port already in use

If the app's port is bound by something else, the start command fails loudly:
the framework prints an error and exits. Supervisord retries a few times and
then leaves the program in `FATAL`, so the failure shows in
`supervisorctl status <name>` and `/var/log/supervisor/<name>-stderr.log`.

`app-install` allocates the port of every app the workspace builds or installs,
and skips one that a program, a registry row or another app's record holds, or
that something is listening on. So this happens only when something took the
port afterwards. Find what holds it (`ss -tlnp`) and stop that. Do not edit
the port into the program file, which is generated.
