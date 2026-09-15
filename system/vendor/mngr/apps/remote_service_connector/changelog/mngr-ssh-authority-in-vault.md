The connector manages gen-2 boxes, slice VMs, and workspace containers with short-lived SSH certificates instead of the static pool key (imbue-ai/mngr-internal#850).

- New `ssh_cert_refresh` cron (every two hours) is the only function holding a Vault credential: it logs in with the tier's AppRole (the new `ssh-ca` Modal Secret), mints a fresh ed25519 key and an 8h certificate for the `connector` and `analytics` roles, and stores them in the `ssh-management-certs` Modal Dict. Every other function reads its bundle from the Dict and caches it in process; no request path touches Vault.

- Lease, claim, share-enable, stop/start, teardown, and reconcile select their credentials by the box's generation: the certificate on gen-2, the pool key on gen-1. A gen-2 operation with no stored certificate answers 503 `management_certificate_unavailable`.

- The connector's certificate carries the `mngr-service`, `mngr-vm`, and `mngr-container` principals and no interactive extensions.
