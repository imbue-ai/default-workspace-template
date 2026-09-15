The connector's Modal proxy attach (gen-2 management plane) now reads `MINDS_CONNECTOR_MODAL_PROXY_ENVIRONMENT` alongside the proxy name, resolving the tier's shared proxy in the Modal environment it actually lives in (the dev tier keeps its single workspace proxy in `main` while each dev env deploys into its own environment).

Two gen-1 -> gen-2 conversion bugs caught live by the dev canary are fixed: the conversion first-boot script now mounts the data disk's filesystem-bearing node (gen-1 lima data disks are partitioned, so the btrfs lives on the first partition, not the raw disk), and it retires lima's per-boot cloud-init hook (which mounted the conversion cidata at /mnt/lima-cidata and then errored cloud-init on every boot of the converted guest).

The in-guest data-filesystem grow oneshot only resizes btrfs mounts now: its /mnt/lima-* glob also matches a converted guest's /mnt/lima-cidata iso9660 mount, which made the oneshot fail every boot.

The proxy name/environment vars are now forwarded into the connector's containers via an inline secret: the first proxied deploy revealed that the Modal Proxy (a function dependency) must evaluate identically at deploy time and in-container, or container startup fails with a dependency-count mismatch.

The grow oneshot also grows a partitioned data disk's partition (via growpart) before the btrfs resize, so a disk grow lands in-guest on converted gen-1 machines too.
