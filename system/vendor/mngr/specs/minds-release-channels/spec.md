# Release channels for the minds desktop app

**Status:** phases 1-2 implemented. This document has been corrected against the
code; every claim below either matches what shipped or is marked as not yet
built. [Findings from implementation](#findings-from-implementation) records what
building it disproved, and [Verification log](#verification-log) records what was
actually observed versus merely reasoned about.

## Overview

Different populations want the app to change under them at different rates.
External users want a stable build that moves roughly monthly.
Internal employees want the newest green build, roughly daily.
Beta sits between them.

This spec adds three user-selectable **release channels** -- `stable`, `beta`, `alpha` -- to the single existing minds desktop app.
There is one installed `Minds.app`, one ToDesktop app id (`26032588hqdzk`), and one bundle id.
A user changes channel from Settings; no separate "Minds Alpha" app is ever installed.

The design leans on ToDesktop for everything expensive and adds one thin layer for the one thing ToDesktop does not do.

- **ToDesktop keeps owning** the build farm, Apple Developer ID signing, notarization, deep-signing every Mach-O under `Contents/Resources` (including the entitlements plist that grants `com.apple.security.virtualization`), artifact hosting, and the public download page.
  None of that is touched.
- **We own one small YAML file per channel** -- `stable-mac.yml`, `alpha-mac.yml` -- naming which already-built, already-notarized ToDesktop build that channel currently points at.
  ToDesktop's "Release" action no longer publishes to clients (revised 2026-08-13; it began as exactly the stable channel).
  One more Release is still required, though: nothing shipped so far has this code at all -- every install in the field runs `@todesktop/runtime` against ToDesktop's feed, so only the Release action can hand them a build that reads our manifests.

### Why ToDesktop cannot express this itself

Verified against the shipped app and the live service (evidence in [Appendix: verified facts](#appendix-verified-facts)):

- ToDesktop resolves every channel request against **one released build per app**.
  Requesting `beta-mac.yml` returns a 404 whose body leaks the server's rewrite -- `26032588hqdzk/beta-mac-build-260801n4rh5zv5d.yml not found` -- naming the *same* build id as `latest`.
  The channel name is passed through to a filename; it does not select a build.
- The release action is per-build with three modes: full release, partial-by-IP, and partial-by-platform.
  There is no percentage rollout, no rollback, and no channel.
- `@todesktop/runtime` has no channel API; `ToDesktopRuntimeParams` is `{autoUpdater, autoCheckInterval, shouldAutoCheckOnLaunch, customLogger, updateReadyAction}`.
- ToDesktop's own answer is "use a second app id", which contradicts the one-app requirement above.

### Why the thin layer is cheap

- ToDesktop already publishes a **complete per-build update manifest** at a public URL, for builds that were never released: `https://download.todesktop.com/<appId>/latest-mac-build-<buildId>.yml`.
  It carries version, per-arch file names, sizes, and sha512 digests.
- Every ToDesktop build stays **permanently and publicly downloadable** whether or not it was released.
- `GenericProvider` resolves the channel file as `${updater.channel || config.channel}-mac.yml`, and `newUrlFromBase` is `new URL(path, base)` -- so an **absolute** `url:` in a manifest overrides the feed's base URL. Verified in the `4.6.5` the runtime bundles and again in the `6.8.9` the app now depends on.

So publishing a channel is: fetch ToDesktop's per-build manifest, rewrite its relative `url:` fields to absolute `https://download.todesktop.com/<appId>/...` URLs, and upload the result as `<channel>-mac.yml`.
No re-hosting of 400 MB artifacts, no filename guessing, and correct digests for free.

**Warning:** the rewritten URLs must keep the `.zip` filename form (`https://download.todesktop.com/<appId>/Minds%20<version>%20-%20Build%20<buildId>-arm64-mac.zip`).
`MacUpdater` selects via `findFile(files, "zip", ["pkg", "dmg"])`, which matches on the URL **pathname extension**.
The alternative `https://dl.todesktop.com/<appId>/builds/<buildId>/mac/zip/arm64` form has no `.zip` suffix and would only be selected by the not-pkg-and-not-dmg fallback -- working by accident, and breaking silently the day a fourth artifact type appears.

## The model

### A line is not a channel

- A **line** is a maintained sequence of versions sharing a major.minor, with a git branch behind it.
- A **channel** is a subscription that resolves to exactly one build at a time.

Under fix-forward, all three channels draw from one line and differ only in how far behind head they sit.
Under release branches (Chrome's model), channels map to different lines.

**The channel artifact is a pointer to a build id, so it is line-agnostic.**
Both regimes produce byte-identical file formats.
Only the promotion policy differs, and only in whether the target build already exists (pointer move) or must be cut from a branch (new build).

**Requirement:** no artifact, config file, or code path may encode "lag" -- no "days behind", no "promote after N".
Soak duration lives in the promotion job, never in the format.
Holding this line makes release branches a process change later rather than a rewrite.

### Invariants

1. **Total order.** For any two builds a user could move between, semver comparison must decide correctly.
2. **Superset.** If version B is newer than version A, B contains every user-visible fix in A.
   Under fix-forward this is automatic.
   Under release branches it is maintained by landing on `main` first and cherry-picking down, never the reverse.
3. **Channel ordering.** `version(alpha) >= version(beta) >= version(stable)`, always.
   Enforced structurally: the moment a `release-0.N` branch is cut, `main` bumps its minor, so release branches only ever bump patch inside an already-shipped minor.
4. **No downgrade, ever.** Moving to a slower channel parks the user at their current version until that channel overtakes them.
   `allowDowngrade` is `false` on every code path.

Invariant 4 is not a preference.
Nothing in `~/.minds` has a down-migration, so a downgrade is a data hazard, not a degraded experience.

### Version numbering: stamped once at cut

Each cut gets a plain semver version (`0.4.17`) with **no prerelease suffix**, stamped once and never re-stamped.
Promotion to a slower channel points that channel at the same build id -- the same signed, notarized bytes.

This is what makes promotion a pointer move rather than a build:

- With prerelease suffixes (`0.5.0-alpha.7` promoted as a rebuilt `0.5.0`), the version string is welded into the bytes (`package.json`, the ToDesktop artifact filenames, the Sentry release id).
  Promotion becomes a rebuild, a re-sign and a fresh notarization, so **what ships is an artifact that has not itself been run by anyone**.
  The delta is only the version string, by construction, so this is a process cost rather than a behavioural risk: the objection is the rebuild and the untested artifact, not that the code differs.
  It is also the difference between a promotion that takes seconds and one that takes a build.
- With one stamp, promotion to stable is repointing a pointer at the build that has been running in alpha for a month.

Most cuts never reach stable, so stable's version sequence is sparse (`0.4.0`, `0.4.12`, `0.4.31`).
That is correct and matches Chrome, where Canary version numbers race ahead and Stable lands on a subset.

**Note:** this reverses a decision taken 2026-07-29 to use the semver prerelease field.
That decision was made while assuming alpha was a separate line; once promotion is a pointer move, a prerelease suffix costs a rebuild for a delta that is only the version string.

Sculptor takes the other path -- `0.5.0rc7` republished as `0.5.0` -- and it works.
The tradeoff is real but small in both directions: that route pays a rebuild and ships bytes nobody ran, this one gives up the ability to mark a build as a candidate in its own version string.

### How a version gets assigned (decided 2026-08-13)

**Every channel cut is a tagged release, alpha included.** An alpha cut runs the same procedure as a stable one in `apps/minds/docs/release.md` -- bump, vendor-sync, prove green, tag both repos -- and differs only in which channel's pointer moves at the end.

The version is **picked**, not derived. `FALLBACK_BRANCH` names a `minds-v<version>` tag **that does not exist yet**; step 7 creates it. That forward reference is what makes the whole thing possible:

> A pointer that names a *SHA* has no fixed point. `build_info.py` is inside the tree `git archive` vendors into dwt, so committing `FALLBACK_BRANCH = <dwt SHA>` changes the content that dwt vendored, which changes the dwt SHA, which changes what must be committed. `D = f(content(B))` while `content(B)` names `D`. A **tag name** is chosen in advance and derived from nothing, so the cycle never forms.

**Race safety comes from the bump commit, not from the tag.** Two concurrent cuts both read main's `package.json`, both pick the same next version, and both try to push the bump -- git rejects the second as non-fast-forward. It re-reads, sees the new version, and takes the next one. The collision surfaces in seconds at bump time rather than twenty minutes later at tag time.

**A failed cut burns its version; it is never reused.** Reuse would mean no bump commit, and the bump commit is the arbiter -- two cuts could then reuse the same number and collide at tagging. Gaps in the tag sequence are harmless. After a burn, main's `package.json` names the last *attempted* cut, so anything asking "what is the current release" must read tags.

Cuts are already serialized: `minds-launch-to-msg.yml` holds the `mac-runner` concurrency group, shared with `minds-runner-reset.yml`, because both need the single self-hosted Mac.

## Expected behavior

### Channels

| Channel | Cadence | Fed by | Default for |
|---|---|---|---|
| `stable` | roughly monthly | a PR editing `release-channels.toml` | everyone; installs predating channels still read ToDesktop's feed |
| `beta` | roughly weekly | a PR editing `release-channels.toml`, by policy after a soak on `alpha` | nobody -- served by the same machinery but listed for no one (decision 4) |
| `alpha` | every green build, roughly daily | a PR editing `release-channels.toml` | opt-in |

Every channel moves the same way: the promote workflow applies whatever
`release-channels.toml` declares. The soak is a policy a reviewer applies, not a
timer, and promoting `alpha` on a green build is a human opening that PR -- see
[Not yet built](#not-yet-built) for both.

- **Every existing install is on `stable` and its behavior does not change.**
  Absence of a stored preference means `stable`.
- New users download from ToDesktop's page, receive the released build, and are on `stable`.
- An internal user installs the normal app and switches to `alpha` in Settings.
  Because `version(alpha) > version(stable)` always holds, the next check offers an update immediately.
  No separate installer or direct build link is needed.

### Switching channels

- **To a faster channel** (`stable` to `alpha`): takes effect on the next check, which the switch triggers immediately.
  An update is offered because the faster channel is at or ahead of the current version.
- **To a slower channel** (`alpha` to `stable`): the user keeps the version they are running and receives nothing until that channel overtakes them.
  This state is **parked**, and it is a first-class status, not "up to date".

**Parked is the highest-risk state in the feature and today's UI would render it as praise.**
Requirements:

- The switch is confirmed before it is applied, and the dialog states the cost: "stable is on 0.4.12. You are on 0.4.30, so you will not be updated until it catches up."
  Producing that sentence requires fetching the target channel's manifest *before* writing the preference, so the switch is an async operation that can fail and be cancelled.
- Nothing warns about the resulting state (revised 2026-08-13; it began as a persistent banner). Being ahead of your channel is temporary and self-correcting -- the channel catches up -- so "you are not receiving updates" reads as a fault when nothing is broken. Every channel in the panel already prints the version it serves, which is the same fact without the alarm.
- "Check for Updates..." while parked never says "You're up to date." The panel reports when the check ran, because parked and up-to-date both leave the screen unchanged, and a check that worked is otherwise indistinguishable from a button that does nothing.

**Parking is not only caused by switching.** Rolling a channel back (see [Failure modes](#failure-modes)) parks everyone who already took the withdrawn build.
That is the case a recovery action could not have served anyway: no channel is serving a version at or above the running one, so there would be nothing honest to offer.
The versions printed beside each channel state the situation on their own.

### Update checking

- On launch and every 10 minutes thereafter, plus on demand from the menu item.
- The check resolves the feed from the stored channel, applies the fixed updater configuration, then checks.
- Failure to reach a channel feed is reported as an error, never as "up to date".

## Architecture

### Feed resolution

| Channel | Base URL | electron-updater `channel` | Manifest owner |
|---|---|---|---|
| `stable` | `https://<releases-host>/` | `stable` | this repo's promote workflow |
| `beta` | `https://<releases-host>/` | `beta` | this repo's promote workflow |
| `alpha` | `https://<releases-host>/` | `alpha` | this repo's promote workflow |
| `stable`, no host configured | `https://download.todesktop.com/26032588hqdzk` | `latest` | ToDesktop |
| a fast channel, no host configured | none -- `feedForChannel` raises | -- | -- |

Serving stable from the same host puts `<releases-host>` in the path for every user, which stable did not have when it read ToDesktop's feed directly (revised 2026-08-13; see decision 6).
An unreachable host stops update *checks* -- the app keeps running the version it has -- and the artifacts still come from ToDesktop either way.

`<releases-host>` comes from `ClientEnvConfig.update_feed_base_url` in the tier's `client.toml`, mirroring `lima_image_base_url`: a public URL, optional, and absent means the feature is simply off.
A build with no value configured offers **stable only**, served by ToDesktop's feed, and still auto-updates.
Requesting a fast channel without one raises rather than silently falling back to stable.

It is a Cloudflare R2 bucket -- `minds-update-feed-<env>`, served at `updates.<domain>` in production and `updates-<env>.<domain>` elsewhere -- provisioned by `scripts/r2/setup_tier.py --kind update-feed`.
The bucket holds only the channel manifests -- roughly a kilobyte each -- because artifacts stay on ToDesktop's CDN.

The custom domain is required for **permanence**, not throughput: this URL ships inside every binary via `client.toml`, so every build ever released keeps requesting that exact hostname forever.
It has to be a name we control and will never need to change; an `r2.dev` URL is account- and bucket-derived.
(The rate-limit argument that requires a custom domain for the Lima image store does not apply here -- that is ~65,000 chunk requests per extract, against one small file polled every ten minutes.)

Production points at it: `update_feed_base_url = "https://updates.imbueminds.com"` is committed into the production `client.toml`, which `minds-update-feed-production` serves once `setup_tier.py --kind update-feed` has provisioned that bucket and hostname on the production Cloudflare account.
That provisioning is an operator step outside this repo and is not yet proven -- see [Verification log](#verification-log).
Because that URL is baked at build time, only builds cut **after** that commit can offer a channel beyond stable; every already-shipped install stays stable-only for good.

### One updater driver

The app drives `electron-updater` directly for all three channels and does **not** use `@todesktop/runtime`'s auto-updater.

This is forced, not stylistic:

- ToDesktop's updater calls `getReleaseStatus` for the **running** build and sets `_isActive = false` when it is not released, never constructing its agent.
  Alpha builds are unreleased by construction, so alpha users would have a permanently dead updater.
- Its `UpdaterAgent` constructor sets `electronUpdater.autoUpdater.allowDowngrade = true` on the shared singleton -- the exact opposite of invariant 4 -- from inside an **async** `_init()` that awaits `will-finish-launching` and an HTTP round trip, so it can land *after* our configuration and silently clobber it.

`todesktop.init()` is therefore never called.
The runtime is still **imported**, deliberately: importing arms ToDesktop's build-time smoke test, which runs under `TODESKTOP_SMOKE_TEST` and drives its own `_init()`.
Constructing the `ToDesktop` object creates no updater -- only `init()` does -- so the import costs nothing at runtime.
Nothing else the app uses comes from the runtime (`isInstalledUsingWindowsMSI` is Windows-only and minds ships no Windows target).

**Requirement:** `electron-updater` is a **direct** dependency of `apps/minds`, pinned to `6.8.9`.
It arrives transitively through `@todesktop/runtime`, but pnpm's store is not flat, so `require('electron-updater')` does not resolve from a package that has not declared it -- the app would crash on startup.
It was first pinned to `4.6.5`, the version the runtime resolves, to keep one `autoUpdater` singleton; that reason turned out not to apply, because the runtime is never `init()`ed and so never constructs an updater at all. Two copies exist on disk and only ours is reachable.

**Requirement:** the updater configuration is applied **immediately before every check**, in this order:

```js
updater.channel = feed.channel;   // MUST be first
updater.allowDowngrade = false;
```

electron-updater's `channel` setter ends with `this.allowDowngrade = true`, so the reverse order silently re-enables downgrades.
See [finding 1](#findings-from-implementation).

**Requirement:** checks are serialized through the app's own promise chain.
`checkForUpdates()` returns an already-in-flight promise when one exists, which would hand a caller a result computed under the *previous* feed configuration -- so "configure, then check" is only atomic if nothing else can interleave.

**Requirement:** the check -- and only the check, never the download -- is bounded by a deadline.
Serializing makes one unsettled promise everyone's problem, and electron-updater has no working timeout under Electron: `builder-util-runtime` arms its 60s socket timeout inside `request.on('socket')`, which `net.ClientRequest` never emits.
A stalled connection would otherwise hold the chain for the life of the process, with the status stuck on `checking` -- a hang is not a rejection -- and every control in the Settings panel disabled with nothing said.
See [finding 12](#findings-from-implementation).

**Requirement:** an update counts as available only when electron-updater says so, through `isUpdateAvailable` on the check result -- the same branch of `doCheckForUpdates` that constructs the `cancellationToken`.
`updateInfo` is the parsed manifest and is populated on **every** successful check, including when the feed is behind the running build.
See [finding 2](#findings-from-implementation).

**Requirement:** switching channels empties the updater cache, via `getOrCreateDownloadHelper()` rather than the `downloadedUpdateHelper` field -- that field stays null until this process downloads something, so reading it would no-op on the case that matters, an artifact cached by a previous run.
All channels share one `updaterCacheDirName` (`minds-updater`), so the previous channel's artifact would otherwise sit there for a later download to reuse.

A download that already **completed** is not taken back, and cannot be: `downloadAndOffer` arms `autoInstallOnAppQuit` before calling `downloadUpdate()`, and MacUpdater reads that flag once as the download completes to decide whether to hand the zip to Squirrel.
Switching therefore changes what the app asks for next, not what is already on its way in.
That is not a backwards move -- `allowDowngrade` is false, so a staged artifact is never older than the running build, which is what the switch confirmation tells the user.

The channel should also be reported to Sentry as a tag alongside the existing release id and `git_sha`.
Without it, crash volume cannot be sliced by channel, and "is alpha worse than stable" is unanswerable -- which is the only question that makes a soak-based promotion policy meaningful.
**Not yet implemented.**

### Channel preference storage

Stored as JSON at `<paths.getDataDir()>/update-channel.json` (`~/.minds/`, or `~/.minds-<env>/`), outside the app bundle.
`electron/update-channel.js` creates the directory if the app has not yet.

**Warning:** ToDesktop stamps `channel: latest` into `app-update.yml` in **every** build.
After any update installs, the new bundle again says `latest`.
The stored preference must be re-read and re-applied on every launch, or users silently fall back to stable on their first update -- a known electron-builder failure class ([electron-builder#5786](https://github.com/electron-userland/electron-builder/issues/5786)).

**Requirement:** the stored value is validated against the known channel set on read.
An unrecognized value resolves to `stable` and is logged, never cast through to a feed URL.

### Publishing a channel

The promote job is a pure metadata operation:

1. `GET https://download.todesktop.com/<appId>/latest-mac-build-<buildId>.yml`.
2. Rewrite each `files[].url` and the legacy top-level `path` to the absolute `https://download.todesktop.com/<appId>/<filename>` form, preserving the `.zip` / `.dmg` extension.
3. Leave `version`, `sha512`, `size`, and `releaseDate` byte-for-byte unchanged.
4. `PUT` the result to `<releases-host>/<channel>-mac.yml`, with a short `Cache-Control` max-age.

The manifest is the only mutable object in the system and every client polls it every 10 minutes, so a long CDN TTL silently becomes the promotion latency.
Artifacts are immutable and content-addressed by digest, so they cache indefinitely; only the manifests need a short TTL.

`stable` is published exactly this way too (see [decision 6](#decisions-owed)), so one manifest format and one gate set cover every channel.

The **Release** button in the ToDesktop dashboard is still needed once, for a different reason: every install in the field predates this code and updates through `@todesktop/runtime` against ToDesktop's feed, so only that action can hand them a build that reads our manifests.
After it, the Release action governs only ToDesktop's own hosted download page.
It stays a click because `todesktop release` is auth-blocked for this app.

### What a "cut" is, and the coupled artifacts

A minds release is not a binary.
It is the triple (ToDesktop build, `FALLBACK_BRANCH` dwt tag, published Lima image), and `FALLBACK_BRANCH` is baked into the binary from `apps/minds/imbue/minds/build_info.py`.
Because the dwt vendor-match invariant requires `system/vendor/mngr` to be the exact archive of the paired mngr SHA, **every cut changes the template, so every cut needs its own dwt tag** -- the existing per-release procedure, unchanged in shape but run roughly 20 times a month instead of once.
Those tags are immutable forever, because shipped binaries resolve `FALLBACK_BRANCH` against them at runtime.
This is an accepted cost, not an open question, but it is the reason a daily cut has to be fully automated rather than operator-driven.

The Lima image is the part that does not come for free.
A tier whose `client.toml` sets `lima_image_base_url` asks for an image keyed to the binary's `FALLBACK_BRANCH`.
If no image exists for that tag, the client gets `VERSION_UNAVAILABLE` and **silently** falls back to building in-VM: creates go from roughly 45 seconds to roughly 5 minutes, nothing turns red, and nobody finds out.

**Requirement (the gate that matters regardless of which option is chosen below):** the promote job must verify `GET <lima_image_base_url>/manifests/<FALLBACK_BRANCH>/root.json` returns a manifest naming that version with an entry for every shipped arch, **before** pointing a channel at the build.
As built, which arches count is per-invocation (`--arch`, `aarch64` by default) rather than derived from what the build ships, so an x86_64 image is gated only when the run asks for it.
A missing or mismatched manifest fails the promotion loudly.
This converts the single worst silent failure in the system into a hard gate.

## Changes

### New: `apps/minds/electron/update-channel.js`

The channel rules, with **no `electron` import at all** -- not just no electron-updater.
`paths.js` pulls in `app`, so taking the data directory as a parameter is what keeps this unit-testable under plain node.

Holds: the channel list and default, preference read/write (with the reason a fallback happened), feed resolution, `applyFeedToUpdater` (the ordering rule), and `computeUpdateStatus`.

`applyFeedToUpdater` lives here rather than in `updater.js` precisely so the ordering rule can be tested without Electron; that is the single most important line in the feature.

### New: `apps/minds/electron/updater.js`

Owns the lifecycle: serialized checks, the launch and interval checks, the on-demand menu check, downloading and offering an update, channel switching, and `peekChannels`.

Replaces the behavior `@todesktop/runtime`'s `Notifier` gave via `updateReadyAction: {showInstallAndRestartPrompt: 'always'}`.

`peekChannels` returns `{version, wouldPark}` per channel rather than raw versions, so semver comparison stays in one place and the renderer only renders the answer.

### `apps/minds/electron/main.js`

- Drop the `todesktop.init({updateReadyAction})` call; keep a bare `require('@todesktop/runtime')` for the build smoke test, with a comment saying why it is not `init()`ed.
- `triggerUpdateCheck` opens the panel and checks there: it focuses a window, navigates it to `/settings?section=updates`, then calls `updater.check()`. It answers in no dialog of its own, because the panel already reports the result, the version each channel serves, and when the check ran -- and two surfaces answering one question is how they drift apart.
- `updater.init({onStatus: broadcastUpdateStatus})` in `onReady`; status is pushed to every live window, so a panel or a shell already on screen reflects a check the moment it settles.
- Five IPC handlers: `get-update-state`, `peek-update-channels`, `set-update-channel`, `check-for-updates`, and `install-update` for the update card's Restart control.
- The message "The auto-updater is disabled until this build is released to the latest channel" is deleted -- it was false for alpha and beta, and its old sibling branch was a bug (see [finding 2](#findings-from-implementation)).

### `apps/minds/package.json`

Adds `electron-updater@6.8.9` (exact) and `semver` as **direct** dependencies. See [finding 3](#findings-from-implementation).

### `apps/minds/electron/preload.js`, `frontend/src/electron-bridge.ts`, `frontend/src/models/settings.ts`, `frontend/src/views/pages/settings/SettingsSections.ts`

An "Updates" settings section: current version, status line, one radio row per available channel with a blurb saying what that channel is for -- never how often it ships, which is a promise the release process has not made -- and the version it currently serves, and a "Check now" button.

The section is hidden entirely in the browser build (`model.visibleSections` filters on `electronBridge.isDesktop`), and every bridge method is optional so a preload predating channels degrades to null rather than throwing.

A switch that would park the user goes through a confirmation modal naming both versions; a switch that would not is applied immediately.

`frontend/src/views/pages/SettingsPage.ts` reads `?section=`, on update as well as on init, so the menu bar's "Check for Updates..." opens the Updates panel on a window already showing Settings. The name is validated against `visibleSections`, and the last one acted on is remembered, so an unchanged query does not drag the panel back off whatever the nav was just clicked to.

### New: `frontend/src/views/shell/UpdateReadyCard.ts`, `views/shell/update-ready.ts`, and the shell that floats it

A downloaded update is offered as a card in the bottom-right corner of the window, not as a dialog over it. It names the version, says it installs on restart, and offers "Restart now" and a dismiss -- nothing behind it is blocked while it is up, because the update installs on the next restart whether or not it is ever touched.

`update-ready.ts` holds the state. It **seeds** from `getUpdateState()` as well as listening for pushes, because a status is broadcast once at the moment it changes and the window is not always listening then -- a download that finishes while the splash screen is up would otherwise be announced to nobody. It ignores `checking`, which is pushed at the top of every one of them, so the card does not blink out and back on a ten-minute timer. Dismissal is per version and per window, and nothing is persisted: the card is regenerated from a status push that repeats on the next check.

`views/shell/Shell.ts` registers the listener once and positions the card; `views/pages/DevStyleguide.ts` catalogues it with a made-up version, so its appearance can be worked on without an update existing. The card itself carries no position, which is what lets both callers place it.

### `apps/minds/imbue/minds/config/data_types.py`, `imbue/minds/envs/local_store.py`

`ClientEnvConfig.update_feed_base_url`, alongside `lima_image_base_url` and emitted by the dev-env writer on the same "only when set" rule.

### `scripts/r2/client.py`

`scripts/r2/` owns everything about R2 itself -- provisioning in `setup_tier.py`, connecting in `client.py` -- and each feature directory owns what it publishes into its bucket.
So the credential read and the boto3 client have one definition, shared by the release-channel publisher and the Lima image publisher, which previously carried a copy each and had already drifted: one named the missing environment variable, the other raised a bare `KeyError` that boto3 then dressed up as an endpoint failure.

### New: `scripts/release_channel/manifest.py`

At the repo root next to `scripts/lima_image/publish.py`, which is the existing home for operator-run release tooling -- **not** `apps/minds/scripts/`, which holds build-time Node scripts.

Fetches ToDesktop's per-build manifest, rewrites its URLs to absolute, and uploads it as `<channel>-mac.yml` with a short `Cache-Control`. Gates before writing anything: the Lima image manifest must exist for the build's `FALLBACK_BRANCH` with each arch the run names, on a tier that configures an image store; the channel must not move backwards without `--allow-rollback`; every rewritten URL must keep a `.zip`/`.dmg` extension; and the version must be plain `X.Y.Z`, which is where the stamp-once rule is mechanically enforced.

It is a library, not a command: it has no CLI, because a second entry point would be a second gate set to keep in step with the reviewed one.

Network access is injected -- `fetch: Fetch = http_get` for the gates, `make_client: MakeS3Client = r2_client` for the upload -- so both are testable without monkeypatching.

"What does this channel serve now" has two sources and they are not equally good.
The manifest is uploaded with a short `max-age`, so reading it back through the public feed inside that window returns the *previous* promotion -- and the rollback gate would then compare against a version the channel has already left, and wave through the backwards move it exists to refuse.
So a run holding the bucket credential reads the object itself, where R2 is read-after-write consistent.
`read_current_channel_manifest` takes which source as a `from_bucket` flag rather than as an injected reader: the choice is made once, from the environment, in `publish.py`'s `main`, which also says out loud which one it used -- a credential-less dry run and the publish that follows it can differ.
The public feed stays the fallback for the `validate` job, which is the one run that can afford a stale answer: it publishes nothing, so the worst it costs is a wrong preview.
Clients are unaffected either way -- they keep fetching the CDN copy, which is what the TTL is for.

### New: promotion is a pull request

`manifest.py` is the single-channel primitive, and has no entry point of its own. What operators touch is a declarative file, so a promotion is reviewable before it takes effect and the channel's history is the file's history.

A channel moves only by repointing its entry. **Removing an entry withdraws nothing** -- no manifest is ever deleted, so the channel keeps serving its last build, and the run names it rather than reporting a promotion. So `git revert` is the undo only between two builds carrying the same version, which is the ordinary `alpha` case; reverting a version bump is refused unless `--allow-rollback` is passed, and CI never passes it, so a backwards withdrawal is run by hand (see `apps/minds/docs/release.md`).

- **`apps/minds/release-channels.toml`** declares which build each channel serves (`build_id`, `version`, `fallback_branch`), `stable` included. `beta` has no entry: it is publishable, and gets one once alpha and stable are proven (see [Decisions owed](#decisions-owed)).
- **`scripts/release_channel/publish.py`** reads that file and makes it true, running every `manifest.py` gate per entry plus two the primitive has no basis for. The declared `version` must equal what the build actually is, or the diff a reviewer approves could say something different from what gets published. And `fallback_branch` must be `minds-v<that version>`: nothing here can read the tag baked into the build, so leaving the previous release's tag beside a bumped version would point the image gate at the previous release's image, find it, and pass -- the silent failure that gate exists to convert into a loud one, reported green. Re-running a promotion that is already applied is a no-op, decided by comparing the manifest it would publish against the one the channel serves -- not their versions, because a version is stamped once at cut and every build until the next one repeats it, so the ordinary `alpha` promotion is a new build at the version already served.
- **`scripts/release_channel/resolve_tier.sh`** derives the app id, bucket, feed URL and image-store URL from files already in the repo, so the workflow carries no copies that can drift from the tier's own config. Bare values, never ready-made flags: the caller builds its own argv, so a config value carrying a space stays one argument instead of splitting into extra flags on a credentialed command. An unset `update_feed_base_url` is the honest "this tier serves no manifest yet" state, and the caller skips publishing rather than inventing a URL.
- **`.github/workflows/minds-release-channels.yml`** runs it, split by trust: the `validate` job runs on the PR with **no credentials**, because every gate reads a public URL; only the push-to-main `publish` job takes the R2 credential, behind the `minds-release` environment.

### Not yet built

- **Automatic alpha cuts. Planned next, once this lands.** Alpha's cadence is a policy nobody enforces: promotion is a human editing `release-channels.toml`, exactly as for stable, so "daily" holds only while someone does it daily. What a job has to do end to end is the release procedure itself -- bump `version` and `FALLBACK_BRANCH`, refresh dwt `system/vendor/mngr`, prove the pair green, tag both repos, then move the pointer -- of which only the last step is new work. Three parts are awkward and should be designed before any of it is written:
  - **Tagging needs a cross-repo credential.** The tag lives in default-workspace-template, and the same constraint that blocks the dwt-tag gate applies: the promote job's token is scoped to this repository.
  - **The bump commit is the race arbiter**, so the job must push it and treat a non-fast-forward as "re-read and take the next number", not as a failure.
  - **The trigger is a policy question.** `launch-to-msg` runs on a twice-daily cron, so "every green build" and "one cut a day" are different jobs, and only the second matches what alpha claims to be.

  Keeping the pointer move as a merged PR is worth preserving even when the rest is automated: it is what makes `git log -p release-channels.toml` the channel history.
- Surfacing the parked state anywhere. Nothing announces it: the Updates panel prints what each channel serves and leaves the reader to compare, on the reasoning above, so a parked user has to go looking and then do the comparison. Main pushes status to every window, and the shell consumes it for the update-ready card, so the delivery half already exists if this is ever wanted.
- A gate on the dwt tag `fallback_branch` names: that it **exists** and is annotated. Its *name* is pinned to `minds-v<the build's version>`, and the Lima image manifest is checked for it, but only when the tier configures an image store; nothing confirms the tag is really in the template repo. The tag lives in default-workspace-template, and the `validate` job's `GITHUB_TOKEN` is scoped to this repository -- so the gate needs a cross-repo credential, which is precisely what the trust split keeps away from PR-authored code.
- The Sentry channel tag.
- `stagingPercentage`.
- Linux (`<channel>-linux.yml`).

### Docs and changelog

- `apps/minds/docs/release.md` gains a "Release channels" section: every channel, stable included, moves by editing `release-channels.toml`. Step 9 becomes that edit, and records that one more Release in ToDesktop is still needed to hand the existing field a build that reads our manifests.
- `dev/changelog/<branch>.md` for the spec, the promote script, and CI; `apps/minds/changelog/<branch>.md` for the app changes.

## Testing

The real update path cannot run in CI: it needs two signed builds, a reachable feed, and a restart.
So the tests split along what is and is not observable without one.

The JS suites below run from `just test-minds-js`, in the `test-minds-js` CI job.
That job is what makes the ordering rule's test a guard rather than a record: it is the only thing that runs either suite, since neither is reachable from pytest.

- **`apps/minds/test/unit/update-channel.test.js`** (plain node).
  Channel validation; the preference falling back to stable *and reporting why*; feed resolution for all three channels; a fast channel without a configured host failing loudly rather than falling back; the availability-vs-parked distinction; and the ordering rule, asserted in **both** directions -- one test proves `applyFeedToUpdater` leaves `allowDowngrade` false, and a second proves the mock's setter really does force it true, so the first test cannot pass vacuously.
- **`apps/minds/frontend/src/models/settings.test.ts`**.
  What the model does with the answer: a switch that would park stops at the confirmation and writes nothing until it is confirmed, cancelling leaves the channel alone, a channel that has caught up is switched to immediately, a channel whose manifest could not be read is refused with a reason, and the section is hidden in the browser build. Plus what a pushed status does to a panel that is already open, including a second window's, that the last check time survives a status that is not a check, and that a failed check leaves the button live rather than reading as one that never returns.
- **`apps/minds/frontend/src/views/pages/settings/SettingsSections.test.ts`**.
  What the panel puts on screen: that being parked is stated by the channel versions rather than by an alarm, that every other outcome with something to say -- a transfer in flight, an artifact waiting on a restart, a check that could not reach the feed, a dev run -- says it, that the last check time is reported (relative while that is the useful answer, absolute once it is not), that an unoffered channel is hidden unless it is the one in effect, and that a channel serving nothing is unselectable -- the model refuses that switch too, but only if the click reaches it.
- **`apps/minds/frontend/src/views/shell/update-ready.test.ts`**.
  That the card seeds from the current state rather than only from a push, that a later push wins over that seed, that neither `checking` nor a check that could not reach the feed withdraws an offer, and that a dismissal sticks for the version dismissed and not for the next one.
- **`apps/minds/frontend/src/views/shell/UpdateReadyCard.test.ts`**.
  That the card names the version and says what restarting costs, and that each of its two controls invokes its own callback -- found by what the control says rather than by its position, so a swap fails here rather than shipping a prominent button that dismisses.
- **`apps/minds/frontend/src/views/pages/SettingsPage.test.ts`**.
  Which panel the page opens on: that `?section=` is honoured on a page already on screen, that it is not re-applied on every redraw once the nav has been clicked elsewhere, and that a section this build does not offer is ignored.
- **`scripts/r2/client_test.py`**.
  That a credential which did not arrive is named -- absent or empty, since a publish job exports all three names unconditionally, so a secret Vault did not supply arrives as an empty string.
- **`scripts/release_channel/manifest_test.py`**.
  The rewrite is asserted against a **verbatim captured** ToDesktop manifest, so it has to survive spaces in filenames, the legacy top-level `path:`, and a quoted `releaseDate:`. Digests, sizes and `releaseDate` must come through byte-for-byte. Plus every gate: missing image, wrong tag, missing arch, rollback without the flag, a prerelease version, and an extensionless URL. And the upload itself, through a `botocore` stub: the key must be the `<channel>-mac.yml` electron-updater asks for, and the `Cache-Control` must carry the TTL the caller passed. The bucket reader gets the same stub treatment, including that only an absent object reads as "never published" -- a denied read or a throttle must refuse the promotion, since taking either as absence skips the rollback gate outright.
- **`scripts/release_channel/publish_test.py`**.
  That each source is really read from, proven by giving the feed a version the bucket does not have, so a run that claims the bucket and reads the feed returns the wrong one -- and by failing outright if a credential-less run builds an S3 client at all. Plus the declarative layer: the shipped `release-channels.toml` parses and names only publishable channels, `stable` is declared and published like any other channel, a channel nobody serves is refused, a missing field is rejected before any network call, a version that disagrees with its build is rejected before the image gate, a `fallback_branch` left on the previous release is rejected, re-running against an unchanged channel is a no-op, a channel the file no longer declares is reported as still being served, and a refused promotion leaves the process with a non-zero exit code -- which is the whole of what makes a gate turn the job red.
- **`scripts/release_channel/resolve_tier_test.py`**.
  The one `grep` the promote job's publishing hangs on: what the shell extracts from the production tier's `client.toml` must equal what `tomllib` parses, and must not be empty. An indented key, a quoted key or a single-quoted value is legal TOML that the shell reads as empty, which skips both the dry-run and the publish and leaves the job green.
- **Manual, in Electron.** The fixture-feed harness behind [Verification log](#verification-log). Not crystallized into CI: it needs a real Electron process, and the electron-updater behaviors it pins are already guarded by the unit test's mock.

**Warning:** an acceptance test asserting "checking the alpha feed returns a version" passes just as well when the app silently fell back to `stable`, because both feeds return a version.
Assertions must name the expected version, and fixture feeds must serve distinguishable ones.

## Findings from implementation

Numbered so commits and review comments can cite them. Each was found by building
the thing, not by reading about it.

1. **electron-updater's `channel` setter forces `allowDowngrade = true`.**
   `set channel(value)` ends with `this.allowDowngrade = true` (`AppUpdater.js`), documented only as an aside in its docstring.
   So `allowDowngrade = false; updater.channel = c;` silently re-enables downgrades, and every channel switch sets the channel.
   Observed end to end: against one fixture feed serving 0.0.1 with the app at 0.3.11, channel-then-allowDowngrade returns no cancellation token (parked); allowDowngrade-then-channel returns one (a downgrade offered).
   This is a second, independent source of the hazard the spec was written to prevent, and unlike ToDesktop's it fires on our own code path.
   Guarded by `applyFeedToUpdater` plus a unit test whose mock reproduces the real setter, in both directions.

2. **`updateInfo` is truthy on every successful check, so it cannot mean "update available".**
   `doCheckForUpdates` returns `{versionInfo, updateInfo}` whether or not an update is available, adding `cancellationToken` only when one is.
   Observed truthy in all four probe cases, including parked and same-version.
   The pre-existing `main.js` check (`if (updateInfo) -> "Update <v> found"`) was correct only because `@todesktop/runtime` normalized `updateInfo` to null first; swapping in raw electron-updater would have made it report "Update found" every single time.
   Both the availability test and the parked/up-to-date distinction now key off `isUpdateAvailable`, which that same branch sets alongside the token.

3. **`electron-updater` had to become a direct dependency, or the app crashes on startup.**
   It arrives transitively via `@todesktop/runtime`, but pnpm's store is not flat, so `require('electron-updater')` from `apps/minds` does not resolve.
   No unit test would have caught this: the tests exercise `update-channel.js`, which does not import it.
   Caught by resolving it directly, then by loading the real module graph inside Electron.
   Pinned exactly. `4.6.5` at first, to share the runtime's `autoUpdater` singleton; `6.8.9` once that reason proved hollow, since the runtime never constructs one.

4. **The ToDesktop runtime is imported but never `init()`ed.**
   The spec said `todesktop.init()` "is not called at all" and implied the import could go too.
   Importing is what arms the build-time smoke test (`initSmokeTest` runs at module scope under `TODESKTOP_SMOKE_TEST` and calls `_init()` itself), and the `ToDesktop` constructor creates no updater.
   Verified in Electron: after importing the runtime and running our `init()`, `autoUpdater.allowDowngrade` is still `false`.
   **Still unverified:** that ToDesktop's smoke test passes on a real build. Confirm on the first build after this lands.

5. **Concurrent checks silently reuse the previous configuration.**
   `checkForUpdates()` returns the in-flight promise when one exists, so an on-demand check racing the interval check gets a result computed under whatever feed was configured first.
   "Apply configuration before every check" is therefore only sound if checks are serialized, which `updater.js` now does through its own chain.

6. **`downloadedUpdateHelper` is null until this process downloads something.**
   The first cut of "discard the staged update on channel switch" read that field directly and would have no-opped on the case that matters -- an update staged by a *previous* run, before the channel changed.
   `getOrCreateDownloadHelper()` derives the directory from `app.baseCachePath` + `updaterCacheDirName` and works with no prior download.

7. **The `.zip` extension rule is real but currently forgiving.**
   `findFile(files, "zip", ["pkg", "dmg"])` prefers a genuine `.zip` extension regardless of list order, and falls back to the first non-pkg/non-dmg entry otherwise.
   So the extensionless `dl.todesktop.com/.../mac/zip/arm64` form *is* selected today -- by the fallback, exactly as the spec predicted.
   `manifest.py` refuses to publish such a URL rather than relying on it.

8. **File and module placement differ from the first draft.**
   `update_channel.js` is `update-channel.js` (the electron directory is kebab-case throughout);
   `applyFeedToUpdater` lives in `update-channel.js`, not `updater.js`, because `paths.js` imports `electron` and would make the ordering rule untestable;
   and the promote script is `scripts/release_channel/manifest.py` at the repo root, matching `scripts/lima_image/publish.py`, not `apps/minds/scripts/`.

9. **The feed host needed a config home, and `ClientEnvConfig` was it.**
   The draft left `<releases-host>` abstract. It is now `update_feed_base_url`, mirroring `lima_image_base_url` exactly: public, optional, and absent means the feature is off.
   That makes "no bucket yet" a first-class, honest state -- the client half is complete and every build offers stable only until a host exists.

10. **`PublicClientEnvConfig` does not exist.**
    `data_types.py` cited it as the guard preventing the deploy writer from adding fields; nothing in the repo defines it.
    The docstring now names the thing that actually holds that line: `write_client_config` in `envs/local_store.py` emits one named key at a time rather than serializing whatever the model happens to hold.

11. **Only `HTTPError` was caught, so an unreachable host produced a traceback rather than a refusal.**
    Found by running the gates against a live bucket: pointing `--lima-image-base-url` at a host that does not resolve raised a raw `urllib.error.URLError` instead of the intended message.
    An operator seeing a stack trace cannot tell whether the gate fired or the script broke.
    Worse for `read_current_channel_manifest`, where the 404 branch means "never published": a DNS failure there would have to be caught, or a network blip could read as "nothing there" and overwrite a channel unguarded.
    All three network call sites now catch `URLError` (after `HTTPError`, which subclasses it) and refuse with a clear message.

12. **electron-updater has no working request timeout under Electron.**
    `builder-util-runtime@9.7.0`'s `addTimeOutHandler` -- the copy under `electron-updater@6.8.9`, not the 8.9.2 one under ToDesktop's 4.6.5 -- arms the 60s socket timeout it is handed inside `request.on("socket", ...)`, and Electron's `net.ClientRequest` never emits that event.
    Observed in a real Electron 40 process: after a completed request the listener is registered (`req.eventNames()` lists `'socket'`) and was never called.
    Serializing every path through one chain -- the fix for [finding 5](#findings-from-implementation) -- makes a single unsettled promise the whole feature's problem: a hang is not a rejection, so the status stays `checking`, the renderer's `finally` never runs, and every control in the Settings panel stays disabled with nothing said until the app restarts.
    The check now races a deadline; the download, which legitimately runs for minutes, does not.

13. **The declarative file could express a promotion but not a withdrawal.**
    Nothing deletes a manifest, so removing an entry left the channel serving its last build while the run reported success -- and reverting a version *bump* is refused by the rollback guard, whose `--allow-rollback` the workflow never passes.
    The design keeps both: deleting the manifest would leave every client on that channel erroring against a feed that serves nothing, and letting a merge roll a version back makes an incident action out of an ordinary approval.
    So the run names a channel it no longer declares but is still serving, and a backwards withdrawal is an operator-run command.

## Verification log

What was observed, and what was not. Anything not listed here was reasoned about, not run.

| Claim | How it was checked | Result |
|---|---|---|
| Channel name selects the manifest filename | Real electron-updater against a local fixture feed | `alpha` fetched `/alpha-mac.yml`, `beta` `/beta-mac.yml` |
| Absolute `url:` overrides the feed base | `newUrlFromBase` on the shipped copy | Absolute wins; relative resolves against the base |
| The ordering rule matters | Same fixture, both assignment orders | Parked vs. downgrade-offered, as in finding 1 |
| `updateInfo` is not an availability signal | Four probe cases | Truthy in all four |
| Same version is not an update | Fixture at the running version | No cancellation token |
| Real manifest rewrite is correct | Live ToDesktop manifest for build `260801n4rh5zv5d` through `rewrite_manifest`, then parsed by electron-updater's own `parseUpdateInfo` from a non-ToDesktop base | All five URLs absolute to ToDesktop; arm64 zip selected with its original sha512 |
| Per-arch selection | `resolveFiles` + the MacUpdater arch predicate | arm64 and x64 each select their own zip |
| Unreleased builds stay downloadable | Ranged GET of build `260718n3rfjcn9z` | 372,297,921 bytes, HTTP 206 |
| Module graph loads in Electron | Loaded `updater.js` at module scope in Electron, ran `describe()` and `init()` | Loads; `allowDowngrade` still `false` after importing the ToDesktop runtime |
| Unit + promotion tests | `just test-minds-js`, `just test-quick scripts/release_channel` | Both green |
| **The whole publish path, for real** | Provisioned `minds-update-feed-dev-weishi` + `updates-dev-weishi.minds-dev.com`, published build `260801n4rh5zv5d` with `publish.py`, then pointed the real electron-updater at it through `feedForChannel` + `applyFeedToUpdater` | Manifest served over HTTPS (`text/yaml`, `max-age=60`); updater resolved `alpha-mac.yml`, reported 0.3.11 to a 0.3.0 client, offered the update, kept `allowDowngrade` false, and selected the arm64 zip on ToDesktop's CDN |
| The production feed | `GET https://updates.imbueminds.com/{stable,alpha,beta}-mac.yml` (2026-08-14), and each answer compared against `rewrite_manifest` of the build its entry declares | `stable` 200 at 0.3.11 and `alpha` 200 at 0.3.12, both `text/yaml`, `public, max-age=60`, artifact URLs absolute to ToDesktop; `beta` 404. Both bodies are byte-identical to what `publish.py` would write for the entry in `release-channels.toml`, so the first run finds both channels already served and writes nothing |
| Rollback gate | Published 0.3.8 over 0.3.11 on the live bucket | Refused; channel unchanged |
| Lima-image gate | Pointed at an unreachable image store | Refused before writing; channel unchanged |

**Now covered:** the workflow's `validate` job skipped its dry-run while no tier configured a feed. With production configured it runs for real on every PR touching the channel file. What *is* covered in CI is the file itself: `test_the_shipped_file_parses_and_declares_only_known_channels` parses the real `release-channels.toml`, so a malformed promotion fails the normal test suite. The first production promotion will be the first CI run of the gates; treat that run's output as unproven rather than routine.

**Not verified, and load-bearing:**

- **The install-and-restart round trip.** Needs two signed builds and a restart. Everything up to the download is now proven end to end against a real bucket; this last step is not.
- **ToDesktop's build smoke test still passes** with the runtime imported but not `init()`ed (finding 4).
- **Whether ToDesktop will release a build that is not the newest.** If it refuses, stable promotion becomes a rebuild and stamp-once holds for every hop except the last.
- **A promotion run by the workflow.** The production bucket, its custom domain, the rewrite, the key name and the TTL are all proven (see the row above), but the manifests now serving `stable` and `alpha` were published by hand. The first run of `minds-release-channels.yml` on main will be the first time CI drives the gates; treat its output as unproven rather than routine.

## Failure modes

| Failure | Behavior | Mitigation |
|---|---|---|
| Bad build reaches alpha | Alpha users update to it | Repoint `alpha-mac.yml` at the previous build. Stops new installs; does not roll back users who already took it, because `allowDowngrade` is false. |
| Channel feed host down | Every channel reports an update-check error, stable included | The app keeps running the version it has, and installs predating channels are unaffected -- they read ToDesktop's feed. Nothing is served from here but manifests; the artifacts stay on ToDesktop's CDN. |
| Stored channel value unrecognized | Resolves to `stable`, logged | Validated on read. |
| User parks and forgets | Weeks with no updates | **Not mitigated.** Settings > Updates prints the version every channel serves beside the one being run, so the state is readable there and nowhere else. Switching back to a faster channel is one click, but nothing prompts it. |
| Lima image missing for a tag | Creates silently 6x slower | Promote job gate, but only on a tier that configures an image store: a hard failure before the channel moves there, and a skip the run states out loud on production, which configures none. |
| dwt tag missing or moved | Agent creation fails at clone | **Not gated.** `fallback_branch` is reached only by the Lima image gate, which reads the image manifest rather than the tag -- and is skipped entirely on a tier with no image store. See [Not yet built](#not-yet-built). |
| Old binary opens newer `~/.minds` | Raises where the state carries a field the older build does not declare | `extra="forbid"` on the shared base models, so this is loud rather than silent -- and it is why invariant 4 holds. See [Decisions owed](#decisions-owed) for what is still open. |

**Note:** `stagingPercentage` is honored natively by electron-updater, and every channel's manifest is one we author, so it is available on `stable` on the same terms as on `alpha` and `beta`.
No channel carries one today: `rewrite_manifest` changes only the artifact URLs and passes the rest of ToDesktop's manifest through, and ToDesktop's manifest has no such field.
Staging a rollout therefore means teaching `publish.py` to write one, which is a change no channel needs before any other.

## Decisions owed

1. **Bake on promotion, not on cut (decided 2026-08-13).**
   Alpha cuts publish no pre-baked Lima image and no pool-host bake. Beta and stable bake before they are promoted.
   This works because neither absence breaks anything -- both degrade to a slow path. A missing Lima image makes the client build the workspace in-VM (~45s becomes ~5min); a pool with no row at the tag falls back to leasing any host and rebuilding its container (`host-pool-setup.md:216`).
   So the daily path stays fully automatable, and the operator-only work -- the minisign key, bare-metal orchestration -- lands on the channels whose cadence can absorb it.
   Consequence, stated plainly: **alpha users get slow creates.** They are internal, and that is the trade.

2. **Version numbering: settled (2026-08-13).**
   Stamp-once, no prerelease suffix, one shared version space, a tagged release per cut. See [How a version gets assigned](#how-a-version-gets-assigned-decided-2026-08-13).
   This reverses the 2026-07-29 prerelease decision.

3. **Old binary reading newer `~/.minds`: the shape is known, the blast radius is not (2026-08-14).**
   Parking makes this rarer but not impossible: a user can install a fresh stable over an alpha-era data directory.
   `imbue_common`'s `FrozenModel` and `MutableModel` both set `extra="forbid"`, and `desktop_client/state.py` repeats it, so an older build reading state that carries a field it does not declare raises a validation error rather than silently dropping it.
   That is the good failure -- loud, before anything is written -- and it is the concrete reason invariant 4 holds: a downgrade is a data hazard, not a degraded experience.
   What is still owed is which files actually diverge in practice, and whether every one of them fails at load rather than partway through a write: mngr's `data.json`, the latchkey permission-request schema (which moved v2 to v3 in July 2026), and the workspace records.
   Allowing downgrade at all -- which is what would make a channel switch take effect immediately instead of parking -- requires those readers to tolerate unknown fields first, and that is a repo-wide policy change rather than a minds-local one.

4. **Beta: settled 2026-08-20. Ship alpha and stable first, add beta once they are proven.**
   Beta is not mechanically different from alpha -- same manifest format, same gates, same promotion -- so nothing is learned by adding it before the two live channels are known to work.
   Raised during review as the channel most internal users should be on, which remains the intent; the sequencing is that the machinery earns that audience first.
   Until then `beta` stays publishable with no entry: selecting a channel whose manifest 404s is refused by the panel, so listing it before publishing to it offers a disabled radio and nothing else.
   The failure mode to watch once it does exist is a promotion obligation nobody discharges -- "why is beta still on 0.4.12" six weeks later.

5. **Resolved 2026-08-13: there was no beta tier to collide with.**
   `paths.js` described the bundled `root_name` as the "production / staging / beta" case, but no beta tier was ever built -- there is no `minds-beta` anywhere, and `envs/` holds `dev`, `staging`, `production` and the two `ci` variants. The collision was a documentation error, not two shipped meanings.
   The comment now says what the axis is: a tier decides which infrastructure the app talks to and which data directory it owns; a channel decides which build it is offered and never moves data.

6. **Stable has no gate, and no kill switch.**
   As designed, the gates ran only for the channels a manifest was published for, and stable was ToDesktop's Release action, which bypassed all of it: nothing verified the Lima image or the pool bake before every user got the build.
   Worse, `stagingPercentage` is a field in a manifest we author, so **alpha and beta have a staged-rollout kill switch and stable does not** -- the channel with the most users and the least tolerance. With `allowDowngrade = false` nobody can be pulled back, so stopping new installs is the only lever there is.
   **Resolved 2026-08-13, once alpha was verified end-to-end on a real install.** Stable is served from our manifest like every other channel, so it now passes the same gates, is promoted by a reviewable PR, and can be rolled back or staged.
   The cost was accepted knowingly: stable is no longer vendor-native, so `updates.imbueminds.com` is in the path for every current build. A feed outage stops update *checks* -- the app keeps running the version it has -- and the artifacts themselves still come from ToDesktop, which was never removed from the path.
   The migration costs one Release in ToDesktop: installs in the field predate this code entirely and update through `@todesktop/runtime`, so nothing we publish can reach them until a build carrying the code is Released. `feedForChannel`'s ToDesktop fallback is a guard for a build cut without a feed host, not the path those installs take.

   Note `stagingPercentage` is native to electron-updater, not something we would build: `AppUpdater.isStagingMatch` reads it from the manifest and each install buckets itself from a UUID at `<userDataPath>/.updaterId`, comparing `readUInt32BE(12) / 0xffffffff < percentage`. The cohort is therefore **fixed** -- the hash covers the user id only, never the version -- so raising the percentage only ever adds installs, and the same installs are early for every build forever. Monotonic and consistent, but the same machines always carry the risk and a failure specific to the other 90% is never caught early.

7. **Linux: deferred (decided 2026-08-12).**
   `latest-linux.yml` exists and ships an x86_64 AppImage with blockmaps, but Linux is not working well enough to carry channels.
   No `<channel>-linux.yml` is published, so a Linux client sees stable only.
   Nothing in the design blocks adding it later: the manifests are per-platform files and `manifest.py` gains a flag.

## Phasing

| Phase | Contents | State |
|---|---|---|
| 0 | Investigate decision 3. Lima-image gate on the existing release procedure. | Gate implemented in `manifest.py`; **decision 3 still open** |
| 1 | Replace the ToDesktop updater with `updater.js`. `allowDowngrade = false`. Sentry channel tag. | Done except the Sentry tag |
| 2 | Channel preference, feed resolution, Settings picker, the switch confirmation, the declarative promotion mechanism. | Done and live: production sets `update_feed_base_url`, so a build cut from here offers alpha alongside stable. Beta is served by the same machinery but listed for nobody (decision 4). The production bucket serves both channels; **no promotion has yet been run by the workflow** (see [Verification log](#verification-log)). |
| 3 | Automatic alpha cuts (see [Not yet built](#not-yet-built)). | Next |
| 4 | `beta` promotion job, soak timer, `stagingPercentage`, dwt-tag gate, Linux. | Not started |

Phase 1 was worth doing on its own: it closes the `allowDowngrade` hazard and finding 2's latent "Update found" bug with no user-visible feature attached.

## Appendix: verified facts

Established 2026-08-11 against the live service and the installed `/Applications/Minds.app` (version 0.3.11, build `260801n4rh5zv5d`).

- `Contents/Resources/app-update.yml` is `channel: latest`, `provider: generic`, `url: https://download.todesktop.com/26032588hqdzk`, `updaterCacheDirName: minds-updater`.
- `GET /26032588hqdzk/beta-mac.yml` returns 404 with body `26032588hqdzk/beta-mac-build-260801n4rh5zv5d.yml not found` -- the server resolved the same build id it serves for `latest`, so the channel name selects a filename rather than a build.
- `GET /26032588hqdzk/latest-mac-build-260718n3rfjcn9z.yml` returns 200 with a complete manifest for version 0.3.8, a build that was never released.
- `dl.todesktop.com/26032588hqdzk/builds/260718n3rfjcn9z/mac/zip/arm64` serves the full 372,297,921-byte artifact; both host forms honor range requests (HTTP 206).
- `latest-mac.yml` lists four artifacts (x64 and arm64, zip and dmg); `latest-linux.yml` lists an x86_64 AppImage with a blockmap; `latest.yml` is 404 (no Windows target).
- `electron-updater` 4.6.5 is bundled inside `@todesktop/runtime` 1.6.4.
  `MacUpdater.doDownloadUpdate` filters candidates by `file.url.pathname.includes("arm64")` before calling `findFile(files, "zip", ["pkg", "dmg"])`, so one manifest correctly serves both arches.
- `GenericProvider.channel` is `updater.channel || configuration.channel`, and `util.newUrlFromBase` is `new URL(pathname, baseUrl)`, so absolute `url:` values in a manifest override the feed base.
- `AutoUpdater._init` sets `_isActive = false` when `getReleaseStatus` reports the running build unreleased, and returns before constructing the agent, making `setFeedURL` a no-op.
- `UpdaterAgent`'s constructor sets `electronUpdater.autoUpdater.allowDowngrade = true`.
- `todesktop.init({autoUpdater: false})` still constructs the agent; it only suppresses the interval check, the launch check, and the `Notifier`.
- CI auth: `.github/workflows/minds-launch-to-msg.yml:330` pulls `minds/release/TODESKTOP_ACCESS_TOKEN` and `minds/release/TODESKTOP_EMAIL` from Vault via GitHub OIDC.
  The self-hosted macOS runner uploads app source; ToDesktop's own machines sign and notarize.

## Related

- `apps/minds/docs/release.md` -- the current release procedure, including the vendor-match invariant and the Lima image publish and gate steps.
- `specs/electron-desktop-app/spec.md` -- the desktop app's bundling and packaging design.
- `agent_docs/release-channels/design.md` in the sculptor repo -- an uncommitted parallel design for Sculptor, which reaches the same "a channel is a delay, not a build" conclusion on different plumbing (S3 prefixes, electron-updater direct, no ToDesktop).
