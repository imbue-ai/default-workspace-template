Stop sending `X-Latchkey-Gateway-Permissions-Override` on the boot-time
timezone fetch: the header (and its `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` env
var) went away with the secondary gateway, so the gateway password header is
the only auth the minds-api-proxy call needs. The fetch no longer treats a
missing override as "gateway env not set" and skips.
