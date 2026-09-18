- The frontend build reads the embed contract and the service icons from
  `system/vendor/mngr-assets/` (fetched from the pinned mngr commit by the npm
  workspace root's `prebuild`/`pretest`) instead of a vendored mngr tree.
  `update_staleness.py` no longer treats a vendored mngr path specially.
