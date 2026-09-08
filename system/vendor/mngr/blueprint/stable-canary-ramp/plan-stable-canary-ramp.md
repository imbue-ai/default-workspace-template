# Canary ramp: rolling a stable build out to a percentage of installs at a time

## Overview

- `stable` gains a rollout percentage, so a new build reaches existing installs gradually -- 10%, then 50%, then 100%, as a guideline rather than a rule -- instead of all at once. Each step is a human editing `release-channels.toml` and merging the PR, exactly like a promotion. The steps are a guideline; nothing in the tooling enforces them.

- The client half already exists and needs no change to gate the rollout. `electron-updater` 6.8.9 reads a top-level `stagingPercentage` from the channel manifest and buckets each install off a UUID it keeps at `~/.minds/.updaterId`; `rewrite_manifest` already copies every non-`url:`/`path:` line through untouched. Teaching the publisher to write the field is the whole mechanism.

- **The percentage is a blast-radius cap, not a rollback.** `allowDowngrade` is false on every code path, and the updater arms `autoInstallOnAppQuit` *before* it starts downloading -- so an install that has fetched the bytes will apply them at next restart no matter what the feed says afterwards. Lowering a percentage stops new uptake and recalls nobody. Recovery from a bad build is always a new release.

- **The one real hazard is that absence means everyone.** An absent, null or non-numeric `stagingPercentage` all resolve to "include this install", as does anything above 100; a negative resolves to include nobody (corrected during implementation -- the plan had out-of-range going the same way in both directions; see finding 14 in `specs/minds-release-channels/spec.md`). And `parse_channels` today validates only its three required fields and silently drops every other key. So a typo, an unwired field, or a revert of a ramp step would each ship to 100% while printing a line that reads like an ordinary promotion. The design's answer is to make the field mandatory on every channel entry, so absence is never a legal state and every mistake becomes a publish-time refusal.

- Two things gate the first real ramp, and neither is the mechanism. Sentry's user id is currently set *after* `Sentry.init`, so a session that crashes at startup carries no user id and the crash-free-*users* rate misses exactly those (corrected during implementation -- the plan had this costing every session; only a crash inside the first 60 seconds loses the id, see finding 18 in `specs/minds-release-channels/spec.md`) -- that fix ships first, in the change this one is stacked on. And the size of the reachable install base is unknown; the best available estimate is low hundreds, at which 10% is a cohort of roughly seventeen fixed machines. Read the number before ramping for real.

## Expected behavior

### The rollout

- A scheduled update check offers a build only to installs inside the current percentage. Everyone else is offered nothing and stays on the version they have.

- The cohort is fixed per install -- the bucket hashes the install id only, never the version -- so raising the percentage strictly adds people. Nobody ever sees an update appear and then vanish.

- Because the cohort is fixed, the installs that took a bad build are also the first to be offered its replacement. The same property that makes the cohort a poor statistical sample makes it the right population to repair first.

- A build that turns out bad is handled by ~~not advancing it: it sits at its current percentage until a later stable release supersedes it, and no halt action is needed.~~ **Corrected during implementation** -- lowering the percentage *is* a partial halt, since the bucket is fixed per install and the percentage is re-read on every check, so a narrower band is strictly smaller and `0` stops the build reaching anyone new (see finding 23 in `specs/minds-release-channels/spec.md`). Nobody who already took it is pulled back, so moving those users still needs a new release.

- A new build abandons any ramp in progress and starts its own from the first step. Whoever took the previous build keeps it until the new one reaches them.

- `alpha` and `beta` serve their build to everyone, as today. Only `stable` ramps.

### What users see

- Every check is gated, including one a user starts. Getting a build the ramp has not reached means switching to a channel that is not ramping and switching back, which parks the user until stable catches up. (Corrected during implementation: a user-started waiver was built, then removed for being a second escape hatch alongside one that already existed -- see finding 16 in `specs/minds-release-channels/spec.md`.)

- Until the ramp reaches them, a user who opens Settings sees the newer version printed beside the stable row while the status still reads up to date. Pressing Check now resolves it by installing. No new UI is added for this state.

