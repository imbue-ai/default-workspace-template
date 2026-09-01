Bumped the monorepo's pinned `modal` SDK from `1.4.3` to `1.5.4` to adopt Modal's V2 Sandbox backend, adding a targeted `exclude-newer-package` uv override for `modal` (1.5.4 postdates the global supply-chain cooldown cutoff but has been public well past the 2-week window).

Enabled the V2 Sandbox backend (`MODAL_SANDBOX_V2=1`) in the two standalone Modal-sandbox scripts (`scripts/snapshot_minds_e2e_state.py`, `scripts/modal_sandbox_list_bug_repro.py`), both via `setdefault` so V1 can still be forced with `MODAL_SANDBOX_V2=0`.

Mirrored the `modal` `exclude-newer-package` override into the public mirror's `mirror/overlay/pyproject.toml` and regenerated `mirror/overlay/uv.lock`, so the mirror gate can resolve `modal==1.5.4` when it re-locks the public tree.
