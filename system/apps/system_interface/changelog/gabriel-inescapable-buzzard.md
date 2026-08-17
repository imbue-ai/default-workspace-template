Reverted the system interface to its state at `c043d0f7a`, as part of taking the accidentally-merged agent branch back off main.

This removes the in-progress harness work that landed via `55d1e6e84`: the per-harness shoulder tap (atomic interrupt-and-flush for codex and pi), the message queue mirror and stop-button retract machinery, the live model-state bar and its pickers, the codex/pi launcher labels and sign-in copy, the Powered-by credit, and the e2e suite's move onto Fortress.

The reverted work is still reachable in git and can be re-landed through its own PRs.
