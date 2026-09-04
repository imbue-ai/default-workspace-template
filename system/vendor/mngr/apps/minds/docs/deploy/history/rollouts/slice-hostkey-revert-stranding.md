# Slices stranded on their bake-time SSH host key

A leased mind whose slice VM came back on its bake-time sshd host key is
unreachable from the device that adopted it, permanently, with no in-app or
CLI action that can fix it. Fourteen production machines were in that state
when this was written.

The root cause is fixed. This document is about the hosts the fix arrived too
late for, and the two ways to recover them.

## Status

**Option A was executed on 2026-08-19.** All 14 stranded hosts were repaired and
the 27 adopted hosts still carrying the broken v1 unit were inoculated -- 41
machines, each verified afterwards as serving its adopted key under the fixed
unit. There is no standing backlog. See [What the sweep
did](#what-the-sweep-did).

**Option B is open as PR #511** (`gabriel/vigilant-ibex`), to be judged on its
own merits as a bet about recurrence rather than as a remedy for the hosts
above -- it could not have helped them. The recovery-UI fix is PR #478.

## Symptom

`mngr start` (and every other command) fails against a machine that is up and
answering:

```
Start step of host restart failed: exited 1: Error: Failed to connect to host:
SSH host key error (Host key for 147.135.97.96 does not match.)
```

The container door (port+1) keeps working, so interactive use of an already-open
workspace can mask it. What breaks is everything that goes through the outer
door: per-agent event streams (so service tabs hang on "loading workspace"),
latchkey provisioning, and every recovery restart.

Reported as Sentry `7a7d4f5d5fe246419592d012d0149404` (minds production,
2026-08-18), on `workspace-1`.

## Mechanism

On `limactl start`, cloud-init gets a fresh instance-id, replays the carve's
provisions, and rewrites `/etc/ssh/ssh_host_ed25519_key` back to the bake-time
key. Adoption's in-VM reconciler (`mngr-key-reconciler.service`, desired state
in `/etc/mngr/`) exists to re-assert the user's rotated key after cloud-init.

The v1 unit was `WantedBy=multi-user.target` + `After=cloud-final.service`, and
Debian's `cloud-final` is itself `After=multi-user.target`. That is an ordering
cycle, which systemd resolves by deleting the reconciler's start job on every
boot (`Found ordering cycle on mngr-key-reconciler.service/start`). So the
rotated key never came back.

The client is then correct to refuse the served key -- it is not the one pinned
user-origin. But the refusal covers every connection to that endpoint,
*including the ones adoption itself would use to reinstate the key*. There is no
way back in.

**Fixed on main 2026-08-18** by `fd2ec204ad` (unit now `WantedBy=cloud-init.target`)
and `13ed2f1883` (`ADOPTION_SCHEMA_VERSION` 2, so already-adopted hosts get
swept), shipped in minds-v0.4.1.

## These hosts do not self-heal

Worth stating plainly, because it was initially read the other way. On
`workspace-1`, `/etc/ssh/ssh_host_ed25519_key` has mtime `Aug 19 04:25 UTC`, the
v2 unit landed at `04:26`, and that unit's journal has **no entries for the
current boot**. The keys came back only because
`/usr/local/sbin/mngr-key-reconciler.sh` was run by hand as root at 04:25:29 and
04:25:38 UTC on the two affected hosts. Absent that, they would still be
stranded.

The per-host client key still authenticates on the outer door. It is only the
client's *host-key pin* that refuses, which is why a root shell can repair the
machine instantly while the app cannot.

## Scope (measured 2026-08-19)

The fleet was measured twice the same day: once during the investigation, and
again immediately before the sweep. The second is the authoritative one and is
what the sweep acted on.

Of the 165 leased hosts reachable on their outer port at sweep time:

| state | count | |
|---|---|---|
| never adopted | 94 | no `/etc/mngr` desired key; unaffected |
| adopted, healthy, fixed unit | 30 | nothing to do |
| adopted, healthy, **broken v1 unit** | **27** | one VM bounce from stranding |
| **adopted and reverted** | **14** | **stranded** |

Every one of the 14 was confirmed on the VM itself:
`/etc/mngr/ssh_host_ed25519_key.pub` differs from the key sshd is serving, and
each still carried the v1 unit (`enabled`, `WantedBy=multi-user.target`).
A further 17 `stopped` hosts and 1 unreachable leased host could not be probed.

The earlier pass that same day, over a wider pool snapshot (298 rows: 174
leased, 106 available, 17 stopped, 1 unreachable), found **10** stranded across
7 users, 51 healthy-on-v1, and 16 on the fixed unit. The drift between the two
measurements -- stranded 10 -> 14, v1 51 -> 27, fixed unit 16 -> 30 -- is the
race itself: clients updating and sweeping their own hosts, while other hosts
bounced and stranded.

The healthy-on-v1 population recovers on its own once each owner's client
reaches 0.4.1+: the schema-v2 sweep reinstalls the fixed unit, which works
because the host is still reachable. That is what moved 16 to 30 in a day. The
stranded population never does -- no client can reach them to sweep them.

### Reproducing the count

Comparing the served key to `pool_hosts.outer_host_public_key` is not
sufficient: a never-adopted host legitimately serves its bake key. The
container-key heuristic (outer serving bake, container rotated) also
over-counts badly -- on the first pass it flagged 27 hosts, of which only 10
were really stranded; the other 17 were slow-path container rebuilds that had
never been adopted. The only reliable test is on the VM:
`/etc/mngr/ssh_host_ed25519_key.pub` exists (adopted) and differs from
`/etc/ssh/ssh_host_ed25519_key.pub` (reverted).

