#!/bin/sh
# Install + enable the outer-VM "minds autostart" systemd unit (imbue_cloud /
# pool-host / ovh / vultr / aws modes -- any vps_docker mode where the agent
# runs in a docker container inside a VM).
#
# Run ONCE on the outer VM (the VPS / pool-slice VM), via the mngr
# `post_host_create_outer_command` create-template hook. It writes two files:
#
#   1. /usr/local/sbin/minds-outer-autostart.sh -- the boot action: start every
#      mngr-managed agent container and relaunch the "system-services" agent
#      inside it. Containers are found by the fixed mngr label rather than a
#      baked-in name, so it survives container rebuilds. The agent container
#      already returns on its own via its docker `--restart` policy and the
#      container entrypoint self-heals sshd; the `docker start` here is a
#      harmless no-op in that case, and `mngr start` is idempotent + flock-
#      serialized so racing the desktop client is safe. `bash -lc` is a login
#      shell (so uv/mngr are on PATH) but does not source /mngr/env, so we
#      source it explicitly for the host_dir/prefix context.
#
#   2. /etc/systemd/system/minds-autostart.service -- a oneshot unit that runs
#      the boot action on every VM boot.
#
# Idempotent: re-running overwrites both files and re-enables the unit.
set -eu

cat > /usr/local/sbin/minds-outer-autostart.sh <<'BOOT_ACTION'
#!/bin/sh
set -u
for container_id in $(docker ps -aq --filter "label=com.imbue.mngr.host-id"); do
    docker start "$container_id" >/dev/null 2>&1 || true
    docker exec --workdir / "$container_id" \
        bash -lc 'set -a; [ -f /mngr/env ] && . /mngr/env; set +a; mngr start system-services' || true
done
BOOT_ACTION
chmod +x /usr/local/sbin/minds-outer-autostart.sh

cat > /etc/systemd/system/minds-autostart.service <<'UNIT'
[Unit]
Description=Start the minds system-services agent on boot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/minds-outer-autostart.sh

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable minds-autostart.service
