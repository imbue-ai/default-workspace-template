Add a `.minds/template/relay-ssh.sh` Vault schema for the per-tier share-relay SSH keypair (`secrets/minds/<tier>/relay-ssh`), so relays can be redeployed from any operator machine instead of stranding SSH access on whoever provisioned them.

Declare `OVH_CLOUD_PROJECT_ID` in `.minds/template/ovh.sh` -- the OVH Public Cloud project that `just provision-share-relay` provisions relay instances in, previously undocumented and absent from Vault.
