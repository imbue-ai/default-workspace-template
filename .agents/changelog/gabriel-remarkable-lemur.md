`update-self` no longer updates a workspace past the version of the Minds app running it.

The workspace template ships the code the app talks to (the system interface, the vendored `mngr`), so a workspace that pulled a template newer than its app would be speaking a protocol the app doesn't know. The update flow now reads the app's own version (`GET /api/v1/app`, allowed for every workspace by default -- no permission prompt) and caps the default target at the newest release at or below it. When the app is running a dev branch rather than a release, there is nothing to compare against and nothing is capped.

If the app can't be reached at all -- it's closed, the gateway is down, or it's too old to report its version -- the update stops with a plain-language reason instead of proceeding uncapped, since an app that can't answer is often exactly the app a newer template would outrun.

An explicit `--override` still works, but one naming a version newer than the app (or a branch/commit whose version can't be compared) now surfaces for confirmation before anything is merged, and the approval message says when a newer release was held back so you know that updating the app unlocks it.

A ceiling is now parsed as a full semantic version rather than only as a stable release, so an app running a release candidate (`minds-v0.4.0-rc1`) caps its workspaces like any other -- previously such a ceiling parsed as "not a stable tag" and was treated as *no ceiling at all*, letting a workspace update arbitrarily far past that app. Prereleases order below the release they precede, so an rc1 ceiling admits the prior release but not the final one. Capping by a prerelease does not make prereleases selectable: the default target is still only ever a stable release.
