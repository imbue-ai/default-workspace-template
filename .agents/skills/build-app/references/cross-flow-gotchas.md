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
`system/supervisord.conf`, name matches the tab's service name) and
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

If your app listens on more than one port (rare, but happens with
admin UIs or metrics endpoints), expose each as its own service
(`<name>-admin`, `<name>-metrics`). `forward_port.py` only registers
one URL per service name.

## Port already in use

If the port you chose is bound by something else, the start command
will fail loudly (the framework will print an error and exit). With
`autorestart=true`, supervisord will keep restarting it, producing a
crash loop visible via `supervisorctl status <name>` and
`/var/log/supervisor/<name>-stderr.log`. Pick a different port.

The scaffolder's port-picking pre-flight (which parses `system/supervisord.conf`
and `data/.state/apps.toml`) catches this before you write the
program entry. For the wrap-existing escape hatch, run `ss -tln`
manually before choosing a port.

## Bind host (wrap-existing path mostly)

The scaffolder generates
`run_simple("127.0.0.1", port, app, threaded=True, ...)` (werkzeug)
which is correct. For the wrap-existing escape hatch, many Node
frameworks default to `0.0.0.0` (Node's
`http.createServer().listen(port)` binds to `::`/`0.0.0.0` when no
host is passed). Pass an explicit loopback host
(`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) -- the
forwarder reaches your app from inside the same container, so binding
all interfaces is unnecessary noise.
