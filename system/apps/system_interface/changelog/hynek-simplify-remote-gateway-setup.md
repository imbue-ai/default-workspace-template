The latchkey scope-catalog endpoint no longer sends
`X-Latchkey-Gateway-Permissions-Override`: the header (and its
`LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` env var) went away with the secondary
gateway, so `resolve_scope_info` takes just the gateway password and
`/api/latchkey/scopes/<scope>` only 503s when the gateway URL or password is
missing.
