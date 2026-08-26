`eval-config-small.json` now declares an `expectations` block on the `todo-app` and `landing-page`
cases (an outcome description plus `deliverable.kind = "minds-app"`), which the harbor harness in
`apps/minds_evals` uses to grade what the agent actually delivered. `greeting` deliberately stays
bare -- it commissions no artifact.

This harness is unaffected: it reads only `id`, `persona`, and `prompts` out of each persona entry
and ignores everything else, so the added key changes nothing about how it runs or scores.
