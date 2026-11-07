# auth_sandbox

The web latchkey auth sandbox's side of signing a web-only workspace in to a service (the mngr monorepo's `specs/web-latchkey-auth-sandbox/spec.md`).
An auth sandbox is a short-lived Modal sandbox built from this template's image that runs the Screen (the streamed Linux desktop) and latchkey's browser sign-in.
The Imbue Studio web chrome frames the Screen, the user completes the login there, and the credential travels to the workspace's own latchkey store under the sign-in dialog's one key.

This package holds the commands owner-exec runs in the sandbox on the chrome's behalf, the claim watcher, the sandbox's boot script and supervisord configuration, and the steps that build the sandbox image.

It is its own uv project, with its own `uv.lock` and venv, and not a member of the root workspace (the root `pyproject.toml` names it under `[tool.uv.workspace] exclude`): its `imbue-mngr-latchkey` dependency carries mngr plugins that a workspace's own mngr must not load, so only the auth sandbox image installs it, and no workspace syncs it.
Its mngr packages stay on the root's pin: `system/scripts/set_mngr_pin.py` moves both pins, and its `--check` refuses a tree where they differ (relock with `uv lock --project system/libs/auth_sandbox` after a move).

## The sandbox's life

Every auth sandbox is made the same way, whether the connector keeps it warm in its pool or makes it when a sign-in asks for one, and starts blank: nothing about a user is in it until the connector claims it.

1. The connector creates the sandbox with the environment below, which names no user, and writes the connect-info file once it has read the tunnels.
2. The boot script lays the sandbox out, starts the claim watcher, supervisord (the Screen and owner-exec) and the Screen's stack.
   owner-exec starts with an empty `authorized_keys` and the Screen in gate mode with no owner token digest, so both refuse every request that is not loopback; the connector's readiness check (`/health` on 8090 and `/_alive` on 8793, from inside the sandbox) needs nothing more.
3. When a user asks for a sign-in, the connector claims the sandbox by writing the claim file.
   The watcher installs the user's key and the Screen's owner token digest, and the chrome can reach both services.
4. The chrome writes the session file and runs `auth-sandbox-prepare` (while the user still reads the dialog), then, at the click, a sign-in command, then reads the credentials back.
5. At the claim's `expires_at` the watcher returns, and the boot script stops supervisord and exits, which ends the Modal sandbox.
   The sandbox also ends if supervisord dies or the watcher fails, and Modal's own timeout ends one nobody claims.

### What the connector passes at create

No secrets and nothing about a user, only these variables, which the boot script and the commands read from the process environment:

- `AUTH_SANDBOX_HOST_ID` (required): the host id the connector minted for the sandbox, `host-` and 32 lower-case hex digits; owner-exec's audience is `container:<this>`.
- `AUTH_SANDBOX_CHROME_ORIGIN` (required): the hosted chrome's origin, with no trailing slash.
  The boot script writes `export SHARE_CHROME_ORIGIN=<origin>` to `/home/user/workspace/data/.secrets/share.env`, where owner-exec reads its CORS origin, and passes it to the Screen as `SCREEN_FRAME_ANCESTORS`.
- `AUTH_SANDBOX_REGION`: the region the sandbox runs in.
  The sign-in pins the Fortress launcher's country from it (`TILION_COUNTRY`), and its timezone too (`TILION_TZ`) when the session names none (`us-east` is New York, `eu-west` Dublin, `ap-northeast` Tokyo, and so on by longest prefix; an unknown region gets UTC and US), so no geo lookup sits on the critical path.
- `AUTH_SANDBOX_ID`: the sandbox's own id, for whatever later needs it.
- `AUTH_SANDBOX_LATCHKEY_BINARY` (optional): which `latchkey` the commands run; the npm global on `PATH` otherwise.

The boot script refuses to start when a required variable is missing or malformed (an origin with a path, a host id of another shape).

### The claim file

The connector writes `/run/auth-sandbox-claim.json` when it claims the sandbox for a user (atomically, a temp file and a rename, mode 0600; it is outside `/run/auth-sandbox/` because a sandbox made on demand is claimed at once, possibly before the boot script has created that directory):

```json
{"authorized_key": "<the user's OpenSSH public key line>", "screen_token_sha256": "<lower-case hex>", "expires_at": <unix seconds>}
```

