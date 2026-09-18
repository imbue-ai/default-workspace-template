CI and the developer scripts follow the desktop app's rename to Mind: the launch-to-first-message and runner-reset workflows, the runner reset script, and the end-to-end harness all install and drive `/Applications/Mind.app`.

The runner reset script had been spelling the bundle `/Applications/minds.app` while the workflow installed `/Applications/Minds.app`; the two only ever agreed because the filesystem is case-insensitive, and its `pgrep` never matched at all. Both now use the one real name.

The ratchet that keeps product references out of mngr-level code now also matches the singular, so it keeps working after the app's rename to Mind. It matches the ordinary word and MIND-<n> Linear references too, which ride in the recorded count rather than in a set of exclusions. Widening it found four real references, all fixed here.

Type checking is clean again. `uv run ty check` had been reporting seven diagnostics repo-wide, six of them because psycopg2's SQLSTATE exception classes are injected into `psycopg2.errors` by its C extension, so nothing declares them to a type checker; the typeshed stubs are now a dev dependency. Importing the submodule explicitly, which the diagnostic suggests, makes it worse: the module resolves and its injected members are then rejected outright.

The runner reset kills the app by matching both the old and new bundle names. `pgrep -f` matches an extended regular expression, in which `\?` is a literal question mark rather than an optional preceding character, so the pattern had been matching nothing at all and the reset left the app running on top of the directory it then tried to remove.

The runner reset removes a pre-rename bundle as well as the current one, so an old install cannot survive a reset on the shared runner.
