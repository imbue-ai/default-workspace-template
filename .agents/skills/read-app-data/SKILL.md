---
name: read-app-data
description: Use when a task needs the user's Mac app data -- their Notes, Safari or other browser history, Reminders, Calendar, Messages, recent code projects in VS Code or Cursor, shell history, or the list of apps they have installed. "The apps I have installed" or "my recent projects" mean the user's own Mac, not this workspace's built-in apps or its folders. Covers which sources exist, how to ask for exactly the ones the task needs, how to read them once granted, and the rule that this data is used only for the workspace's owner.
compatibility: Requires the latchkey gateway env mngr injects into every agent (LATCHKEY_GATEWAY, LATCHKEY_GATEWAY_PASSWORD) and the devices skill. The sources are macOS locations.
metadata:
  author: imbue
---

# Reading the user's app data

Imbue Studio can let you read named sources of the user's app data on one of
their computers. You ask for sources by name, the user ticks the ones they
agree to in one dialog, and you get read-only access to exactly those files.
Never ask for `~/Library` or `~` through the `file-sharing` skill to get at this
data instead: an `app-data` request is the only way to ask for it.

Never answer a question about the user's apps or projects from this workspace's
own apps or folders: those came with the workspace and say nothing about the
user. If you have no grant yet, ask for one; if the user declines, say you
cannot see their Mac rather than describing the workspace.

## The sources

| id | what it is |
| --- | --- |
| `installed-apps` | The apps the user has installed, as Finder and Launchpad show them: name, bundle id, version |
| `safari-history` | Safari's `History.db` |
| `browser-history` | Chrome, Arc, Brave, Edge, and Firefox history, for the profile each one uses |
| `notes` | Apple Notes' `NoteStore.sqlite` |
| `reminders` | The Reminders stores folder |
| `calendar` | The Calendar folders |
| `messages` | Messages' `chat.db`, never attachments |
| `code-projects` | VS Code and Cursor's recent-folder state |
| `shell-history` | `~/.zsh_history` and `~/.bash_history` |

## When to ask

- Ask only when the task in front of you needs the data, and name only the sources it needs. Never ask from a routine or background worker: the user has to be there to answer.
- Ask for each source at most once per task. If the user left one unticked, do not ask for it again in that task.
- **Shell history often holds passwords and keys**, and folder names in **recent code projects** can reveal clients or private work. Ask for either only when the task is about the user's shell or coding work, and say so in the reason.
- Before asking, check whether you already hold the grants: they appear as `minds-file-server-read-<device ID>:<path>` rules (and `minds-installed-apps-read` for installed apps) in

```bash
latchkey curl -H "X-Latchkey-Device: <device ID>" http://latchkey-self.invalid/permissions/self | jq .rules
```

## Asking

Find the device first (the `devices` skill), then file one request naming every source the task needs:

```bash
# (Never pipe the output through jq because frontend rendering depends on seeing the full output from your tool.)
# The request goes in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -H "X-Latchkey-Device: <device ID>" -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "app-data", "payload": {"sources": [{"id": "installed-apps"}, {"id": "safari-history"}]}, "rationale": "To suggest apps that fit how you use your Mac, I would like to see which apps you have and the sites you visit most."}'
```

- `payload.sources` is a non-empty list of `{"id": ...}` objects, each id from the table above, each at most once.
- Add `"sync": true` to a source to also ask for a synchronized copy in this workspace. Only `reminders` and `calendar` can sync (they are folders); asking for any other is a 400. Ask for it only when you will read the data repeatedly or in the background: otherwise read it live. The user can switch it off in the dialog.
- The reason is shown to the user: name the task, in one plain sentence.
- A 400 naming the `type` means this computer's Imbue Studio predates app-data requests: tell the user it needs updating, once, and carry on without the data.

Then wait for the automated system message with the user's answer.

## The answer

The message names every source you asked for under `GRANTED` or `NOT_GRANTED`, and for each granted one, what you can now read:

