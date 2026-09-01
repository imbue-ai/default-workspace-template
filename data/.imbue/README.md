# data/.imbue/

Imbue's own collection footprint inside this workspace, kept here so that
everything Imbue records is readable from in here.

- `analytics/` - Appears only in imbue-hosted workspaces of explorer-plan
  accounts: the analytics collection script (injected on each run so you can
  always read what ran) and its run audit. See
  `docs/system/analytics-collection.md`.
- `plans/` - Plans written for offline analysis when an app is built. Nothing
  in the workspace reads them back and no agent may use them; see the
  `CLAUDE.md` in that folder.
