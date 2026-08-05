Repoints the broken `docs/system/style_guide.md` symlink. It targeted
`vendor/mngr/style_guide.md`, which resolves relative to `docs/system/` and so
pointed at a path left stale by the `system/` relayout; it now resolves to the
real file.

The repo-wide prose ratchets skip symlinks resolving into `system/vendor/`,
which is what they always intended -- until now that only worked because this
link was broken.
