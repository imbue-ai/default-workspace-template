Added the phase-5.5 incremental gen-2 rollout spec (`blueprint/slice-fleet-cutover/phase-5.5-incremental-rollout.md`): replaces the per-tier flag-day cutover with per-workspace migration (channel-cohorted 0.5.x/0.6.x pools, `cutover migrate`/`rollback` built on the product's gen-1 stop artifact, a `max_box_generation` lease guard for old clients, empty-box-only repaves).

Implemented the spec: the plan doc's Phase 5 and phase-6 gate now describe the incremental rollout (channel cohorting on the minds-v0.6.0 boundary, per-workspace migrate/rollback, empty-box repaves).
