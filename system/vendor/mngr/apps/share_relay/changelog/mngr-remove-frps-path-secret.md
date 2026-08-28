The frps plugin-auth secret no longer rides in the connector callback URL path (where every relay callback wrote it into the tier's access logs, see issue #616). The rendered plugin `addr` now carries it as URL userinfo (`https://<secret>@<connector>`), which frps's Go HTTP client delivers as an `Authorization: Basic` header; the plugin `path` is just `/frps/auth/<relay-id>`.

`RelayConfiguration` gains a `plugin_auth_secret` (`SecretStr`, validated userinfo-safe) alongside the now secret-free `plugin_auth_url`, and `share-relay render` / `share-relay deploy` read the secret from `FRPS_AUTH_SECRET` in the environment instead of accepting it inside the `--plugin-auth-url` argument (keeping it out of shell history and `ps` too).

The frp behavior-check harness (`frp_verification.py`) gains a fourth pinned behavior: an `httpPlugins` `addr` with URL userinfo reaches the plugin endpoint as an `Authorization: Basic` header with a secret-free path.
