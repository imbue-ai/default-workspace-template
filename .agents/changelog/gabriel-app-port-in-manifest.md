The build-app scaffolder can now see every port the workspace holds. Its port
pre-flight read the supervisord config and the runtime app registry, and missed
any port declared in neither -- chat's 8010, the file viewer's 8300 and the
terminal's 7681, none of which appear in a supervisord command because those apps
register themselves at runtime. On a fresh clone or a worker worktree the
registry does not exist at all, so the pre-flight could hand a new app a port one
of them already held, and the first sign of it was the new app crash-looping on a
failed bind.

The pre-flight now also reads every `system/apps/*/app.toml`, where an app
declares the origin it serves. The hand-maintained "avoid 8000, 8010 and 8081"
note in the skill is gone -- it had already gone stale, missing four ports -- and
so is the advice to check with `ss -tln`, which is not installed in every
workspace and exits with an error rather than reporting nothing is bound.

A scaffolded app now names its port once, in its manifest: the generated program
command no longer repeats it, and the generated runner reads it from `app.toml`
(the `<APP>_PORT` override still works). The three built-ins that register
themselves still hold a matching constant in their own source, which a new guard
pins to the manifest. migrate-workspace reads the manifests on
both sides when it reconciles ports, so an app that never started on the source
still reports the port it holds.
