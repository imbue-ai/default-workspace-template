A visiting agent's grant is now only its record in the grants file.

- A grant with workspace scope no longer adds the agent's key to `~/.ssh/authorized_keys`. Each time the gateway starts it removes the lines an earlier release wrote (the ones marked `imbue-agent-access`), under the file's lock, and leaves every other line alone.

- The answers of `POST /_agent/join` and `GET /_agent/me` no longer say where the owner-exec daemon is. For a grant of the whole workspace they carry `"exec": {"origin": "", "audience": "", "host_public_key": ""}`, and for a grant of named apps no `exec`, as before. `WorkspaceCoordinates` no longer holds the host's public key.

- A copy of the `wsvisit` helper that signs its requests to the owner-exec daemon can no longer run a command or read or write a file. It prints its notice to reinstall when it joins and each time such a request fails, which is what the empty `exec` values are for: with no `exec` that copy would read its grant as covering named apps only and say nothing. `get`, `primer`, `skills`, `changes`, `me` and `invite` still work for it.
