Add a `.minds/template/relay-ssh.sh` Vault schema for the per-tier share-relay SSH keypair (`secrets/minds/<tier>/relay-ssh`), so relays can be redeployed from any operator machine instead of stranding SSH access on whoever provisioned them.

Declare `OVH_CLOUD_PROJECT_ID` in `.minds/template/ovh.sh` -- the OVH Public Cloud project that `just provision-share-relay` provisions relay instances in, previously undocumented and absent from Vault.

Declare `BROKER_GOOGLE_CLIENT_ID` / `BROKER_GOOGLE_CLIENT_SECRET` in `.minds/template/sharing.sh`: the Web-application Google OAuth client backing the accounts broker's "Continue with Google" share sign-in. Distinct from the supertokens secret's Desktop-type `GOOGLE_CLIENT_*` pair (which serves the CLI's loopback flow and cannot accept https redirect URIs); each broker host's `/share/oauth/google/callback` URL must be registered on it.
