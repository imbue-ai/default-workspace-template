`minds-admin cutover preflight` and `cutover migrate` no longer refuse a workspace whose checkout carries no `minds-v*` tag (a workspace created from a branch rather than a release tag holds no tags at all): the version probe now falls back to the `FALLBACK_BRANCH` pin of the workspace's vendored mngr, which names the release image the migrate replays it onto.

The cutover migrate's image-seed bake (which checks the template out at the migrating workspace's release tag to build the image its container is replayed from) now runs its inner `mngr create` with `MNGR_ALLOW_UNKNOWN_CONFIG=1`, so a template older than the operator's mngr (tags before minds-v0.5.0 still name the renamed `auto_dismiss_dialogs` field) no longer fails the seed and strands the workspace parked. Real pool-row bakes keep the strict parse.

`cutover migrate` now publishes a workspace's release image (the seed bake on the target box) right after the harvest and before the product stop, so a version whose image cannot be built fails with the workspace still running for its owner rather than parked.

The migrate's image-seed bake now stops as soon as its `mngr create` returns (the release image tar is saved on the box by then) and tears its throwaway slice down, instead of running the pool-row finalize and gates: a pre-0.5.0 template's boot chat failed the primary-agent check, so every seed of such a version failed three full carves after the tar already existed.

`cutover preflight` and `cutover migrate` now probe the workspace container's home layout and refuse a legacy-layout workspace (a slow-path rebuild whose `/home/user` lives in the container's writable layer, with only `.mngr` on the volume), naming `minds-admin repair-home-layout --migrate` as the remedy: the migrate transplants the data disk only, so such a workspace came back with an empty home.

`cutover migrate --workspace` now validates its value as a full UUID and answers a usage error for a prefix (the preflight table prints 8-character prefixes), instead of a raw database error.

`cutover migrate` now corrects a gen-1 row still carrying migration 039's unmeasured `disk_gb` fallback (44, stamped on every workspace that was stopped on no box when 039 ran) from the data disk it measures at the harvest, instead of refusing the mismatch; `cutover preflight` reports the pending restamp as a warning. A measured stamp that disagrees is still refused.

`cutover migrate --source-server-id` now re-selects the rows an earlier run parked off that box mid-migration onto the same target (a parked row no longer sits on any box, so the box's row listing missed it and a resume needed `--workspace`); records parked onto a different target are reported in the log instead. The dry run says which rows would resume from their record rather than start afresh.
