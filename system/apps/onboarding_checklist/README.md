# onboarding_checklist (the app)

The Onboarding checklist: one small page listing the four things worth trying
in a new workspace, in order, with a check mark on the ones the workspace's
owner has done. A click on an entry shows where that thing is (its pointer),
whether or not it is done; the page is headed "Click to learn more", and each
entry that can be clicked ends in an arrow. It is an app like any other: it is in the launcher,
its window can be closed and opened again, and no desktop is given a shortcut
of it (`seeds_shortcuts = false` in `app.toml`). The desktop's tour opens it as
the tour ends, by sending the message `onboarding:show-checklist`, which the
manifest registers (`[[message_handlers]]`), so the shell never names the app.

The items, their order, their words and the rule for done come from the
`onboarding_activity` library (`system/libs/onboarding_activity`), which the
chat's new-chat suggestions read too. The app stores nothing.

| Entry | Its pointer | How the page asks for it |
|---|---|---|
| `new_chat` | the chat list's New chat button pulses, with a bubble at it | `shell:message` carrying `onboarding:show-new-chat-pointer`, which the chat app registers |
| `phone_access` | the chrome's share panel opens with a bubble at its phone control | `minds:open-share-settings {coachMark: "mobile-access"}` |
| `build_app` | the chat window comes forward and a card pops out of the agent's avatar | `shell:show-coach-card {text, target: "avatar"}` |
| `connect_data` | the chrome's Settings button, which holds a workspace's permissions, is ringed, with a card under that end of the title bar | `shell:show-coach-card {text, chromeAnchor: "permissions"}` |

`phone_access` and `connect_data` need a chrome that draws coach marks: where
the shell's handshake does not say one does, those two entries are listed but
cannot be clicked. The two card texts are the shared library's
(`system/libs/workspace_ui/src/onboarding_pointers.ts`), which the chat app
sends too.

The page is a build of `frontend/`, one of the npm workspace's frontends
(`system/package.json`), into `src/onboarding_checklist/static/`. It reads the
items when it loads, when the shell says its window is shown, when the window
gets focus, and every 10 seconds while it is on screen; it reads nothing while
hidden. The Flask server in `src/onboarding_checklist/server.py` serves, on
port 8070:

- `GET /`: the page (a placeholder until the frontend is built), with its
  bundle under `/assets/`.
- `GET /api/items`: `{"items": [{"id", "label", "is_done"}, ...]}`, the four
  items in order. An item whose state cannot be read (an unreadable marker, an
  unreadable registry) is answered `is_done: false` and logged at warning. The
  route is not limited to the workspace's owner.
- `GET /api/health`: `{"status", "is_frontend_built"}`.
- `GET /api/schema`: the OpenAPI document of these routes
  (the app SDK, `imbue.app_sdk`); the manifest's `api_schema_path`.
- `GET /_static/app_contract.js`: the shell's browser-side contract module,
  served from this origin as every app serves it.
- `GET /_static/themes/<path>`: the workspace's theme bundles, served from this
  origin as every themed app serves them.

It runs as the `onboarding-checklist` supervisord program
(`system/supervisord.conf.d/onboarding-checklist.conf`) from the workspace venv
(`.venv/bin/onboarding-checklist`). The program line registers `app.toml` and
the port through `forward_port.py` before the server starts, at boot, so the
launcher lists the app and the tour can open it on a workspace nobody has
opened it in. The shell stops the program a minute after its last window
closes and starts it again on the next request (`stop_when_no_windows`). The
entry point never registers, so a preview (`[preview]` in `app.toml`) boots it
on a free port beside the live one with `ONBOARDING_CHECKLIST_PORT` and
`ONBOARDING_CHECKLIST_HOST`.

```bash
# Backend, from the repo root
.venv/bin/onboarding-checklist

# Frontend
cd system/apps/onboarding_checklist/frontend && npm run dev

# Tests
cd system/apps/onboarding_checklist && uv run --no-sync pytest
cd system/apps/onboarding_checklist/frontend && npx vitest run
```
