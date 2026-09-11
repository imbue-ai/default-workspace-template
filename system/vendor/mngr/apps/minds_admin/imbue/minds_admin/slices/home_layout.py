"""Operator repair moving a slow-path-rebuilt slice workspace onto the ``home/`` volume layout.

A workspace the imbue_cloud slow path rebuilt before the rebuild forwarded
``volume_home_path`` carries the legacy volume layout: the volume holds only
``host_dir/`` (symlinked from the container's ``/home/user/.mngr``), while the
rest of ``/home/user`` -- the workspace checkout, the user's apps and skills,
dotfiles -- lives in the container's writable layer. host_backup reads
``<snapshot>/home`` and so fails every tick, and nothing outside the container
persists the home tree.

The repair runs one script as root inside the slice VM, which drives the
workspace container through ``docker exec``:

1. probe the layout (``/home/user`` a symlink = already ``home/``; a real
   directory whose ``.mngr`` symlinks onto the volume = legacy);
2. quiesce: stop every running chat agent through the workspace's own mngr
   (the same gate the backup restore script applies), ``supervisorctl stop
   all``, and wait for any in-flight restic run to exit;
3. take a read-only btrfs snapshot of the host subvolume under
   ``<mount>/rollback/`` (outside the ``snapshots/`` dir host_backup reaps);
4. copy ``/home/user`` out of the container onto the volume as ``home/``,
   rename ``host_dir/`` to ``home/.mngr`` (same subvolume, so a rename), and
   leave an empty ``host_dir/`` behind exactly as a bake does;
5. inside the container, move ``/home/user`` aside to
   ``/home/user.pre-migration`` and symlink ``/home/user`` onto
   ``/mngr-vol/home`` -- every recorded absolute path keeps resolving;
6. ``supervisorctl restart all`` and wait for the system interface and
   host-backup to read RUNNING.

Rollback reverses steps 4-5 while the aside copy and the btrfs snapshot still
exist. Both actions always restart the services they stopped, even on failure.
"""

import base64
import binascii
import shlex
from collections import Counter
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

_PROBE_TIMEOUT_SECONDS: Final[float] = 300.0
# Quiesce waits (chat stops, an in-flight restic run) plus a multi-GB copy plus
# the service restart: generous, and the script reports progress as it goes.
_MIGRATE_TIMEOUT_SECONDS: Final[float] = 3600.0

# Every verdict line the in-VM script prints starts with this; the rest is
# ``key=value`` tokens, free text carried base64-encoded so the parser never
# has to guess at quoting.
RESULT_MARKER: Final[str] = "MNGR_HOME_LAYOUT"


class HomeLayoutAction(LowerCaseStrEnum):
    """What the in-VM script is asked to do."""

    PROBE = auto()
    MIGRATE = auto()
    ROLLBACK = auto()


class VolumeLayout(LowerCaseStrEnum):
    """The layout the probe found on the workspace container."""

    # /home/user is a symlink onto the volume's home/ (the post-declutter bake layout).
    HOME = auto()
    # /home/user is a real directory in the container's writable layer; only .mngr symlinks onto the volume.
    LEGACY = auto()
    UNKNOWN = auto()


class HomeLayoutStatus(LowerCaseStrEnum):
    """Per-workspace outcome, as emitted in the JSON report."""

    # Probe verdicts.
    HOME_LAYOUT = auto()
    LEGACY_LAYOUT = auto()
    # Action verdicts.
    MIGRATED = auto()
    ROLLED_BACK = auto()
    # Preflight refused (nothing was changed).
    BLOCKED = auto()
    FAILED = auto()


class HomeLayoutProbe(FrozenModel):
    """What the in-VM script measured before deciding anything."""

    layout: VolumeLayout = Field(description="The container's home layout")
    container: str = Field(description="The workspace container's name")
    home_bytes: int = Field(description="Size of the container's /home/user tree (not following symlinks)")
    free_bytes: int = Field(description="Free bytes on the volume's data disk")


