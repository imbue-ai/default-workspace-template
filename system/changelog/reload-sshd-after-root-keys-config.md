`system/scripts/setup_system.sh` now sends SIGHUP to the sshd listener after
writing `/etc/ssh/sshd_config.d/60-workspace-root-keys.conf`, so the config is
actually loaded on providers that provision over SSH (Modal, Lima) rather than
through a Dockerfile `RUN`.

sshd only reads its configuration at startup. On the Dockerfile-built providers
this script runs at image build time, before any sshd exists, so the file is
already on disk when the container starts sshd. Modal and Lima instead run the
same script as an `extra_provision_command`, after mngr has already started
sshd -- so the file landed under a listener that would never re-read it, and
every later connection kept resolving `AuthorizedKeysFile` against the passwd
home that the preceding home move had just repointed at `/home/user`. Since
mngr writes root's key to `/root/.ssh/authorized_keys`, authentication failed
for every connection after setup.

This previously worked by accident: the workspace's apt phase reinstalled
`openssh-server`, whose postinst restarted sshd and picked the file up as a side
effect. Removing that cross-release upgrade (mngr now builds Modal hosts from an
explicit trixie base) removed the accidental restart and exposed the gap.

SIGHUP is sshd's documented reload mechanism -- the listener re-execs itself and
re-reads the config, and established sessions, which are separate `sshd-session`
children, are unaffected. The signal is skipped entirely when no sshd is running,
so image builds are unchanged.
