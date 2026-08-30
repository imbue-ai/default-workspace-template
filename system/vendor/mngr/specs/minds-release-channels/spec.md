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
| `beta` | roughly weekly | a PR editing `release-channels.toml`, by policy after a soak on `alpha` | nobody -- selectable and serving a build since 2026-08-20, but nobody's default (decision 4) |
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

## Staged rollout

A promotion says *which* build a channel serves. A staged rollout says *how much
of the channel is offered it yet*. `stable` ramps a new build over several days --
10% -> 50% -> 100% is a guideline, not a rule, and nothing enforces it -- one
merged PR per step; `alpha` and `beta` stay at 100.

The client half is electron-updater's, not ours: `stagingPercentage` is a
top-level key it reads off the manifest, and each install buckets itself off the
UUID at `<userDataPath>/.updaterId`. We write the key and leave the bucketing
where it is.

**So the whole feature is publisher-side. Not one line of `apps/minds/electron/`
changes.** Two client changes were written and both were removed: a waiver
letting a user-started check ignore the percentage (finding 16), and a loosening
of `isAlreadyStaged` for an install the rollout no longer offers a build it has
already staged. The second was removed while narrowing was still refused, which
left that case unreachable. Finding 23 made it reachable again and the loosening
has not come back, so the function stays as it was and the gap is open --
finding 25.

### Invariants, in addition to the four above

5. **Required, never optional.** Every entry declares `rollout_percentage`.
   Absence is not "no rollout" -- electron-updater offers the build to everyone
   when the manifest declares no percentage, and does the same for a null, a
   non-numeric one, or anything above 100. Nothing clamps, so the only spelling
   that does not reach everyone reaches nobody instead: a negative. None of them
   is what the file meant, so absence must not be expressible. See finding 14.
