Bare-metal ordering and prep guards learned from the first non-standard hil box orders.

- The gen-2 `server prep` / `setup` now checks for a usable TPM 2.0 (`systemd-cryptenroll --tpm2-device=list`) right after its package install and refuses a box without one before its storage partition is touched, naming the remedy (an OVH BIOS ticket to enable Intel PTT / AMD fTPM, or a replacement unit). Previously a TPM-less box failed at the enrollment step, after the storage partition had already been LUKS-formatted.

- `server pricing` treats OVH's `unknown` availability as unorderable (like `unavailable`), so a config OVH no longer stocks in a datacenter can no longer be a row's base storage and show a `?` delivery with a price nobody can buy. By default it also prices only two-drive NVMe software mirrors (`softraid-2x<size>nvme`), the one storage shape the gen-2 reinstall layout and slice carves support; `--any-storage` prices every config as before.

- `server order` refuses a `--storage` code that is not a two-drive NVMe software mirror before resolving credentials or building a cart; `--allow-unsupported-storage` overrides it for deliberate layout work.

- `server pricing` never prices OVH's GAME-range plans (`24risegame*`, `24sysgame*`, `24skgame*`) and `server order` refuses them outright, with no override: their Game anti-DDoS firewall drops the management WireGuard's inbound UDP, and they sit on 1 Gbit/s ports.

- `server await-delivery` reads the delivered server's physical port speed (OVH's `linkSpeed`, which the catalog never states) and refuses a box below 10000 Mbit/s, recording it at `failed` with its service name and address so `setup` never reinstalls it (`setup` on such a row now names the terminal status and the remedy -- cancel the renewal and delete the row, or a deliberate `set-status` override -- instead of pointing back at `await-delivery`). The restore's parallel object fetch collapsed to 0.12 Gbit/s on the one 1G-port box the fleet ever had, against 1.70 Gbit/s on a 10G box.

- `server setup` / `server prep` no longer finish on a gen-2 box whose `:22` lockdown went live while neither the WireGuard tunnel nor the overlay route could reach it. The prep now echoes a marker when it installs the lockdown, and the post-prep round trips refuse the public-address fallback (the header-backup upload and staging cleanup used to warn and the box was marked `ready` unreachable).
