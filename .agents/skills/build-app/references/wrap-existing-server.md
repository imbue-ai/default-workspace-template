# Wrapping an existing server

The escape hatch from the scaffolder: follow this in place of SKILL.md's steps
3 to 5 when the app is a pre-existing third-party server with its own start
command (Jupyter, Grafana, an `npx`-installed dashboard). Do not scaffold
anything, and do not put a Python proxy in front of the tool: it adds a hop,
costs an extra process, and complicates WebSocket and streaming behaviour. The
name and the icon are step 2's; steps 6 and 7 apply unchanged.

## Contents

- Pick a port and bind loopback
- Write the manifest
- Write the program
- Confirm it is what the user wanted
- Give the tool the shell contract, if it can take it

## Pick a port and bind loopback

- **Port.** Pick one by the scaffolder's own check, done by hand:
  `scaffold-output.md` states it under `--port`, and `ss -tln` shows what is
  bound. Two things show up nowhere: the `agent-observer` program binds no
  port at all, and a preview of any app (`update-app`'s `preview_app.py`)
  takes free ports at boot, so there is nothing to avoid for either.
- **Bind host.** Bind `127.0.0.1`, not `0.0.0.0`. The forwarder reaches the
  app from inside the same container, so binding all interfaces is noise. The
  scaffolder's starter does this already. A wrapped tool may not: many Node
  frameworks default to `0.0.0.0` (`http.createServer().listen(port)` binds
  `::`/`0.0.0.0` when no host is passed), so pass an explicit loopback host
  (`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`). Python defaults are
  usually loopback already.

## Write the manifest

Save the icon as `system/apps/<name>/icon.svg` and write the app's manifest
beside it as `system/apps/<name>/app.toml` (as the `files` app does):

```toml
name = "<name>"
display_name = "<What users see>"
icon = "icon.svg"
priority = "user"
program = "<name>"
```

## Write the program

Add a `[program:<name>]` block as its own
`system/supervisord.conf.d/<name>.conf`. It runs `forward_port.py --manifest`
and then the tool's own start command:

```ini
[program:<name>]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --manifest system/apps/<name>/app.toml --url http://localhost:<port> && exec <existing_start_command>"
directory=/home/user/workspace
autostart=true
autorestart=true
startsecs=30
startretries=5
```

For Jupyter, `<existing_start_command>` is
`jupyter notebook --port <port> --ip 127.0.0.1 --no-browser`.

- **The `forward_port.py` call MUST come first in the command.** The port must
  be registered before the app starts listening; otherwise the shell's
  announcement of the registration races with the backend coming up.
- Keep the `oom_tag_service.py user` prefix and the `bash -c "..."` wrapper.
  `scaffold-output.md`, "The supervisord program", says what each is for.

When the start takes several steps or env exports, put them in a wrapper script
and run that instead. Only the `command` line of the block changes:

```bash
# system/scripts/run_<name>.sh
#!/usr/bin/env bash
set -euo pipefail
python3 system/scripts/forward_port.py --manifest system/apps/<name>/app.toml --url http://localhost:<port>
exec <existing_start_command>
```

```ini
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash system/scripts/run_<name>.sh
```

After writing the file, start the program:

```bash
supervisorctl reread && supervisorctl update
```

The full program schema and the logging knobs are in
`.agents/shared/references/service-processes.md`.

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