6. **Nested bands.** Which installs a percentage admits is decided by that number
   alone: the bucket hashes the install id and never the version, so every band is
   a subset of every wider one. Widening therefore strictly adds installs and
   nobody is offered a build that then disappears; narrowing strictly removes
   them, which is not a mistake to guard against but the halt
   ([The halt is the percentage itself](#the-halt-is-the-percentage-itself)).
7. **Every check is gated, including the ones a user starts.** There is no
   waiver. A held-back user who wants the build now switches to a channel that
   is not ramping -- `beta` carries 100 by construction -- takes it there, and
   switches back, parking until stable catches up. That round trip is already
   designed, already explained by the panel's parked copy, and already the only
   escape hatch; a second one for the ramp alone earned neither its complexity
   nor its failure modes (see finding 16).

### The halt is the percentage itself

Lowering `rollout_percentage` stops a bad build part-way through a ramp, and
nothing refuses it. `isStagingMatch` buckets each install off the UUID at
`~/.minds/.updaterId`, which is fixed for the life of the install, and compares it
against the percentage in the manifest it just fetched. So a band is nested and a
narrower one is strictly smaller: whoever has not polled yet stops being offered
the build. `0` stops it reaching anyone new.

What it does **not** do is pull anyone back. `allowDowngrade` is false, and
`downloadAndOffer` arms `autoInstallOnAppQuit` *before* the transfer, so an
install that has fetched the bytes applies them at its next restart whatever the
feed says next. Narrowing is a partial halt, not a rollback; moving those users
needs a new build, which is the withdrawal path.

Reverting a ramp step therefore does what a reader expects: it restores the
previous, smaller band. Pausing a ramp is also still just not opening the next PR.

### What the rollout does not reach

- **New downloads.** The public download link resolves `files[]` out of
  `stable-mac.yml` and redirects; it reads no other key. A brand-new install
  therefore gets the newest stable build during a ramp, which is deliberate: a
  fresh install has no existing `~/.minds` for a bad build to endanger, which is
  the whole reason invariant 4 exists. The accepted cost is that a broken build is
  a broken *first* experience for anyone signing up mid-ramp.
- **Installs predating the feed.** They read ToDesktop's own feed and are governed
  by nothing we publish.

### Evaluating a ramp

Sentry's `release` is the `package.json` version on both events and sessions, and
a ramped build always carries a different version from the one it is replacing --
so "is the new build worse" is answerable per release without the channel tag
listed under [Not yet built](#not-yet-built). Two caveats belong on the record:
the early cohort is a fixed set of machines rather than a random sample, so this
compares two populations rather than treatment and control; and a user only runs
the bytes at their next restart, so a soak measured in hours mostly measures
whether the download succeeded.

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

Fetches ToDesktop's per-build manifest, rewrites its URLs to absolute, and uploads it as `<channel>-mac.yml` with a short `Cache-Control`. Gates before writing anything: the Lima image manifest must exist for the build's `FALLBACK_BRANCH` with each arch the run names, on a tier that configures an image store; every rewritten URL must keep a `.zip`/`.dmg` extension; and the version must be plain `X.Y.Z` (`assert_plain_release_version`, called on its own rather than as a side effect of the backwards-move check).

It is a library, not a command: it has no CLI, because a second entry point would be a second gate set to keep in step with the reviewed one.

Network access is injected -- `fetch: Fetch = http_get` for the gates, `make_client: MakeS3Client = r2_client` for the upload -- so both are testable without monkeypatching.

"What does this channel serve now" has two sources and they are not equally good.
The manifest is uploaded with a short `max-age`, so reading it back through the public feed inside that window returns the *previous* promotion -- and every use of that answer is then wrong against a version the channel has already left: the line naming what it serves today, the BACKWARDS label, and the comparison that would otherwise report an applied promotion as a no-op.
So a run holding the bucket credential reads the object itself, where R2 is read-after-write consistent.
`read_current_channel_manifest` takes which source as a `from_bucket` flag rather than as an injected reader: the choice is made once, from the environment, in `publish.py`'s `main`, which also says out loud which one it used -- a credential-less dry run and the publish that follows it can differ.
The public feed stays the fallback for the `validate` job, which is the one run that can afford a stale answer: it publishes nothing, so the worst it costs is a wrong preview.
Clients are unaffected either way -- they keep fetching the CDN copy, which is what the TTL is for.

### New: promotion is a pull request

`manifest.py` is the single-channel primitive, and has no entry point of its own. What operators touch is a declarative file, so a promotion is reviewable before it takes effect and the channel's history is the file's history.

A channel moves only by repointing its entry. **Removing an entry withdraws nothing** -- no manifest is ever deleted, so the channel keeps serving its last build, and the run names it rather than reporting a promotion. So `git revert` is the undo only between two builds carrying the same version at the same percentage, which is the ordinary `alpha` case. Reverting a version bump moves the channel back to the older build, which publishes like any other move and is named as backwards on the report line. Reverting a ramp step restores the previous, smaller band, which is a supported move and the way a bad build is stopped part-way through a ramp.

- **`apps/minds/release-channels.toml`** declares which build each channel serves and how much of the channel is offered it (`build_id`, `version`, `fallback_branch`, `rollout_percentage` -- see [Staged rollout](#staged-rollout)), `stable` included. Every field is required and an unrecognised key is refused. `beta` carries an entry like the other two; what is still owed is the promotion cadence that keeps it moving (see [Decisions owed](#decisions-owed)).
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
- The Sentry channel tag. Note that a *tag* would slice error events and not
  crash-free-session rate: a session envelope carries only `release`,
  `environment`, `ip_address` and `user_agent`, never tags. A percentage ramp
  does not need it -- the ramped build and its control always carry different
  `release` strings -- so this is owed to the channel ladder, not to the ramp.
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
  The rewrite is asserted against a **verbatim captured** ToDesktop manifest, so it has to survive spaces in filenames, the legacy top-level `path:`, and a quoted `releaseDate:`. Digests, sizes and `releaseDate` must come through unchanged. Plus every gate that remains: missing image, wrong tag, missing arch, a prerelease version, and an extensionless URL -- and that a backwards version move is reported rather than refused, in both directions, since standing still is not a decrease either. And that the published manifest declares exactly one `stagingPercentage`, parseable and top level, with the artifacts and digests untouched -- a stray key from upstream is replaced rather than joined, since two keys is an unparseable document rather than a merge. And that a channel is read back as the whole document rather than as its version, because two builds between cuts carry the same version. And that a document which is not a manifest at all is refused naming which one it was, rather than read past into a missing field -- unparseable, or a scalar, which is what an error page served with a 200 loads as. And that reading a percentage back refuses anything the writer would have refused, split the same two ways it splits them: not a whole number, or outside 0-100. This tool is the key's only writer, so a value out there means the object was hand-edited, and the reported state would otherwise name a percentage no client honoured. And the upload itself, through a `botocore` stub: the key must be the `<channel>-mac.yml` electron-updater asks for, and the `Cache-Control` must carry the TTL the caller passed. The bucket reader gets the same stub treatment, including that only an absent object reads as "never published" -- a denied read or a throttle must refuse the promotion, since taking either as absence would report a first publish over a channel the run cannot see.
- **`scripts/release_channel/publish_test.py`**.
  That each source is really read from, proven by giving the feed a version the bucket does not have, so a run that claims the bucket and reads the feed returns the wrong one -- and by failing outright if a credential-less run builds an S3 client at all. Plus the declarative layer: the shipped `release-channels.toml` parses and names only publishable channels, `stable` is declared and published like any other channel, a channel nobody serves is refused, a missing field is rejected before any network call, a version that disagrees with its build is rejected before the image gate, a `fallback_branch` left on the previous release is rejected, re-running against an unchanged channel is a no-op, a channel the file no longer declares is reported as still being served, and a refused promotion leaves the process with a non-zero exit code -- which is the whole of what makes a gate turn the job red.
  And the refusals that make an absent rollout inexpressible: an unknown field (the misspelling is the dangerous typo), a value that is not a whole number, one outside 0-100 -- while `0` survives as a value meaning nobody. And that a string field TOML let through as a bool, a number, a list or an empty string is refused by name too, since `build_id` reaches a URL. So is `channel` inside a table, which is the one key `extra="forbid"` cannot see -- it is a real field, so it reaches the constructor twice and raises past `main`'s handler instead of being named. Plus that every report line names the percentage, and that a ramp step, which changes nothing else in the manifest, republishes rather than reading as a no-op.
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
    Nothing deletes a manifest, so removing an entry left the channel serving its last build while the run reported success -- and reverting a version *bump* was refused by the rollback guard, whose `--allow-rollback` the workflow never passed (since removed -- finding 24).
    The design kept both: deleting the manifest would leave every client on that channel erroring against a feed that serves nothing, and letting a merge roll a version back was held to make an incident action out of an ordinary approval.
    The first half stands -- the run names a channel it no longer declares but is still serving.
    The second did not survive finding 24: a backwards withdrawal goes through the reviewed file like any other move, and the report line names it.

14. **Absence of `stagingPercentage` is the largest rollout, and the publish path produced absence on every mistake.**
    `AppUpdater.isStagingMatch` returns true for an absent, null or non-numeric value (6.8.9, `AppUpdater.js:314-324`), driven directly and confirmed for `undefined`, `null`, `"abc"`, `true`, `[]` and `{}`.
    Meanwhile `parse_channels` validated only its three required fields and copied exactly those into `ChannelEntry`, silently dropping every other key -- verified by parsing a table carrying `rollout_percentage`, `stagingPercentage` and `staging_pct` at once and getting an entry with none of them.
    So `rollout_percentge = 10` would have published a full rollout while printing an ordinary promotion line.
    The design's answer is that the field is required and unknown keys are refused, which makes absence inexpressible rather than dangerous.
    Two smaller traps sit under the same finding: `0` is a legal percentage meaning *nobody* and is falsy in Python, so the file's own `if not fields.get(f)` idiom would have read it as missing and inverted it; and nothing downstream clamps, so `150` reaches everyone and `-5` reaches nobody.

15. **The plan's "restate the percentage whenever the build changes" gate is not expressible, and was replaced.**
    `version` and `fallback_branch` can be checked against the build because the build knows its own version. A percentage has no ground truth in the build -- any number is legal for any build -- so "you bumped `build_id` and left the percentage stale" cannot be distinguished from "you meant that percentage".
    What replaced it is visibility rather than a gate: every publish line now names the percentage, including the credential-free dry run that runs on the PR. Before, a rung on the ladder, a jump to 100%, a malformed value and a stranded rollout all printed the byte-identical `stable: would publish 0.4.2 (currently 0.4.2)`.

16. **The rollout waiver was built, then removed, and the reason it was removed is the reason to record.**
    It let a user-started check ignore the percentage. Making that correct took a module-level flag whose lifetime spans *two* serialized tasks: `startDownload` re-checks before downloading, because `downloadUpdate()` takes no argument and serves whatever the last `checkForUpdates()` left on the shared updater -- so a waiver scoped to the check that queued the download is already restored by the time the download runs, and the gate re-applies to the very fetch the user asked for. That was got wrong first; the symptom is a button that appears to work and fetches nothing, the same class as findings 2 and 12.
    It was removed because it was a *second* escape hatch. Switching to a channel that is not ramping already gets a held-back user the build, and switching back parks them until stable catches up -- a round trip the spec already designs, the panel already explains, and the tests already cover. One general mechanism beat one general mechanism plus a ramp-specific one, and deleting it took `main.js` out of the change entirely.

17. **`isUserWithinRollout`'s default delegates to `this`, and survives a bare call only by accident.**
    The override is a public settable property whose setter takes any truthy value (`if (value)`, `AppUpdater.js:98-101`), and the default is `updateInfo => this.isStagingMatch(updateInfo)` -- an arrow assigned in the constructor, so its `this` is bound and a bare call happens to work.
    `isStagingMatch` itself is `private` in the type declarations, so it is not contract, and neither is the arrow.
    The wrapper therefore had to invoke the captured predicate with the updater as its receiver, which the first version did not -- caught by writing the mock as a method rather than an arrow, where it threw `this.isStagingMatch is not a function`.
    Recorded rather than deleted with the waiver (finding 16): anything that wraps this property again inherits the same trap, and nothing about it is contract.

18. **Sentry sessions crashing at startup carried no user id, so the crash-free-*users* rate missed exactly them.**
    `Sentry.setUser` ran 66 lines after `Sentry.init`. `mainProcessSessionIntegration.setup()` opens the release-health session from inside `init`, and `startSession` copies the user off the combined scope at that moment (`@sentry/core exports.js:264-276`).
    The *live* session does get it -- `Scope.setUser` calls `updateSession` when a session is already open on the scope it writes to (`scope.js:193-195`), and that is the same isolation scope -- so a cleanly ended session always carried a `did`. Driven against the installed package to be sure: old ordering, live envelope `did` present.
    What it missed is the snapshot `@sentry/electron`'s `sessions.startSession` writes to disk right there, from a copy of the session taken before `setUser` can run. A crash is reported on the *next* run, rebuilt from the last snapshot written -- `makeSession` carries only a `did` the snapshot already had.
    That window is bounded, and narrower than this finding first claimed: `startSession` also arms a `PERSIST_INTERVAL_MS` (60s) timer that re-writes the live session, id included. So the old ordering lost the id from a crash that beat the first re-persist -- a startup crash, which is the failure a ramp exists to catch -- and not from every crashed session. Driven, old ordering: snapshot `did` `undefined` at 0.4s, `"anon-1234"` at 62s, and a session rebuilt from the 62s snapshot carries it.
    `Sentry.setUser` writes to the isolation scope, which exists before the client does, so moving the call above `init` is the whole fix.
    Found here because the ramp depends on it, but shipped as its own change rather than with the rollout.

19. **The first merge republishes every channel, and the first ramp cannot be the build already being served.**
    Both observed by running the real CLI as a dry run against the live production feed.
    The manifests in the bucket declare no percentage and the new code always writes one, so the text differs and all three channels republish -- a behaviourally identical write, since absence and 100 mean the same thing to the client, but a write to production on merge nonetheless.
    A ramp can also start on the build a channel already serves, though it reaches nobody new: everyone the channel was going to offer 0.4.2 to has already been offered it, so the percentage only binds the installs that have not polled since.

20. **"The same build" is the build id, not the same bytes, and comparing bytes made the narrowing gate fail open.**
    The gate answers "is this build the one the channel serves" before it compares percentages, and answering it by comparing the two manifests whole means any incidental byte difference reads as a *new* build -- which may start its ramp anywhere, so the gate returns having checked nothing.
    Driven directly against the captured real manifest: a 50% -> 10% narrowing that is refused on the nominal case was allowed once the served manifest carried a trailing blank line, a trailing space on a url line, a key added upstream by ToDesktop, or urls left relative.
    Two of those need nobody to do anything wrong -- ToDesktop adding a key of its own, or this repo changing what `rewrite_manifest` emits -- and each disarms the gate for every channel at once, with a green dry run that looks identical either way.
    The harm is bounded, since narrowing recalls nobody. That is the argument for not building a halt; it is not an argument for a gate the spec, the runbook and both changelogs describe as absolute. A gate that fails open reproduces exactly the false belief of mitigation that the "no halt" decision exists to prevent.
    The fix is that `entry.build_id` reaches the gate. ToDesktop names it in every artifact filename (`Minds 0.3.11 - Build 260801n4rh5zv5d-arm64-mac.zip`), so it outlives the rewrite, the encoding and anything added beside it. The whole-text comparison stays as the fallback for a manifest whose artifacts do not carry the id, which is what makes the change one that can only ever add refusals.

21. **A YAML re-render *is* a copy here, and the argument against parsing was really an argument about which YAML.**
    The manifest was edited a line at a time -- two regexes for `stagingPercentage`, one for the urls, a whole-number regex standing in for a type, and a guard against emitting the key twice -- on the stated premise that re-rendering ToDesktop's document would not preserve it.
    Driven against the live build manifest for `260825un55i8ix7`: parse and re-emit preserves every value -- version, digests, sizes, `releaseDate`, and any key ToDesktop set that this code knows nothing about. It is byte-identical too if the dumper is told to indent sequences under their key, but nothing reads the layout, so that is not carried: the published manifest is pyyaml's default block style, which js-yaml reads identically.
    So the premise was wrong, and the four regexes plus the duplicate-key guard are gone -- the last unreachable, because a mapping holds one key by construction.
    The real hazard is a different one, and it was never about re-rendering. pyyaml is a **YAML 1.1** parser and the client's js-yaml is **1.2**: `010` is 8 to one and 10 to the other, `yes` is true to one and a string to the other, `1:30` is 90 to one.
    Reading the manifest by a different schema than the client decides blast radius from a number the client never saw, and it fails *open* -- a `stagingPercentage: 010` read as 8 lets the monotonicity gate wave through a move to 9%, which is a narrowing.
    Two fixes were built and both were dropped: a hand-rolled 1.2 loader rebuilding pyyaml's resolvers and int constructor (46 lines, and still wrong on `0b101` and `1_000`), and `ruamel.yaml`, which is 1.2 natively and needs four lines (correct on all 22 spellings tried). What settled it is that the hazard is not reachable from a manifest ToDesktop produces: electron-builder writes them *with js-yaml*, whose emitter is canonical 1.2, and re-reading a stock-pyyaml round-trip of the real manifest yields an equal document -- which it could not if any scalar in it read differently under 1.1.
    So the reader is stock pyyaml, and the gap is documented rather than closed. Four shapes are read differently from the client and are silently in range: `010`, `017`, `050` and `1:30`. Every one needs the bucket object hand-edited, and the worst outcome is a narrowing slipping past the gate, which recalls nobody. `test_a_spelling_this_tool_never_writes_is_read_as_yaml_1_1` pins it so that closing it later is deliberate. *(Both consequences named above were the gate's, and finding 23 removed it. A misread value now only mis-states the percentage on the report line, which makes the decision to document rather than close the gap easier, not harder.)*

22. **The build-id check was a substring match over the rendered manifest, so text anywhere in the file could claim a build.**
    Finding 20 replaced a whole-text comparison with `entry.build_id`, but the id was then looked for with `build_id in current.text` -- which re-rendered the document to YAML and searched all of it, not the artifact urls the id actually lives in.
    electron-builder manifests can carry free text (`releaseNotes` is a supported field), so a manifest for a *different* build that merely mentioned this one read as the same build, and the narrowing gate compared percentages across two builds. Reproduced directly, and covered by `test_a_build_id_outside_an_artifact_does_not_make_it_that_build`.
    The id is now looked for in the `url` and `path` values themselves, found through the same rule the rewrite uses. The generator that does it also retires the out-parameter `rewrite_manifest` threaded through its helpers to answer whether any artifact was found.
    Both halves of this gate have now failed open once, in the same direction, for the same underlying reason: a question about structure answered against text.


23. **The narrowing gate was protecting against the one action an operator would actually want, and findings 20 and 22 were both inside it.**
    The refusal rested on "narrowing recalls nobody, so it buys nothing an operator could act on". The first half is true. The second is wrong, and reading `isStagingMatch` in the shipped electron-updater says so: the staging id is fixed per install and the percentage is re-read from the manifest on every check, so a narrower band is strictly smaller and everyone who has not polled yet stops being offered the build. Lowering the percentage *is* a partial halt -- the capability [There is no halt](#the-halt-is-the-percentage-itself) was written to say we did not have -- and `0` stops a build reaching anyone new.
    So the gate forbade stopping the bleed. `50 -> 5` is not a typo to guard against; it is the operator responding to a bad build.
    Removing it deletes `assert_not_a_rollout_decrease` and `_serves_build` (35 lines) and nine tests. `_serves_build` existed only to serve this gate, and it is where **both** of the gate's own defects lived -- finding 20 and finding 22, each failing open, each caught in review. The whole build-identity question was machinery built to support a refusal that should not have been made, and it was got wrong twice.
    What is given up is a check on a mistyped percentage. `release-channels.toml` is the reviewed artifact and every PR dry-runs it, printing `stable: would publish 0.4.2 to 5%`; a missed typo makes a rollout slower, and is fixed by the next PR. The version gate (`assert_not_a_rollback`) was kept here, on the argument that a version downgrade genuinely cannot be undone by a later manifest -- which finding 24 then reversed.


24. **`--allow-rollback` guarded a coupling that every stable release already has.**
    Finding 23 removed the rollout gate; the version gate survived it on the argument that a backwards move has a consequence outside this repo -- the connector's download fallback -- that the run cannot check. The runbook says otherwise at its own step: the connector fallback is bumped on **every** stable release, forward or back. So the flag was not guarding a rollback-specific hazard, it was guarding a routine one.
    What a backwards move actually does, from `isUpdateAvailable`: `allowDowngrade` is false, so an install on the newer version is never offered the older one and stays put; a new download takes the older build. Nobody is moved backwards. It is the same shape as narrowing -- it changes who *arrives*, not who is already there.
    And CI never passed the flag, so withdrawing a bad stable build could not go through the reviewed path at all. `git revert` of a bad promotion, the obvious undo, failed in CI. That is friction at the exact moment it is least wanted.
    The refusal is now a report: `assert_not_a_rollback` becomes `is_a_version_decrease`, and the publish line says `-- BACKWARDS, so lower the connector download fallback too`. The plain-X.Y.Z rule it also carried is unchanged, since a prerelease version breaks the stamp-once model whichever direction it moves.
    Both gates removed in findings 23 and 24 shared a premise: that the operator's emergency action is the thing to protect against. The two dials only ever change who a build *reaches*; neither can move an install that already has it.


25. **Removing the narrowing gate re-opened the case the `isAlreadyStaged` loosening was built for.**
    `isAlreadyStaged` requires `isUpdateAvailable`, which electron-updater also sets false for an install outside the channel's staged rollout while still naming the staged version as the feed version. So an install that downloaded a build at 50% and falls outside the band when the operator drops to 10% reads as "nothing staged": `runCheck` publishes `up-to-date`, and `views/shell/update-ready.ts` clears `readyVersion` on any status that is not `update-downloaded` -- withdrawing the restart card for bytes already handed to Squirrel, which install at the next restart anyway since `allowDowngrade` is false and `autoInstallOnAppQuit` is armed before the transfer.
    The loosening -- keying `isAlreadyStaged` on the staged version alone, which is already proof the artifact is staged because `downloadedVersion` names a version *this* process fetched and is cleared on a channel switch -- was written for the waiver's route to this state and removed with it, on the argument that narrowing was refused so no other route existed. Finding 23 removed that refusal, and narrowing is the operator's halt: the installs shown a withdrawn restart card are exactly the ones the halt excluded.
    Not fixed here. The fix is in `apps/minds/electron/update-channel.js` and this change is deliberately publisher-side.


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
| Unit + promotion tests | `just test-minds-js`, `just test-quick scripts/release_channel` | Both green (129 JS on 2026-08-26; 97 promotion tests on 2026-08-28) |
| **The whole publish path, for real** | Provisioned `minds-update-feed-dev-weishi` + `updates-dev-weishi.minds-dev.com`, published build `260801n4rh5zv5d` with `publish.py`, then pointed the real electron-updater at it through `feedForChannel` + `applyFeedToUpdater` | Manifest served over HTTPS (`text/yaml`, `max-age=60`); updater resolved `alpha-mac.yml`, reported 0.3.11 to a 0.3.0 client, offered the update, kept `allowDowngrade` false, and selected the arm64 zip on ToDesktop's CDN |
| The production feed | `GET https://updates.imbueminds.com/{stable,alpha,beta}-mac.yml` (2026-08-14), and each answer compared against `rewrite_manifest` of the build its entry declares | `stable` 200 at 0.3.11 and `alpha` 200 at 0.3.12, both `text/yaml`, `public, max-age=60`, artifact URLs absolute to ToDesktop; `beta` 404. Both bodies carry the same document `publish.py` would write for the entry in `release-channels.toml`, so the first run finds both channels already served and writes nothing |
| Rollback gate | Published 0.3.8 over 0.3.11 on the live bucket | Refused; channel unchanged. *(Gate since removed -- finding 24. A backwards move now publishes and is named on the report line.)* |
| Lima-image gate | Pointed at an unreachable image store | Refused before writing; channel unchanged |

**Now covered:** the workflow's `validate` job skipped its dry-run while no tier configured a feed. With production configured it runs for real on every PR touching the channel file. What *is* covered in CI is the file itself: `test_the_shipped_file_parses_and_declares_only_known_channels` parses the real `release-channels.toml`, so a malformed promotion fails the normal test suite.

**Not verified, and load-bearing:**

- **The install-and-restart round trip.** Needs two signed builds and a restart. Everything up to the download is now proven end to end against a real bucket; this last step is not.
- **ToDesktop's build smoke test still passes** with the runtime imported but not `init()`ed (finding 4).
- **Whether ToDesktop will release a build that is not the newest.** If it refuses, stable promotion becomes a rebuild and stamp-once holds for every hop except the last.
- ~~**A promotion run by the workflow.**~~ Resolved 2026-08-25: three real publish runs have landed, the last one promoting 0.4.2 to all three channels.

### Staged rollout (2026-08-25)

| Claim | How it was checked | Result |
|---|---|---|
| The rollout gate, against the real library | Constructed the real `AppUpdater` (6.8.9) on a temp data dir, let it mint a `.updaterId`, and found a percentage its bucket falls outside of | Excluded at that percentage, included at 100% and with no percentage declared. Drove the since-removed waiver too (finding 16): it worked, and was removed for being a second escape hatch rather than for being wrong |
| `isStagingMatch`'s fail-open branches | Same real updater, driven with `undefined`, `null`, `"abc"`, `true`, `[]`, `{}`, `0`, `150`, `-5`, `99.9` | Everything but `0` and `-5` includes the install; `99.9` truncates to 99 |
| The published manifest is parseable and declares one key | `with_rollout_percentage` over the captured real ToDesktop manifest, parsed with pyyaml | One top-level `stagingPercentage`, artifacts and digests byte-identical to the rewrite |
| Parse and re-emit preserves ToDesktop's values | Live build manifest `260825un55i8ix7` through `parse_manifest`, then re-emitted | Every value survives, including keys this code knows nothing about. *(Byte identity was measured against the ruamel round-trip, which was replaced: the shipped reader is stock pyyaml and re-emits in its default block style, so the layout differs and nothing reads it -- finding 21.)* |
| Editing by document matches editing by line | Old and new `rewrite_manifest` + `with_rollout_percentage` over the live manifest at 0/10/50/100% | The same document at every percentage. *(Measured as identical bytes against the ruamel round-trip; the shipped pyyaml emitter dedents the `files:` sequence, so the documents match and the bytes do not -- finding 21.)* |
| A build id outside an artifact does not claim the build | A manifest for another build carrying `releaseNotes` that names this one | `_serves_build` returns False; the substring-over-text form returned True. *(Helper since removed with the gate it served -- finding 23.)* |
| Where the reader disagrees with the client's YAML | 22 scalar spellings through stock pyyaml and the shipped js-yaml 4.1.1 | Four disagree silently and in range (`010`, `017`, `050`, `1:30`); the rest agree or are refused |
| No scalar in the real manifest is schema-sensitive | Stock-pyyaml round-trip of the live build manifest | Every scalar re-emitted in the spelling it arrived in, so no value in it reads differently under 1.1; the `files:` sequence indentation is the only difference (finding 21) |
| Unknown-key and range refusals | `parse_channels` over tables carrying a misspelled key, a string, a float, a bool, `150`, `-5`, and `0` | Every malformed spelling refused; `0` accepted and rendered |
| Lowering the percentage narrows who is offered a build | `isStagingMatch` in the shipped electron-updater 6.8.9 | The staging id is fixed per install and the percentage is re-read each check, so a smaller band is a strict subset -- narrowing is a partial halt, not a no-op (finding 23) |
| The narrowing gate against a served manifest that only looks different | Drove `assert_not_a_rollout_decrease` at 50% -> 10% with the served manifest carrying a trailing blank line, a trailing space on a url line, a key added upstream, and urls left relative | Every one of them was allowed while the gate identified the build by comparing the two manifests whole, and every one is refused once the build id reaches it (finding 20). *(Gate since removed -- finding 23.)* |
| What the Sentry ordering costs, and for how long | Drove the real `@sentry/electron` `sessions.js` (7.13.0) on a stubbed `electron` app, old ordering, reading the on-disk snapshot over time | Snapshot `did` `undefined` at 0.4s and `"anon-1234"` at 62s, so the loss is bounded by the first re-persist tick rather than covering every crashed session (finding 18) |

**Not verified, and load-bearing (rollout):**

- **A ramp against a real feed.** Every gate and the client predicate are proven, but no manifest has yet been published carrying a percentage, and no install has been held back by one in the field.
- **That the Sentry fix produces a `did` on a real crashed session envelope.** The ordering is proven against the installed SDK (finding 18); the observable check is a session in Sentry that crashed in its first minute carrying a distinguished id, which needs a shipped build and a startup crash in it.
- **The held-back Settings panel.** `computeUpdateStatus` returns `up-to-date` for a held-back install, run directly; the resulting rendered panel was read, not rendered.

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

**Note:** `stagingPercentage` is now written by `publish.py` from a required `rollout_percentage` on every entry.
See [Staged rollout](#staged-rollout) for the model and the guards, and finding 14 for why the field is required rather than optional.

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

   **Discharged 2026-08-20.** `beta` was given an entry the same day, on stable's build, so its radio is live rather than dead. What is still open is the half above: nothing promotes it on a cadence, and the phase table still lists a `beta` promotion job as not started.

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

   **Correcting the pessimism, 2026-08-25.** The fixed cohort cuts the other way too, and this matters more than the sampling cost given that nobody can be pulled back. Because the ramp restarts at its first step with every build, the installs that took a bad build are also the first offered its replacement. The property that makes the cohort a poor sample makes it the right population to repair first.

7. **Linux: deferred (decided 2026-08-12).**
   `latest-linux.yml` exists and ships an x86_64 AppImage with blockmaps, but Linux is not working well enough to carry channels.
   No `<channel>-linux.yml` is published, so a Linux client sees stable only.
   Nothing in the design blocks adding it later: the manifests are per-platform files and `manifest.py` gains a flag.

## Phasing

| Phase | Contents | State |
|---|---|---|
| 0 | Investigate decision 3. Lima-image gate on the existing release procedure. | Gate implemented in `manifest.py`; **decision 3 still open** |
| 1 | Replace the ToDesktop updater with `updater.js`. `allowDowngrade = false`. Sentry channel tag. | Done except the Sentry tag |
| 2 | Channel preference, feed resolution, Settings picker, the switch confirmation, the declarative promotion mechanism. | Done and live: production sets `update_feed_base_url`, so a build cut from here offers alpha alongside stable. The production bucket serves all three: `beta` was given an entry on 2026-08-20 (decision 4). Corrected 2026-08-25: the workflow has now run three real publishes (2026-08-20, 08-21, 08-25), the last promoting 0.4.2 to all three channels. |
| 3 | Automatic alpha cuts (see [Not yet built](#not-yet-built)). | Next |
| 4 | `stagingPercentage`: a required per-entry percentage and the publish-side guards. Every check is gated; there is no client-side waiver. | Done; see [Staged rollout](#staged-rollout) |
| 5 | `beta` promotion job, soak timer, dwt-tag gate, Linux. | Not started |

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
