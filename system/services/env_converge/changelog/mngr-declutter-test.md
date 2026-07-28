Integration branch combining the workspace-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: convergence units and the overlay list move to `system/scripts/env.d/` (the package itself moves to `system/libs/env_converge`).

Event emission serializes the envelope with `model_dump()`: it previously called `to_jsonl_dict()`, which only exists on `imbue_common`'s `LogEvent`, so the slow phase crashed at its first emitted event (env.d units after that point never ran and the rootfs stamp was never written).
