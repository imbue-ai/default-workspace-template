Integration branch combining the workspace-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this bucket: the workspace root is decluttered into `creations/`, `data/`, `docs/`, and `system/` on the trixie + `/home/user` layout, and apt sources now default to imbue's snapshot-pinned mirror at `https://apt.imbuepackages.com` with the pinned timestamp advanced to the first cut on the live mirror (20260725T000000Z).

Fix `system/scripts/write_apt_sources.sh` to resolve the committed `.mngr/apt-snapshot-timestamp` from the repo root: after the declutter moved the script to `system/scripts/`, the no-argument invocation (the Lima/Modal fresh-provision path via `setup_system.sh`) looked for the timestamp one level too shallow (`system/.mngr/`) and failed.
