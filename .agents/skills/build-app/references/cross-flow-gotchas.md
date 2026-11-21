# App gotchas

Your app owns its own browser origin: the forwarder routes
`http://<name>.<workspace-host>/` (and, on shares, the same prefix
rule on the share hostname) straight to the backend URL you
registered. Nothing proxies, rewrites, or prefixes your app's
traffic, so root-absolute URLs (`href="/api"`), `new WebSocket("/ws")`,
`Set-Cookie: Path=/`, service workers, and redirects
(`Location: /login`) all work exactly as written.
Most apps "just work" -- the Flask scaffolder picks defaults that
sidestep the remaining traps. This file is loaded on demand when
verification surfaces something odd; skim for the symptom that matches.

## "The tab is stuck on the loading page"

Symptom: the user clicks the service tab and sees the forwarder's
auto-retrying loading page instead of the app.

Root cause: nothing is answering on the port registered for that
service name. Either:

- The backend never came up (check `supervisorctl status <name>` and
  `/var/log/supervisor/<name>-stderr.log`).
- The backend bound to a different host or port than what was
  registered (e.g. bound to a Unix socket, or a port that doesn't match
  the `--url` passed to `forward_port.py`).
- The name `forward_port.py` registered (the `name` in the `app.toml`
  passed as `--manifest`, or the `--name` flag of a manifest-less line)
  does not match the service name the tab points at.

Fix: re-check pre-flight (bind to 127.0.0.1, port matches
`system/supervisord.conf.d/<name>.conf`, name matches the tab's service name) and
Step 3 verification.

## Service names must be DNS-safe

The name becomes the service's hostname label, so it must be lowercase
letters/digits with single hyphens (the scaffolder requires kebab-case;
`forward_port.py` additionally tolerates underscores for legacy names
like `system_interface`) and must not be `localhost` or start with
`host-` or `agent-` (reserved for workspace hostname coordinates).
Both reject invalid names; if a registration fails loudly at startup,
check the name first.

## Don't hardcode `localhost:<port>` in browser-facing code

Inside the container your app is reached at `http://127.0.0.1:<port>/`,
but the user's *browser* reaches it at the service's own origin.
Relative URLs and root-absolute paths (`/api`, `/socket`) are always
correct; an absolute `http://localhost:<port>/...` baked into HTML, JS,
or a `Location` header points the browser at its own machine and
breaks. Same for redirects: emit relative or root-absolute `Location`
values (`/login` is fine -- it stays on your origin), never the
backend's localhost address.

## WebSockets

Connect to a relative path and derive the scheme from
`location.protocol` so that HTTPS-served pages (shared workspaces) use
`wss:` -- hardcoding `ws:` is blocked by browsers as mixed content on
HTTPS:

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

If the app's port is bound by something else, the start command
will fail loudly (the framework will print an error and exit).
Supervisord retries it a few times and then leaves the program in
FATAL (`startsecs`/`startretries` in the program file bound the loop),
so the failure shows up via `supervisorctl status <name>` and
`/var/log/supervisor/<name>-stderr.log`.

`app-install` allocates the port on both paths and skips one that a
program, a registry row or another app's record holds, or that something
is listening on, so this happens only when something took the port
afterwards. Find what holds it (`ss -tlnp`) and stop that; do not edit
the port into the program file, which is generated.

## Bind host (wrap-existing path mostly)

The scaffolder generates
`run_simple(HOST, PORT, app, threaded=True, ...)` (werkzeug), where
`HOST` is `127.0.0.1` unless the app's `<PACKAGE_UPPER>_HOST` env var
says otherwise, which is correct. For the wrap-existing escape hatch, many Node
frameworks default to `0.0.0.0` (Node's
`http.createServer().listen(port)` binds to `::`/`0.0.0.0` when no
host is passed). Pass an explicit loopback host
(`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) -- the
forwarder reaches your app from inside the same container, so binding
all interfaces is unnecessary noise.
