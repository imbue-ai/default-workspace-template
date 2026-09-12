Operator tooling reaches gen-2 boxes with short-lived SSH certificates signed by the tier's Vault SSH CA instead of the static pool key (imbue-ai/mngr-internal#850).

- New per-tier operator identity directory `~/.mindsadmin/<tier>/` (`MINDS_ADMIN_IDENTITY_DIR` relocates the root) holding `wireguard.key` and the operator SSH key `ssh_id`; any command that dials a gen-2 box signs a 12h certificate for it through the operator's own `vault login` (re-signed under 6h remaining). The WireGuard key moved here from `~/.minds-wireguard/<tier>.key` and the `MINDS_WIREGUARD_PRIVATE_KEY_PATH` override is gone; `wireguard install-onetun` installs to `~/.mindsadmin/bin/onetun`.

- Every box command (`server prep/setup/ssh/list/audit/sweep/warm/reap/destroy/drain`, `pool create/warm-cache/destroy/teardown`, `wireguard sync-peers`, `cutover`) resolves its management key per box generation: the certified operator key on gen-2, the pool key on gen-1. `server sweep-ci-slices` lists a box whose key cannot be resolved (a gen-2 box outside an activated env, or a failed certificate sign) as unreachable and still sweeps the rest of the fleet.

- Gen-2 `server setup` reinstalls with a throwaway per-setup key whose private half is discarded, and gen-2 prep installs the tier's CA trust (`TrustedUserCAKeys` plus a principals file each for the `debian` bootstrap user and the slice service user; root on the box gets no principals file and accepts no certificate), deletes those two accounts' `authorized_keys`, and requires `[ssh_ca] public_key` in the tier's `deploy.toml`. Slice bakes and cutover repaves pass the CA into the VM and container.

- `server audit` reports `expected_authorized_key_count`, `trusted_ca_public_key`, and `is_trusted_ca_correct`; a gen-2 box must authorize no static key and pin exactly the tier's CA.

- The box telemetry collector that gen-2 prep installs records every accepted management login with the certificate key id and serial (a `management_login` event) and flags static-key logins on gen-2 boxes (a `MANAGEMENT_SSH_ANOMALY` signal with reason `static_key_login`).

- `env deploy` runs the connector's `ssh_cert_refresh` function once after every connector deploy so the tier's management certificates exist before the first request needs them.
