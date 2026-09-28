- Local Docker workspaces' `/tmp` tmpfs is now capped at 1 GiB (`[providers.docker]` start args `--tmpfs /tmp:exec,size=1g`), matching cloud workspaces, whose `/tmp` mngr caps at an eighth of the VM's RAM. A large write to `/tmp` now fails with "No space left on device" instead of filling the container's memory and taking the workspace down. AGENTS.md tells agents `/tmp` is a small RAM disk and where large files go.

- The workspace app model contract (section 17) and the desktop interface contract (section 13) say an isolated instance's `copies/` also hold what its `--copy` flags name.