class HomeLayoutTarget(FrozenModel):
    """One leased slice pool host to repair, resolved from the pool DB."""

    host_id: str = Field(description="pool_hosts.host_id")
    host_name: str = Field(description="pool_hosts.host_name")
    status: str = Field(description="pool_hosts.status at resolution time")
    vm_name: str = Field(description="The slice's lima instance name on its box")
    server: BareMetalServer = Field(description="The box the slice lives on")


class HomeLayoutOutcome(FrozenModel):
    """The result of one action on one workspace."""

    host_id: str = Field(description="pool_hosts.host_id")
    host_name: str = Field(description="pool_hosts.host_name")
    vm_name: str = Field(description="The slice's lima instance name")
    server_id: str = Field(description="The bare_metal_servers row id of the slice's box")
    status: HomeLayoutStatus = Field(description="How the workspace ended up")
    detail: str = Field(default="", description="Failure description or an action note")
    probe: HomeLayoutProbe | None = Field(default=None, description="The measurements, when the script got that far")


class HomeLayoutReport(FrozenModel):
    """The summary the command emits: per-workspace outcomes plus counts."""

    home_layout: int = Field(description="Workspaces already on the home/ layout (probe)")
    legacy_layout: int = Field(description="Workspaces on the legacy layout (probe)")
    migrated: int = Field(description="Workspaces moved onto the home/ layout")
    rolled_back: int = Field(description="Workspaces moved back to the legacy layout")
    blocked: int = Field(description="Workspaces whose preflight refused (nothing changed)")
    failed: int = Field(description="Workspaces whose action failed (investigate individually)")
    unreachable: tuple[str, ...] = Field(description="host_ids whose box or VM could not be reached")
    outcomes: tuple[HomeLayoutOutcome, ...] = Field(description="Per-workspace outcomes")


# Runs inside the workspace container via ``bash``, with the workspace's own
# env file sourced: without it (MNGR_PREFIX in particular) the workspace's
# mngr cannot see its tmux sessions and reports every chat as STOPPED. Stops
# every non-main agent that is not already STOPPED -- an idle (WAITING) chat
# still holds a claude process whose working directory is the home tree --
# then supervisord's programs, then waits for any in-flight restic run.
# Prints one last line: ``QUIESCED chats=<n>`` or ``QUIESCE_FAILED <detail>``.
# The agent list is JSON, so a python one-liner picks the names out of it;
# everything else is plain shell.
_IN_CONTAINER_QUIESCE_SCRIPT: Final[str] = """\
cd /home/user/workspace || { echo "QUIESCE_FAILED no /home/user/workspace"; exit 0; }
set -a; [ -f /home/user/.mngr/env ] && . /home/user/.mngr/env; set +a
export MNGR_ALLOW_UNKNOWN_CONFIG=1
listed=$(uv run mngr list --format json --provider local 2>/dev/null) \\
    || { echo "QUIESCE_FAILED mngr list failed"; exit 0; }
live=$(printf '%s\\n' "$listed" | python3 -c '
import json, sys
names = []
for line in reversed(sys.stdin.read().splitlines()):
    try:
        payload = json.loads(line.strip())
    except ValueError:
        continue
    if isinstance(payload, dict) and "agents" in payload:
        names = [
            str(agent.get("name") or agent.get("id"))
            for agent in payload["agents"]
            if isinstance(agent, dict) and agent.get("type") != "main" and agent.get("state") != "STOPPED"
        ]
        break
sys.stdout.write("\\n".join(names))
')
chats=0
while IFS= read -r name; do
    [ -n "$name" ] || continue
    uv run mngr stop "$name" >/dev/null 2>&1 || { echo "QUIESCE_FAILED could not stop chat $name"; exit 0; }
    chats=$((chats + 1))
done <<MNGR_LIVE_CHATS
$live
MNGR_LIVE_CHATS
supervisorctl stop all >/dev/null 2>&1
deadline=$(( $(date +%s) + 600 ))
while pgrep -x restic >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo "QUIESCE_FAILED an in-flight restic run did not finish within 10 minutes"; exit 0; }
    sleep 5
done
echo "QUIESCED chats=$chats"
"""

