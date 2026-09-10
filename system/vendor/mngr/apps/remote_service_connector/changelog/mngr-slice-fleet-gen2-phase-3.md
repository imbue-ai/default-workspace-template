Slice-fleet-gen2 phase 3 (management-plane lockdown machinery):

- Every connector function (the web app, the transition supervisors, and the crons) can now attach the tier's Modal Proxy, read at deploy time from `MINDS_CONNECTOR_MODAL_PROXY_NAME` (threaded by `minds-admin env deploy` from the tier's `management_plane.toml`). With a proxy attached, all connector egress -- in particular the SSH to gen-2 boxes, whose management sshd allowlists exactly the proxy's static IPs -- leaves from those addresses. Empty/unset keeps direct egress.

- Migration 035 adds `bare_metal_servers.wg_public_key` (the box's WireGuard public key, generated on-box at gen-2 prep; operator client configs pin each box peer by it).
