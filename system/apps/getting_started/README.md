# getting-started

The Getting Started app: the ways into the workspace, on one page a window of the desktop
frames at the app's own origin (`docs/system/blueprint/launcher-and-getting-started/`).
Top to bottom: a search field over its own content; "Start something", eight intents two to a row (four at first, the rest behind "See more"), each
with a seeded first message; "Start from a template", the published template catalog by
shelf, with a detail page (the drawing, the write-up, what the template needs, its
repository) whose two actions adopt the template into this machine or have a new machine
made from it. Every tile and both actions start a chat through one contract message,
`shell:start-with-text`, which the desktop runs as its launcher's primary text action; the
page names no app, and the Getting Started window stays where it is while the chat comes up
beside it.

It runs as the `getting-started` supervisord program (`system/supervisord.conf.d/getting-started.conf`)
from its own uv tool environment (`system/scripts/build_workspace.sh`), serving on
`http://127.0.0.1:8030`:

- `GET /`: the page (the frontend's build, `src/getting_started/static/index.html`; a
  placeholder until it is built), with its bundle under `/assets/`.
- `GET /api/health`: `{"status", "is_frontend_built"}`, the app's liveness probe (the update
  apply's post-restart probes cover critical apps only, which this one is not).
- `GET /api/templates-catalog`: the template catalog (`catalog/README.md` at the repo root),
  fetched from `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL` (the shell's old variable name, kept
  so nothing outside the workspace changes), reused for six hours, and kept as a last good
  copy under `data/.state/getting-started/template_catalog.json`; `{"catalog": null}` when
  no URL is configured, a 503 when nothing could ever be loaded.
- `GET /_static/app_contract.js`: the shell's browser-side contract module, served from this
  origin as every app serves it.

On startup it registers `app.toml` and its port through `forward_port.py`, and starts the
first-visit opener (`first_window.py`): while `data/.state/getting-started/first_window.json`
does not say the window was delivered, it polls the shell's client list and, for the first
connected client, posts an `open` of `/` on the first desktop and a `place` at the left
complement of the pinned chat's frame through the loopback op route, then records the
delivery. The shell seeds nothing for it; this is the same shape the chat app's auto-open
takes for the welcome chat.

The frontend (`frontend/`) is a member of the npm workspace at `system/package.json`, built
into `src/getting_started/static/` by `npm run build`; its tests run with `vitest run`.

```bash
# Backend, from the repo root
uv run getting-started

# Frontend
cd system/apps/getting_started/frontend && npm run dev

# Tests
cd system/apps/getting_started && uv run pytest
```
