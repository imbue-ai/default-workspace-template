The spec that governs the chat app and the workspace shell, the workspace app model, lives in default-workspace-template at `docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md`, next to the code it governs; this repo keeps no spec of its own for it. (An interim `split-chat-apart` plan, which kept mngr and harness knowledge in the workspace shell and froze the per-kind addressing schemes, was drafted and dropped on this branch before it merged.)

The minds-eval-harbor outcome verification spec no longer names the deleted `system/apps/terminal/run_ttyd.sh` as the terminal's registration path: the terminal registers from inside the `terminal-app` package's own entry point.

The same spec describes the supervisord join as reading both forms of a program's `forward_port.py` registration (`--name`, or the block's own program name for a `--manifest` registration), matching the evidence collector.

`scripts/snapshot_minds_e2e_state.py` gives the workspace image's `docker build` 900 seconds (through `MNGR__PROVIDERS__DOCKER__BUILD_TIMEOUT_SECONDS`) instead of the docker provider's 600-second default: the build is network-bound and a healthy one already runs 8 to 10.5 minutes, so the default cut off four consecutive `build-minds-snapshot` runs during a stretch of npm registry slowness in the pi extension installs.

The snapshot script's unit test now also rejects unbound names in every scope of the in-sandbox runner program (a plain string run in a separate process), at module level and inside any helper the program defines, so a reference to one of the script's own constants fails at unit-test time instead of as a NameError after the multi-minute image build in CI.

The workspace app model's phase 10 lands on the paired default-workspace-template branch: the chat app is its own package, program, and frontend build there, and the evals bridge in this repo follows it to the chat app's registered port.

The minds-eval-harbor specs (`concise.md`, `outcome_verification.md`) describe the bridged calls as reaching the workspace's chat app at its registered port, where they named the system_interface on port 8000.
