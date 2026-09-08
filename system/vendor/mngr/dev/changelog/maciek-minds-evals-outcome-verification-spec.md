Add `specs/minds-eval-harbor/outcome_verification.md`: a design spec for outcome verification in the harbor persona evals (`apps/minds_evals`).

The spec enumerates the space of artifact-aware verification (static file checks, the agent's own tests, liveness probes against the workspace's app registry, LLM-driven UI flows, and vision judging of screenshots), maps each option onto what harbor, rewardkit, and the Minds workspace already provide, and specifies the design: a per-case `expectations` block in the eval config, a trial-time evidence-collection phase in the driver, and a grade-time `outcome` scoring dimension combining programmatic checks with an LLM judge over the captured evidence.

Add `specs/minds-eval-harbor/flow_executor_forwarded_origin.md`: the replacement executor for UI flows -- a box-side Playwright browser driving the delivered app's forwarded origin through the `mngr forward` plugin, superseding the workspace-browser-fleet executor (whose security-model coupling and under-the-product fidelity gap are recorded in the main spec) and keeping both target surfaces (direct origin, full Minds UI) open in the schema.

Spec only -- no behavior changes.
