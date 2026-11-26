# The app's code

What a scaffolded app's starter sets up, and the rules the app's code keeps.

## Contents

- The starter
- The app owns its origin
- Where files go
- The shell page script
- Markup an element reference can name
- The content-security policy
- Raw data and its source
- Finding another app
- Data migrations
- Calling Claude

## The starter

`system/apps/<package>/src/<package>/runner.py` builds a `Flask` app and serves
it with `werkzeug.serving.run_simple(..., threaded=True)`. A freshly scaffolded
one has `GET /` (a placeholder page, which the app's own routes replace) and
`GET /health` (returns `{"status": "ok"}`).

- **Use sync handlers** (`def`, not `async def`). The threaded Werkzeug server
  handles concurrent requests on separate threads, so no asyncio is needed.
- **`HOST` and `PORT`** are read from the `<PACKAGE_UPPER>_HOST` env var
  (loopback when unset) and the `<PACKAGE_UPPER>_PORT` env var. Bind them in
  `run_simple`. The app's program sets `<PACKAGE_UPPER>_PORT` to the port the
  workspace allocated, so the runner holds no port of its own; with the
  variable unset (the app run on its own, outside a workspace) the operating
  system picks a free one. Never write a port into the app's code.
- **`DATA_DIR`** defaults to `data/.apps/<name>/` and honors the
  `<PACKAGE_UPPER>_DATA_DIR` env var. See "Where files go".
- Those env vars are what let a later edit boot a throwaway instance on a
  spare port against a copy of the data (the `update-app` skill).
- The starter's helpers come from the app SDK, `imbue.app_sdk` (its `routes`,
  `api_schema`, `shell_modules`, `request_guard`, `identity` and `apps`
  modules). An app from before the SDK imports the same names from
  `app_schema` and `workspace_layout.identity`, which re-export them; write new
  code against the SDK.
- A route the app adds owes the app's API document a docstring and a route
  table entry: `api-schema.md`, beside this file.

## The app owns its origin

The app serves at `/`, and nothing rewrites its traffic, so it needs no path
prefix and no `root_path` or `ROOT_PATH`. Root-absolute URLs (`href="/api"`),
WebSockets (`new WebSocket("/ws")`), cookies (`Set-Cookie: Path=/`), service
workers and redirects (`Location: /login`) all work exactly as written.

## Where files go

- **Persistent state** (caches, cursors, last-visit timestamps, JSON snapshots,
  user records: anything written and read across runs) is read and written
  under `DATA_DIR`, never a hardcoded `data/.apps/<name>/` at the call site.
  `DATA_DIR` is relative to `/home/user/workspace`, where the program runs. A
  hardcoded path silently bypasses the override and exposes the live data to
  an instance that was meant to run on a copy. Do NOT use `Path(__file__)`-based
  paths for state.
- **A store the app can rebuild** (downloads, extracted archives, clones,
  caches) gets its own directory under `DATA_DIR` with an empty `.nobackup`
  file in it, so the hourly backup skips it.
- **Static assets shipped beside the code** (templates, default configs,
  bundled JSON): `Path(__file__).parent / "assets/..."` is the right pattern.

## The shell page script

The scaffolded index page carries the shell page script (`SHELL_PAGE_SCRIPT` in
the runner), a module script that:

- connects the page to the workspace shell framing it;
- reports where the page is on the handshake, so the shell reopens this app's
  window at the place it was showing;
- installs the element context menu: the right-click menu whose last rows
  ("Copy reference", "Explain...", "Modify...") hand the clicked element to a
  chat as a `REF-<id>.json` attachment an agent can resolve
  (`.agents/shared/references/element-references.md`,
  `docs/system/blueprint/element-reference-menu/`);
- opens the page's links where the workspace opens them.

The rules that follow from it:

- **Keep the script on every page the app serves.** An app that drops it always
  reopens at its origin, gets the browser's own menu, and has its links
  navigate its own window.
- **Keep the `/_static/` routes.** The runner serves the two modules the
  script imports from the app's own origin, at `/_static/app_contract.js` and
  `/_static/context_menu.js`, through the SDK's
  `register_shell_module_routes(app)`, which fetches each from the running
  shell. Keep that call, and its `SHELL_MODULE_ROUTE_KEY` entry in
  `ROUTE_TABLE`. A module import is a fetch without cookies, which the
  forwarder refuses across origins.
- **Write plain `<a href>` links** and no link handling of your own. A
  `window.open` to any of the workspace's own addresses is routed the same way
  and returns `null`.

