# The app's manifest (`app.toml`)

Every app has a manifest at `system/apps/<package>/app.toml`. Read this when
you write or edit one: to decide `stop_when_no_windows`, to register something
you built for the app outside its directory, or to rename the app.

`forward_port.py --manifest` reads the file on every start, so a restart is
what applies an edit. After editing it, check it:

```bash
uv run --no-sync app-manifest validate-manifest system/apps/<package>/app.toml
```

## What the scaffold writes

- `name`: the app's registered name.
- `display_name`: what users see for the app.
- `description`: the one-liner. It is what an agent reads to know what the app
  is for, and the app's API document carries it.
- `api_schema_path`: `/api/schema`, where the app describes its own API
  (`api-schema.md`).
- `icon`: the icon file beside the manifest.
- `priority = "user"`: the app is shed before any built-in under memory
  pressure.
- `program`: its supervisord program.
- `stop_when_no_windows = true`: see the next section.
- A `[preview]` table: how `update-app`'s `preview_app.py` boots a throwaway
  instance of the app, on a free port over a scratch copy of its data.

## `stop_when_no_windows`

With `true`, the shell stops the app's program a minute after the app's last
window closes and starts it again on the next request. With `false`, the app
keeps running for the life of the workspace. A manifest without the field reads
as `false`.

- **Leave it `true` for an app that only answers requests.** The stop frees the
  whole process while nobody looks at it, its data on disk is untouched, and
  the next request wakes it.
- **Set it to `false` when the app does work between requests** that a stop
  would lose or interrupt: a background thread that keeps something live, a
  poller or file watcher, a websocket or subscription to an outside service, a
  job that must finish after the user closes the window, or an API another
  agent drives with no window open. A stopped app runs nothing until its next
  request, and the wake serves that request, not the work that was in flight.
- **Work on a cadence is never a reason to keep the app up**, and never a
  thread or scheduler inside it. A refresh every N minutes or a nightly sync is
  a routine with `--app <name>` (the `manage-routines` skill), which the
  Routines app shows and the user can run, pause and retime.
- The reverse holds: an app that no longer does anything between requests goes
  back to `true`.

## What you build for the app outside its directory

Anything you build for the app outside `system/apps/<package>/` -- a skill that
drives it, a script, a doc -- is registered in the manifest under
`[[references]]`, with a `note` naming the surface it uses. That is how it
travels with the app through hardening, testing and publishing.

A credential the app needs is declared under `[[secrets]]`; `scaffold-output.md`,
"An app that needs a credential", has the block and the program command that
goes with it.

## Renaming an app

An app's `display_name` names more than its windows: every window's "Chat about
this app" button opens the chat titled `<display_name> Chat` (the multiplayer
spec, section 9). The button finds that chat again by the `app_chat` label it
carries with the app's registered name, so a renamed app keeps its chat. Its
title is what no longer matches, so rename the chat too.
`system/scripts/rename_chat.py` does it through the chat app's rename route
(`POST /api/chats/<id>/rename`, beside the send route `message_chat.py` posts
to):

```bash
python3 system/scripts/rename_chat.py --title "<old display name> Chat" --new-title "<new display name> Chat"
```

It exits 0 when the chat was renamed or there is no chat under the old title
(nothing to do), and non-zero when the chat app is unreachable or refused.
