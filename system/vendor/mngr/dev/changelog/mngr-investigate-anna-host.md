# Snapshot build script: refreshed the workspace-create exec budget comment

Updated the sizing comment on the 1500s sandbox-exec budget in `scripts/snapshot_minds_e2e_state.py` to match the e2e runner's raised 900s post-submit create budget: the in-sandbox workspace container build legitimately takes ~8-10.5 minutes in CI, so the comment no longer describes it as "a few minutes" with large headroom. No functional change.
