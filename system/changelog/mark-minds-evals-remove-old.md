Retired the `eval_worker` service. It was the in-workspace driver for the bespoke `mngr_minds_eval`
persona-eval harness, which has been removed from the mngr monorepo in favor of the host-side
harbor-based `minds_evals` harness (the new harness drives conversations from the host and needs no
in-workspace worker). The `system/services/eval_worker/` package, its `[program:eval-worker]`
supervisord block, and its workspace-dependency wiring are removed. Normal workspaces are unaffected
-- the worker already no-op'd whenever its slotted `test_case_metadata.json` was absent, which was
every normal workspace.
