Drop the secondary Latchkey gateway from the skills: gateway provisioning now
gives every workspace exactly one (primary) gateway.

The `latchkey` skill no longer tells agents to retry a failed `latchkey curl`
against `$LATCHKEY_GATEWAY_SECONDARY`; instead it explains that curl exit code
7 means the single gateway is unreachable and there is nothing to fall back to.
The `github-sync` skill's wrap-up report no longer promises that a per-VPS
secondary gateway covers pushes while the user's machine is offline -- pushes
queue and go out with the next commit.

The `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` env var went away with the
secondary gateway too, so the git-through-the-gateway recipes in the
`latchkey`, `publish-inspiration`, `update-published-inspiration`,
`use-inspiration`, and `update-installed-inspiration` skills now pass only the
`X-Latchkey-Gateway-Password` header.
