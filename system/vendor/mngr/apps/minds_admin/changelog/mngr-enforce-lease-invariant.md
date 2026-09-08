- New `minds-admin workspaces release <host-db-id>`: retire a confirmed-abandoned lease through the connector's own release chain (stop artifacts, slice VM, workspace record, row), in any lifecycle status -- including `stopped` rows, which `pool destroy` cannot claim.

- New `minds-admin sweep lease-records [--dry-run] [--grace-seconds N]`: run the connector's lease-vs-record sweep on demand; `--dry-run` is the audit view of pool-lease vs workspace-record drift.
