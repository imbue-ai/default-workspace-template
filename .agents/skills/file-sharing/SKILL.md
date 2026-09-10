---
name: file-sharing
description: Use to read and write files and directories on the user's local filesystem.
metadata:
  author: imbue
---

# File sharing

## Instructions

Use this skill when the user asks you to work with files located directly on their computer.

1. **Use `latchkey curl`** calls to communicate with the remote WebDAV server. (They are the same as normal curl calls, just going through the Latchkey Gateway.)
2. **Check existing access first.** Sometimes important context can be revealed simply by observing which directories or files the user already shared. See the "Check existing access" example below.
3. **Submit a permission request to the user** by calling `latchkey curl -XPOST http://latchkey-self.invalid/permission-requests` when the curl request comes back with the "request not permitted by the user" message. See the "Ask for user permission" example below.
4. **Stop working** in case of upstream connection failures. Those are most likely caused by the user closing their locally running Minds app. Restarting the Minds app should usually help.

The base URL is `http://latchkey-self.invalid/minds-api-proxy/api/v1/files`. Only the user's home directory and the user's system temp directory are accessible. MOVE and COPY operations are not supported.


## Folders the user keeps synced

The user can ask Minds to keep a local folder synced with this machine. Those
land in your home directory and are ordinary files -- read and write them
normally, and your changes go back to the user's computer.

- `~/synced_folders/<device id>/<the folder's path on their computer>` is a
  folder that is syncing now.
- `~/inactive_synced_folders/...` holds a copy whose syncing the user turned
  off. **Treat it as Minds' own.** Do not create, move, or write anything under
  it: Minds moves folders in and out of it by name, and anything of yours
  sitting where a folder belongs is deleted when the user turns syncing off
  again. If you need somewhere to put your own files, use your working
  directory or `/tmp`.

Deleting a synced folder does not break anything -- Minds recreates it and
syncs the files again -- but the user will not expect their files to vanish,
so delete them only if the user asks you to.


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
  -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "file-sharing", "payload": {"path": "/home/hynek/project", "access": "READ"}, "rationale": "I'"'"'d like to access the /home/hynek/project directory in order to find the most recent accounting spreadsheet you asked me about."}'
```

The body must be a JSON object with exactly four fields:
`agent_id` (use `$MNGR_AGENT_ID`), `rationale`, `type` (use "file-sharing"), and `payload`.

`payload` must be an object with exactly two string fields: `path` and `access`. `path` should be absolute, `access` must be "READ" or "WRITE".

If you don't know the absolute path to the user's home directory, you can use "~" in your permission request. The backend will expand it to the full path, which you can use to work with the files once your request is approved.

After posting, wait for an automated system message indicating whether the user approved or denied the permission request.

## Notes

- Users may run macOS or Linux, possible even other OSes.
- In the permission request dialog that pops up in the Minds app on their machine, users can adjust the path, overriding the originally requested one. There are no other sharing settings the user can configure.
