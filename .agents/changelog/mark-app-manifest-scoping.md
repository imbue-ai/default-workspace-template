An app's `app.toml` declares the skills, scripts, and docs built for it that
live outside its directory -- `[[references]]` entries with a `note` naming the
surface each one uses -- plus a `[scope] exclude` list of globs, and
`app-manifest footprint` turns that manifest plus `system/supervisord.conf` into
one scope file. The harden worker writes that file
(`data/.tasks/harden/<slug>/scope.json`, named by the task frontmatter's
`scope_file`) as the first thing it does, and the four places that each derived
an app's footprint by hand read it instead: the app type's tests run over the
whole footprint rather than the app directory alone, so a referenced skill's
tests run when the app's routes move; the merge-time freshness check in
`harden-contention.md` diffs the footprint's paths rather than a hardcoded pair;
`publish-template` proposes the footprint as the include set at its scope gate;
and `verification.md`'s two invocations carry the scope file, a read budget, and
an expansion rule into the review agents through `$ARGUMENTS`, replacing the
creation-context paragraph. A change landing outside the footprint is either
registered as a reference or explained in the worker's report under `Outside
footprint:`. The `type-skill.md` side is the reverse lookup: a skill that calls
an app's routes, CLI, or store checks `app-manifest references --for-path` and
adds the missing entry to the app's manifest as part of its own change. The two
code-guardian gates stay parked, and `verification.md` says so: no run loads it.
