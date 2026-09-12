Refreshed `system/vendor/mngr` to mngr `05a40d71b7` for the minds-v0.5.2 release, and regenerated the root `uv.lock` that pins the vendored mngr libraries as editable path deps.

The snapshot is the `git archive` of that exact mngr commit, which is the vendor-match invariant the release depends on: the desktop binary runs the mngr SHA while the in-VM agent imports this vendored copy, so the two must agree file for file.
