Reverted bootstrap to its state at `c043d0f7a`, as part of taking the accidentally-merged agent branch back off main.

The initial chat agent is created with `--transfer none` again rather than `--type claude`, and the boot-time `uv sync --all-packages --frozen` venv converge is removed. Both landed via `55d1e6e84` as part of in-progress harness work and can be re-landed through their own PRs.
