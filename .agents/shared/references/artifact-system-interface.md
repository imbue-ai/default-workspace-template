# Artifact: system interface

`apps/system_interface` -- the live web workspace UI (dockview shell, chat
panels, progress view) and its Flask backend. This reference describes what the
system interface *is* and how you run and test it.

It is what the user is looking at *right now*, so you always work against an
**isolated instance** -- nothing you do reaches the live UI directly.

## Where the source lives

- Backend: `apps/system_interface/imbue/system_interface/` (Flask + flask-sock,
  served by the threaded Werkzeug server).
- Frontend: `apps/system_interface/frontend/src/` (TypeScript + Vite + Tailwind
  + mithril/dockview). Build output goes to the gitignored
  `apps/system_interface/imbue/system_interface/static/`.

## How to run and test it (in-process, never the live service)

- A fresh worktree has no `.venv`, so run `uv sync --all-packages` once before
  any `uv run`.
- If your change needs a new dependency, add it the normal way (`uv add` for
  Python, `npm install <pkg>` for the frontend) and **commit the manifest
  changes** (`pyproject.toml` / `uv.lock` / `package.json` /
  `package-lock.json`).
- Backend: exercise the edited Python **in-process** -- never install the global
  `system-interface` tool and never touch the running service. `cd
  apps/system_interface && uv run pytest` imports `create_application` and
  exercises it via Flask's test client (and a threaded Werkzeug server in-process
  for WebSocket/SSE tests), so your edits are picked up with no reinstall and no
  restart.
- Frontend: `cd apps/system_interface/frontend && npm run build` (you must
  produce a clean build) plus `npm run lint` and `npm run test`.
- To drive the UI manually during development, launch a **throwaway** instance on
  an alternate port against fixture data, e.g. `SYSTEM_INTERFACE_PORT=8137 uv run
  system-interface` from `apps/system_interface/` -- a disposable instance, never
  the live one.

## Testing specifics

- **For any change that touches the frontend, you MUST look at the rendered page
  -- not just assert on the DOM.** A clean build and passing Playwright
  assertions prove the markup and wiring exist; they do NOT prove the page
  *looks* right -- layout, spacing, alignment, overflow/truncation,
  color/contrast, z-order, and whether your change broke something visually
  elsewhere. Capture screenshots of every page and state your change affects
  (driving the same isolated Playwright instance; `page.screenshot(...)`, and
  `page.set_viewport_size(...)` if layout is width-sensitive), then **actually
  open and view those images and judge them with your own eyes.** Fix and
  re-screenshot until correct. These development screenshots are a manual check,
  not a committed test.
- **Verify the change really works** by driving the UI with Playwright against
  an isolated instance. The harness in
  `apps/system_interface/imbue/system_interface/test_e2e.py` already spins up an
  isolated threaded Werkzeug server on an alternate port, builds fake
  agent/session fixtures via `_make_agent_fixture`, and drives it with Playwright
  (auto-skips when browsers aren't installed). Extend it -- and use it as the
  same instance you screenshot.
- For each kind of test, use **exactly one** of crystallized-vs-ad-hoc -- do not
  duplicate the same coverage in a committed test and a throwaway manual check.
- Run the suites that apply: backend `pytest` (`cd apps/system_interface && uv
  run pytest`), and for frontend changes `npm run lint` + `npm run test`.

## Working in isolation

- Do not run `mngr start --restart system-services`, restart the live service,
  or `npm run build` against the served tree -- you only ever drive a throwaway
  instance on an alternate port.