# Runs inside the workspace container after the switch: names the processes
# whose working directory is still the old home tree. The system-services
# chain (the tmux session, bootstrap, supervisord and their shells) stays
# there until the container next restarts and is harmless, since supervised
# programs are restarted with the new path; anything else is reported so the
# operator can decide. Prints ``STALE_CWD <n> <comm,...>``.
_IN_CONTAINER_STALE_CWD_SCRIPT: Final[str] = """\
count=0; names=""
for p in /proc/[0-9]*; do
    case "$(readlink "$p/cwd" 2>/dev/null)" in
        /home/user.pre-migration*)
            comm=$(cat "$p/comm" 2>/dev/null)
            case "$comm" in tmux:*|bash|sh|uv|supervisord|sleep|python3) continue ;; esac
            count=$((count + 1)); names="$names,$comm" ;;
    esac
done
echo "STALE_CWD $count ${names#,}"
"""


# Runs inside the workspace container after ``supervisorctl restart all``:
# waits for the programs a workspace cannot do without to read RUNNING.
# Prints ``SERVICES_OK`` or ``SERVICES_DOWN <name=state ...>``.
_IN_CONTAINER_WAIT_SERVICES_SCRIPT: Final[str] = """\
deadline=$(( $(date +%s) + 240 ))
while :; do
    down=""
    for name in system_interface host-backup; do
        state=$(supervisorctl status "$name" 2>/dev/null | awk '{print $2}')
        [ "$state" = RUNNING ] || down="$down $name=${state:-absent}"
    done
    if [ -z "$down" ]; then echo "SERVICES_OK"; exit 0; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then echo "SERVICES_DOWN$down"; exit 0; fi
    sleep 5
done
"""


# Runs inside the workspace container under python3. overlayfs refuses to
# rename a directory that exists in the image layer (EXDEV), and ``mv`` then
# silently degrades to a full copy -- of a multi-GB home tree, onto the VM's
# root disk. So the aside move goes entry by entry: rename what can be renamed
# (the workspace checkout and everything created in the container), and copy
# only the image-layer entries, after checking the root disk can hold them.
# ``restore`` reverses it. Prints ``SWITCHED ...``, ``RESTORED`` or
# ``SWITCH_FAILED <detail>``.
_IN_CONTAINER_SWITCH_SCRIPT: Final[str] = """\
import errno, os, shutil, sys

mode = sys.argv[1]
home, aside, volume_home = "/home/user", "/home/user.pre-migration", "/mngr-vol/home"


def tree_bytes(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
            except OSError:
                pass
    return total + os.lstat(path).st_blocks * 512


if mode == "aside":
    os.mkdir(aside)
    renamed, pending = [], []
    for name in os.listdir(home):
        try:
            os.rename(os.path.join(home, name), os.path.join(aside, name))
            renamed.append(name)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            pending.append(name)
    # Renames cost nothing; only image-layer entries are copied and need
    # root-disk room (plus a margin), so a full root disk blocks nothing when
    # every entry renames.
    need = sum(tree_bytes(os.path.join(home, name)) for name in pending)
    st = os.statvfs("/")
    free = st.f_bavail * st.f_frsize
    if pending and free < need + 512 * 1024 * 1024:
        for name in renamed:
            os.rename(os.path.join(aside, name), os.path.join(home, name))
        os.rmdir(aside)
        sys.stdout.write("SWITCH_FAILED the root disk has %d bytes free but the image-layer entries need %d\\n" % (free, need))
        sys.exit(0)
    for name in pending:
        shutil.move(os.path.join(home, name), os.path.join(aside, name))
    os.rmdir(home)
    os.symlink(volume_home, home)
    sys.stdout.write("SWITCHED renamed=%d copied=%d\\n" % (len(renamed), len(pending)))
elif mode == "restore":
    os.remove(home)
    os.mkdir(home)
    for name in os.listdir(aside):
        shutil.move(os.path.join(aside, name), os.path.join(home, name))
    os.rmdir(aside)
    sys.stdout.write("RESTORED\\n")
"""


