Two provisioning fixes left behind by earlier repo-wide migrations.

`system/scripts/setup_system.sh` now sends SIGHUP to the sshd listener after
writing the root-keys config, so the config is actually loaded on providers that
provision over SSH (Modal, Lima) rather than through a Dockerfile build step.
sshd only reads its configuration at startup. Providers that build from a
Dockerfile run this script before any sshd exists, so the file is baked into the
image and read when sshd first starts. Modal and Lima instead run it after mngr
has already started sshd, so the file landed under a listener that would never
re-read it, and every later connection resolved the authorized-keys path against
the passwd home that the preceding home move had just repointed at `/home/user`.
Since mngr writes root's key under `/root/.ssh`, authentication failed. This
previously worked only by accident, because the workspace's apt phase reinstalled
openssh-server and Debian's package scripts restart sshd on reinstall. SIGHUP is
sshd's documented reload mechanism; established sessions are unaffected, and the
signal is skipped when no sshd is running.

The `gcp` and `azure` create templates are caught up with three migrations that
skipped them. Both are byte-identical to each other and describe themselves as
mirroring the `aws` template, but neither had been touched since they were added
on 2026-07-09, leaving four references to paths and binaries that no longer
exist: the image build pointed at a Dockerfile at the repo root, `target_path`
still used the pre-cutover `/mngr/code/` layout, the outer-VM autostart unit
looked for the start script under the old `scripts/` directory, and the
first-boot seed used the pre-rename `fct-seed` binary name. They now match the
`aws` template apart from one deliberate host-environment forwarding line.
