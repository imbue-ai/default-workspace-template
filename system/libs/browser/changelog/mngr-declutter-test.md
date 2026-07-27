Integration branch combining the workspace-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the browser fleet manifest and screenshots move to `data/.state/`, and repo-root detection follows the new `system/scripts` + `system/libs` layout (the package itself moves to `system/libs/browser`).

The browser readiness gate checks the installed Fortress binary (the env.d unit's own satisfied condition) instead of the retired deferred-install marker file, which nothing writes on the decluttered layout -- new-browser creation was permanently stuck on "Chromium is still installing".
