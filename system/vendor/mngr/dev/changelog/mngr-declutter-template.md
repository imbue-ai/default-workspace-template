`private.just` recipes (sync-vendor-mngr, minds-start, bake helpers) and the minds dev-workflow/release skills follow the default-workspace-template root declutter: the vendored mngr now lives at `system/vendor/mngr/` inside the template.

`just forward-system-interface` injects the Cloudflare tunnel token at the workspace's new secrets location, `data/.secrets/cloudflare_tunnel.env` (previously `runtime/secrets/`).

`scripts/rename_template_repo.py`'s follow-up hint points at `system/vendor/mngr` (the vendored tree's decluttered location).

The Lima image bake (`scripts/lima_image/bake_provision.sh`) resolves the template's toolchain build scripts from the cloned tree's own layout (`system/scripts/` on decluttered refs, `scripts/` on pre-declutter tags) and exports `REPO_ROOT` so those scripts build against the bake clone (the decluttered scripts otherwise default to `/home/user/workspace`, which does not exist in the bake VM), so baking against a decluttered template ref no longer fails.
