Integration branch combining the workspace-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this bucket: the workspace root is decluttered into `creations/`, `data/`, `docs/`, and `system/` on the trixie + `/home/user` layout, and apt sources now default to imbue's snapshot-pinned mirror at `https://apt.imbuepackages.com` with the pinned timestamp advanced to the first cut on the live mirror (20260725T000000Z).

Fix `system/scripts/write_apt_sources.sh` to resolve the committed `.mngr/apt-snapshot-timestamp` from the repo root: after the declutter moved the script to `system/scripts/`, the no-argument invocation (the Lima/Modal fresh-provision path via `setup_system.sh`) looked for the timestamp one level too shallow (`system/.mngr/`) and failed.

Fix `system/scripts/run_ttyd.sh` the same way: its `REPO_ROOT` also stopped at `system/` after the declutter, so the vendored OSC-52-patched ttyd web client was looked up at a nonexistent `system/system/vendor/...` path and the terminal silently fell back to the stock client (dropping clipboard integration).

Fix `system/scripts/default_workspace_template_seed.sh` to invoke `seed_home_skeleton.sh` at its decluttered `system/scripts/` location: the docker-provider seed step still used the pre-declutter `scripts/` path, so post-create seeding (and with it workspace creation) failed on decluttered images.

Add the missing `[providers.imbue_cloud_slice]` block to `.mngr/settings.toml` with the user-data-layout knobs (`host_dir=/home/user/.mngr`, `volume_home_path=/home/user`, `host_log_dir=/var/log/mngr`): the slice pool bake creates the services agent with `MNGR_HOST_DIR=/home/user/.mngr`, but the slice provider still carved with the legacy `/mngr` layout, so the bake finalize (and any later lease) could not find the baked agent.

Fix two workspace-runtime bugs from the declutter/env-converge cutover: the browser fleet's readiness gate now checks the installed Fortress binary (`/opt/fortress/tilion-fortress/tilion`) instead of the retired deferred-install marker (which nothing writes anymore, so every new-layout workspace reported "Chromium is still installing" forever), and env-converge event emission serializes with `model_dump()` (it called a `to_jsonl_dict` that only exists on a sibling class, crashing the slow phase at its first event).

The `[providers.imbue_cloud_slice]` block is marked `is_enabled = false` (like the other pool provider blocks) so the in-workspace mngr, which lacks the imbue_cloud plugin, skips it instead of failing settings parsing with an unknown-backend error (which broke the workspace's own `mngr create`/`stop` during slice bakes).
