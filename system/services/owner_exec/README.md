# owner-exec

An owner-authenticated in-workspace exec service: the hosted minds web client
(and the desktop, over the local forward channel) drive a workspace through
this instead of an SSH session.

## Why it exists

A web-only workspace has no desktop and no SSH client in the browser, but the
browser needs SSH-equivalent authority to finish a create, provision backups,
and edit sharing grants. Rather than run an SSH protocol stack in the browser,
this service exposes a small HTTP surface -- run a command, read a file, write
a file, read/replace the grants document -- and authenticates every request by
an **Ed25519 signature over the request envelope**. The signing key's public
half must be in the workspace's `~/.ssh/authorized_keys`, so authorization is
exactly SSH's model: possession of a key the workspace trusts. The browser
already holds the workspace private key (it generated the keypair at create
and stores it encrypted under the account DEK).

## How requests are authenticated

Each request carries four headers:

- `X-Exec-Signature` -- base64 Ed25519 signature over the canonical signing
  string.
- `X-Exec-Public-Key` -- the OpenSSH public key line used to sign.
- `X-Exec-Timestamp` -- unix seconds; must be within +/-60s of the server clock.
- `X-Exec-Nonce` -- a fresh random string; rejected if seen again inside the
  window.

The signing string binds the method, path, a SHA-256 of the body, the
workspace's **audience** (its share domain, read from `data/.secrets/share.env`),
the timestamp, and the nonce (see `signing.py`). Domain binding means a captured
envelope cannot be replayed against a different workspace; the timestamp window
plus the nonce cache stop replay against the same one. Exec is unavailable when
the workspace is not shared (no audience). The share gateway's owner-session
`forward_auth` sits in front of this as defense in depth, but the signature is
the real gate.

## Endpoints

- `POST /run` -- body `{"command": ["...", ...], "cwd"?, "timeout_seconds"?}`;
  streams newline-delimited JSON events (`{"type": "stdout"|"stderr", "data"}`
  then `{"type": "exit", "code"}`).
- `POST /read-file` -- body `{"path"}`; returns `{"exists", "content_b64"}`.
- `POST /write-file` -- body `{"path", "content_b64", "mode"?}`; atomic write.
- `GET /grants` / `PUT /grants` -- read/replace `data/.secrets/share_grants.toml`
  (TOML-validated on write). This is the single writer of the sharing grants,
  used by both the web client and the desktop. Writes are compare-and-swap
  capable: `GET` returns `{"grants_toml", "revision"}` (the revision is a
  digest of the file bytes; `""` while no file exists), and `PUT` accepts an
  optional `base_revision` -- when given and stale, the write is refused with
  `409 {"error", "revision", "grants_toml"}` carrying the current document so
  the caller can merge and retry. Omitting `base_revision` is a deliberate
  blind replace. Successful writes return the new `revision`.
- `GET /_alive` -- unauthenticated loopback liveness (for supervisord / the
  forward readiness probe).

## Running

Registered as a supervisord program and reachable at its own service origin
(`owner-exec-<rand>.<workspace-origin>`), both locally (via `mngr forward`) and
on a share. Listens on `127.0.0.1:8793`.
