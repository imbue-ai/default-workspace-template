`update-self` reads the prose it follows outside its own directory from the release being updated to, not from the workspace's older tree.

- **`update_self.py stage-shared-prose --ref <ref>`** (new) extracts `.agents/shared/`, the `harden-worker` skill and `launch-task`'s references from the target release into `data/.tasks/update-self/skill-at-target/`, beside the staged skill. When the ref predates the skill it stages the workspace's own copies, as `bootstrap-skill` does for the skill. It refuses, staging nothing, when a path is missing.

- **The lead runs it in Step 3a**, after the ceiling re-check settles the target. From Step 3 on, SKILL.md and every reference name those files by their staged path, and say to read a further `.agents/...` path a staged file names from the staged copy when one is there. The worker guide says the same. Steps 1-2 run before anything is staged and name the workspace's copies.

- **A test holds the prose to this**: every markdown path outside the skill's directory, from Step 3 on, uses the staged prefix and names a file `stage-shared-prose` stages.

- **`validate_skill.py`** counts a path into a copy of the tree (`<somewhere>/.agents/...`) as a link to the file it copies, so the depth check sees through the staged prefix.

The scripts the skill mirrors into its own package are unchanged.
