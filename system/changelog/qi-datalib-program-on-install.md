The Datalib app's supervisord program no longer ships in `system/supervisord.conf.d/`. A workspace has no datalib program until the `datalib` skill is first used, so a backup restore's `supervisorctl restart all` no longer starts it and then reports "datalib" as a service that did not come back up.

- `system/supervisord.conf.d/datalib.conf` moved to `system/apps/datalib/datalib.conf`, now with `autostart=true`: once installed, datalib starts at boot and after a restore like any other app.
- `.gitignore`: ignores `system/supervisord.conf.d/datalib.conf`, the copy the install script writes.