Needs `vault login -method=oidc role=minds_production` (the default `employee`
role is denied on `secrets/minds/production/*`), the pool DSN at
`secrets/minds/production/neon/DATABASE_URL`, and the pool key at
`secrets/minds/production/pool-ssh/POOL_SSH_PRIVATE_KEY`.

## Option A -- operator sweep

Over the pool key (still in every slice VM root's `authorized_keys` until the
deferred strip phase): run `/usr/local/sbin/mngr-key-reconciler.sh` to flip the
served key back to the pinned one, and install the v2 unit while there.

- Fixes all 14 **today**, independent of what client version each owner runs.
- Inoculates the 27 without waiting on client updates, removing the
  bounce-versus-update race entirely.
- Precedent exists: `mngr imbue_cloud admin repair-keys` is the same shape and
  has run fleet-wide (188/188 VMs during the 0.3.17 rollout).
- No permanent change to the trust model.
- Touches 41 live user machines, so it wants an explicit go-ahead.
- One-time. Does nothing for a future recurrence from a different cause.

## Option B -- app-level recovery

`restore_reverted_bootstrap_pins` (on branch `gabriel/vigilant-ibex`, PR #511):
when an endpoint serves *exactly* the bake-time key this device recorded in its
own `lease.json` at lease time, re-pin that key bootstrap-origin so the machine
is reachable, and let the following verification pass rotate it back to fresh
user-origin material. Runs on the two paths that hit the stale pin first --
`mngr start`, and the container probe every host resolution begins with.

- Self-service: no operator, no incident, works at 2am.
- General. Recovers from *any* cause of the same revert, not just this systemd
  bug -- a cloud-init change, a corrupted unit, a boot where the reconciler
  fails for some other reason.
- **Fixes none of the 14.** It only runs in a client the user has installed,
  and all 14 belong, by construction, to users on a client without it.
- Costs a permanent, narrow exception to "a served key matching neither the pins
  nor an in-flight rotation is refused, never re-trusted" -- documented in
  `libs/mngr_imbue_cloud/README.md`, the workspace glossary, and the
  user-controlled-keys addendum in `security-boundaries-audit.md`. Residual: an
  operator who re-keyed a slice *to that device's lease-time recorded key* is
  re-trusted for one verification pass. (The pool key is a wider door than that,
  and is open today.)
- Adds up to two unauthenticated served-key probes per endpoint per full
  verification.

The pool-key strip does not force this option. `strip-readiness-checklist.md`
item 3 requires operator repair to keep working through box access
(`limactl` / box pool key) and names the `repair-keys` design as the pattern to
hold every path to. Operator repair survives the strip; the strip is also
design-only today.

## Recommendation

They are not alternatives for the same population, which is the crux: only
Option A could fix the machines that were broken, and only Option B can fix the
next one.

A was done, and was the only thing that could help the 7 affected users -- B
reaches a stranded user only once *their* client carries it, and every stranded
host by construction belonged to someone whose client did not.

B is therefore a forward-looking bet: it buys a self-service path for a future
revert from some other cause, priced at a permanent exception in a trust model
whose whole point is that the service is trusted exactly once, at lease handoff.
If that bet is not worth taking, a smaller version keeps the diagnosis without
the re-trust -- detect the serving-the-recorded-key case and replace the generic
"Host key does not match" with a specific, actionable message, so the user
learns what is wrong and that an operator can fix it.

## What the sweep did

Per host, over the pool key: install the fixed unit and the reconciler script,
`systemctl daemon-reload`, `systemctl reenable` (which drops the stale
`multi-user.target` symlink), then run the script once. It deliberately does
**not** touch `/etc/mngr/root_authorized_keys.desired`: the script overwrites
`/root/.ssh/authorized_keys` from that file, so rewriting it wrongly would lock
out the owner. A read-only pre-flight confirmed on every target that the desired
file already contained the pool key and was a superset of what was live -- none
would have dropped a line, and none did.

Two things worth carrying into any repeat:

- **Verify against the content hash the client compares**, not against exit
  status. The client's verify/heal compares
  `sha256sum` of the unit concatenated with the script; installing text that
  differs by even a byte makes the client reinstall over the sweep forever. The
  first pilot attempt exited 0 while having written a corrupted script (a
  `&&` mangled by shell substitution in the sweep's own payload) -- caught only
  because the resulting hash did not match what the client renders. Render the
  unit and script from the checkout and ship them base64-encoded so nothing
  passes through shell quoting.
- **Re-check the population immediately before sweeping.** It moved
  substantially between two measurements on the same day (see
  [Scope](#scope-measured-2026-08-19)): stranded 10 -> 14, adopted-on-v1
  51 -> 27, adopted on the fixed unit 16 -> 30. Clients updating and hosts
  stranding were both in flight.

Not covered by the sweep: 17 `stopped` hosts, which cannot be probed and will
strand on next start if they are adopted and still on v1, plus one unreachable
leased host. Worth re-running the audit until the v1 population reaches zero.

## See also

- [A slice VM restart wipes the owner's outer SSH key](./slice-restart-wipes-owner-ssh-key.md)
  -- the `authorized_keys` half of the same cidata-replay problem, and the
  `repair-keys` sweep that fixed it.
- [Reboot-resilience rollout](reboot-resilience.md) -- slice autostart
  on box boot, which is what makes VM bounces routine.
- `libs/mngr_imbue_cloud/README.md`, "Adoption and key rotation".
