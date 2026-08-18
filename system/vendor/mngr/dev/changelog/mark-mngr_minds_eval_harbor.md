Added `specs/minds-eval-harbor/concise.md`: the design for converting `apps/mngr_minds_eval` to a Harbor-framework eval as a three-PR stack (new `apps/minds_evals` app alongside the old harness, a side-by-side comparison, then replacement).

The design was settled after mapping the existing harness onto the harbor 0.21.0 API surface and smoke-testing the load-bearing mechanisms on Modal (single-container tasks, DinD compose, and nested Modal sandbox creation from inside a harbor environment).
It specifies a nested-sandbox topology that preserves the production workspace-creation path, a host-side persona-driver harbor agent, pure-rewardkit verification, and harbor-native results for both CI and local development.

Also logged a stale-comment conflict in `uncertainties.md` (`s3_store.py` batch-prefix comment vs code).

Added an "Implementation corrections (PR1)" section recording where the built harness diverged from the design during implementation (the workspace auth mechanism, the harbor dependency pinning, reward gating via a finalize step, the clean judged conversation artifact, the SHA-as-file and `/home/user` snapshot details, and the nightly operating model).
