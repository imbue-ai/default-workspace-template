#!/bin/sh
# Put the regenerable caches on the Lima VM's btrfs data disk instead of its
# boot disk.
#
# The host env points XDG_CACHE_HOME / npm_config_cache at /var/cache/user so
# the uv, npm, and playwright caches stay off the persistent /home/user tree and
# out of backups (the docker layout keeps them container-local). On lima the
# equivalent "local" disk is the VM's boot disk, which is the pre-baked image's
# fixed 20 GiB and cannot grow, while /home/user is the resizable btrfs data
# disk. So here ~/.cache becomes a nested btrfs subvolume on the data disk and
# /var/cache/user a symlink to it: the caches grow on the disk that can grow,
# and the host-backup btrfs snapshots of the home tree skip the nested subvolume
# just as restic skips **/.cache.
#
# Runs as root once at create, from the lima create template
# (.mngr/settings.toml), before install_dependencies.sh fills the cache.
# Idempotent: a second run finds the symlink and exits. Whatever the pre-baked
# image already holds under /var/cache/user is carried over.
set -eu

CACHE_DIR=/home/user/.cache
VAR_CACHE_USER=/var/cache/user

if [ -L "$VAR_CACHE_USER" ]; then
    exit 0
fi

# An earlier home-skeleton seed may have left ~/.cache as a symlink pointing
# back at /var/cache/user; it is replaced by the real directory below.
if [ -L "$CACHE_DIR" ]; then
    rm -f "$CACHE_DIR"
fi
if [ ! -e "$CACHE_DIR" ]; then
    if [ "$(findmnt -no FSTYPE -T /home/user/)" = "btrfs" ]; then
        btrfs subvolume create "$CACHE_DIR"
    else
        mkdir -p "$CACHE_DIR"
    fi
fi

if [ -d "$VAR_CACHE_USER" ]; then
    cp -a "$VAR_CACHE_USER/." "$CACHE_DIR/"
    rm -rf "$VAR_CACHE_USER"
fi
mkdir -p "$(dirname "$VAR_CACHE_USER")"
ln -s "$CACHE_DIR" "$VAR_CACHE_USER"
