Slice-fleet gen-2 phase 4 (telemetry and alerting):

- New `slices/box_telemetry.py`: the gen-2 box telemetry collector (a python3 script on a 60s systemd timer) rendered into the gen-2 prep. It emits per-VM nftables counters (with delta-based SMTP-block / connection-rate / egress-rate signals), the conntrack-pressure gauge, the declared-vs-`ethtool` link-speed audit, the prep-artifact integrity check (against a hash manifest prep records after installing the artifacts), the qemu-children tripwire, and management-plane auth-log/sudo anomaly signals as journald JSON lines the existing otelcol pipeline ships.

- `build_gen2_box_prep_script` installs the collector + timer + artifact manifest (content-converged), adds `ethtool` to the gen-2 package set, and takes the box's declared `uplink_mbps` (threaded from the row by `server prep` / `setup`).
