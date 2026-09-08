Fix the `minds-start-cloud` justfile recipe to build the desktop client's SPA bundle (frontend generate + build) before launching Electron, matching `minds-start`. Without it, launching the cloud-mode client from a fresh worktree served 404s because the gitignored `static/ui/` bundle was never produced.

Exclude `apps/minds/docs/deploy/**` from the public mirror (`mirror/copy.bara.sky`): the relocated deployment/operations docs and the new per-release deploy history stay private by default. Also update doc-path references in the release skills, `.minds/template/storage.sh`, and `scripts/rename_template_repo_test.py`.

Register the new `apps/minds/test_bundled_agent_types.py` in the snapshot-stage offload config's scoped discovery paths (`offload-modal-minds-snapshot.toml`), so the bundled-agent-types guard actually runs in the `minds_snapshot_resume` CI stage.
