Reverted the `.agents` tree to its state at `c043d0f7a`, as part of taking the accidentally-merged agent branch back off main.

This removes the `engineering-subordinate` output style and the codex/pi harness selection threaded through the worker-skill installer, the `launch-task` worker creator, the workspace migration script, and the `crystallize-creation`, `heal-creation`, `update-creation`, and `update-system-interface` skills. All of it landed via `55d1e6e84` and can be re-landed through its own PRs.
