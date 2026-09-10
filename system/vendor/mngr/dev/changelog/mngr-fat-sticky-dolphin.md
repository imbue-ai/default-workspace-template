The `release-minds` skill now starts by disabling the code-guardian stop hook's base-branch merge and reporting back which state it found, so an agent cutting a release does it without being told.

The hook merges and pushes `origin/main` on every stop, in the mngr checkout and in the `default-workspace-template` checkout holding the release's frozen `system/vendor/mngr`, which is exactly the state a release must keep pinned.
