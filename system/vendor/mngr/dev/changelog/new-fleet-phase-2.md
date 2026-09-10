Slice-fleet cutover phase 2 (mngr-internal `new-fleet-phase-2`):

- `specs/slice-fleet/spec.md` carries a status header: its phases 3-6 are superseded by `blueprint/slice-fleet-cutover/plan-slice-fleet-cutover.md`; the conversion, the migration release test, the generation lease filter and the lazy `disk_gb` backfill it described are deleted.

- The cutover plan records the phase-2 decisions (migration 039 stamps a gen-1 row with its post-cutover disk size; the restore grows the transplanted disk by the 16 GiB gen-2 base and the preflight fit check counts it; the stop/start release test stays gated until phase 5) and lists the three small follow-ups that land in the phase-2 PR after the main changes.

- The cutover plan records the three follow-ups as landed (gen-2 image content hash, single-flight box dials, the shared sweep classifiers) and `specs/remote-workspaces-in-ci.md` describes the CI slice sweep as generation-agnostic.
