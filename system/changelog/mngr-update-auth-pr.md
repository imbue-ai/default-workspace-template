This is the merge of `minh/auth-provider-lanes` (the provider-accounts sign-in work) into current `main`; see that branch's `minh-auth-provider-lanes.md` entry for what actually changed.

The Dockerfile conflict is resolved to `main`'s shape (every toolchain pin lives in `system/scripts/setup_system.sh`, one combined installer RUN layer) while keeping the branch's Antigravity bump: the copied installer is `agy_install-1.1.22.sh`, matching the setup_system.sh reference the branch already carried.