- `beta` remains selectable and never ramps -- it sits at the everyone value permanently -- and by the existing release flow it is already on the build stable is ramping toward. So switching to beta is a second way to get the ramping build immediately, and a user who does so stays on beta afterwards.

- Brand-new downloads are unaffected. The download link resolves the newest stable build for 100% of new users, on the reasoning that a fresh install has no existing `~/.minds` state a bad build could endanger. The accepted cost is that during a ramp a broken build becomes a broken first experience for new signups.

- Installs predating the self-hosted feed are not governed by the ramp at all; they read ToDesktop's own feed until they take a build that names ours.

### Publishing and review

- A ramp step is a merged PR against `release-channels.toml`, so `git log -p` on that file remains the record of who was exposed to what and when.

- The publish output names the percentage. Today a legitimate ramp step, a jump to 100%, a malformed value and a zero all print an identical line, which makes a percentage-only PR unreviewable in CI.

- Publishing refuses an entry with no percentage and a value that is not an integer in 0-100, ~~an entry whose build changed without the percentage being restated,~~ ~~and a decrease while the build is unchanged~~. A new build may start at any step, and so may a lower one. **Both struck refusals were dropped** -- the first is not expressible, the second forbade the halt; see the corrected items under [Changes](#changes).

- Reverting a ramp-step commit ~~is refused rather than dangerous: it restores a lower percentage, which the decrease rule rejects.~~ **Corrected during implementation** -- it publishes, restoring the previous, smaller band, which is what a reader expects of a revert (finding 23). Pausing is still done by not advancing.

## Changes

- `release-channels.toml` gains a rollout percentage on every channel entry, required, with an explicit value meaning "everyone" so absence is never legal. `alpha` and `beta` carry that value permanently; only `stable` moves.

- The published channel manifest carries the percentage as a top-level key. Nothing on the client needs to learn to read it.

- The channel-entry parser stops silently ignoring unrecognised keys, and validates the percentage as an integer in range -- treating a missing value as absent rather than as falsy, since zero is a meaningful value that Python's usual truthiness idiom would discard.

- Publishing strips any percentage arriving in the upstream build manifest before writing its own, so a published file can never carry two keys and become unparseable for every client on the channel.

- ~~A new publish-time gate joins the existing family that keeps stale fields from riding alongside a bumped build: the percentage must be restated whenever the build changes.~~ **Corrected during implementation.** Not expressible: `version` and `fallback_branch` can be checked against the build because the build knows its own version, but any percentage is legal for any build, so "you left it stale" cannot be told from "you meant it". Replaced by the item below, which makes the value visible to review instead of guessing at intent. See finding 15 in `specs/minds-release-channels/spec.md`.

- ~~A new publish-time gate refuses a decrease at an unchanged build.~~ **Corrected during implementation.** Built, then removed: it forbade the one action an operator would want, and both of its own defects lived in the build-identity machinery it needed to tell one build from another. See findings 20, 22 and 23.

- All three publish result strings, including the credential-free dry run that runs on the PR, name the percentage. This is now the only thing standing between a reviewer and a stranded rollout riding onto the next release.

- The updater needs no change to apply the rollout: electron-updater applies it itself, to every check. ~~User-initiated checks bypass it...~~ **Corrected during implementation** -- a waiver was built and then removed, which took `electron/main.js` out of the change entirely. See finding 16.

- Sentry's user id is set before initialisation rather than after, so the snapshot the session integration persists during init carries it and a startup crash is countable per release. This ships first, in the change this one is stacked on, and is not part of it.

- The release procedure documents the ladder, states that lowering the percentage is the partial halt and `git revert` the undo for either dial, and records that recalling an install which already took a bad build still needs a new release. (Corrected during implementation: the plan had ~~recovery as a new release rather than a rollback or a halt, and a ramp step as not undone by reverting it~~ -- see findings 23 and 24.)

- Changelog entries for `apps/minds` and `dev`, the latter because the publishing scripts live under `scripts/`.
