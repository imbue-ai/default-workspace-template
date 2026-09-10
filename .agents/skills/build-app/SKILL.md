---
name: build-app
description: "Use when you want to create a new app for the user -- a page, dashboard, or tool they can open as a tab. Runs an interactive flow: confirm the look and feel on a cheap throwaway mock first, then build the real app to a usable state, then harden it in the background. Covers scaffolding a new Flask app (canonical path) and the escape hatch for wrapping a pre-existing third-party server."
metadata:
  author: imbue
  crystallized: true
---

# How to build an app

An "app" is a web service rendered as a dockview tab at its own browser origin (`http://<name>.<workspace-host>/`). The workspace forwarder routes traffic directly to your registered port without rewriting or proxying. Root-absolute URLs (`/api`), WebSockets (`/ws`), cookies, and redirects work directly without path prefixes.

Choose between two paths:
- **Canonical path**: Scaffold a new Flask app using `scaffold_flask_lib.py`. Use this when authoring custom routes and markup.
- **Escape hatch**: Wrap a pre-existing third-party server (Jupyter, Grafana, custom Node tools, etc.). Skip to [Escape hatch: wrap an existing server](#escape-hatch-wrap-an-existing-server).

To modify or remove existing apps, use the `update-app` skill instead. Never modify `system/apps/system_interface/` (the top-level workspace UI).

---

## Initial Action: Fire the plan recorder

Before doing anything else, execute this once to log context for offline analysis. It returns immediately and requires no action:

```bash
system/scripts/imbue_plan_extra/write_plan.sh build-app <<'IMBUE_PLAN_BRIEF'
<user request and settled context>
IMBUE_PLAN_BRIEF
```

Run it exactly as written with the quoted heredoc delimiter. Do not pipe, redirect, create a ticket step, or wait on it. Ignore any failure and continue immediately.

---

## Interactive Delivery Flow Overview

Building an app follows a strict phased lifecycle to avoid wasted development effort:

1. **Step 0: Clarify and plan**: Identify genuine blockers, state single-user defaults, propose a short plan, and obtain approval.
2. **Step 1: Scaffold**: Create the app skeleton, manifest, icon, and supervisord program.
3. **Step 2: Throwaway mock (Hard Gate)**: Display a static mock UI and iterate until the user confirms the look-and-feel.
4. **Step 3: Build real routes**: Implement functional backend logic, data persistence, and raw data surfacing.
5. **Step 4: Verify and refresh**: Smoke test the endpoint, verify with Playwright, and refresh the user tab.
6. **Step 5: Finalize in background (Hard Gate)**: Obtain user confirmation on the working app, then hand off comprehensive testing and ratchets to a background worker.

---

## Step 0: Clarify and plan

1. **Ask only genuine blockers**: Ask only questions where the answer is uncertain and expensive to reverse later. Phrase choices in terms of user-visible consequences rather than technical terms.
2. **Apply sensible defaults**: Default to single-user operation, minimal persistent storage, and conventional web patterns. State defaults in a single line.
3. **Propose a concise plan**: Outline the app name, main UI components, and data source. Wait for user approval before scaffolding.

---

## Pre-flight Checklist (Both Paths)

- **App name**: Must be kebab-case (lowercase letters, digits, single hyphens), DNS-safe, and must not start with `host-` or `agent-`. Avoid reserved names like `system_interface` or `browser`.
- **App icon**: Draw a monochrome SVG glyph in the workspace house style:
  ```xml
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="..."/>
  </svg>
  ```
  Use a transparent background and `stroke="currentColor"`. No fill or hardcoded colors unless requested.
- **Port**: Bind to loopback `127.0.0.1` (never `0.0.0.0`). The scaffolder automatically selects the lowest free port >= 8080 (skipping 8000, 8010, 8081).

---

## Step 1: Scaffold the app (Canonical path)

Run the scaffolding script:

```bash
uv run .agents/skills/build-app/scripts/scaffold_flask_lib.py \
    --name <service-name> \
    --description "<one-line description>" \
    --icon-file <path-to-svg> \
    [--display-name "<title shown in UI>"] \
    [--port <port>] \
    [--extra-dep <package>]
```

Flags:
- `--name`: Required. Kebab-case service name.
- `--description`: Required. One-line library description.
- `--icon-file`: Required. Path to the `.svg` icon.
- `--display-name`: Optional. User-visible title (defaults to description, max 64 chars).
- `--port`: Optional. Explicit port number (auto-selected if omitted).
- `--extra-dep`: Optional, repeatable. Add dependencies (e.g. `--extra-dep "jinja2>=3.1"`).
- `--skip-uv-sync`: Optional. Skips final sync and tool install (for dry runs).

### Generated structure

- `system/apps/<package>/app.toml`: App manifest (`name`, `display_name`, `icon`, `instances = false`, `priority = "user"`).
- `system/apps/<package>/pyproject.toml`: Package configuration with script entrypoint `<name> = "<package>.runner:main"`.
- `system/apps/<package>/src/<package>/runner.py`: Starter sync Flask application using `werkzeug.serving.run_simple(..., threaded=True)`.
  - Defines `DATA_DIR` (defaults to `data/.apps/<name>/`, overridable by `<PACKAGE_UPPER>_DATA_DIR`).
  - Defines `PORT` (overridable by `<PACKAGE_UPPER>_PORT`).
  - Includes the location beacon script:
    ```html
    <script>window.parent.postMessage({type: "shell:location", path: location.pathname + location.search}, "*");</script>
    ```
    Retain this snippet on all served HTML pages so dockview can restore the tab URL on reload.
- `system/apps/<package>/test_<package>_ratchets.py`: Default code ratchets.
- `system/apps/<package>/README.md`: Starter documentation.
- `system/supervisord.conf`: Appends a `[program:<name>]` block with `oom_tag_service.py user` priority and automatic port registration via `forward_port.py`.
- Tool environment: Installs the entrypoint via `uv tool install -e system/apps/<package>`.

### Start the service

Update supervisord and verify that the program is running:

```bash
supervisorctl reread && supervisorctl update
supervisorctl status <name>
```

If it fails to start, inspect `/var/log/supervisor/<name>-stderr.log`.

---

## Step 2: Throwaway mock (Look-and-feel gate)

Do NOT implement real backend routes, database models, or complex data fetching before completing this gate.

1. **Design UI**: If authoring HTML, invoke the `frontend-design` skill before writing markup.
2. **Build mock content**: Replace the placeholder route in `runner.py` with static markup demonstrating the layout, components, and states (empty, populated, busy).
   - If handed a `sample.json` by `fetch-process-show`, render that data directly in the mock.
   - If the user asks for functionality that requires backend support, mock its visual behavior and approximate feel in the frontend first.
3. **Open tab in workspace**:
   ```bash
   python3 system/scripts/layout.py open <name>
   ```
4. **Iterate with user**: Present the tab and gather feedback. Update the mock markup and reload the tab:
   ```bash
   python3 system/scripts/layout.py refresh <name>
   ```
5. **HARD GATE**: Obtain explicit user confirmation that the look-and-feel is correct before building real backend logic.

---

## Step 3: Build real routes to a usable site

Once the mock look-and-feel is confirmed, implement the functional site:

1. **Synchronous handlers**: Use standard `def` routes (not `async def`). Werkzeug serves requests concurrently in separate threads.
2. **Retain location beacon**: Keep the `window.parent.postMessage` snippet in all rendered HTML pages.
3. **Claude integration**: If the app calls Claude for summaries or tasks, follow the `use-ai-integration` skill.
4. **Preserve and surface raw data**:
   - Persist original source records and origin identifiers alongside derived metrics.
   - Provide an unobtrusive "view raw" control rendering data natively (JSON pretty-printed, markdown rendered, HTML email in a sandboxed `<iframe>`).
   - Include an "open in <source>" link back to external origins when applicable.
5. **File-path conventions**:
   - **Persistent state**: Always read and write persistent data using the `DATA_DIR` constant from `runner.py`. Never hardcode `data/.apps/<name>/` directly.
   - **Static assets**: Reference packaged assets using `Path(__file__).parent / "assets/..."`.

---

## Step 4: Verify and refresh

### 1. Verify service health and rendering

Run the verification sequence (see [references/verify.md](references/verify.md)):

```bash
# Check registry entry
grep -A1 '<name>' data/.state/apps.toml

# Check HTTP status code (expected: 200)
curl -sf http://127.0.0.1:<port>/ -o /dev/null -w "%{http_code}\n"
```

Verify UI rendering using a headless Playwright check:

```python
# /tmp/verify_<name>.py
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://127.0.0.1:<port>/", wait_until="networkidle")
    assert "<expected-page-marker>" in page.content()
    browser.close()
```

Run with `uv run python /tmp/verify_<name>.py`.
If you encounter errors (connection refused, infinite loading, WebSocket failures), consult [references/cross-flow-gotchas.md](references/cross-flow-gotchas.md).

### 2. Refresh workspace tab

Update the open tab with the live app:

```bash
python3 system/scripts/layout.py refresh <name>
```

To dock the tab in a specific project view, pass `--view <view-name>` to `layout.py open <name>`. See the `manage-layout` skill for advanced docking operations.

---

## Step 5: Finalize in background (Working site gate)

Once the working site is live and verified:

1. **Confirm with user**: Ask the user to test the functional app ("Does this look good to lock in?"). Continue fast foreground iterations if they request changes.
2. **HARD GATE**: Do NOT run thorough test suites, coverage checks, or code-review ratchets in the main agent turn.
3. **Hand off to worker**: Once the user explicitly confirms the working site, delegate background hardening to the `crystallize-creation` skill with `type=app`:
   - Pass the app slug (`<name>`).
   - Pass a task description specifying the package path, URL/port, and behavior.
   - The background worker will write comprehensive tests, enforce ratchets, and pass review gates without blocking the user.

---

## Escape Hatch: Wrap an existing server

Use this path when integrating existing third-party servers (Jupyter, Grafana, custom Node tools) instead of scaffolding a Flask app.

1. **Add icon and manifest**:
   - Save SVG icon to `system/apps/<name>/icon.svg`.
   - Create `system/apps/<name>/app.toml`:
     ```toml
     name = "<name>"
     display_name = "<Display Title>"
     icon = "icon.svg"
     instances = false
     priority = "user"
     program = "<name>"
     ```
2. **Add supervisord entry**:
   Add a `[program:<name>]` block in `system/supervisord.conf`. The `forward_port.py` command must run before the service starts:
   ```ini
   [program:<name>]
   command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --manifest system/apps/<name>/app.toml --url http://localhost:<port> && <existing_start_command>"
   directory=/home/user/workspace
   autostart=true
   autorestart=true
   ```
   *(For complex startups with environment variables, create a wrapper script `system/scripts/run_<name>.sh` instead).*
3. **Bind loopback**: Configure the server to bind to `127.0.0.1` (e.g. `HOST=127.0.0.1`).
4. **Start and test**:
   ```bash
   supervisorctl reread && supervisorctl update
   supervisorctl status <name>
   python3 system/scripts/layout.py open <name>
   ```
5. Confirm functionality with the user before configuring complex integrations.

---

## CLI Reference & Tooling

### `layout.py`
- Open tab: `python3 system/scripts/layout.py open <name> [--view <view-name>]`
- Reload tab: `python3 system/scripts/layout.py refresh <name>`
- Inspect open tabs: `python3 system/scripts/layout.py list`

### `forward_port.py`
- Register via manifest: `python3 system/scripts/forward_port.py --manifest system/apps/<name>/app.toml --url http://localhost:<port>`
- Unregister: `python3 system/scripts/forward_port.py --name <name> --remove`

### Reference Documents
- Verification sequence: [references/verify.md](references/verify.md)
- Troubleshooting and common gotchas: [references/cross-flow-gotchas.md](references/cross-flow-gotchas.md)
- Public share URLs: [references/public-url.md](references/public-url.md)
- App teardown and cleanup: [references/cleanup.md](references/cleanup.md)
