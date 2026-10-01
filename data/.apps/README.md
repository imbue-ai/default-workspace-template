# data/.apps/

Stored data for each app, one folder per app name. An app's code lives at
`/home/user/workspace/system/apps/<name>/`; whatever it saves -- records,
caches, snapshots, user content -- goes in `data/.apps/<name>/`.

Dot-prefixed because it is machinery-managed: apps read and write here through
their `DATA_DIR` constant, so the layout inside each folder belongs to the app,
not to the user.

Everything here is in the hourly backup except two things. The backup's
exclude patterns match at any depth, so a directory here named like a build
output or cache (`build`, `dist`, `target`, `.cache`, `node_modules`, `.venv`,
and the rest of the defaults) is skipped too: never keep user data in one. And
a directory holding a `CACHEDIR.TAG` file keeps only the tag, which is how an
app marks a store it can rebuild. See `system/services/host_backup/README.md`
for the patterns and "Slow backups".
