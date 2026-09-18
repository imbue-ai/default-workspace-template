- The update-self refresh reinstalls the `mngr` tool from the public-repo commit
  `pyproject.toml` pins for `imbue-mngr`, and adds the plugins
  `system/config/mngr_plugins.toml` assigns each tool from that same commit. A
  workspace built before the pin, whose tool receipt names editable paths into
  the vendored tree the update deletes, is refreshed from the manifest alone
  rather than failing on those paths.

- `submit-upstream-changes/references/mngr-changes.md` describes mngr as a pinned
  dependency: there is no git checkout of it in a workspace and no way to run
  another mngr there, so mngr changes are developed in an mngr-internal checkout on
  a developer's machine, land as their own mngr PR, and reach the template by a
  pin bump.

- `classify-merge` no longer emits the `editable_tool` path class: no release diff can
  touch a vendored mngr tree, so nothing classifies as one.

- The `assist` skill treats an mngr issue as report-only: it needs an mngr change and a
  template pin bump, not a workspace fix or a new desktop-app build.

- The `publish-template` skill drops its Google-OAuth push-protection note: the client id
  it described lived in the vendored tree, which the template no longer carries.
