# Gen-2 telemetry: box signals and alerting

Phase 4 of [specs/slice-fleet-gen2](../../../../specs/slice-fleet-gen2/spec.md). Gen-2 boxes evaluate their own tier-1 abuse and integrity conditions on-box and emit them as journald lines; the existing otelcol pipeline ships those into the tier's OpenObserve, where one trivial alert rule per signal files GitHub issues (with an optional SMTP fallback). This doc is the operator runbook for that machinery.

## The pieces

| Piece | Where |
|---|---|
| The box collector (`mngr-box-telemetry` script + systemd timer) | rendered into the gen-2 prep by `minds-admin server prep` / `setup` (`minds_admin`'s `slices/box_telemetry.py`) |
| The signal vocabulary (shared by emitter and alert rules) | `apps/observability/imbue/observability/box_signals.py` |
| Per-tap interface metrics | free: the taps are ordinary `msliceN` interfaces, already covered by the box otelcol's hostmetrics network scraper |
| OpenObserve alert rules, templates, destinations | `observability provision-alerts` (`alert_provisioning.py`) |
| Dashboards | the Evidence prototype at `apps/analytics/dashboards/` (per-tap throughput chart) |

## The on-box collector

`minds-admin server prep` installs a python3 collector at `/usr/local/sbin/mngr-box-telemetry`, run every 60s by `mngr-box-telemetry.timer`. Its stdout lands in journald under the `mngr-box-telemetry` identifier, which the box's otelcol journald pipeline ships into the tier's `box_logs` stream. Every run emits plain JSON event lines (raw data for charts and debugging), and — for conditions worth waking someone — `MNGR_BOX_SIGNAL <NAME> {...}` marker lines the server-side alert rules match verbatim.

The design decision (settled in the spec): no third-party nftables exporter, and all thresholds/allowlists live in this PR-reviewed rendered code rather than in server-side SQL. The OpenObserve rules are deliberately trivial substring matches.

What each run collects:

- **Per-slice nftables counters** (`nft -j list counters`, table `inet mngr_slices`): egress/ingress bytes+packets, new connections, blocked SMTP attempts — labeled with the slice ordinal AND its instance name (read from the slice's env file, so the pool host id is on every line). Deltas against the previous run turn the cumulative counters into rates.
- **Conntrack pressure**: `nf_conntrack_count` / `nf_conntrack_max` (all tenants share the table; exhaustion silently breaks every VM).
- **Link-speed audit**: `ethtool` on the default-route interface vs the row's declared `uplink_mbps` (baked in at prep).
- **Prep-artifact integrity**: sha256 of the installed template unit, slice helper, sudoers, the slice DHCP server's config, unit and udp/67 policy (they decide which address every guest is handed and who may talk to the server), `wg0.conf`, the storage volume's `/etc/crypttab` entry, the bind-mount units and the journal-flush drop-in that keep the journal, the service user's home and the temp directories on the encrypted volume (a change there would silently move state back onto the plain root partition), and the collector's own script and units (tampering with the watcher must itself signal) against the manifest prep records at `/var/lib/mngr-box-telemetry/prep-artifacts.sha256` after installing them. Re-prepping refreshes the manifest, so a legitimate artifact bump never reads as drift.
- **Storage volume**: `findmnt` on the storage root (`/srv/mngr-slices`) against the opened LUKS mapper (`/dev/mapper/mngr-storage`), emitted as a `storage_volume` event. Anything else raises `STORAGE_VOLUME_LOCKED`: nothing mounted means the TPM unlock failed at boot and every slice on the box is down until `minds-admin server unlock` opens it; a plain device means the box was never encrypted and its slices sit in plaintext (see "Storage encryption on a gen-2 box" in [host-pool-setup.md](host-pool-setup.md)).
- **Qemu-children tripwire**: any child process of a slice qemu (whose seccomp sandbox denies spawn) is flagged.
- **Management-plane auth logs** (journal scan with a cursor, identifiers `sshd`/`sshd-session`/`sudo`/`systemd`): direct root logins and password logins always; logins from outside the proxy/WireGuard allowlist and password *attempts* only once the tier's `[management_plane.modal_proxy]` is configured (before the lockdown, the open `:22` makes internet scan noise unavoidable, and operator laptop logins are legitimate). Sudo is checked against the scoped grants: `debian` (full sudo by design) and root are expected; the slice service user (`slicehost`) may run exactly the per-ordinal `systemctl` slice-unit verbs; anything else signals. Every accepted management login is also captured as a `management_login` event carrying the certificate's key id (`operator:<who>`, `connector:<token>`) and serial, so the auth trail names the person or service rather than a shared key; a login on a gen-2 box that presents a static key instead of a certificate is a `static_key_login` anomaly (nothing on a gen-2 box should authorize one). Anomalies are aggregated per run (a flood of journal lines becomes one signal line with a count). The same scan matches systemd's OOM-kill lines for `mngr-slice@*` units (the hardened unit's `MemoryMax` backstop firing, which must never happen: pressure resolves inside the workspace container) and emits one `SLICE_UNIT_OOM_KILLED` line per run naming the killed units.

The signals (see `box_signals.py` for the authoritative list): `SMTP_BLOCKED`, `NEW_CONNECTION_RATE` (sustained > 150/s, half the enforced ceiling), `EGRESS_RATE` (sustained > 50% of the declared uplink), `CONNTRACK_PRESSURE` (> 80%), `LINK_SPEED_MISMATCH`, `PREP_ARTIFACT_DRIFT`, `QEMU_CHILD_PROCESSES`, `MANAGEMENT_SSH_ANOMALY`, `SUDO_ANOMALY`, `SLICE_UNIT_OOM_KILLED`, `STORAGE_VOLUME_LOCKED`.

Rollout is the usual gen-2 mechanism: re-run `minds-admin server prep` and the content-converged sections install or refresh the collector on existing boxes.

## Provisioning the alerting

```bash
export OBSERVABILITY_ROOT_EMAIL=... OBSERVABILITY_ROOT_PASSWORD=...   # tier Vault: secrets/minds/<tier>/observability
export OBSERVABILITY_ALERTS_GITHUB_TOKEN=...                          # PAT with issue-write on the target repo
uv run observability provision-alerts --ssh-host <instance-ip> --tier dev
```

Idempotent create-or-converge: one template + one webhook destination + one alert rule per signal (`mngr_box_<signal>` over `box_logs`, period 10 min, evaluated every 10 min, threshold 1 row, silence 4 h). Re-run it to push template or rule changes.

- **GitHub issues** are the primary destination: a direct webhook to `POST /repos/imbue-ai/mngr-internal/issues` (override with `--github-repository`), the PAT in the destination's `Authorization` header. Dedupe is OpenObserve-side: the 4-hour silence window means at most one issue per signal per tier per window; the direct webhook cannot update an existing issue (accepted limitation — SMTP is the fallback if this proves too spammy in practice).
- **Alert payloads are payload-free** (the telemetry spec's condition for having destinations at all): the issue carries the alert name, stream, tier, and trigger time — never log content. Investigation happens in the tier's OpenObserve over an SSH tunnel.
- **SMTP fallback**: pass `--email-recipient infra@imbue.com` to also provision an email destination. Note the instance itself must have SMTP configured (OpenObserve `ZO_SMTP_*` environment) before email delivery works; the instance deploy does not set that up today.

## Verifying end to end (the phase-4 exit checklist)

Steps 1-4 were run against the dev tier (canary box + dev instance) when phase 4 landed; re-run them when bringing the machinery to a new tier.

1. On a prepped gen-2 box: `systemctl status mngr-box-telemetry.timer` and `journalctl -t mngr-box-telemetry -n 20` — JSON event lines every minute.
2. In the tier's OpenObserve (SSH tunnel, `box_logs` stream): the same lines arriving with the box's hostname. The journald entries flatten into per-field columns — the line text is `body_message` and the unit's identifier `body_syslog_identifier` (what the provisioned SQL filters on; verified against live dev ingest).
3. `observability provision-alerts` (see above), then confirm the destination delivers: the instance's destination-test endpoint (`POST /api/default/alerts/destinations/test` over the tunnel) with the GitHub destination's shape must answer `success: true` and create (then close) a test issue.
4. Trigger a breach by hand (e.g. append a comment line to `/etc/systemd/system/mngr-slice@.service` on a canary box to fire `PREP_ARTIFACT_DRIFT`, then re-run prep to converge it back) and confirm a single deduplicated GitHub issue appears.
5. From inside a slice VM (first bake onward): `nc -w3 example.com 25` must fail AND increment `slice_<N>_smtp_blocked`, and the next collector run must emit `MNGR_BOX_SIGNAL SMTP_BLOCKED`.
