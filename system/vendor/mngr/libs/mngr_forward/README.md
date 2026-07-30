# mngr_forward

Auth + workspace-origin forwarding plugin for `mngr`.

`mngr forward` runs a local proxy that serves
`[<service>.]<host-id>.localhost:<port>/*` and byte-forwards each request to
the matching backend. The bare `host-<hex>.localhost` origin maps to the
configured backend (`--service NAME`, the default workflow, or a fixed remote
port via `--forward-port REMOTE_PORT`); `<service>.host-<hex>.localhost`
origins map to that agent-registered service, and deeper labels
(`sub.<service>.host-<hex>.localhost`) route to the same service -- they are
the service's own sub-origin space. Remote agents are reached via a per-host
SSH tunnel.

The plugin is opt-in:

```bash
mngr plugin enable forward
mngr forward --service system_interface
```

## Quick start (browser user)

```bash
mngr forward --service system_interface --open-browser
```

This listens on `127.0.0.1:8421`, prints a one-time login URL to stderr (or
emits a `login_url` JSONL event on stdout with `--format jsonl`), and streams
discovered agents and their events to stdout as a merged JSONL stream wrapped
in a `{stream, agent_id?, payload}` envelope. After the browser visits the
login URL, navigations to `host-<hex>.localhost:8421/` are byte-forwarded to
that host's resolved `system_interface` URL through an SSH tunnel, and
`<service>.host-<hex>.localhost:8421/` reaches any other registered service.
One session cookie (set with `Domain=host-<hex>.localhost` by the `/goto/`
bridge) covers the whole workspace-origin family.

## Reverse tunnels

`--reverse <remote-port>:<local-port>` (repeatable) auto-sets up reverse SSH
tunnels for every known agent on a remote host. The `<remote-port>` may be
`0` to ask sshd for a dynamic assignment; the actual bound port is reported
in a `forward.reverse_tunnel_established` envelope event.

## Manual mode

`--no-observe --forward-port REMOTE_PORT` runs `mngr list --format json` once
and forwards a fixed snapshot. `--no-observe` is invalid with `--service NAME`.

## Sub-process integration

Consumers (notably `minds run`) can spawn `mngr forward --format jsonl
--preauth-cookie <opaque-token>`, parse the envelope JSONL stream off stdout,
and pre-set the `mngr_forward_session` cookie in their browser session so the
OTP flow is bypassed.

## Status

Experimental.
