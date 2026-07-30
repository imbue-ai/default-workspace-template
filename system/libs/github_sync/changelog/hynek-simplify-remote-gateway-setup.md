Remove the secondary-gateway fallback: gateway provisioning now gives every
workspace exactly one (primary) Latchkey gateway.

`LATCHKEY_GATEWAY_SECONDARY` is gone from the config module, the visibility
check is a single `latchkey curl` attempt (an unreachable gateway is simply
`unknown`, retried on the next tick), and the post-commit hook no longer pushes
a proxied URL through the secondary gateway when the normal push fails -- the
failure lands in `/tmp/post-commit-push.log` and is retried on the next commit.
