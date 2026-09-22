# Linux packaging for the minds desktop app

**Status:** implemented (issue imbue-ai/mngr-internal#940); Linux is not yet listed on the stable channel, so the public download link still answers 404 for it.
**Audience:** whoever builds, releases, or debugs the Linux desktop artifacts.

## Overview

The minds desktop app is an Electron shell around a Python backend, built and hosted by ToDesktop from one upload of `apps/minds/`.
ToDesktop has been producing a Linux x86_64 AppImage for every build since 0.4.2, but the artifact is broken by construction.
`apps/minds/scripts/build.js` stages the native tools (`uv`, `git`, `restic`, `desync`, `limactl`, the latchkey `curl`) for the machine that runs it, which in CI is the arm64 `minds-runner` Mac, and ToDesktop packages every platform from that single upload.
The AppImage therefore ships arm64 Mach-O binaries and cannot start its backend.

This spec makes the Linux artifacts real: an AppImage and a `.deb` for x86_64, each carrying Linux-native tools, updating through the same release channels the macOS build uses, reachable from the public download link once promoted to stable, and verified by CI on every build.
It also gives Linux users an honest create form: the local backends need software the app cannot bundle (Docker, gVisor, QEMU and KVM), so the form says what is missing and hands over the command that installs it.

Related documents:

- `specs/electron-desktop-app/spec.md` -- the desktop app's bundling design, which promised an AppImage.
- `specs/minds-release-channels/spec.md` -- the channel machinery this extends to Linux (its decision 7 deferred Linux).
- `specs/minds-managed-git/concise.md` -- the dugite-native git payload, whose Linux target is already proven by CI.
- `apps/minds/docs/desktop-app.md` -- user-facing description of the packaged app.
- `apps/minds/docs/deploy/ops/app-release.md` -- the release runbook, including channel promotion and the download link.

## Verified facts this design relies on

Established on 2026-09-11 against the live services, the ToDesktop CLI 1.28.1 package, and electron-updater 6.8.9 sources.

- ToDesktop publishes a per-build Linux manifest at `https://download.todesktop.com/<app>/latest-linux-build-<buildId>.yml` for every build, released or not, and serves the AppImage at `https://dl.todesktop.com/<app>/builds/<buildId>/linux/appImage/x64`.
- The ToDesktop config accepts `platformOverrides.linux.extraResources` (CLI 1.13 and later) and `targetOverrides.<platform>.<arch>.extraResources` (CLI 1.28 and later).
  The build service did not honour the Linux `platformOverrides` list in any of three shapes tried on 2026-09-11 (see "Staged layout"), so the config uses `targetOverrides`, whose documentation is the one that describes per-target resource lists.
- `linux.noSandbox: "probe"` (CLI 1.24 and later) passes `--no-sandbox` to Electron only when the host has no unprivileged user namespaces.
  For AppImage targets it requires `appBuilderLibVersion` 26.9.0 or later.
- electron-updater picks its Linux implementation from a `package-type` file electron-builder writes into the resources directory: `deb` selects `DebUpdater`; anything else selects `AppImageUpdater`.
- `AppImageUpdater` requires the `APPIMAGE` environment variable, which the AppImage runtime sets.
  Without it `checkForUpdates` resolves to `null` and `isUpdaterActive()` is false.
  Installing replaces the running file in place with `mv -f` and relaunches it.
- `DebUpdater` installs with `dpkg -i` under `pkexec` (or `gksudo`, `kdesudo`, `sudo`), which raises the system password prompt, and relaunches the app when `isForceRunAfter` is set.
  electron-updater's quit handler installs with `isSilent = true`, so leaving `autoInstallOnAppQuit` armed on a `.deb` would raise the password prompt at quit.
- The staged binary sets measure 332 MB for darwin-arm64 and 324 MB for linux-x64.
- Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor, which is why an unmodified Electron AppImage refuses to start there.

## Decisions

1. **Ship both a `.deb` and an AppImage for Linux x86_64.**
   The `.deb` installs to a fixed path, declares its runtime libraries, registers the `minds://` handler and a menu entry, and runs Chromium's sandbox: its post-install script installs an AppArmor profile for the executable, and the app itself drops the `--no-sandbox` the probe launcher passes on Ubuntu 24.04 and later (decision 13).
   The AppImage is the portable fallback for non-Debian distributions.
   Both come from the same ToDesktop build, so they carry identical bytes for everything but the packaging.
2. **Stage every shipped target's binaries in `build.js` and select them with `targetOverrides`.**
   The build host's architecture no longer decides what ships.
   A shared payload (wheels, pyproject, latchkey, bundled config) is built once and copied into every per-target tree beside that target's native tools, so each target's list names exactly one source.
3. **macOS ships arm64 only.**
   The x64 and universal macOS targets were disabled in the ToDesktop dashboard on 2026-09-11.
   They were broken for the same staging reason and are documented as unsupported.
4. **Linux installs update only on an explicit click.**
   On macOS the downloaded update installs on the next quit.
   On Linux the app never arms `autoInstallOnAppQuit`; the update card and the Settings panel offer an install action, and only that click runs the installer.
   For the `.deb` the password prompt appears right after the click, which is the only place it makes sense.
5. **An AppImage run without `APPIMAGE` reports the updater as disabled**, not as an error.
   That is what an extracted or CI-launched run looks like, and nothing is wrong with it.
6. **Every channel entry names the platforms it publishes for.**
   `release-channels.toml` gains a required `platforms` list, and `publish.py` writes one manifest per listed platform.
   Absence is not expressible, because a build cut before this work has a Linux manifest whose binaries are arm64.
   The three current entries say `["mac"]`; the first Linux-capable build is listed for `["mac", "linux"]` on alpha and beta only.
7. **The download link learns two Linux platforms**, `linux-deb-x64` and `linux-appimage-x64`, with `linux` aliased to the `.deb`.
   Each resolves from `stable-linux.yml` by artifact extension.
   Until Linux reaches stable there is nothing to serve, so the route answers 404 for those platforms and records no download event.
8. **Every local backend stays selectable on Linux, with the form saying what is missing.**
   The backend probes Docker, gVisor, and Lima's host requirements and returns, per backend, whether it is available and the command that installs it.
   The create form shows the command with a copy button under the compute selector and on the local preset card, and the local preset picks the first backend that is ready.
9. **The AppImage registers its own desktop entry on first launch**, in the user's XDG data directories, so the `minds://` scheme and a menu entry work without an installer.
10. **The detached latchkey supervisor is left running on Linux**, as on macOS.
    Under an AppImage its Node runtime lives in a mount that disappears at quit, so a gateway respawn between app sessions fails until the next launch restarts the supervisor.
    Recorded as a known limitation; the next launch heals it.
11. **CI verifies the Linux artifacts on every build** by extracting both and asserting the staged executables are x86-64 ELF files that run, and by installing the bundled Python environment on Linux.
    The full launch-to-message flow on a Linux runner is a follow-up.
12. **`ELECTRON_BUNDLING_AUDIT.md` is deleted.**
    Its critical findings describe a build pipeline that no longer exists; the still-valid observations are folded into `docs/desktop-app.md`.
13. **The `.deb` relaunches itself without `--no-sandbox` when a sandbox is available.**
    ToDesktop's probe launcher runs `unshare -Ur true` from an unconfined `/bin/sh`, which fails under Ubuntu 24.04's AppArmor restriction even though the `.deb`'s post-install script has loaded a profile (`profile "minds" "/opt/Mind/minds" flags=(unconfined) { userns, }`) under which the executable itself can create user namespaces (verified on Ubuntu 26.04 on 2026-09-14: the binary launched directly runs with the namespace sandbox).
    ToDesktop's config offers no per-package `noSandbox` and no way to change the launcher, so the correction is made by the app: before anything else runs, `electron/linux-sandbox.js` checks for the flag, for the executable's profile in `/proc/self/attr/current` (a bare label only; a stacked `minds//&unconfined` label does not carry the permission) or for a root-owned 4755 `chrome-sandbox`, and relaunches with the flag removed and a `--minds-sandbox-relaunched` marker that makes a second relaunch impossible.
    The relaunch spawns the replacement from the browser process through a detached shell that waits for the old process to exit (`electron/linux-relaunch.js`), rather than `app.relaunch`: Electron's Linux relauncher is a helper that Chromium's `base::LaunchProcess` starts with `no_new_privs` (its default for child processes) and the replacement inherits that one-way flag, under which `pkexec` cannot elevate and every `.deb` update install fails with "pkexec must be setuid root" (verified on the test box on 2026-09-14).
    The same applies to the restart after any Linux update. electron-updater's `DebUpdater` restarts the app through `app.relaunch`, so the first update on the test box came back with `no_new_privs` set and could not have installed the next one; its `AppImageUpdater` starts the replaced file at once, before the old process has quit, so on the test box the new instance died on the single-instance lock, its arrival focused the window over the running-workspaces prompt (which then looked like a hang), and nothing came back once the prompt was answered. `installStagedUpdate` therefore switches the updater's own restart off for every Linux install (`relaunchedBy: 'app'`) and arms the same detached shell instead, from the AppImage file (no arguments; its launcher adds them) or the `.deb`'s executable; `electron/appimage-updater.js` subclasses electron-updater's AppImage updater to keep its file replacement and drop its start. The quit-time dialogs are owned by the app's most recent window so no raise can hide them.
    Setting `noSandbox: false` instead was rejected: it would break the AppImage on those distributions, which has no profile and cannot get one.

## Build pipeline

### Staged layout

`pnpm build` produces:

```
apps/minds/resources/
  darwin-arm64/payload/
    uv/ uv-shims/ git/ lima/ desync/ restic/ curl/
    wheels/ pyproject/ latchkey/      a copy of the shared payload
  linux-x64/payload/
    uv/ git/ lima/ desync/ restic/ curl/
    wheels/ pyproject/ latchkey/      a copy of the shared payload
```

The shared payload (every workspace package's wheel, `pyproject.toml` + regenerated `uv.lock` with the bundled `client.toml`, the flat latchkey bundle + its Electron-as-Node shim) is built once under `resources/shared/`, copied into every target tree by `copySharedPayloadInto`, and the staging copy removed so only the target trees count against the upload budget.
A name present in both a target's tools and the payload is an error, not an overwrite.

`todesktop.js` maps one tree per target, with no base `extraResources` list:

```js
targetOverrides: {
  mac: { arm64: { extraResources: [{ from: 'resources/darwin-arm64/payload/', to: '.' }] } },
  linux: { x64: { extraResources: [{ from: 'resources/linux-x64/payload/', to: '.' }] } },
},
```

**Warning:** every list's source must be a directory of the same name (`payload`, `TARGET_PAYLOAD_DIR_NAME` in `download-binaries.js`), differing only in its parent, and each list names exactly one.
ToDesktop's `targetOverrides` documentation requires it: "corresponding entries must use the same `to` value and source file or directory name; their `from` paths can differ".
`build_test.py` pins the count, the shared name, and the absence of a base or `platformOverrides` list.

The `platformOverrides` route was tried first and the build service never delivered the Linux list, in three shapes on 2026-09-11:

| Build | Base list | `platformOverrides.linux` list | Linux packages received |
|---|---|---|---|
| `260911mvzuhrage` | `shared/`, `darwin-arm64/` | `shared/`, `linux-x64/` | `shared/` only |
| `2609118jzsgber0` | `darwin-arm64/` | `linux-x64/` | nothing |
| `2609113pd1yg0nx` | `darwin-arm64/payload/` | `linux-x64/payload/` | the base list's bytes: arm64 Mach-O tools and `uv-shims` |

The mac app was complete in every case.
So a base entry reaches Linux only when the Linux list has an entry of the same name, and then with the base entry's bytes; the Linux list's own bytes never ship.

Each list lands its contents at the root of the packaged resources directory, so `electron/paths.js` resolves the same `<resources>/<tool>/...` paths it does today and needs no change.
Dev mode is unchanged: `ensure-binaries.js` still links `resources/<tool>` for the host, and a later `pnpm build` cleans and restages `resources/`.

The shipped targets are declared once, in `download-binaries.js`:

```js
const SHIPPED_TARGETS = [
  { platform: 'darwin', arch: 'aarch64', dirName: 'darwin-arm64' },
  { platform: 'linux', arch: 'x86_64', dirName: 'linux-x64' },
];
```

`downloadBinaries(resourcesDir, target)` takes the target explicitly; `build.js` iterates `SHIPPED_TARGETS`.
The `BINARIES` table's `isSupported` predicates already exclude `uv-shims` from Linux.
Lima and desync stay in the Linux set: Lima is a supported Linux backend once the host has QEMU and KVM (see "Local backend prerequisites").

### Format assertion

After staging each target, `build.js` calls `assertStagedExecutablesMatchTarget(dir, target)`, which reads the magic bytes of every binary's `requiredPath` and of the git payload's `libexec/git-core/git-remote-http` (the real remote helper; `git-remote-https` is one of the shim scripts) and refuses the build when the format does not match the target: `ELF` with machine `x86-64` for linux-x64, 64-bit Mach-O with CPU type `arm64` for darwin-arm64.
This is the check that would have caught the shipped AppImage.
`classifyExecutable(buffer)` is a pure function with a node unit test.

### Upload budget

`estimateToDesktopUploadBytes` prices every `from` entry across the top-level lists and every `platformOverrides` and `targetOverrides` list (`extraFileEntries`); each list's entries go to their own archive directory, so a source two lists name is priced twice.
`uploadSizeLimit` rises from 650 to 1100 MB: the two tool sets are 656 MB, and the shared payload (about 115 MB) ships once per target on top of that.
The first CI build with both targets estimated 785 MB with the shared tree uploaded once; with the payload copied into both trees the estimate is about 900 MB.

### Linux packaging config

```js
appBuilderLibVersion: '26.15.7',
linux: {
  category: 'Development',
  noSandbox: 'probe',
},
```

`26.15.7` is the newest app-builder-lib release older than the repo's 14-day dependency cooldown and satisfies the 26.9.0 floor `noSandbox` needs for AppImage.
Pinning it changes the macOS build too, which `minds-launch-to-msg.yml` verifies.
`@todesktop/cli` moves from 1.23.2 to 1.28.1 so `noSandbox: "probe"` is understood.
Deb dependencies stay at ToDesktop's defaults, which cover Electron's GTK, NSS, and AT-SPI libraries.

## Electron shell

### Install policy

A new pure module `electron/install-policy.js` answers how a downloaded update gets installed:

| Platform | Package | Policy | Password prompt |
|---|---|---|---|
| macOS | `.app` | `on-quit`: installs when the app quits, or on "Restart now" | no |
| Linux | AppImage | `on-request`: installs only from the install control | no |
| Linux | `.deb` | `on-request`: installs only from the install control | yes, from `pkexec` |

The package type is read from `<resources>/package-type`; a missing file means AppImage.
`updater.js` arms `autoInstallOnAppQuit` only under the `on-quit` policy, and `describe()` publishes `installPolicy` and `needsPasswordToInstall` so the renderer can word the offer.
`installNow()` installs the staged download through `install-policy.js`'s `installStagedUpdate()`; on Linux that is the only install path. A failure electron-updater reports during the install (a cancelled `pkexec` prompt, a `dpkg` refusal) leaves the app running with the download still staged, and travels to the renderer as the `{error}` payload of the `install-update` invoke, which the update card and the Settings panel show.

The update card reads "Installs when you restart" with a "Restart now" button under `on-quit`, and "Installs when you ask" with an "Install and restart" button under `on-request`, adding "you'll be asked for your password" for the `.deb`.
The Settings panel's status line and button change the same way.

### Updater availability

`isUpdaterUsable()` becomes `app.isPackaged && autoUpdater.isUpdaterActive()`.
On macOS that is the current behaviour.
On a Linux AppImage without `APPIMAGE` it is false, so the panel shows the updater as disabled rather than erroring every ten minutes.

### AppImage desktop entry

A new pure module `electron/linux-desktop-entry.js` renders a freedesktop entry for the running AppImage:

```
[Desktop Entry]
Type=Application
Name=Mind
Comment=Persistent, autonomous AI agents
Exec="<APPIMAGE>" %U
Icon=minds
Terminal=false
Categories=Development;
MimeType=x-scheme-handler/minds;
StartupWMClass=Mind
```

On every packaged Linux launch with `APPIMAGE` set, `main.js` writes the icon, scaled to 512px (the largest fixed size the hicolor theme indexes), to `$XDG_DATA_HOME/icons/hicolor/512x512/apps/minds.png`, the entry to `$XDG_DATA_HOME/applications/minds.desktop`, then runs `update-desktop-database` and `xdg-mime default minds.desktop x-scheme-handler/minds`, all best-effort and logged.
Writing on every launch keeps the entry pointing at the file's current location after an in-place update renames it.
The `.deb` ships its own system-wide entry from electron-builder and skips this.
A user with both installed sees the user-level entry win, which is the freedesktop precedence rule.

## Local backend prerequisites

### Backend

A new module `apps/minds/imbue/minds/desktop_client/local_prerequisites.py` probes the machine the backend runs on:

| Key | Available when | Install command (apt hosts) |
|---|---|---|
| `DOCKER` | `docker` on `PATH` and `docker version` answers within 3 s | Docker's convenience script, then `sudo usermod -aG docker $USER`; when `docker` is already on `PATH`, `sudo systemctl start docker` and the same `usermod` instead |
| `RUNSC` | `DOCKER` available and `docker info` lists a `runsc` runtime | gVisor's apt repository, `runsc install -- --overlay2=none`, daemon restart |
| `LIMA` | on macOS always; on Linux the host architecture's QEMU on `PATH` (`qemu-system-x86_64`, or `qemu-system-aarch64` on arm64) and `/dev/kvm` readable and writable | `sudo apt install` of the matching package (`qemu-system-x86` or `qemu-system-arm`), `sudo usermod -aG kvm $USER`; a `/dev/kvm` that does not exist at all gets no command, since nothing installs a device the kernel does not offer |

On macOS the table reports Docker the same way and Lima as always available, since the bundled `limactl` runs the VM through Virtualization.framework.
Hosts without `apt` get the vendor's install page instead of a command.
Every probe is bounded to 3 seconds and the docker daemon is probed once per request.

The create form's defaults response gains:

- `local_prerequisites`: the per-key availability, a one-line summary, the install command, and a docs URL.
- `local_launch_mode`: the launch mode the local preset selects -- the first available of Lima on macOS, or Docker then Lima on Linux, falling back to the platform's first choice when none is available.

### Frontend

- The local preset applies `local_launch_mode` instead of the hard-coded `LIMA`.
- A `PrerequisiteNotice` component renders the summary, the command in a code block with a copy button, and a "Check again" link that refetches the defaults.
- The advanced view shows the notice under the compute selector when the selected mode's prerequisite is unavailable, and under the runtime selector when `runsc` is selected but unavailable.
- The local preset card shows the notice when no local backend is available.
- Options stay enabled: the user installs the software, clicks "Check again", and creates without leaving the page.

## Release channels

### Declaration

```toml
[channels.alpha]
build_id = "..."
version = "0.5.3"
fallback_branch = "minds-v0.5.3"
rollout_percentage = 100
platforms = ["mac", "linux"]
```

`platforms` is required, non-empty, and drawn from `mac` and `linux`.
`publish.py` refuses an entry without it, exactly as it refuses a missing `rollout_percentage`.

### Publishing

For each listed platform, `manifest.py` fetches `latest-<platform>-build-<buildId>.yml`, rewrites its artifact references to absolute ToDesktop URLs, requires the extensions that platform's updater selects on (`.zip` and `.dmg` for mac; `.AppImage` and `.deb` for linux), stamps the rollout percentage, and uploads `<channel>-<platform>.yml`.
A listed platform with no ToDesktop manifest refuses the whole entry.
The Lima image gate runs once per entry, not per platform.
Every report line names the platform.
A channel manifest that exists in the bucket for a platform no entry lists is reported as still serving, per platform.

The client needs no change: electron-updater asks a generic feed for `<channel>-linux.yml` on Linux x64 by itself.

### First Linux release

The first build cut with this work is listed for `["mac", "linux"]` on alpha and beta.
Stable keeps `["mac"]` until a manual pass on Ubuntu 24.04 and Debian 12 covers both artifacts (install the `.deb`, run the AppImage), sign-in, a mind on each local backend the machine supports and one cloud backend, a message round trip, a `minds://` link, and an in-app update of each artifact type, through the `.deb`'s password prompt.
That pass is the item in `apps/minds/docs/deploy/next_deploy.md`, which is the checklist to follow.

## Connector download link

`accounts_web.py` generalizes the stable-link resolver to a platform table:

| Platform | Alias | Manifest | Artifact suffix | Fallback |
|---|---|---|---|---|
| `mac-arm64` | `mac` | `stable-mac.yml` | `-arm64.dmg` | pinned build, bumped at every stable promotion |
| `linux-deb-x64` | `linux` | `stable-linux.yml` | `.deb` | none until Linux reaches stable |
| `linux-appimage-x64` | | `stable-linux.yml` | `.AppImage` | none until Linux reaches stable |

The resolver caches per platform for a minute.
A platform whose manifest cannot be read and which has no fallback answers 404, and no download event is recorded.
The drift tests that pin the mac fallback to the stable entry apply to a Linux fallback once one is set.

During alpha and beta there is no public Linux download link.
Internal testers take the artifact URLs the CI job prints in its summary.

## CI verification

`minds-launch-to-msg.yml` gains a `linux_artifacts` job on `ubuntu-latest` that runs after `build` and in parallel with the macOS jobs:

1. Download the AppImage and the `.deb` for the build id.
2. Extract the AppImage with `--appimage-extract` and the `.deb` with `dpkg-deb -x`.
3. Run `apps/minds/scripts/verify-linux-artifacts.sh` against each tree, which asserts for `uv`, `git`, `git-remote-http`, `restic`, `desync`, `limactl`, and the two latchkey `curl` binaries that `file` reports an x86-64 ELF and that each one's version or help flag exits 0 (`git-remote-http` has none, so only its format is checked), asserts the `.deb` tree carries `resources/package-type` reading `deb`, and runs the bundled `uv sync --project resources/pyproject` into a temporary virtual environment followed by `uv run minds --help`, which proves the wheels and lockfile resolve on Linux.
4. Print both artifact URLs to the job summary.

`update_marker` and the Slack summary treat the new job like the existing ones.

## Docs and changelog

- `apps/minds/docs/desktop-app.md`: rewrite "Building for distribution", "Bundled binaries", and "How the shipped binaries are chosen" for the per-target layout; replace the macOS Intel section with the dashboard decision; add a Linux section covering the two artifacts, installation, the runtime libraries an AppImage needs, prerequisites for local backends, updates and the password prompt, deep links, and the known latchkey limitation.
- `apps/minds/docs/deploy/ops/app-release.md`: `platforms` in step 9, the Linux manifests in the confirmation commands, and the Linux platforms in "The public download link".
- `apps/minds/docs/deploy/setup/update-feed.md`: the example entries gain `platforms`.
- `specs/minds-release-channels/spec.md`: decision 7 resolved, Linux removed from "Not yet built", phase 5 updated.
- `apps/minds/docs/deploy/next_deploy.md`: the manual Linux pass before a stable promotion.
- Changelog entries under `apps/minds/changelog/`, `dev/changelog/`, and `apps/remote_service_connector/changelog/`.

## Testing

- `apps/minds/scripts/build_test.py`: the config declares the Linux override, every `from` lives under `resources/`, `noSandbox` is `probe`, `appBuilderLibVersion` is pinned at or above 26.9.0, `build.js` iterates `SHIPPED_TARGETS` and calls the format assertion, and the upload estimator prices every override list (a source two lists name is priced once per list).
- `apps/minds/test/unit/executable-format.test.js`: the magic-byte classifier on ELF and Mach-O headers.
- `apps/minds/test/unit/install-policy.test.js`: the policy table and package-type reading.
- `apps/minds/test/unit/linux-desktop-entry.test.js`: the rendered entry and the XDG paths.
- `apps/minds/frontend/src/views/pages/create/form-model.test.ts`: the local preset follows `local_launch_mode`; the prerequisite lookups.
- `apps/minds/imbue/minds/desktop_client/local_prerequisites_test.py`: the probes with fake `which` and command runners, the remediation text per platform and package manager, and the preset choice.
- `apps/minds/imbue/minds/desktop_client/ui_api_create_test.py`: the defaults carry the new fields.
- `scripts/release_channel/manifest_test.py` and `publish_test.py`: a captured Linux manifest, per-platform filenames and extensions, the `platforms` field's refusals, a listed platform with no manifest, and per-platform undeclared reports.
- `apps/remote_service_connector/.../accounts_web_test.py`: the Linux platforms, the alias, the 404 without a fallback, and the per-platform cache.
- The CI job above is the acceptance test for the artifacts themselves.

## Failure modes

| Failure | Behaviour | Mitigation |
|---|---|---|
| ToDesktop refuses the larger upload | `pnpm dist` fails before any build | Raise `uploadSizeLimit` if ToDesktop's own limit allows, or trim the payload; the estimator's number in the log says by how much |
| A Linux binary is staged with the wrong format | Build fails at the format assertion | None needed; that is the assertion's job |
| `package-type` is absent from the `.deb` | The AppImage updater runs inside a `.deb` install and fails at install time | The CI job asserts the file; `on-request` policy means the failure surfaces only on an explicit click |
| AppImage run from an extracted tree | Updater reports disabled | Expected; documented |
| Docker daemon unreachable at create | Notice with the install command before submit; create fails clearly if submitted anyway | Probe on the defaults request |
| Gateway dies between AppImage sessions | Latchkey unavailable until the next launch | Next launch restarts the supervisor |
| Update installed while a second AppImage copy is in use | Only `$APPIMAGE` is replaced | Documented |

## Open items

Resolved 2026-09-11 by build `260911ij8wwfg3w`, the first whose `linux_artifacts` job passed: the `.deb` is served at `dl.todesktop.com/<app>/builds/<id>/linux/deb/x64`, the AppImage carries no `package-type` file and the `.deb` reads `deb`, every bundled tool in both packages is a runnable x86-64 ELF, and the bundled wheels resolve into a working `minds` on Ubuntu.
A directory listed in two lists is uploaded once per list, which no longer matters since each list names one directory.

- The full launch-to-message flow on a Linux runner with Docker.
- An x86_64 pre-baked Lima image, so Linux Lima creates take the fast path.
- A packaged linux/arm64 build. `download-binaries.js` provisions linux/arm64 binaries for dev-mode runs only (a Raspberry Pi; see `apps/minds/docs/raspberry-pi.md`).
