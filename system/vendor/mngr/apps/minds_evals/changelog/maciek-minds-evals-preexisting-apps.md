The evidence collector tells a *delivered* app apart from one the workspace already served by
measuring what was there before the agent ran, rather than by matching names against a fixed list.

One probe taken **before turn 1** -- the same `workspace_state` probe the evidence phase runs later,
issued once the workspace has booted and been signed in -- answers both halves of the pre-existing
set, because neither is complete alone:

- The **app registry as it actually stood**. The only source that sees a template app which
  registers its port from inside the script its supervisord program runs: the terminal does exactly
  that, as do the owner-exec and vm-exec daemons, and a config-only derivation would score the
  workspace's own terminal as the case's deliverable.

- The workspace's own **`system/supervisord.conf`**, which the same probe cats and which at that
  moment is still the pinned template's file verbatim, parsed through its `forward_port.py --name`
  invocations. This covers a template app whose service is slow enough that it had not registered
  its port yet: the file is on disk from the moment the workspace is cloned, whatever its services
  are doing.

The set is their union, which stays correct as the template gains and loses apps and for a dwt fork
or branch an eval config points `dwt_repo`/`dwt_branch` at.

The registry is the half that must be readable. Without it the set is *unknown* rather than empty:
the app, HTTP, and UI-flow entries whose meaning depends on the distinction are recorded with status
`error` and reason `preexisting_unknown`, never `failed`, so an agent is never charged for an app it
never shipped just because the instrument could not look. A config section the probe came back
without only means that half contributes nothing. The unconditional registry and service capture
runs either way.

`manifest.json` carries `preexisting_registrations`, the sorted set the collector excluded, so a
grader or reader can see what was subtracted rather than infer it; it is `null` when the set is
unknown, which the manifest keeps apart from a workspace that served nothing. Its `schema_version`
is unchanged: the field is additive, and every consumer reads the manifest by key.
