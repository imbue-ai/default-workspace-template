# Reuse what already exists

- `fetch-process-show` now claims the case where the ask names the surface
  rather than the data ("visualize my roadmap from the export I uploaded"), and
  `build-app` sends an app that reads records from outside itself there before
  it plans anything. An app whose ingestion was hand-written inside `build-app`
  has no entry point anyone can re-run when the next batch of data lands.

- `update-self`'s plan tests read the set of app tools off the tree but spelled
  out the template's five when asserting what a shared manifest refreshes, so
  the file failed in any workspace where the user had built an app. The
  assertion now reads the same set it derives.
