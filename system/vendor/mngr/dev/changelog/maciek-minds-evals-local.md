Added `specs/minds-eval-harbor/improvements.md`, a follow-up backlog for the harbor-based Minds persona eval (`apps/minds_evals`) alongside the design spec it accompanies.

It records what a local end-to-end verification run turned up -- Modal environments leaking one per trial, the unmeasured placeholder baseline behind the wordiness guard, the oracle fixture scoring below its own documented floor on conversational cases -- plus the credential-injection change that would put the eval back on production's shared-config auth path, what a periodic CI run still needs, and the packaging cleanup that would drop the workspace-global `rich` override.

It also notes a reproducibility hole: the eval resolves `mngr_branch` to an exact SHA but carries `dwt_branch` as a plain branch name and clones it inside the box at trial time, so one dataset builds its workspaces from whatever `default-workspace-template` main happens to be that day.

Finally it folds in two adjacent threads. The model-routing work (FSR and NVIDIA's Switchyard proxy) lands on this harness as its measurement instrument, so the backlog covers why routing through the LiteLLM proxy is what makes per-case cost attribution possible, why that makes the credential change a prerequisite rather than a neighbour, and what to test before building anything. And dwt PR #427, which adds codex and pi harnesses to the workspace, would reach the eval the moment it merges -- while offering no programmatic way to authenticate either new harness.

Documentation only: no code, configuration, or dependency changes.
