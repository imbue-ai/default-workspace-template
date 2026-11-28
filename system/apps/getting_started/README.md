# getting-started

The Getting Started app: the ways into the workspace, on one page a window of the desktop
frames at the app's own origin (`docs/system/blueprint/launcher-and-getting-started/`).
Top to bottom: a search field over its own content; "Start something", eight intents two to
a row (four at first, the rest behind "See more"), each with a seeded first message; "Start
from a template", the published template catalog by shelf, with a detail page (the drawing,
the write-up, what the template needs, its repository) whose two actions adopt the template
into this workspace or have a new workspace made from it. The catalog also lists published
apps, on the shelves that name them and in an "All apps" row: an app's card shows its icon,
and its detail page offers "Add to this workspace", which starts a chat on
`/install-app <repository_url>`, and "Create a new workspace with this", which asks the agent
to create a workspace with the app installed (through the `imbue-studio` skill, as the
template action does). A shelf entry that names no template and no app the catalog carries is
dropped, so the page never shows a card it cannot act on. Every tile and every action starts a
chat through one contract message, `shell:start-with-text`, which the desktop runs as its
launcher's primary text action; the page names no app, and the Getting Started window stays
where it is while the chat comes up beside it. The page wears the workspace's theme: it is built
from the shared components and design tokens, and declares one part of its own, `start-glyph`
(a Start tile's two-tone glyph), which a theme may recolor (`[theming]` in `app.toml`).

It runs as the `getting-started` supervisord program (`system/supervisord.conf.d/getting-started.conf`)
from the workspace venv (`.venv/bin/getting-started`), serving on
`http://127.0.0.1:8030`:

- `GET /`: the page (the frontend's build, `src/getting_started/static/index.html`; a
  placeholder until it is built), with its bundle under `/assets/`.
- `GET /api/health`: `{"status", "is_frontend_built"}`, the app's liveness probe (the update
  apply's post-restart probes cover critical apps only, which this one is not).
- `GET /api/schema`: the OpenAPI document of these routes, built from the app's url map
  (the app SDK, `imbue.app_sdk`); the manifest's `api_schema_path`.
- `GET /api/templates-catalog`: the template catalog (`catalog/README.md` at the repo root),
  fetched from `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL` (the shell's old variable name, kept
  so nothing outside the workspace changes), reused for six hours, and kept as a last good
  copy under `data/.state/getting-started/template_catalog.json`; `{"catalog": null}` when
  no URL is configured, a 503 when nothing could ever be loaded. The catalog carries
  `templates`, `apps` (empty for a catalog that lists none), and `shelves`; each template's
  drawing and each app's icon is resolved to a URL (`thumbnail_url`, `icon_url`).
- `GET /_static/app_contract.js`: the shell's browser-side contract module, served from this
  origin as every app serves it.
- `GET /_static/themes/<path>`: the workspace's theme bundles and the files they load, served
  from this origin as every themed app serves them (`workspace_themes.flask_routes`).

On startup it registers `app.toml` and its port through `forward_port.py`. It opens no
window of its own: a new workspace opens on the welcome chat, and the desktop's tour
introduces the apps (a workspace made before this kept the window it was given; the ledger
that recorded it, `data/.state/getting-started/first_window.json`, is left alone).
A boot with `--no-register` skips the registration, which would reach
the live workspace: that is how its `[preview]` table boots it (`getting-started --no-register
--state-dir {scratch}/state` on a free port, what `update-app`'s `preview_app.py` runs), with
the catalog cache in the scratch state directory.

The frontend (`frontend/`) is a member of the npm workspace at `system/package.json`, built
into `src/getting_started/static/` by `npm run build`; its tests run with `vitest run`.

```bash
# Backend, from the repo root
.venv/bin/getting-started

# Frontend
cd system/apps/getting_started/frontend && npm run dev

# Tests
cd system/apps/getting_started && uv run --no-sync pytest
```
