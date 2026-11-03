# auth_sandbox

The web latchkey auth sandbox's side of signing a web-only workspace in to a service (the mngr monorepo's `specs/web-latchkey-auth-sandbox/spec.md`).
An auth sandbox is a short-lived Modal sandbox built from this template's image that runs the Screen (the streamed Linux desktop) and latchkey's browser sign-in.
The Imbue Studio web chrome frames the Screen, the user completes the login there, and the credential travels to the workspace's own latchkey store under a one-time transfer key.

This package holds the commands owner-exec runs in the sandbox on the chrome's behalf, the sandbox's boot script and supervisord configuration, and the steps that build the sandbox image.

## The commands

All four are on `PATH` in the image (console scripts of this package, linked into `/usr/local/bin/`), and all of them read the sandbox's tmpfs at `/run/auth-sandbox/` (`--runtime-dir` moves it, for tests).
Keys travel in files and on stdin, never in an argument; nothing here prints a key.

- `auth-sandbox-sign-in <service> [--account A]`: runs latchkey's browser sign-in on the Screen, exactly as the desktop's Approve does (`imbue.mngr_latchkey.core.Latchkey`: `initialize()`, whose config merge keeps the template's browser entry, then `auth_browser(service, account)` with Imbue's Google OAuth client, the Notion MCP redirect pin and the `browser-prepare` fallback).
  `--account` names the account the credential is stored under, which only a service that cannot tell who logged in needs (and the chrome passes only to one): latchkey refuses a named account its store does not hold for a service that detects the login account, and the sandbox's store is fresh.
  Exits 0 when the credential is stored in the sandbox's store, else 1 with latchkey's reason as the last line on stderr (a cancelled login, a refused permission under `--strict`, a service that needs typed credentials).
- `auth-sandbox-set-credentials <service> --account A --values-file PATH`: for a service without a browser sign-in (`authOptions` of `set` only).
  The file is a JSON object `{"values": {"<placeholder>": "<value>", ...}, "set_credentials_example": "..."}`; the example is optional and is otherwise asked of latchkey (`services info --offline`), falling back to the generic bearer form.
  The command builds the `latchkey auth set` argv the desktop's form builds (`imbue.mngr_latchkey.credential_commands`) and deletes the values file once read.
