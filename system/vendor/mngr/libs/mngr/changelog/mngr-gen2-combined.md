Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

SSH utilities and `create_pyinfra_host` present an OpenSSH certificate found beside a private key (paramiko floor 3.2), plus the `imbue_cloud` command doc updates for the gen-2 fleet.

Constituent entries: `new-fleet-base.md`, `new-fleet-phase-4-impl.md`, `mngr-new-fleet-testing.md`, `mngr-ssh-authority-in-vault.md`

Gen-2 small follow-ups: every sshd mngr launches inside a host container (the provisioning start, the self-healing PID-1 entrypoint, and the Modal sandbox launch, all fed by `SSHD_START_OPTIONS`) now runs with `PasswordAuthentication no` and `KbdInteractiveAuthentication no`. Nothing set either before, so Debian's stock default (`yes`) applied -- inert while no account has a password hash, but dead config on a fleet that authenticates by key or certificate only. Existing containers pick it up on their next sshd relaunch.
