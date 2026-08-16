The bootstrap now removes the stale `data/.secrets/cloudflare_tunnel.env`
left behind on workspaces updated from a pre-share-gateway template. Nothing
consumes the file anymore (the cloudflared service was replaced by
share-gateway, which reads `share.env`), so deleting it retires the old
sharing stack's last live credential: after `update-self` restarts services
under the new supervisord.conf, cloudflared is gone and its tunnel token no
longer lingers on disk. Idempotent and best-effort (a removal failure logs a
warning and never blocks boot), and marked `# CLEANUP:` for removal once no
supported workspace predates the share gateway.
