This is the merge of `minh/auth-provider-lanes` (the provider-accounts sign-in work) into current `main`; see that branch's `minh-auth-provider-lanes.md` entry for what actually changed.

Conflicts resolved here keep both branches' intent: `main`'s boot-time update-apply recovery (marker constants, the rollback, the DRI-agent wake, the recovery cron guard) stays, while the initial-chat creation (its constants, helpers, and tests) goes, as the branch removed the boot chat. `main()` now runs the rollback recovery first, then the branch's every-boot `_ensure_git_identity` and the signal-gated `_initialize_workspace_main_branch`.

Two adjustments beyond the textual merge: `main`'s ordering test (`test_main_rolls_back_before_the_venv_sync_and_wakes_the_agent_after_it`) inlines the environment setup its now-deleted `_bootstrap_env` fixture provided, and the two git-identity tests isolate `GIT_CONFIG_GLOBAL` so a developer machine's own identity cannot leak into their assertions.
