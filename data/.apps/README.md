# data/.apps/

Stored data for each app, one folder per app name. An app's code lives at
`/home/user/workspace/system/apps/<name>/`; whatever it saves -- records,
caches, snapshots, user content -- goes in `data/.apps/<name>/`.

Dot-prefixed because it is machinery-managed: apps read and write here through
their `DATA_DIR` constant, so the layout inside each folder belongs to the app,
not to the user.
