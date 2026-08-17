# Strip-readiness checklist (user-controlled keys, Phase 5 -- design only)

Companion to [plan-user-controlled-keys.md](./plan-user-controlled-keys.md).
"Stripping" is the deferred final phase of the user-controlled-keys work:
removing the pool management key and the bake key from adopted slices'
`authorized_keys`, so that after adoption the user's devices hold the *only*
credentials that open a slice's VM root and container. This document is the
readiness checklist -- no stripping is implemented until every item below is
resolved. It is versioned with the plan so the checklist and the fleet evolve
together.

## How the strip mechanically works

Adoption already made this cheap: the in-VM reconciler owns
`/etc/mngr/root_authorized_keys.desired` and re-asserts it on every boot, and
`render_desired_authorized_keys` currently preserves all pre-existing lines
(pool + bake keys included) by design. The strip is therefore *a desired-state
content change*: stop preserving the pool/bake key lines when rendering the
desired file (client-side, in `ensure_adopted` / `rotate`), and the reconciler
propagates the removal on its next run and every boot thereafter. No new
mechanism, no fleet-wide operator action -- each host strips the next time an
up-to-date client verifies it. The container's `authorized_keys` needs the
same treatment through the existing append/remove commands.

## Checklist

Every item must be confirmed (with the code path or telemetry named) before
the desired-state change ships.

1. **Stop/start supervisor's in-VM operations.** Audit
   `apps/remote_service_connector` workspace-lifecycle supervisors and the
   box-side stop/start scripts: today the box-level scripts run via the pool
   management key on the *box* (`hosts.py` `_pool_ssh_client`, limactl
   drive), which stripping does not touch -- but confirm no step SSHes into
   the *VM root* or the *container* using pool/bake keys (e.g. graceful
   container shutdown, readiness probes after restore). Any such step must
   either move to box-level `limactl shell` or be retired before the strip.

2. **Web-only sharing primitive's container access.** The server-side
   `enable-sharing` path (kept for web-created workspaces after Phase 1)
   writes `share.env` and the grants document into the container over SSH
   with the pool key (`_enable_sharing_core` in the connector's `hosts.py`).
   For *adopted* rows this must be retired or re-routed
   (box-level `limactl` injection, or requiring the desktop's client-side
   path) before stripping, or web-created workspaces that later get adopted
   lose the share flow. Decide and document the mixed case: a web-created,
   never-adopted workspace may keep the server path.

3. **Operator repair and diagnostics paths.** `mngr imbue_cloud admin
   repair-keys` and any runbook that says "SSH into the slice" must be
   confirmed to work through box access (`limactl` / box pool key) only.
   Grep the operator docs and `slices/` code for direct VM-root or container
   SSH that assumes the pool key is in the *VM's* `authorized_keys`; the
   sweep's current design (box access + copies upward) is the pattern to
   hold every path to.

4. **Rebuild and lease paths.** The slow-path rebuild and the bake tooling
   still provision fresh (not-yet-adopted) slices with pool/bake keys --
   that is fine (the connector is trusted at handoff); confirm the rebuild
   completes and re-runs adoption *before* any strip-state client verifies
   the host, so a rebuild never oscillates between stripped and unstripped
   desired states.

5. **Boot-window gap.** Between VM boot and the reconciler's run, sshd
   serves whatever cloud-init replayed -- including the bake-time
   `authorized_keys`. Post-strip this is the only remaining window where
   bake keys open the VM. Closing it means ordering sshd start after the
   reconciler unit (or having cloud-init render the desired file itself);
   decide whether the strip ships with or before that ordering change.

6. **Trigger condition (open question from the plan).** Define the
   fleet/telemetry condition that flips the strip on: candidate criterion is
   "N consecutive days with zero repair-keys sweep findings and zero
   adoption-failure warnings across the tier, and every active slice's
   record shows a post-adoption client version". Until it is defined and
   measurable, the strip stays off everywhere.

7. **Break-glass story post-strip.** With pool/bake keys gone from adopted
   hosts, the only operator paths into a wedged slice are box-level
   (`limactl shell`, disk access). Confirm the break-glass runbook
   (single-host `repair-keys`, backup restore) needs no in-VM SSH, and that
   support accepts "the operator cannot SSH into your VM" as the shipped
   posture (it is the point of the feature).

## Rollout shape (when the checklist clears)

Tier pipeline as usual (dev -> staging -> production), strip enabled by
client version, not by server flag: clients that render desired states
without pool/bake keys simply start winning on their next ensure-adopted
verification. A hotfix release that re-adds the preserved lines is the brake,
exactly as for adoption itself.