@pure
def _python_in_container_command(script: str, *args: str) -> str:
    """A ``sh -c`` body that runs ``script`` under the container's python3 with ``args`` (base64 keeps the quoting trivial)."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    suffix = "".join(f" {shlex.quote(arg)}" for arg in args)
    return f"printf %s '{encoded}' | base64 -d | python3 -{suffix}"


@pure
def _bash_in_container_command(script: str) -> str:
    """A ``sh -c`` body that runs ``script`` under the container's bash (base64 keeps the quoting trivial)."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"printf %s '{encoded}' | base64 -d | bash"


@pure
def build_vm_script(action: HomeLayoutAction) -> str:
    """The in-VM bash script for ``action`` (run as root; drives the container via ``docker exec``).

    Every exit prints one ``RESULT_MARKER`` line; ``probe=1`` lines carry the
    measurements and precede the verdict. The script never uses ``set -e``:
    each step checks its own status so a failure after the quiesce still pays
    the service restart it owes.
    """
    quiesce_command = _bash_in_container_command(_IN_CONTAINER_QUIESCE_SCRIPT)
    wait_services_command = _bash_in_container_command(_IN_CONTAINER_WAIT_SERVICES_SCRIPT)
    switch_aside_command = _python_in_container_command(_IN_CONTAINER_SWITCH_SCRIPT, "aside")
    switch_restore_command = _python_in_container_command(_IN_CONTAINER_SWITCH_SCRIPT, "restore")
    stale_cwd_command = _bash_in_container_command(_IN_CONTAINER_STALE_CWD_SCRIPT)
    return f"""\
set -u
# The operator's SSH session is not the migration's lifetime. No hop in the
# ssh -> limactl shell -> sudo chain allocates a pty, so a dropped client does
# not hang up the script; it closes the script's stdout, and the next echo
# would otherwise kill bash with SIGPIPE between the quiesce and the restart.
trap '' HUP PIPE
ACTION={action.value}
is_resume_owed=0
emit() {{ echo "{RESULT_MARKER} $*"; }}
b64() {{ printf '%s' "$1" | base64 -w0; }}
in_container() {{ docker exec "$cid" sh -c "$1"; }}
resume_services() {{
    if [ "$is_resume_owed" = 1 ]; then
        is_resume_owed=0
        echo "restarting the workspace services"
        docker exec "$cid" supervisorctl restart all >/dev/null 2>&1 || true
        services_verdict=$(docker exec "$cid" sh -c {shlex.quote(wait_services_command)} 2>/dev/null || echo "SERVICES_DOWN unknown")
        echo "services: $services_verdict"
    fi
}}
finish() {{
    resume_services
    emit "status=$1 detail_b64=$(b64 "$2")"
    exit 0
}}

cid=$(docker ps -q --filter label=com.imbue.mngr.host-id | sed -n 1p)
[ -n "$cid" ] || finish failed "no running workspace container on this VM"
cname=$(docker inspect -f '{{{{.Name}}}}' "$cid" | sed 's#^/##')
vol=$(docker inspect -f '{{{{range .Mounts}}}}{{{{if eq .Destination "/mngr-vol"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}' "$cid")
[ -n "$vol" ] && [ -d "$vol" ] || finish failed "container $cname has no /mngr-vol volume mount"
# The volume is a per-host btrfs subvolume bind-mounted at docker's volume
# path; the rollback snapshot must be created on the same btrfs, whose mount
# the outer snapshot helper reaches through /mngr-btrfs.
mount_root=$(readlink -f /mngr-btrfs)
[ -n "$mount_root" ] && [ "$(findmnt -n -o FSTYPE "$mount_root" 2>/dev/null)" = btrfs ] && [ -d "$mount_root/snapshots" ] \
    || finish failed "/mngr-btrfs does not resolve to the btrfs data mount that holds the snapshots dir"

if in_container '[ -L /home/user ]'; then
    layout=home
elif in_container '[ -d /home/user ] && [ -L /home/user/.mngr ]'; then
    layout=legacy
else
    layout=unknown
fi
home_bytes=$(in_container 'du -sxB1 /home/user 2>/dev/null | cut -f1')
[ -n "$home_bytes" ] || home_bytes=0
free_bytes=$(df -B1 --output=avail "$vol" | awk 'NR==2')
[ -n "$free_bytes" ] || free_bytes=0
emit "probe=1 layout=$layout container=$cname home_bytes=$home_bytes free_bytes=$free_bytes"

if [ "$ACTION" = probe ]; then
    [ "$layout" = home ] && finish home_layout "already on the home/ layout"
    [ "$layout" = legacy ] && finish legacy_layout "legacy layout: /home/user is in the container's writable layer"
    finish failed "unrecognized container layout"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
staging="$vol/.home-layout-staging"

if [ "$ACTION" = migrate ]; then
    [ "$layout" = legacy ] || finish blocked "layout is $layout, not legacy; nothing to migrate"
    [ ! -e "$vol/home" ] || finish blocked "$vol/home already exists; inspect before retrying"
    [ ! -e "$staging" ] || finish blocked "$staging exists from an interrupted run; inspect before retrying"
    [ -d "$vol/host_dir" ] || finish blocked "$vol/host_dir is missing; the volume is not in the legacy shape"
    in_container '[ "$(readlink -f /home/user/.mngr)" = /mngr-vol/host_dir ]' \\
        || finish blocked "/home/user/.mngr does not resolve to /mngr-vol/host_dir"
    in_container '[ ! -e /home/user.pre-migration ]' \\
        || finish blocked "/home/user.pre-migration already exists in the container; inspect before retrying"
    needed=$(( home_bytes + home_bytes / 10 + 1073741824 ))
    [ "$free_bytes" -gt "$needed" ] || finish blocked "only $free_bytes bytes free on the data disk; need $needed"

    echo "quiescing the workspace (stopping chats, services, in-flight backups)"
    is_resume_owed=1
    quiesce_verdict=$(docker exec "$cid" sh -c {shlex.quote(quiesce_command)} 2>&1 | sed -n '$p')
    case "$quiesce_verdict" in
        QUIESCED*) echo "$quiesce_verdict" ;;
        *) finish failed "quiesce did not complete: $quiesce_verdict" ;;
    esac

    echo "snapshotting the host subvolume for rollback"
    mkdir -p "$mount_root/rollback"
    rollback_snapshot="$mount_root/rollback/pre-home-layout-$stamp"
    btrfs subvolume snapshot -r "$vol" "$rollback_snapshot" >/dev/null \\
        || finish failed "btrfs snapshot of $vol failed; nothing was changed"

    echo "copying /home/user out of the container onto the volume"
    mkdir "$staging" || finish failed "could not create $staging"
    if ! docker cp "$cid:/home/user" - | tar -x -C "$staging"; then
        rm -rf "$staging"
        finish failed "docker cp of /home/user failed; nothing was changed (rollback snapshot kept at $rollback_snapshot)"
    fi
    [ -d "$staging/user" ] || {{ rm -rf "$staging"; finish failed "the copied archive did not contain a user/ directory"; }}
    rm -f "$staging/user/.mngr"
    echo "verifying the copy"
    container_entries=$(in_container 'find /home/user -xdev \\( -type f -o -type l \\) | wc -l')
    copied_entries=$(find "$staging/user" \\( -type f -o -type l \\) | wc -l)
    # The dropped .mngr symlink is the one entry the container has and the copy does not.
    [ "$container_entries" -eq $((copied_entries + 1)) ] \\
        || {{ rm -rf "$staging"; finish failed "copy verification failed: container has $container_entries files, the copy has $copied_entries (expected one fewer); nothing was changed"; }}
    mv "$staging/user" "$vol/home" || finish failed "could not move the copied home tree into place"
    rmdir "$staging"
    copied_bytes=$(du -sxB1 "$vol/home" | cut -f1)

    echo "moving host_dir under home/.mngr"
    mv "$vol/host_dir" "$vol/home/.mngr" || finish failed "could not rename host_dir under home/; the home copy is at $vol/home"
    mkdir "$vol/host_dir"

    echo "switching the container's /home/user onto the volume"
    switch_verdict=$(docker exec "$cid" sh -c {shlex.quote(switch_aside_command)} 2>&1 | sed -n '$p')
    case "$switch_verdict" in
        SWITCHED*) echo "$switch_verdict" ;;
        *)
            rmdir "$vol/host_dir" && mv "$vol/home/.mngr" "$vol/host_dir"
            finish failed "could not switch /home/user inside the container ($switch_verdict); host_dir restored, home copy left at $vol/home"
            ;;
    esac
    in_container '[ "$(readlink -f /home/user/.mngr)" = /mngr-vol/home/.mngr ] && [ -d /home/user/workspace ] && [ -f /home/user/.mngr/host_id ]' \\
        || finish failed "post-switch verification failed; run --rollback"
    resume_services
    stale=$(docker exec "$cid" sh -c {shlex.quote(stale_cwd_command)} 2>/dev/null || echo "STALE_CWD ? unknown")
    finish migrated "home tree ($copied_bytes bytes) now on the volume; rollback snapshot at $rollback_snapshot; aside copy at /home/user.pre-migration in the container; processes still in the old tree: ${{stale#STALE_CWD }}"
fi

if [ "$ACTION" = rollback ]; then
    [ "$layout" = home ] || finish blocked "layout is $layout, not home; nothing to roll back"
    in_container '[ -d /home/user.pre-migration ]' \\
        || finish blocked "no /home/user.pre-migration in the container; this workspace was not migrated by this tool (or the aside copy was cleaned up)"
    [ -d "$vol/home/.mngr" ] || finish blocked "$vol/home/.mngr is missing"
    [ -d "$vol/host_dir" ] && [ -z "$(ls -A "$vol/host_dir")" ] || finish blocked "$vol/host_dir is missing or not empty"

    echo "quiescing the workspace (stopping chats, services, in-flight backups)"
    is_resume_owed=1
    quiesce_verdict=$(docker exec "$cid" sh -c {shlex.quote(quiesce_command)} 2>&1 | sed -n '$p')
    case "$quiesce_verdict" in
        QUIESCED*) echo "$quiesce_verdict" ;;
        *) finish failed "quiesce did not complete: $quiesce_verdict" ;;
    esac

    echo "switching the container's /home/user back to the aside copy"
    restore_verdict=$(docker exec "$cid" sh -c {shlex.quote(switch_restore_command)} 2>&1 | sed -n '$p')
    [ "$restore_verdict" = RESTORED ] || finish failed "could not switch /home/user back inside the container ($restore_verdict)"
    rmdir "$vol/host_dir" || finish failed "could not remove the empty $vol/host_dir after switching the container back; the host_dir data is still at $vol/home/.mngr"
    mv "$vol/home/.mngr" "$vol/host_dir" || finish failed "could not move home/.mngr back to host_dir"
    mv "$vol/home" "$vol/.home-rolled-back-$stamp" \\
        || finish failed "host_dir restored, but the migrated home copy could not be moved aside; it is still at $vol/home"
    in_container '[ "$(readlink -f /home/user/.mngr)" = /mngr-vol/host_dir ] && [ -d /home/user/workspace ]' \\
        || finish failed "post-rollback verification failed"
    resume_services
    finish rolled_back "legacy layout restored; the migrated home copy is kept at $vol/.home-rolled-back-$stamp"
fi

finish failed "unknown action $ACTION"
"""


