Multi-relay sharing, phase 1 (blueprint/multi-relay), ops surface.

Adds the phase 1 + phase 2 specs under `blueprint/multi-relay/`.

`just provision-share-relay` takes an ordinal (regions run several relays), new `just register-share-relay` / `just deregister-share-relay` recipes write the connector's fleet inventory, `just deploy-share-relay` takes the registered relay id, and `just dns-share-relay` reconciles the region's A-record set over every relay IP.

`just provision-dev-relay` self-registers the relay it provisions (via the tier's MINDS_ADMIN_KEY from Vault) and no longer relies on env-var relay config; `.minds/template/sharing.sh` drops the `SHARE_RELAY_ENDPOINTS` / `SHARE_DEFAULT_REGION` keys.
