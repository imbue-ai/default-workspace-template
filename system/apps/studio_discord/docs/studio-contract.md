# What this app needs from Imbue Studio

The app shares no code and no history with the workspace it runs in. Everything it needs from Studio is listed
here, in two halves: what the **workspace does for the app** (once, when it starts the app), and what the **app
asks of Studio** at runtime (HTTP only).

## The workspace's side: start and register

The workspace runs the app as a supervisord program from the workspace root. Before starting it, the workspace
registers the app's window with its own script:

```
python3 system/scripts/forward_port.py --manifest <this repo>/app.toml --url http://localhost:<port>
```

Studio has no HTTP route for registering a window; `forward_port.py` (which writes the shell's app registry) is the
only way, so it stays on the workspace's side of the line. The app itself never calls it. The manifest
(`app.toml`) and icon (`icon.svg`) are this repo's; their formats are the workspace's `app.toml` schema and its
`docs/system/app-icons.md` rules.

Who writes that program:

- a workspace built from the default workspace template carries this app vendored at `system/apps/studio_discord/`
  with its program in `system/supervisord.conf.d/studio-discord.conf`;
- any other workspace runs `install.py` from a checkout of this repo, which writes the same program pointing at the
  checkout.

## The app's side: HTTP calls

| What | Call | Notes |
|---|---|---|
| Shell page modules | `GET {shell}/_static/app_contract.js`, `GET {shell}/_static/context_menu.js` | Fetched server-side once per process and re-served from the app's own origin (browsers cannot import them cross-origin). |
| Is Discord access granted? | `GET $LATCHKEY_GATEWAY/permissions/self` | Any `discord-api` rule holding `discord-read-messages` (or `discord-read-all` / `any`). |
| Ask the user for Discord access | `POST $LATCHKEY_GATEWAY/permission-requests`, body `{agent_id: $MNGR_AGENT_ID, type: "predefined", payload: {scope: "discord-api", permissions: [discord-read-messages, discord-read-channels, discord-read-users]}, rationale}` | Shows in the user's Studio notifications. Its outcome cannot be looked up (`GET /permission-requests/<id>` answers 403), so the window waits for the grant to appear and offers Ask again / Cancel. |
| Read Discord | `GET $LATCHKEY_GATEWAY/gateway/https://discord.com/api/v10/{path}` | `path` is only `users/@me`, `channels/<studio id>`, `channels/<studio id>/messages`, or `channels/<studio forum id>/threads/search`; anything else is refused before a request is made (`discord._allowed`). |
| Who is online | `GET https://discord.com/api/guilds/<guild>/widget.json` | Public, no sign-in, not through the gateway. |
| Uninstall: take the icon off every desktop | `GET {shell}/api/desktops`, then `POST {shell}/api/desktops/<id>/shortcuts/remove` with `{"app": "studio-discord", "launch": "open"}` per desktop | What was removed is kept, so Undo puts each icon back at the same spot. |
| Undo uninstall | `POST {shell}/api/desktops/<id>/shortcuts` with `{target, mode, cell}` | Only on desktops that still exist. |

Every request carries the agent string `studio-discord/0.1`: Discord's edge refuses the standard library's default (Cloudflare error 1010).

Known gap: a desktop created after uninstalling is still seeded with the icon, because the manifest's `[default_shortcut]` is read by the shell, not by this app.

## Settings the app reads

| Variable | Default | Set by |
|---|---|---|
| `STUDIO_DISCORD_PORT` | `8086` | the program line |
| `STUDIO_DISCORD_HOST` | `127.0.0.1` | previews |
| `STUDIO_DISCORD_DATA_DIR` | `data/.apps/studio-discord` (relative to the workspace root) | previews |
| `MINDS_WORKSPACE_SERVER_URL` | `http://127.0.0.1:8000` | the workspace |
| `LATCHKEY_GATEWAY`, `LATCHKEY_GATEWAY_PASSWORD` | -- | the workspace (inherited by every program) |
