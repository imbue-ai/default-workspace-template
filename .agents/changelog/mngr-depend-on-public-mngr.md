- The update-self refresh reinstalls the `mngr` tool from the public-repo commit
  `pyproject.toml` pins for `imbue-mngr`, and adds the plugins
  `system/config/mngr_plugins.toml` assigns each tool from that same commit.

- `submit-upstream-changes/references/mngr-changes.md` describes mngr as a pinned
  dependency: there is no git checkout of it in a workspace and no way to run
  another mngr there, so mngr changes are developed in an mngr-internal checkout on
  a developer's machine, land as their own mngr PR, and reach the template by a
  pin bump.