@pure
def parse_vm_script_output(stdout: str) -> tuple[HomeLayoutStatus, str, HomeLayoutProbe | None]:
    """Interpret the script's stdout into ``(status, detail, probe)``.

    Output without a verdict line counts as a failure: the script always prints
    one, so its absence means the command was cut short.
    """
    probe: HomeLayoutProbe | None = None
    verdict: tuple[HomeLayoutStatus, str] | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith(RESULT_MARKER + " "):
            continue
        tokens = dict(token.split("=", 1) for token in stripped[len(RESULT_MARKER) + 1 :].split() if "=" in token)
        if tokens.get("probe") == "1":
            probe = HomeLayoutProbe(
                layout=_parse_layout(tokens.get("layout", "")),
                container=tokens.get("container", ""),
                home_bytes=_parse_int(tokens.get("home_bytes", "")),
                free_bytes=_parse_int(tokens.get("free_bytes", "")),
            )
            continue
        status_text = tokens.get("status", "")
        if status_text not in {member.value for member in HomeLayoutStatus}:
            verdict = (HomeLayoutStatus.FAILED, f"unrecognized verdict {stripped!r}")
            continue
        verdict = (HomeLayoutStatus(status_text), _decode_detail(tokens.get("detail_b64", "")))
    if verdict is None:
        return HomeLayoutStatus.FAILED, f"script produced no verdict: {stdout[-300:]!r}", probe
    return verdict[0], verdict[1], probe


