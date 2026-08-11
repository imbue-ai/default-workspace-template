Reverted the automation runner to its state at `c043d0f7a`, as part of taking the accidentally-merged agent branch back off main.

The persistent automation agent is created with `--transfer none` again rather than a selectable `--template "$HARNESS"`. The harness-selection work landed via `55d1e6e84` and can be re-landed through its own PR.