## Markup an element reference can name

A reference names an element by what its markup carries: its `id`, its `data-*`
attributes, its classes, a selector. So give a list row the `id` or a `data-*`
attribute of the record it shows, and an interactive control a stable `id` or a
first class that names it. Then a reference resolves to one thing.

## The content-security policy

Every page is served under the starter's `Content-Security-Policy`
(`CONTENT_SECURITY_POLICY` and the `after_request` that sets it). Keep it: the
app is reachable from the internet once the workspace is shared. It admits only
the app's own origin.

- **Anything a page pulls from elsewhere is blocked by the browser until its
  origin is added to the matching directive**: a CDN script (`script-src`),
  Google Fonts (`style-src` and `font-src`), an image hosted elsewhere
  (`img-src`), a third-party API called from the browser (`connect-src`).
- An inline `<script>` needs its `inline_script_source` in `script-src`. Never
  `'unsafe-inline'` or `*` for scripts.
- The browser reports every block to the starter's `/_csp-report` route, which
  logs it to stderr as an `ERROR` line containing
  `Content-Security-Policy blocked <url> on <page> (<directive>)` (a blocked
  inline script adds its line and first characters). So when something on a
  page does not show up, read the app's stderr log
  (`/var/log/supervisor/<name>-stderr.log`; the `check-app-errors` skill finds
  these lines too) instead of guessing.

## Raw data and its source

When the app renders data *derived* from underlying records (a summary, a
reformatted list, extracted fields), include by default, without the user
asking:

- a "view raw" control showing the original record **rendered in its native
  format** (an HTML email as the rendered email, not escaped source; JSON
  pretty-printed; markdown rendered: the faithful original minus your
  processing);
- for records from an external service, an "open in <source>" link back to the
  origin (open the email in Gmail).

When you render untrusted third-party HTML (a raw email body is the common
case), sandbox it with a sandboxed `iframe` or a sanitizer, so the page cannot
run scripts or phone home through tracking pixels.

This is the surfacing half of the preserve-and-surface principle in
`AGENTS.md`. The derived page inevitably leaves gaps (a field you did not
extract, a rendering you did not anticipate), and the raw and source controls
let the user bridge them without waiting for a rebuild. Design it in from the
first version. It depends on the data layer having persisted the raw payload
and the source reference, so confirm that is available and flag it if it is
not. Keep it unobtrusive (a
small per-record control, not clutter) and do not call it out in chat: always
present, never announced.

## Finding another app

Nothing outside an app may hold that app's port. The workspace allocates the
port, so it differs from one workspace to the next and can change in this one.
Never write another app's `http://localhost:<port>` into code, a script, a
skill or a doc. Find the app by its name:

- **Python code** uses the SDK:

  ```python
  from imbue.app_sdk.apps import resolve_app_url

  url = resolve_app_url("<name>")
  ```

  It returns the app's URL inside the workspace, read from the shell at the
  time of the call. It raises `AppNotRegisteredError` when the workspace has no
  such app and `ShellUnreachableError` when the shell does not answer (both in
  `imbue.app_sdk.errors`).
- **A script or a skill** calls the same function, from the repo root:

  ```bash
  uv run --no-sync python -c 'from imbue.app_sdk.apps import resolve_app_url; print(resolve_app_url("<name>"))'
  ```

- **For a person** (a chat reply, a doc), write the `link` that
  `uv run --no-sync workspace-layout list` prints for the app. It opens the
  app's window; a program cannot request it.

## Data migrations

When a change to the app changes the shape of what it stores under
`data/.apps/<name>/` (a renamed field, a file split in two, a new required
key), the app brings older data forward itself when it starts: it looks at
what is on disk and converts anything still in the old shape before it serves
a request. Do not convert the data by hand and leave the app unable to read
the old shape. The app may later be installed and updated in another
workspace, where its data outlives its code: an update replaces the app's
files and leaves `data/.apps/<name>/` as the older version wrote it, and
nobody there knows what that shape was. Keep the conversion, and a test of
it, in the app, and keep it for every shape a published version ever wrote,
since an adopter can skip versions.

## Calling Claude

If the app needs to call Claude (classify or summarize content, run a one-shot
agentic task, or launch a full agent), follow the `use-ai-integration` skill.
It picks the path (a keyed `litellm` call or the keyless `claude_p.py` helper),
covers the `claude -p` environment fix and the cost model, and saves you from
hand-rolling the call.