@pure
def _parse_layout(text: str) -> VolumeLayout:
    if text in {member.value for member in VolumeLayout}:
        return VolumeLayout(text)
    return VolumeLayout.UNKNOWN


@pure
def _parse_int(text: str) -> int:
    return int(text) if text.isdigit() else 0


@pure
def _decode_detail(encoded: str) -> str:
    try:
        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return encoded


def repair_home_layout_on_target(
    client: LimaSliceVpsClient, target: HomeLayoutTarget, action: HomeLayoutAction
) -> HomeLayoutOutcome:
    """Run ``action`` on one leased slice workspace; never raises for an in-VM verdict."""
    is_mutating = action != HomeLayoutAction.PROBE
    rc, stdout, stderr = client.run_in_vm_as_root(
        target.vm_name,
        build_vm_script(action),
        timeout=_MIGRATE_TIMEOUT_SECONDS if is_mutating else _PROBE_TIMEOUT_SECONDS,
        label=f"home-layout-{action.value}-{target.host_name}",
        is_streaming=is_mutating,
    )
    if rc != 0 and RESULT_MARKER not in stdout:
        return HomeLayoutOutcome(
            host_id=target.host_id,
            host_name=target.host_name,
            vm_name=target.vm_name,
            server_id=str(target.server.id),
            status=HomeLayoutStatus.FAILED,
            detail=f"in-VM script could not run: {stderr.strip() or f'exited {rc}'}",
        )
    status, detail, probe = parse_vm_script_output(stdout)
    logger.info(
        "{} {} ({} on box {}): {} -- {}",
        action.value,
        target.host_name,
        target.vm_name,
        target.server.id,
        status.value,
        detail,
    )
    return HomeLayoutOutcome(
        host_id=target.host_id,
        host_name=target.host_name,
        vm_name=target.vm_name,
        server_id=str(target.server.id),
        status=status,
        detail=detail,
        probe=probe,
    )


@pure
def build_home_layout_report(outcomes: list[HomeLayoutOutcome], unreachable: list[str]) -> HomeLayoutReport:
    counts = Counter(outcome.status for outcome in outcomes)
    return HomeLayoutReport(
        home_layout=counts[HomeLayoutStatus.HOME_LAYOUT],
        legacy_layout=counts[HomeLayoutStatus.LEGACY_LAYOUT],
        migrated=counts[HomeLayoutStatus.MIGRATED],
        rolled_back=counts[HomeLayoutStatus.ROLLED_BACK],
        blocked=counts[HomeLayoutStatus.BLOCKED],
        failed=counts[HomeLayoutStatus.FAILED],
        unreachable=tuple(unreachable),
        outcomes=tuple(outcomes),
    )
