- The update-self refresh reinstalls the `mngr` tool from the source
  `pyproject.toml` gives `imbue-mngr` -- a pinned public-repo commit, or an
  editable path into a local tree at `system/vendor/mngr` -- and adds the plugins
  `system/config/mngr_plugins.toml` assigns each tool from that same source.
  Editable extras are keyed on the package name, so a pin-to-tree switch replaces
  a plugin the receipt still names by its git pin rather than installing it twice.

- `submit-upstream-changes/references/mngr-changes.md` describes mngr as a pinned
  dependency: there is no git checkout of it in a workspace, so mngr changes get
  their own checkout and PR on the mngr repo, and the template picks them up by
  moving its pin.
