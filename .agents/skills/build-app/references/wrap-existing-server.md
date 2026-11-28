# Wrapping an existing server

The escape hatch from the scaffolder: follow this in place of SKILL.md's steps
3 to 5 when the app is a pre-existing third-party server with its own start
command (Jupyter, Grafana, an `npx`-installed dashboard). Do not scaffold
anything, and do not put a Python proxy in front of the tool: it adds a hop,
costs an extra process, and complicates WebSocket and streaming behaviour. The
name and the icon are step 2's; steps 6 and 7 apply unchanged.

## Steps

1. Look at what the tool exposes
2. Bind loopback
3. Write the manifest
4. Register the app
5. Confirm it is what the user wanted
6. Give the tool the shell contract, if it can take it

## Look at what the tool exposes

**A wrapped server has no request guard.** Nothing checks who is calling it,
so it trusts anything that can reach its port. That includes a page of another
site open in the workspace's own browser, which can send it requests, and,
once the workspace is shared, anyone the app is shared with. A scaffolded app
can install the SDK's request guard
(`imbue.app_sdk.request_guard.install_request_guard`), which refuses such
callers ahead of every route; a wrapped tool cannot.

So before you wrap a tool, look at what it exposes. When it exposes anything
sensitive or destructive (the user's files, credentials, a shell or code
execution, an admin action, anything that deletes), tell the user so in plain
terms before you stand it up, and prefer a scaffolded app, whose routes can
sit behind the guard, over wrapping the tool as it is. If the tool has
authentication of its own (a token, a password), leave it on.

## Bind loopback

Bind `127.0.0.1`, not `0.0.0.0`. The forwarder reaches the app from inside the
same container, so binding all interfaces is noise. The scaffolder's starter
does this already. A wrapped tool may not: many Node frameworks default to
`0.0.0.0` (`http.createServer().listen(port)` binds `::`/`0.0.0.0` when no
host is passed), so pass an explicit loopback host (`HOST=127.0.0.1`,
`app.listen(port, "127.0.0.1")`). Python defaults are usually loopback
already.

Do not pick a port: the workspace allocates one when the app is registered,
and the manifest says where the tool takes it.

## Write the manifest

The app is still a directory, `system/apps/<package>/` (`<package>` is the
app's name with each hyphen replaced by an underscore), holding the icon as
`icon.svg` and the app's manifest as `app.toml`. The manifest's `[run]` table
gives the tool's existing start command:

```toml
name = "<name>"
display_name = "<What users see>"
icon = "icon.svg"
priority = "user"
program = "<name>"

[run]
command = "<the program the existing start command runs>"
args = ["<each argument>", "--port", "{port:main}"]
```

- `command` is one word, the program alone, run as given (on `PATH`, or a path
  relative to the repo root, where the program runs). Each argument is its own
  entry in `args`.
- Put `{port:main}` wherever the server takes its port: the workspace
  allocates the port and fills it in. `{host}` is the loopback address
  (`127.0.0.1`), for a server that takes its bind host as an argument.
- A server that takes its port or host from the environment gets an `env`
  table instead: `env = { PORT = "{port:main}", HOST = "{host}" }`.

For Jupyter, the `[run]` table is:

```toml
[run]
command = "jupyter"
args = ["notebook", "--port", "{port:main}", "--ip", "{host}", "--no-browser"]
```

A start that takes several steps (exports, a bootstrap) goes in a script
inside the app's directory, which `[run]` then runs: `command = "bash"`,
`args = ["system/apps/<package>/run.sh", "{port:main}"]`, with the script
ending in `exec <existing_start_command>`.

## Register the app

```bash
uv run --no-sync app-install add <package>
supervisorctl reread && supervisorctl update
```

`app-install add` allocates the app's port, writes its record
(`system/config/apps/<name>.toml`), and generates its program file
(`system/supervisord.conf.d/<name>.conf`); it prints the port in its result.
The generated program registers the manifest and the port through
`forward_port.py` before it starts the tool, and carries the
`oom_tag_service.py user` prefix. `scaffold-output.md`, "The record and the
supervisord program", says what each part is for.

Do not write a program block by hand, and never edit the generated file. To
change the command, change `[run]` and run
`uv run --no-sync app-install regenerate <name>`, then
`supervisorctl reread && supervisorctl update`.

The full program schema and the logging knobs are in
`.agents/shared/references/service-processes.md`.

Commit the app once it is registered; with GitHub sync on, then follow
`link-new-app.md`.

## Confirm it is what the user wanted

There is no markup you author on this path, so there is no mock to build: the
demonstration is the wrapped tool itself. Stand it up, show it to the user, and
confirm it is what they wanted before investing in configuration or integration
around it.

Step 6's verification, and the gotchas reference it names, apply to this path
unchanged.

## Give the tool the shell contract, if it can take it

A wrapped tool's pages do not carry the shell page script. So a plain click on
one of their links to a local service or another app navigates the tool's own
window, and the workspace records only that somebody clicked into the app's
window, never that they kept using it (the shell's `window_input` log, desktop
contracts.md section 6).

If the tool can serve one more static file from its own origin and add a script
to its pages (Jupyter's custom JS, a dev server's plugin hook), give it the
contract:

- Serve the shell's built contract module at `/_static/app_contract.js`. The
  file is
  `system/apps/system_interface/imbue/system_interface/static/_static/app_contract.js`,
  which the shell's frontend build generates from
  `system/libs/workspace_ui/src/app_contract.ts`.
- Add
  `<script type="module">import { connectToShell } from "/_static/app_contract.js"; connectToShell({});</script>`
  to its pages. That also makes them report their input like a scaffolded
  app's.

If it can do neither, leave it as it is.
