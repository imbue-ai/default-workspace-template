Variable machine sizing (specs/slice-fleet, phase 1): the new `specs/slice-fleet/spec.md` (variable machine sizing and the gen-2 completion) supersedes `specs/slice-fleet-gen2/spec.md`, which gains a pointer header and remains as the historical record.

This branch also carries the earlier slice-fleet gen-2 phase 1-4 and variable-sizing blueprint work as its base; see the `mngr-slice-fleet-gen2-phase-*` and `mngr-variable-sizing` entries in this same PR.

The canary-session handoff (`blueprint/slice-fleet-variable-sizing/HANDOFF.md`) records what the dev canary verified, the five live-caught bugs, the environment state, and the remaining phase-2 work (conversion, drain, management-plane activation, telemetry hand-verification).
