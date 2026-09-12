New `minds-admin wireguard install-onetun` command: downloads the pinned onetun release (0.3.10) for the current platform (Linux x86_64/aarch64, macOS arm64), verifies it against a sha256 recorded in the repo, and installs it to `~/.minds-wireguard/bin/onetun` -- a well-known location the box-management dial resolver now checks after `MNGR_ONETUN_PATH` and PATH. Idempotent and non-interactive (CI-safe): an already-current binary is a no-op, any other version is refreshed.

The dial resolver now warns (instead of silently logging at debug) when the operator's WireGuard key is present but no onetun binary can be found -- the operator clearly intends to use the userspace-tunnel transport, and the warning names the install command.

Removed `minds-admin wireguard sync-peers --via-wireguard`: the automatic dial resolver already prefers the overlay (userspace tunnel, then kernel route) and falls back to the public address, so a flag that forced the kernel-route dial had no remaining use.

New `minds-admin server ssh --server-id <id> [-- <command>]`: an interactive management SSH session (or a one-off command) on a box, over the same automatically resolved dial as every other box-management command -- so a locked-down box needs no `wg-quick` and no root. Pool-key auth with the box's recorded host key strictly pinned.
