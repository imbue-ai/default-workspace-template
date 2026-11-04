The services event stream now carries each app's display name, so the Imbue Studio desktop can call an app what the workspace calls it.

A `service_registered` event gained a `display_name` field, the manifest's `display_name` (empty for a row registered with `--name`/`--url` and no manifest). A change to it alone re-announces the app, exactly as a change to its URL, label, or icon does. Until now the desktop learned only an app's registered name, so its share panel could offer "files" where the workspace shows "File Viewer"; the name a user reads could not leave the workspace at all.

The field is additive and older consumers ignore it.
