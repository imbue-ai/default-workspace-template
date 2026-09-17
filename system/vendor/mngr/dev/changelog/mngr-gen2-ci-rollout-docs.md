The remote-workspaces-in-CI spec records that both standing CI boxes were repaved to gen-2 on 2026-09-13.

The slice-fleet cutover blueprint gains `migrate-latchkey-state.md`: the spec for carrying a workspace's machine-owned latchkey state (credential store, gateway config and policy, supervisor programs, tmpfs secrets) through `minds-admin cutover migrate`, written from the 2026-09-13 staging drill that found both migrated workspaces signed out of every service.

`specs/workspace-stop-kinds.md`: the spec for recording *why* an imbue_cloud machine was stopped (owner / maintenance / idle / suspension) and who may start it again, written from the 2026-09-13 staging re-test in which the desktop's unattended recovery restarted a workspace between the migrate's stop and its park. The phase-5.5 blueprint's "Expected behavior" now describes that window as the `maintenance` stop kind (409 `workspace_under_maintenance`, the desktop's Maintenance badge, no unattended start) instead of the retired `workspace_migrating` code, and notes that a <= 0.6.0 desktop needs an app restart after its migration for its desktop-forwarded latchkey routes.

`specs/workspace-stop-kinds.md` and `blueprint/slice-fleet-cutover/migrate-latchkey-state.md` record that their live staging drills passed on 2026-09-14 (the old-client case of the stop-kinds spec; the latchkey leg with the desktop open).
