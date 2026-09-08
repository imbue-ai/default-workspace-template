# Verifying an app

Run both checks. `curl` confirms the backend answers on its registered
port; Playwright catches rendering bugs that `curl` misses (a blank
page, JS errors, a marker that never appears).

Both checks run against `http://127.0.0.1:<port>/` -- the URL you
registered with `forward_port.py`. The browser-facing origin
(`http://<name>.<workspace-host>/`) is served by the host-side
forwarder and is **not reachable from inside the container**, so
in-container verification targets the local port directly. That is an
honest proxy for the tab: nothing rewrites or transforms traffic
between the origin and your port, so a page that renders correctly at
`http://127.0.0.1:<port>/` renders identically in the tab. What it
cannot prove is the registration itself, so check that too (step 0).

## Step 0: confirm the registration

```bash
grep -A1 '<name>' data/.state/apps.toml
```

The service name must appear with the URL you expect. If it is
missing, `forward_port.py` was not run, failed (e.g. an invalid,
non-DNS-safe name), or registered a different name (the manifest's
`name` for a `--manifest` line, the `--name` flag otherwise) -- the tab
would show the forwarder's loading page forever.

## Step 1: curl the registered backend

```bash
curl -sf http://127.0.0.1:<port>/ -o /dev/null -w "%{http_code}\n"
```

`<port>` is the port in the service's `forward_port.py --url` (see
`system/supervisord.conf` or `data/.state/apps.toml`). Expected: `200`.

Common failures:

- **Connection refused** -- the app crashed or never came up. Check
  `supervisorctl status <name>` and
  `/var/log/supervisor/<name>-stderr.log`.
- **200 here but the tab shows the loading page** -- the registered
  URL doesn't match the port the app actually bound, or the name in
  `apps.toml` doesn't match the tab's service name. See
  cross-flow-gotchas.md.

## Step 2: Playwright assertion

`curl` alone does not catch rendering bugs. Use Playwright
(preinstalled in the root venv per `CLAUDE.md`):

```python
# /tmp/verify_<name>.py
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://127.0.0.1:<port>/", wait_until="networkidle")
    title = page.title()
    body = page.content()
    print("title:", title)
    print("body len:", len(body))
    assert "<your-expected-marker>" in body, body[:500]
    browser.close()
```

Run with `uv run python /tmp/verify_<name>.py`.

Pick a marker that **only** appears when your app rendered correctly
-- a heading, a data-driven element. Do not assert on `<html>` or
`<body>`; those appear in error pages too.
