---
name: file-sharing
description: Use to read and write files and directories on the user's local filesystem.
metadata:
  author: imbue
---

# File sharing

## Instructions

Use this skill when the user asks you to work with files located directly on their computer.

**First, look for a synced copy.** If the folder you need is already under
`~/synced_folders/`, it is on this machine as ordinary files: work on it
directly with your normal tools and skip everything below. See "Folders the
user keeps synced", including what happens to anything you write. The rest of
this section is for a shared path that has no copy here.

1. **Use `latchkey curl`** calls to communicate with the remote WebDAV server. (They are the same as normal curl calls, just going through the Latchkey Gateway.)
2. **Check existing access first.** Sometimes important context can be revealed simply by observing which directories or files the user already shared. See the "Check existing access" example below.
3. **Submit a permission request to the user** by calling `latchkey curl -XPOST http://latchkey-self.invalid/permission-requests` when the curl request comes back with the "request not permitted by the user" message. See the "Ask for user permission" example below.
4. **Stop working** in case of upstream connection failures. Those are most likely caused by the user closing their locally running Minds app. Restarting the Minds app should usually help.

The base URL is `http://latchkey-self.invalid/minds-api-proxy/api/v1/files`. Only the user's home directory and the user's system temp directory are accessible. MOVE and COPY operations are not supported.


## Folders the user keeps synced

For a shared folder, the user can additionally ask Minds to keep a copy of it
on this machine. When there is one, **use it instead of the WebDAV server
above**: it is ordinary local files, so your normal tools work on it, there is
no round trip per file, and it keeps working while the user's computer is
asleep or offline. Fall back to `latchkey curl` only for a shared path that has
no copy here.

- `~/synced_folders/<device id>/<the folder's path on their computer>` is a
  folder that is syncing now. So `/Users/kim/notes` from device `host-abc`
  is at `~/synced_folders/host-abc/Users/kim/notes`.
- `~/inactive_synced_folders/...` holds a copy whose syncing the user turned
  off. **Treat it as Minds' own.** Do not create, move, or write anything under
  it: Minds moves folders in and out of it by name, and anything of yours
  sitting where a folder belongs is deleted when the user turns syncing off
  again. If you need somewhere to put your own files, use your working
  directory or `/tmp`.

### Whether your writes reach the user

Only if you were granted **write** access to that path. Check before you rely
on it -- the rules list names each permission `minds-file-server-read-<path>`
or `minds-file-server-write-<path>`:

```bash
latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules
```

With **read and write**, changes travel both ways: what you write here reaches
the user's computer within seconds, and what they change reaches you.

With **read only**, the sync runs one way. Anything you write into the folder
is **reverted**, and files you create there are **deleted**, the next time the
sync runs -- silently, with no error. Do not use a read-only synced folder as a
place to put your work. Write it in your own working directory and tell the
user where it is, or ask for write access.

### What does not come across

- **`.git` is not copied.** A synced folder that is a repository on the user's
  computer arrives here as a working tree with no history: `git status` in it
  will say it is not a repository. That is expected, not a fault to repair --
  do not run `git init` there.
- **Symlinks are not copied**, in either direction. One you create here never
  reaches the user, and one on their computer is simply absent here.

### Deleting

**Deleting a file in a synced folder deletes the user's copy of it**, usually
within seconds, if you have write access. There is no undo. Delete only what
the user asked you to delete.

Emptying a synced folder entirely is refused by the sync rather than
propagated, so the user's files survive that -- but do not rely on it: it is a
safety net for the whole-folder case only, and it stops the sync until the user
sorts it out.

## Examples

### Check existing access

```bash
latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules
```

### Retrieving a file
```bash
latchkey curl -O http://latchkey-self.invalid/minds-api-proxy/api/v1/files/home/hynek/project/notes.txt
```

### Writing a file
```bash
latchkey curl -T localfile.txt http://latchkey-self.invalid/minds-api-proxy/api/v1/files/home/hynek/project/remotefile.txt
```

### Listing a directory
```bash
latchkey curl -s -X PROPFIND -H "Depth: 1" http://latchkey-self.invalid/minds-api-proxy/api/v1/files/home/hynek/project/ | xmlstarlet sel -N d=DAV: -t -m "//d:response/d:href" -v . -n
```

### Ask for user permission

When a request comes back with the "request not permitted by the user" message, ask the user for permission:

```bash
# 2. Ask for the necessary missing permissions.
# (Never pipe the output through jq because frontend rendering depends on seeing the full output from your tool.)
# The request goes in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "file-sharing", "payload": {"path": "/home/hynek/project", "access": "READ"}, "rationale": "I'"'"'d like to access the /home/hynek/project directory in order to find the most recent accounting spreadsheet you asked me about."}'
```

The body must be a JSON object with exactly four fields:
`agent_id` (the chat the request belongs to: use `${MINDS_CHAT_ID:-$MNGR_AGENT_ID}`, since the chat app
sets `MINDS_CHAT_ID` on every agent it creates and an agent created any other way is its own
chat), `rationale`, `type` (use "file-sharing"), and `payload`.

`payload` must be an object with exactly two string fields: `path` and `access`. `path` should be absolute, `access` must be "READ" or "WRITE".

If you don't know the absolute path to the user's home directory, you can use "~" in your permission request. The backend will expand it to the full path, which you can use to work with the files once your request is approved.

After posting, wait for an automated system message indicating whether the user approved or denied the permission request.

## Notes

- Users may run macOS or Linux, possible even other OSes.
- In the permission request dialog that pops up in the Minds app on their machine, users can adjust the path, overriding the originally requested one. There are no other sharing settings the user can configure.