`auth-sandbox-await-claim`, which the boot script starts before supervisord, polls for it every 250 ms and checks it: the key one non-empty line, the digest 64 lower-case hex digits, `expires_at` a positive number.
It then writes the key as the whole of `/home/user/.ssh/authorized_keys` and the digest to `/run/auth-sandbox/screen_token_sha256` (both 0600, through a rename, so neither owner-exec nor the Screen's gate, which read them per request, sees a half-written file), and waits until `expires_at`.
A claim that does not validate is logged and ignored, and the sandbox stays unusable until the connector's sweep retires it; the watcher reads the file again only if it is replaced.
A claim whose `expires_at` has already passed admits no one: the watcher returns at once and the sandbox ends.

## The session

One key per sign-in dialog: the chrome mints `K` (32 random bytes, base64), which is the sandbox store's `LATCHKEY_ENCRYPTION_KEY` and also the key the credentials travel to the workspace VM under, so nothing in the sandbox re-encrypts.
The chrome writes everything the session starts with in one file, `/run/auth-sandbox/session.json` (mode 0600, over owner-exec's `write-file`):

```json
{"latchkey_key": "<K>", "browser_state": "<base64 of the machine's browser_state.json.enc under K>" | null, "workspace_host_id": "<host-...>" | null, "timezone": "<IANA>" | null}
```

`browser_state` is the workspace machine's own browser state, which the VM hands the chrome already encrypted under `K`, so a second service is a consent click rather than a login; it is null when the machine holds none. The session is installed before the user picks a service or an account, so every sign-in gets it: one that adds an account to a service the machine already holds runs with `--fresh`, which removes it before the browser starts, and a service that takes typed credentials never sends it back.
`workspace_host_id` seeds the launcher's fingerprint (`TILION_SEED`), so a service sees one device across that workspace's sign-ins, and `timezone` is the zone the launcher reports (`TILION_TZ`).

When the sign-in has stored the credential, the chrome reads `credentials.json.enc`, `data-format-version` and, after a browser sign-in, `browser_state.json.enc` from `/run/auth-sandbox/latchkey/` and hands them, with `K`, to the workspace VM's `mngr-latchkey apply-state`, which merges them into the machine's store with `K` as the source key.

## The commands

The three the chrome runs over owner-exec are on `PATH` in the image (console scripts of this package in its environment at `/opt/auth-sandbox/venv`, linked into `/usr/local/bin/`), and all of them read the sandbox's tmpfs at `/run/auth-sandbox/` (`--runtime-dir` moves it, for tests).
Keys travel in files and in a child's environment, never in an argument; nothing here prints a key.

- `auth-sandbox-prepare --session-file PATH [--describe SERVICE]`: installs the session: writes `K` to `latchkey_key`, the browser state (when the file has one) as `browser_state.json.enc` in the latchkey directory, and `workspace_host_id` and `timezone` to `session_settings.json`, then deletes the session file (also when it is malformed).
  It prints nothing unless `--describe` names a service; then it prints what latchkey knows of it, `latchkey services info --offline SERVICE`'s JSON (its `authOptions`, `setCredentialsExample` and so on), or `{}` when latchkey knows no such service.
- `auth-sandbox-sign-in <service> [--account A] [--fresh]`: first describes the service as above.
  A service that lists its `authOptions` without `browser` takes typed credentials: the command launches nothing, prints `{"services_info": <the description>}` and exits 4.
  Otherwise (a description that names no options, `{}` included, or one latchkey could not give, signs in through the browser, as on the desktop) it runs latchkey's browser sign-in on the Screen, exactly as the desktop's Approve does (`imbue.mngr_latchkey.core.Latchkey`: minds' part of the config merged in, keeping the template's browser entry, then `auth_browser(service, account)` with Imbue's Google OAuth client, the Notion MCP redirect pin and the `browser-prepare` fallback), under `K` and with the fingerprint from the session's settings.
  `--account` is the account the user picked; the command names it to latchkey only for a service whose description says it cannot tell who logged in (`capabilities.detectsLoginAccount` false), since latchkey stores a detecting service's credential under the account it detects and refuses a named account its store does not hold, and the sandbox's store is fresh.
  `--fresh` removes the browser state prepare installed before launching, so the browser starts without the machine's session, as the desktop's add-account does.
- `auth-sandbox-set-credentials <service> --account A --values-file PATH`: for a service without a browser sign-in (`authOptions` of `set` only).
  The file is a JSON object `{"values": {"<placeholder>": "<value>", ...}, "set_credentials_example": "..."}`; the example is optional and is otherwise asked of latchkey (`services info --offline`), falling back to the generic bearer form.
  The command builds the `latchkey auth set` argv the desktop's form builds (`imbue.mngr_latchkey.credential_commands`) and deletes the values file once read.

On success each sign-in command prints, as its last line, the store's accounts for the service, `{"accounts": {...}}`, the service's entry of `latchkey auth list --offline` (`{"<account>": {"credentialType": ..., "credentialStatus": ...}}`, the default account under `""`), which names the account the chrome transfers; `auth-sandbox-sign-in` adds the description under `"services_info"`.

Exit codes: 0 done; 1 latchkey refused, its reason as the last line on stderr (a cancelled login, a refused permission under `--strict`, or an `auth list` that failed, and for prepare a `services info` that failed for a reason other than an unknown service); 3 a material the command needs is missing or malformed (the session file, the key and settings prepare keeps from it, the values file, or the `latchkey` binary and its config themselves), or a file or directory of the sandbox's cannot be read or written; 4 the sign-in's service takes typed credentials. 2 is click's usage-error code, which a malformed invocation exits with.
None of the commands runs `latchkey --version`: the image build pins the version and checks it, and the commands only look the binary up.

Two more scripts are not for the chrome: `auth-sandbox-await-claim`, the claim watcher, which the boot script runs from this package's environment (it exits 0 at the session's expiry and 3 when it cannot install the claim), and `auth-sandbox-render-config`, which runs at image build only and writes the template `config.json`.

## The tmpfs layout

Everything user-specific lives under `/run/auth-sandbox/`, which dies with the sandbox and is never snapshotted:

| Path | Written by | Holds |
|---|---|---|
| `session.json` | the chrome, over owner-exec | the session file, until prepare reads and deletes it |
| `latchkey_key` | `auth-sandbox-prepare` | `K`, the session's latchkey encryption key |
| `session_settings.json` | `auth-sandbox-prepare` | `{"workspace_host_id", "timezone"}` for the sign-in's fingerprint |
| `screen_token_sha256` | the claim watcher | the Screen's owner token digest, read by its gate per request |
| `latchkey/` | the boot script, prepare, then latchkey | the sandbox's `LATCHKEY_DIRECTORY`: the template `config.json`, the browser state, and the credentials a sign-in stores, all under `K` |
| `host/data.json` | the boot script | `{"host_id": ...}`, which owner-exec reads its audience `container:<host id>` from |

The sign-in also sets `TILION_NO_ACTIVATE=1`, `DISPLAY=:99`, the Screen's `XDG_RUNTIME_DIR` and `PULSE_SERVER`, `LATCHKEY_DISABLE_COUNTING=1`, and drops any `LATCHKEY_GATEWAY` from its environment.

## The boot script and how the sandbox is reached

The boot script (`sandbox/boot.sh`, installed at `/opt/auth-sandbox/boot.sh`) creates the tmpfs layout, copies the template `config.json` into the latchkey directory, writes an empty `authorized_keys` and `share.env`, starts the claim watcher and the sandbox's supervisord (`sandbox/supervisord.conf`: the Screen service with its stack group and watcher, and owner-exec; no share gateway, no chat, no agents, no env-converge), asks the Screen service to start its stack, and waits for whichever of the watcher and supervisord ends first; then it stops the other and exits.
It sets `HOME=/home/user` and `MNGR_HOST_DIR=/run/auth-sandbox/host`, and exports for the programs `SCREEN_LISTEN_HOST=0.0.0.0`, `OWNER_EXEC_LISTEN_HOST=0.0.0.0` (read by `system/scripts/run_owner_exec.sh`), `SCREEN_OWNER_TOKEN_SHA256_FILE=/run/auth-sandbox/screen_token_sha256` (which turns on the Screen's gate, `system/apps/screen/README.md`, "Gate mode"), `SCREEN_FRAME_ANCESTORS`, `SCREEN_CONNECT_INFO_PATH=/run/auth-sandbox-connect-info.json`, `SCREEN_VIDEO_CODEC=vp8`, `SCREEN_VIDEO_QUALITY=high`, `SCREEN_FIXED_SIZE=1288x806`, `SCREEN_VIDEO_PROFILE=control`, `SCREEN_DESKTOP_PANEL=off` and `SCREEN_ICE_LITE=on`.
The codec and the size are for whatever browser the user signs in from: neko streams VP8, which every browser decodes over WebRTC (Firefox decodes the workspace's default H.264 only with Cisco's OpenH264 plugin), and the display keeps one size, exactly latchkey's login window (Playwright's 1280x720 viewport in a 1288x805 window, plus one row for the even height VP8 needs), where a small viewer's frame would have shrunk the display under it and a larger display would spend the viewer's screen on empty desktop; the viewer scales the picture to its frame.
The window fills the display only from its top left corner, where Chromium does not put a new window (it places one 10 px in), so the browser latchkey launches is `sandbox/sign-in-browser.sh` (installed at `/opt/auth-sandbox/sign-in-browser.sh`, and named by the template `config.json`), which runs the Fortress launcher with `--window-position=0,0`; latchkey passes Chromium no arguments of ours.
The window is all browser only without a compositing manager: under one, Chromium draws its restored window's shadow inside its own X window (16 px left and right, 10 px above, 32 px below), so the 1288x805 window would show a 1256x763 browser framed in black, its 1280x720 page cut 24 px short on the right and 44 px at the bottom.
So before supervisord starts, the boot script writes the user's `xfwm4` channel file (`~/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml`) with `/general/use_compositing` false, and xfwm4 runs without its compositor.
The window then fills the display edge to edge apart from the one row of even height below it: a 4 px frame border on each side, the tab strip and toolbar in the top 87 rows, and the page in the 1280x714 between, its viewport's bottom 6 px under the window's lower edge (Playwright sizes a Linux window for a toolbar 6 px shorter than Fortress's).
The Screen streams its `control` profile (30 fps, 8000 kbit/s at high quality, VP8 tuned for text: `system/apps/screen/README.md`, "Profiles, visibility and audio") throughout, since the sandbox exists for one user actively signing in: without the pin a sign-in would start on the visibly blurrier `idle` profile (5 fps, 800 kbit/s) until the user's first click, and drop back to `watch` or `idle` whenever they pause.
The desktop session runs without the XFCE panel (the window manager, the settings daemon and the desktop only), since the user signs in through one browser window, and neko runs as an ICE lite agent, since the Modal TCP tunnel is the one way to reach it.

The sandbox is reached over Modal's own tunnels, with no relay and no certificate of ours in it:

| What | Port | Modal | Reached as |
|---|---|---|---|
| The Screen | 8090 | `encrypted_ports` | `https://<host>.w.modal.host`, gated: loopback, or the owner session the owner token opens once the sandbox is claimed |
| owner-exec | 8793 | `encrypted_ports` | `https://<host>.w.modal.host`, every request signed with the claiming user's key |
| neko's ICE TCP mux | 59000 | `unencrypted_ports` | raw TCP at `<host>.r<n>.modal.host:<port>`, the WebRTC media |

## The connect-info file

The connector writes `/run/auth-sandbox-connect-info.json` (outside `/run/auth-sandbox/`, which is root-only) right after it reads the sandbox's tunnels, which may be before or after the boot script runs, so the boot script never touches it.
It is a connect-info document naming the raw TCP tunnel to the media port:

```json
{"candidates": [{"host": "<raw tcp host>", "port": <raw tcp port>, "protocol": "tcp", "guest_port": 59000}], "ice_servers": [], "expires_at": null}
```

The Screen reads it on every `GET /api/connect-info` (`SCREEN_CONNECT_INFO_PATH`) and answers the tunnel's hostname as the IPv4 addresses it resolves to (Firefox ignores a candidate named by a hostname), so the viewer's WebRTC reaches neko through the tunnel; an absent or unreadable file is simply no answer from that broker.

## The image

`auth_sandbox.modal_image_build` builds the product `system/Dockerfile` into a Modal image, creates a sandbox from it, runs the steps of `image_build.py` (the first-boot seed and the Screen unit as the CI image does them, the pinned latchkey checked, this package installed from its own lock into `/opt/auth-sandbox/venv` on the workspace venv's Python, the boot script, the sign-in browser script, the supervisord config and the commands installed, the template `config.json` rendered with the sign-in browser script as its browser and verified with `latchkey ensure-browser --source existing-config`, the claim watcher checked), snapshots the filesystem and prints the image id.
Run it from the repo root with Modal credentials for the target workspace, after `uv sync --locked --project system/libs/auth_sandbox`: `uv run --no-sync --project system/libs/auth_sandbox --with modal python -m auth_sandbox.modal_image_build` (`modal` is not a dependency, so it comes in for the one run).
The steps are plain data over a small sandbox interface, and the tests run them over a fake.

## Tests

The suite runs from this directory in its own venv, as the template's CI runs it: `uv sync --locked`, then `uv run --no-sync pytest` (the root suite does not collect it). The unit tests run over fakes (`mock_latchkey_test.py`).
`test_transfer_with_latchkey.py`, at the library's root with the ratchets, runs `auth-sandbox-prepare` and `auth-sandbox-set-credentials` against the real `latchkey` on `PATH` (the version `setup_system.sh` pins) with fabricated browser states, and merges what the chrome transfers into a machine store as the VM does; it skips when there is none.