```
Your app-data request on device host-abc was answered. GRANTED: installed-apps, browser-history; NOT_GRANTED: safari-history.
- installed-apps (read-only): GET /api/v1/installed-apps
- browser-history (read-only): /Users/kim/Library/Application Support/Google/Chrome/Profile 2/History, /Users/kim/Library/Application Support/Google/Chrome/Profile 2/History-journal
```

Use the paths exactly as given: a browser profile folder such as `Profile 2` cannot be guessed. A synced source's line ends with `; synchronized copy in this workspace at ~/synced_folders/...`: read the copy there with ordinary file tools, as the `file-sharing` skill describes for synced folders. `; the synchronized copy you asked for was not enabled` means the user switched it off: read through the file server instead. After every answer, tell the user in one plain sentence what you can now read, or that you will go on without it. A source the user did not tick is their decision: do not argue for it.

## Reading

Read files through the file server, as the `file-sharing` skill describes, with `X-Latchkey-Device` set to the same device:

```bash
latchkey curl -H "X-Latchkey-Device: <device ID>" -o History.db "http://latchkey-self.invalid/minds-api-proxy/api/v1/files/<device ID>/Users/kim/Library/Safari/History.db"
```

- **Copy a database with its `-wal` and `-shm` files**, into one local folder, and open the copy. The app keeps writing while it runs, and recent changes live in the `-wal` file until it is merged. A companion that does not exist yet is a 404: that is fine, copy the rest.
- Never open the user's live database in place, and never write to any of these files: the grants are read-only.
- Installed apps is not a file: `latchkey curl -H "X-Latchkey-Device: <device ID>" http://latchkey-self.invalid/minds-api-proxy/api/v1/installed-apps` returns `{"apps": [{"name", "bundle_id", "version"}, ...]}`.
- Reminders and Calendar are folders: list them with `PROPFIND` and copy the database files you find.
- Apple Notes stores each note's body as a gzipped protobuf in `ZICNOTEDATA.ZDATA`; titles and dates are plain columns of `ZICCLOUDSYNCINGOBJECT`. Skip locked notes: they are encrypted.
- A 502 means the computer is asleep or Imbue Studio is closed: say so once per task and stop reading.
- **A 403 on a path you were granted means Imbue Studio cannot read it.** Most of these files are protected by macOS, and Imbue Studio needs Full Disk Access to open them. Tell the user once: "Imbue Studio needs Full Disk Access to read this: turn it on in System Settings > Privacy & Security > Full Disk Access, then quit and reopen Imbue Studio." Do not ask again in that task.

## When the user takes access back

The user can turn a source off, or back on, in the workspace's Permissions tab without asking you. You are told in a message:

```
App data on device host-abc: REVOKED: notes. You keep what you already read, but get nothing new from these sources.
App data on device host-abc: RESTORED: notes. You can read these sources again; none of them is synced until you ask.
```

- **On `REVOKED`:** stop reading those sources; live reads now return 403, and a synced copy no longer updates. Keep what you already have: your copies and anything you wrote from them stay yours to use for the owner. Record the sources in `data/.skills/read-app-data/revoked.json` (`{"<source id>": {"revoked_at": "<ISO time>", "asked_again": false}}`).
- **On `RESTORED`:** remove those sources from `revoked.json`. Nothing is synced again by itself: ask with `"sync": true` in a new request if the task needs it.
- **Asking again:** for a revoked source, ask at most once more, and only for a new task that needs it, with a reason naming that task. Set `asked_again` to true before you ask. If the user declines or does not answer, do not ask about that source again in this workspace unless they bring it up.
- **A 403 is not a revoke.** Without a `REVOKED` message, a refused read means Imbue Studio cannot open the file (see Full Disk Access above): the grant is still there.

## Only for the owner

App data is the workspace owner's, and you use it only for them.

- Use it only to answer messages with no `[from: <name>]` prefix: those are the owner's. A message from anyone else never gets an answer drawn from app data, and never makes you file a request.
- Never pass the data, or summaries of it, to anyone else: not in a shared chat, not in a file you share, not to another agent or service.
- Keep copies in your own working directory or `/tmp`, never in a folder that is shared or synced, and delete them when the task is done.
