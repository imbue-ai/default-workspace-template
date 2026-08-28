MIND-186: Synced the root `uv.lock` to the `modal` dependency-floor changes in this PR. The `requires-dist` metadata for `mngr-minds-eval` and `minds-evals` now records `modal>=1.4.3` (previously `>=1.0` and unfloored, respectively).

No resolved versions change: `modal` stays pinned at 1.4.3 by the `modal==1.4.3` owner pins in `libs/mngr_modal` and `libs/modal_proxy`. Only the lockfile's recorded dependency specifiers move.
