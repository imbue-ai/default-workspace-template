Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this bucket: the user-data-layout blueprint and apt-mirror-worker plan, `private.just`/skill updates following the template root declutter (vendored mngr at `system/vendor/mngr/`, secrets at `data/.secrets/`, layout-aware Lima bake), the `just deploy-apt-mirror` / `just test-apt-mirror-worker` recipes with the mirror Worker CI job, and `.minds/template/apt-mirror.sh` as the schema for the single global apt-mirror Vault entry.

`just propagate-changes` resolves the workspace docker container by its current name (`<prefix><name>`, as the minds app creates them) and falls back to the older `<prefix><name>-host` form, instead of failing on the stale suffixed name.
