The generator's console script is now `minds-evals` (it was `minds-evals-harbor`), so dataset generation reads `uv run --project apps/minds_evals minds-evals generate --config <f> --output <dir>`. `just minds-evals-generate` already invokes it under the new name.

The name was free because the pre-harbor harness that held it, `apps/mngr_minds_eval`, is deleted in this change: this app is the only Minds persona eval harness now. Its history is reachable with `git log --all -- apps/mngr_minds_eval`, including its own CHANGELOG.md and UNABRIDGED_CHANGELOG.md.

Nothing else about running an eval changes: same driver, same config schema, same task layout.

`configs/eval-config-byo.json` and `configs/eval-config-byo-small.json` are removed. Both pinned `mngr_branch: minds-byo-cloud-accounts`, a branch that no longer exists on the remote, so generating from either failed at branch resolution. `eval-config.json` and `eval-config-small.json`, both on `main`, are what remain.

The wordiness guard's default baseline (120 words per agent turn) is documented for what it is: an unmeasured seed, not a figure taken off real batches. Ground it by taking the mean over a batch of real runs and setting `avg_word_count_baseline` in the eval config.
