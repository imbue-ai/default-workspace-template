# Verifying an App

Verification confirms that your app is registered, responds on its port, and renders correctly.

> In-container checks target `http://127.0.0.1:<port>/`. The browser origin (`http://<name>.<workspace-host>/`) is routed by the host-side forwarder and is not directly reachable from inside the container.

---

## 1. Confirm Registration

Verify that the service name and backend URL appear in the workspace app registry:

```bash
grep -A1 '<name>' data/.state/apps.toml
```

If missing: Confirm `forward_port.py` executed successfully and the service name is valid and DNS-safe.

---

## 2. Check HTTP Endpoint

Query the registered backend port:

```bash
curl -sf http://127.0.0.1:<port>/ -o /dev/null -w "%{http_code}\n"
```

Expected output: `200`.

- **Connection refused**: The app crashed or failed to start. Run `supervisorctl status <name>` and check `/var/log/supervisor/<name>-stderr.log`.
- **200 OK but tab displays loading page**: The registered port in `apps.toml` differs from the port the app bound, or the registered name does not match the tab name. See [cross-flow-gotchas.md](cross-flow-gotchas.md).

---

## 3. Verify UI Rendering (Playwright)

Use a headless Playwright assertion to verify that the frontend renders without JavaScript errors:

```python
# /tmp/verify_<name>.py
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://127.0.0.1:<port>/", wait_until="networkidle")
    content = page.content()
    assert "<expected-marker>" in content, f"Marker missing. Page head: {content[:500]}"
    browser.close()
```

Run the check:

```bash
uv run python /tmp/verify_<name>.py
```

Assert on a specific text string or element rendered only by your app (avoid asserting on generic tags like `<html>` or `<body>`).
