# Verifying an app

Run verification against `http://127.0.0.1:<port>/` (the port registered with
`forward_port.py`). The browser-facing origin (`http://<name>.<workspace-host>/`)
is served by the host-side forwarder and is **not reachable from inside the container**,
so in-container verification targets the local port directly.

## Quick verification with smoketest_app.py (Recommended)

Use `system/scripts/smoketest_app.py` for instant verification (<0.1s for HTTP/marker checks,
~1.3s for browser rendering & screenshots). It resolves the port automatically from
the app name:

```bash
# Fast HTTP readiness & content marker check (<0.1s):
python3 system/scripts/smoketest_app.py <name> --marker "<expected-heading-or-text>"

# Full headless browser render + visual screenshot (~1.3s):
python3 system/scripts/smoketest_app.py <name> --marker "<expected-heading-or-text>" --screenshot /tmp/app_mock.png
```

### Auto-Reload Detection
Starter apps generated with `scaffold_flask_lib.py` enable Werkzeug's reloader (`use_reloader=True`).
When you edit `runner.py` or templates, changes take effect within ~50ms **without requiring a `supervisorctl restart`**.
To guarantee you never see stale code, `smoketest_app.py`:
1. Compares the server's startup timestamp from `http://127.0.0.1:<port>/health` against `runner.py`'s file modification time (`mtime`).
2. Actively polls until your `--marker` appears in the response body.

## Alternative / Manual Checks

### Step 0: confirm the registration

```bash
grep -A1 '<name>' data/.state/apps.toml
```

The service name must appear with the URL you expect. If it is missing, `forward_port.py`
was not run or failed.

### Step 1: active retry curl

Rather than hardcoded `sleep` calls, use active connection retries to answer instantly:

```bash
curl --retry 20 --retry-connrefused --retry-delay 0.1 -sf http://127.0.0.1:<port>/ -o /dev/null -w "%{http_code}\n"
```

Expected: `200`.

Common failures:
- **Connection refused** -- the app crashed or never came up. Check
  `supervisorctl status <name>` and `/var/log/supervisor/<name>-stderr.log`.
- **200 here but the tab shows the loading page** -- the registered URL doesn't match
  the port the app actually bound, or the name in `apps.toml` doesn't match the tab's
  service name. See cross-flow-gotchas.md.

### Step 2: Headless Playwright script (if not using smoketest_app.py)

If writing a custom Playwright script, use optimized flags and `domcontentloaded`
to avoid slow cold-start and `networkidle` timeouts:

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

Run with `uv run python /tmp/verify_<name>.py`.

