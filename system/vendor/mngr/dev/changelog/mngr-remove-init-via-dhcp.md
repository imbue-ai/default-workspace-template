Spec and plan updates for the gen-2 DHCP placement change (imbue-ai/mngr-internal#849).

- `specs/slice-fleet-gen2/spec.md`: the phase-2 "restore cidata is regenerated per placement via a controlled cloud-init replay" design is marked superseded and replaced by the DHCP design (box-side dnsmasq on the slice taps, placement-free cidata copied verbatim by restores, the helper's DHCP accept, the slice units wanting the DHCP unit); the adoption bullet states the gen-2 contract (cloud-init once at first boot; host key written once at bake and replaced once at adoption; network by DHCP); the box-installed-artifacts and box-prep bullets list `dnsmasq-base`, the `mngr-dhcp` service user, and the slice DHCP server's config, sandboxed unit and udp/67 policy among the prep-installed pieces, the box-installed-artifacts bullet now says prep converges those artifacts on content (it claimed they carried version markers), and the qemu-slice-backend bullet's `qemu_slice.py` inventory lists the DHCP renderers alongside the unit / helper / sudoers and the carve reserve script.

- `blueprint/slice-fleet-cutover/plan-slice-fleet-cutover.md`: DHCP on the taps is no longer listed as deferred.

- The spec's box-prep bullet now gives the pre-created per-slice user count as 512 (`GEN2_MAX_SLICE_COUNT`); it said 64.