- `auth-sandbox-import-browser-state`: re-encrypts `/run/auth-sandbox/incoming/browser_state.json.enc` (the workspace machine's browser state, encrypted under the transfer key) into the sandbox's latchkey directory under the sandbox's key, so a second service is a consent click rather than a login.
  Prints `{"browser_state": "<path>"}`.
- `auth-sandbox-export <service> [--account A]`: re-encrypts the service's credentials (and the browser state, when the sandbox holds one) into `/run/auth-sandbox/export/` under the transfer key and prints the paths it wrote: `{"credentials": ..., "data_format_version": ..., "browser_state": ... | null}`.
  The chrome reads those files over owner-exec and hands them to the workspace VM's `mngr-latchkey apply-state` with the transfer key.

Exit codes: 0 done; 1 latchkey refused (its reason on stderr); 3 a material the command needs is missing or malformed (a key file, the incoming state, the values file, or the `latchkey` binary and its config themselves, which the desktop's wrapper checks on every run), or a file or directory of the sandbox's cannot be read or written. 2 is click's usage-error code, which a malformed invocation exits with.

A fifth script, `auth-sandbox-render-config`, runs at image build only: it writes the template `config.json`.

## The tmpfs layout

Everything user-specific lives under `/run/auth-sandbox/`, which dies with the sandbox and is never snapshotted:

| Path | Written by | Holds |
|---|---|---|
| `latchkey_key` | the boot script | the sandbox's own latchkey encryption key (32 random bytes, base64) |
| `transfer_key` | the chrome, over owner-exec | the dialog's one-time transfer key |
| `incoming/browser_state.json.enc` | the chrome | the workspace's browser state under the transfer key |
| `latchkey/` | the boot script, then latchkey | the sandbox's `LATCHKEY_DIRECTORY`: the template `config.json`, the credentials a sign-in stores, the imported browser state |
| `export/` | `auth-sandbox-export` | `credentials.json.enc`, `data-format-version`, `browser_state.json.enc` under the transfer key |
| `host/data.json` | the boot script | `{"host_id": ...}`, which owner-exec reads its audience `container:<host id>` from |

## What the connector passes at create

The connector creates the sandbox with no secrets, only these variables, which the boot script and the commands read from the process environment:

- `AUTH_SANDBOX_HOST_ID` (required): the host id the connector minted for the sandbox, `host-` and 32 lower-case hex digits; owner-exec's audience is `container:<this>`.
- `AUTH_SANDBOX_AUTHORIZED_KEY` (required): the user's OpenSSH public key line, which the boot script writes as the whole of `/home/user/.ssh/authorized_keys`, the key owner-exec verifies every request against.
- `AUTH_SANDBOX_CHROME_ORIGIN` (required): the hosted chrome's origin, with no trailing slash.
  The boot script writes `export SHARE_CHROME_ORIGIN=<origin>` to `/home/user/workspace/data/.secrets/share.env`, where owner-exec reads its CORS origin, and passes it to the Screen as `SCREEN_FRAME_ANCESTORS`.
- `AUTH_SANDBOX_SCREEN_TOKEN_SHA256` (required): the lower-case hex SHA-256 of the Screen's owner token, which the connector mints, returns to the chrome once and never stores.
  The boot script passes it to the Screen as `SCREEN_OWNER_TOKEN_SHA256`, which turns on the Screen's gate (`system/apps/screen/README.md`, "Gate mode").
- `AUTH_SANDBOX_ID`, `AUTH_SANDBOX_CONNECTOR_URL`: the sandbox's own id and the connector it belongs to, for whatever later needs them.
- `AUTH_SANDBOX_REGION`: the region the sandbox runs in.
  The sign-in pins the Fortress launcher's timezone and country from it (`TILION_TZ`, `TILION_COUNTRY`; `us-east` is New York, `eu-west` Dublin, `ap-northeast` Tokyo, and so on by longest prefix; an unknown region gets UTC and US) so no geo lookup sits on the critical path.
  `AUTH_SANDBOX_TIMEZONE` and `AUTH_SANDBOX_COUNTRY` override what the region implies.
- `AUTH_SANDBOX_WORKSPACE_HOST_ID`: the workspace being signed in; the launcher's fingerprint seed (`TILION_SEED`) is derived from it, so a service sees one device across that workspace's sign-ins.
- `AUTH_SANDBOX_LATCHKEY_BINARY` (optional): which `latchkey` the commands run; the npm global on `PATH` otherwise.

The boot script refuses to start when a required variable is missing or malformed (a key with a line break, an origin with a path, a host id or a digest of another shape).

The sign-in also sets `TILION_NO_ACTIVATE=1`, `DISPLAY=:99`, the Screen's `XDG_RUNTIME_DIR` and `PULSE_SERVER`, `LATCHKEY_DISABLE_COUNTING=1`, and drops any `LATCHKEY_GATEWAY` from its environment.

## The boot script and how the sandbox is reached

The boot script (`sandbox/boot.sh`, installed at `/opt/auth-sandbox/boot.sh`) creates the tmpfs layout, generates the sandbox key, copies the template `config.json` into the latchkey directory, writes `authorized_keys` and `share.env`, starts the sandbox's supervisord (`sandbox/supervisord.conf`: the Screen service with its stack group and watcher, and owner-exec; no share gateway, no chat, no agents, no env-converge), asks the Screen service to start its stack, and waits.
It sets `HOME=/home/user` and `MNGR_HOST_DIR=/run/auth-sandbox/host`, and exports for the programs `SCREEN_LISTEN_HOST=0.0.0.0`, `OWNER_EXEC_LISTEN_HOST=0.0.0.0` (read by `system/scripts/run_owner_exec.sh`), `SCREEN_OWNER_TOKEN_SHA256`, `SCREEN_FRAME_ANCESTORS` and `SCREEN_CONNECT_INFO_PATH=/run/auth-sandbox-connect-info.json`.

The sandbox is reached over Modal's own tunnels, with no relay and no certificate of ours in it:

| What | Port | Modal | Reached as |
|---|---|---|---|
| The Screen | 8090 | `encrypted_ports` | `https://<host>.w.modal.host`, gated: loopback, or the owner session the owner token opens |
| owner-exec | 8793 | `encrypted_ports` | `https://<host>.w.modal.host`, every request signed with the owner's key |
| neko's ICE TCP mux | 59000 | `unencrypted_ports` | raw TCP at `<host>.r<n>.modal.host:<port>`, the WebRTC media |

## The connect-info file

The connector writes `/run/auth-sandbox-connect-info.json` (outside `/run/auth-sandbox/`, which is root-only) right after it reads the sandbox's tunnels, which may be before or after the boot script runs, so the boot script never touches it.
It is a connect-info document naming the raw TCP tunnel to the media port:

```json
{"candidates": [{"host": "<raw tcp host>", "port": <raw tcp port>, "protocol": "tcp", "guest_port": 59000}], "ice_servers": [], "expires_at": null}
```

The Screen reads it on every `GET /api/connect-info` (`SCREEN_CONNECT_INFO_PATH`), so the viewer's WebRTC reaches neko through the tunnel; an absent or unreadable file is simply no answer from that broker.

## The image

`auth_sandbox.modal_image_build` builds the product `system/Dockerfile` into a Modal image, creates a sandbox from it, runs the steps of `image_build.py` (the first-boot seed and the Screen unit as the CI image does them, the pinned latchkey checked, the template `config.json` rendered and verified with `latchkey ensure-browser --source existing-config`, the boot script, the supervisord config and the commands installed), snapshots the filesystem and prints the image id.
Run it from the repo root with Modal credentials for the target workspace: `uv run --no-sync --with modal python -m auth_sandbox.modal_image_build` (`modal` is not a workspace dependency, so it comes in for the one run).
The steps are plain data over a small sandbox interface, and the tests run them over a fake.

## Tests

`uv run --no-sync pytest system/libs/auth_sandbox` runs the unit tests over fakes (`mock_latchkey_test.py`).
`test_transfer_with_latchkey.py`, at the library's root with the ratchets, runs the import and export commands against the real `latchkey` on `PATH` (the version `setup_system.sh` pins) with fabricated stores and browser states, and skips when there is none.
