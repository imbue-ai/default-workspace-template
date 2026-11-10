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

The message names every source you asked for under `GRANTED` or `NOT_GRANTED`, and for each granted one, what you can now read. Two more lists appear only when they apply: `GRANTED_PENDING_RESTART` for sources granted while Imbue Studio's Full Disk Access is still off (their reads are refused until the user turns it on and restarts Imbue Studio; do not ask for them again), and `NOT_GRANTED_ASKING_TURNED_OFF` for sources that need Full Disk Access when the user has turned off requests for it (do not ask for them again):

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
- **Two different refusals answer 403; read the body to tell them apart.**
  - **"Request not permitted by the user"** comes from the gateway: you have no grant for that path any more. The user turned the source off, usually with a `REVOKED` message to you; if none came, treat it as revoked all the same (see "When the user takes access back").
  - **Any other 403 on a path you were granted** (typically "Operation not permitted") comes from the user's Mac: the grant is there, but Imbue Studio cannot open the file, because macOS protects it and Imbue Studio's Full Disk Access is off. Ask for it, at most once per task (see "Asking for Full Disk Access").

## Asking for Full Disk Access

Only after a refused read on a granted path, as above. To ask for sources you do not hold yet, file `app-data`: its dialog sets up Full Disk Access itself when a source needs it.

First read the status, with `X-Latchkey-Device` set to the same device:

```bash
latchkey curl -H "X-Latchkey-Device: <device ID>" http://latchkey-self.invalid/minds-api-proxy/api/v1/full-disk-access
```

- `is_asking_allowed: false`: the user turned off requests for Full Disk Access. Do not ask; tell them once that you cannot read that source, and carry on without it.
- `status: "ON"`: it is on now; read again.
- Otherwise file the request, in a tool call of its own:

```bash
latchkey curl -H "X-Latchkey-Device: <device ID>" -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "full-disk-access", "payload": {}, "rationale": "To read your Notes, which macOS protects, Imbue Studio needs Full Disk Access."}'
```

The answer says `ENABLED` (read again), `DECLINED` (do not ask again in this task), `ASKING_TURNED_OFF` (do not ask again at all), `REFUSED_SHARED_WITH_FULL_ACCESS` (this workspace is shared too widely to ask), or `NOT_APPLICABLE` (the computer is not a Mac). Turning it on can take a restart of Imbue Studio; until then the request stays open and nothing arrives, so carry on with whatever else the task needs. A 400 naming the `type` means this computer's Imbue Studio predates the request: tell the user to turn it on in System Settings > Privacy & Security > Full Disk Access, then quit and reopen Imbue Studio, once.

While Full Disk Access is off, Imbue Studio stops every synchronized copy of a source it protects (Notes, Reminders, Calendar, Safari, Messages), and tells you with `App data on device host-abc: SYNC_STOPPED: reminders. ...`. The copy stays in the workspace but no longer updates, and nothing restarts it when access comes back: stop relying on it being current, and to sync again ask in a new `app-data` request with `"sync": true` once a task needs it, or let the user switch it on in the Permissions tab.

## The first-chat offer

A workspace's welcome ends by saying you can suggest apps that fit how the user uses their Mac. Follow up on that once per workspace, in its first chat:

1. Check `data/.skills/read-app-data/first_chat_offer.json`. If it exists, the offer was made or skipped already: do nothing.
2. Skip it, and record the skip, when the workspace already holds app-data grants (see "When to ask"), or when the first message carries a `[from: <name>]` prefix. (A workspace shared with full access cannot ask: Imbue Studio refuses its request as `REFUSED_SHARED_WITH_FULL_ACCESS`; record that as the skip.)
3. If the user's first message is urgent (an error, a `/assist`, something broken), wait: make the offer after your first calm reply in this chat instead.
4. Answer the user's message as usual. Then end the same reply with one plain line, for example "I can also suggest apps that fit how you use your Mac: approve what I can look at, or skip it.", and in the same turn file one `app-data` request with `"origin": "first-chat-offer"` in the payload:
   - `installed-apps` and `browser-history`, always;
   - `notes`, `reminders`, and `calendar` too, when the Full Disk Access status (see "Asking for Full Disk Access") is `ON` or `is_asking_allowed` is true;
   - never `messages`, `shell-history`, or `code-projects`.
5. Write `{"offered_at": "<ISO time>", "request_id": "<id>"}` (or `{"skipped_at": ..., "reason": ...}`) to `data/.skills/read-app-data/first_chat_offer.json`, so no later chat repeats it.

In the dialog only the sources the user approved in their other workspaces start ticked; the rest are theirs to tick. When the answer comes, follow the `suggest-apps` skill from its step 3 if anything was granted; if nothing was, say nothing more about it.

## When the user takes access back

The user can turn a source off, or back on, in the workspace's Permissions tab without asking you. You are told in a message:

```
App data on device host-abc: REVOKED: notes. You keep what you already read, but get nothing new from these sources.
App data on device host-abc: RESTORED: notes. You can read these sources again; none of them is synced until you ask.
```

- **On `REVOKED`:** stop reading those sources; live reads now return 403, and a synced copy no longer updates. Keep what you already have: your copies and anything you wrote from them stay yours to use for the owner. Record the sources in `data/.skills/read-app-data/revoked.json` (`{"<source id>": {"revoked_at": "<ISO time>", "asked_again": false}}`).
- **On `RESTORED`:** remove those sources from `revoked.json`. If a request of yours for those sources is still waiting, withdraw it (`latchkey curl -H "X-Latchkey-Device: <device ID>" -XDELETE http://latchkey-self.invalid/permission-requests/<request id>`): the user gave access back from the Permissions tab instead, and an unanswered request would sit in their list for nothing. Nothing is synced again by itself: ask with `"sync": true` in a new request if the task needs it.
- **Say only what you were told.** "You approved it" means you received `GRANTED` for that request; access that came back through `RESTORED` was turned back on in the Permissions tab. If reads work again and neither message came, say that access is back, not how.
- **Asking again:** for a revoked source, ask at most once more, and only for a new task that needs it, with a reason naming that task. Set `asked_again` to true before you ask. If the user declines or does not answer, do not ask about that source again in this workspace unless they bring it up.
- **A refusal from the user's Mac is not a revoke.** Only "Request not permitted by the user" means the grant is gone; any other 403 on a granted path means Imbue Studio cannot open the file (see Full Disk Access above), and the grant is still there.

## Only for the owner

App data is the workspace owner's, and you use it only for them.

- Use it only to answer messages with no `[from: <name>]` prefix: those are the owner's. A message from anyone else never gets an answer drawn from app data, and never makes you file a request.
- Never pass the data, or summaries of it, to anyone else: not in a shared chat, not in a file you share, not to another agent or service.
- Keep copies in your own working directory or `/tmp`, never in a folder that is shared or synced, and delete them when the task is done.
