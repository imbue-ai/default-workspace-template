New `.minds/template/ssh-ca.sh` Vault template for the connector's SSH-CA AppRole credentials (`VAULT_SSH_APPROLE_ROLE_ID` / `VAULT_SSH_APPROLE_SECRET_ID`); `.minds/template/pool-ssh.sh` is marked as the gen-1-only static key that goes away with the last gen-1 box (imbue-ai/mngr-internal#850).


The public mirror overlay lock (`mirror/overlay/uv.lock`) records `imbue-mngr`'s paramiko floor moving to 3.2 for the certificate-aware SSH key loader.
