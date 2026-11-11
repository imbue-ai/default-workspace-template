# Verifying an app

Verify against `http://127.0.0.1:<port>/`, the port registered with
`forward_port.py`. The browser-facing origin (`http://<name>.<workspace-host>/`)
is served by the host-side forwarder and is **not reachable from inside the
container**, so a check from inside targets the local port directly.

## Contents

- The one call: `verify_app.py`
- Manual checks

## The one call: `verify_app.py`

`system/scripts/workspace_scripts/verify_app.py` resolves the port from the
app's name, restarts the app, checks it, and shows it, so one call covers the
whole apply, check and show loop. The HTTP and marker checks take under 0.1 s;
a browser render and screenshot about 1.3 s.

```bash
# After a code change: restart (no 30 s wait), check, render, and open the window beside the chat:
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<expected-heading-or-text>" --screenshot /tmp/app_mock.png --open

# The same for an app already on the user's screen (reloads its windows instead):
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<expected-heading-or-text>" --refresh

# Fast HTTP readiness and content marker check only (<0.1s):
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --marker "<expected-heading-or-text>"

# Full headless browser render and screenshot (~1.3s):
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --marker "<expected-heading-or-text>" --screenshot /tmp/app_mock.png
```

- `--restart` asks supervisord to stop the program and start it again *without*
  waiting (`supervisorctl restart` would block for the program's
  `startsecs=30`), then polls the port, which answers within a second. While it
  polls it watches the program's state, so a crash on start fails the call at
  once with the last lines of `/var/log/supervisor/<name>-stderr.log`. Leave it
  out right after `scaffold_flask_lib.py --start`, which already started the
  app.
- `--open` opens the window only after the checks pass, beside the chat that
  asked (`workspace-layout open <name> --beside`; with no chat on that desktop
  `--beside` changes nothing). The open waits a few seconds for a freshly
  registered app to appear in `data/.state/apps.toml`, so it is safe right
  after a start. For an app already on the user's screen use `--refresh` and
  drop `--screenshot`.
- While the app is still framed as a mock, drop `--open` and `--refresh` and
  end the call with a `mock_frame.py` command (`mock-frame.md`).
- When a changed API route needs exercising, append one `curl` of it to the
  same tool call.

## Manual checks

Use these when `verify_app.py` does not tell you what is wrong.

**Confirm the registration.**

```bash
grep -A1 '<name>' data/.state/apps.toml
```

The app's name must appear with the URL you expect. If it is missing,
`forward_port.py` was not run or failed.

**Curl with retries.** Retry the connection instead of sleeping, so the check
answers at once:

```bash
curl --retry 20 --retry-connrefused --retry-delay 0.1 -sf http://127.0.0.1:<port>/ -o /dev/null -w "%{http_code}\n"
```

`<port>` is the port in the app's `forward_port.py --url` (see
`system/supervisord.conf.d/<name>.conf` or `data/.state/apps.toml`). Expected:
`200`.

- **Connection refused**: the app crashed or never came up. Check
  `supervisorctl status <name>` and `/var/log/supervisor/<name>-stderr.log`.
- **200 here but the window shows the loading page**: the registered URL does
  not match the port the app bound, or the name in `apps.toml` does not match
  the window's app. See `cross-flow-gotchas.md`.

**A Playwright script of your own.** Playwright is installed in the root venv and drives Fortress, the workspace's
Chromium build, when it is installed. Use these flags and `domcontentloaded`
to avoid a slow cold start and `networkidle` timeouts:

```python
# /tmp/verify_<name>.py
from pathlib import Path
from playwright.sync_api import sync_playwright

fortress = Path("/opt/fortress/tilion-fortress/tilion")
kwargs = {
    "args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--single-process"],
    "headless": True,
}
if fortress.exists():
    kwargs["executable_path"] = str(fortress)

with sync_playwright() as p:
    browser = p.chromium.launch(**kwargs)
    page = browser.new_page()
    page.goto("http://127.0.0.1:<port>/", wait_until="domcontentloaded", timeout=5000)
    body = page.content()
    assert "<your-expected-marker>" in body, body[:500]
    browser.close()
```

Run it with `uv run --no-sync python /tmp/verify_<name>.py`.
