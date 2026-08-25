import hashlib
import os
import sys
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.test_utils import poll_until
from imbue.remote_service_connector import box_scripts

_INSTANCE = "mngr-slice-test-" + "a" * 32
_DISK = _INSTANCE + "-data"

# The rendered scripts need a GNU userland (flock, setsid, /proc): they only
# ever run on the Linux boxes, and macOS ships none of it.
_linux_only = pytest.mark.skipif(sys.platform != "linux", reason="needs flock, setsid, and /proc")


def test_render_transfer_env_quotes_values_and_omits_empty_identity() -> None:
    env_text = box_scripts.render_transfer_env(
        box_scripts.TransferEnv(
            s3_endpoint="https://s3.example",
            s3_region="us-east-va",
            access_key_id="AKIA",
            secret_access_key="se'cret",
            bucket="bkt",
            key_prefix="host-aa/gen-1",
            instance_name=_INSTANCE,
            age_recipient="age1xyz",
        )
    )
    assert "export AWS_SECRET_ACCESS_KEY='se'\"'\"'cret'" in env_text
    assert "WS_AGE_IDENTITY" not in env_text

    with_identity = box_scripts.render_transfer_env(
        box_scripts.TransferEnv(
            s3_endpoint="https://s3.example",
            s3_region="us-east-va",
            access_key_id="AKIA",
            secret_access_key="secret",
            bucket="bkt",
            key_prefix="host-aa/gen-1",
            instance_name=_INSTANCE,
            age_identity="AGE-SECRET-KEY-1XYZ",
        )
    )
    assert "export WS_AGE_IDENTITY=AGE-SECRET-KEY-1XYZ" in with_identity


def test_upload_script_streams_both_disks_and_meta() -> None:
    script = box_scripts.render_upload_script(_INSTANCE, _DISK)
    assert f'"$HOME/.lima/$WS_INSTANCE/disk" "{box_scripts.DISK_OBJECT}" DISK' in script
    assert f'"$HOME/.lima/_disks/{_DISK}/datadisk" "{box_scripts.DATADISK_OBJECT}" DATADISK' in script
    assert 'age -e -r "$WS_AGE_RECIPIENT"' in script
    assert "s5cmd --endpoint-url" in script
    assert "STAGE uploaded" in script
    # Every failure path publishes a status the supervisor can read --
    # errtrace (-E) makes the ERR trap fire inside upload_one/download_one
    # too, so the status carries the actual failing command.
    assert "trap 'fail \"command failed: $BASH_COMMAND\"' ERR" in script
    assert "set -Eeuo pipefail" in script


def test_download_script_verifies_shas_and_waits_for_sshd() -> None:
    script = box_scripts.render_download_script(
        _INSTANCE,
        _DISK,
        expected_sha_by_name={"DISK": "aa11", "DATADISK": "bb22", "META": "cc33"},
        vm_ssh_port=23000,
        container_ssh_port=23001,
    )
    assert "DISK aa11" in script
    assert "DATADISK bb22" in script
    assert "wait_ssh 23000" in script
    assert "wait_ssh 23001" in script
    assert "age -d -i" in script
    assert box_scripts.STOP_MARKER_FILENAME in script
    # A stuck lock fails the restore in minutes with a real error instead of
    # parking it behind the transfer timeout.
    assert f"flock -w {box_scripts.DOWNLOAD_LOCK_WAIT_SECONDS} 8 || fail" in script


def test_restore_reserve_script_claims_slot_and_rewrites_ports() -> None:
    script = box_scripts.render_restore_reserve_script(
        instance_name=_INSTANCE,
        disk_name=_DISK,
        slot_count=6,
        old_vm_ssh_port=22000,
        old_container_ssh_port=22001,
        expected_meta_sha="cc33",
    )
    assert box_scripts.RESTORE_BOX_FULL_MARKER in script
    assert box_scripts.RESTORE_NO_PORTS_MARKER in script
    assert box_scripts.RESTORE_RESERVED_MARKER in script
    # The port rewrite goes old port -> unique placeholder -> new port in two
    # sed passes: a chosen port may equal the OTHER forward's old port, and a
    # single sequential pass would then re-match the just-rewritten line.
    assert "s/hostPort: 22000$/hostPort: MNGR_NEW_VM_PORT/" in script
    assert "s/hostPort: 22001$/hostPort: MNGR_NEW_CONTAINER_PORT/" in script
    assert "s/hostPort: MNGR_NEW_VM_PORT$/hostPort: $vm_port/" in script
    assert "s/hostPort: MNGR_NEW_CONTAINER_PORT$/hostPort: $container_port/" in script
    assert f'mkdir -p "$HOME/.lima/_disks/{_DISK}"' in script


def test_stop_and_finalize_commands_cover_marker_and_teardown() -> None:
    stop_commands = box_scripts.build_stop_vm_commands(_INSTANCE)
    assert any("limactl stop" in command for command in stop_commands)
    assert any(box_scripts.STOP_MARKER_FILENAME in command for command in stop_commands)

    finalize_commands = box_scripts.build_finalize_stop_commands(_INSTANCE, _DISK)
    assert any("limactl delete --force" in command for command in finalize_commands)
    assert any("limactl disk delete --force" in command for command in finalize_commands)


def test_launch_detached_command_clears_stale_status_synchronously() -> None:
    command = box_scripts.build_launch_detached_command(_INSTANCE, "upload.sh")
    # A stale status file (failed earlier attempt, leftover download status)
    # must be gone before the launch returns, so pollers never misread it.
    assert "rm -f status" in command
    assert command.index("rm -f status") < command.index("setsid nohup bash upload.sh")


def test_parse_status_text_handles_blank_and_malformed_lines() -> None:
    parsed = box_scripts.parse_status_text("STAGE=uploaded\n\nnot a pair\nFINISHED=1\nERROR=a=b\n")
    assert parsed["STAGE"] == "uploaded"
    assert parsed["FINISHED"] == "1"
    assert parsed["ERROR"] == "a=b"
    assert "not a pair" not in parsed


def test_parse_reserved_ports_line_extracts_ports_or_none() -> None:
    assert box_scripts.parse_reserved_ports_line("noise\nMNGR_RESTORE_RESERVED 23000 23001\n") == (23000, 23001)
    assert box_scripts.parse_reserved_ports_line("MNGR_RESTORE_RESERVED nope 23001") is None
    assert box_scripts.parse_reserved_ports_line("") is None


# Real-bash runs of the rendered scripts against stubbed box tooling.

_STUB_S5CMD = "#!/bin/bash\nprintf 'payload'\n"
_STUB_AGE = "#!/bin/bash\ncat\n"
# ``zstd -q -d -f -o <target>``: write stdin to the target.
_STUB_ZSTD = '#!/bin/bash\nwhile [ $# -gt 1 ]; do if [ "$1" = -o ]; then shift; break; fi; shift; done\ncat > "$1"\n'
# wait_ssh runs ``timeout 3 bash -c '...'`` and greps its output for SSH.
_STUB_TIMEOUT = "#!/bin/bash\necho SSH-2.0-stub\n"
# ``limactl start`` records whether the download lock's fd 8 reached it and
# leaves a detached sleeper behind (standing in for the hostagent + qemu,
# which inherit every descriptor and outlive limactl). ``limactl delete
# --force`` removes the instance dir unless $HOME/limactl-delete-is-broken
# exists.
_STUB_LIMACTL = """\
#!/bin/bash
if [ "$2" = start ]; then
    if [ -e /proc/$$/fd/8 ]; then echo fd8=open > "$HOME/limactl-start-fd8"; else echo fd8=closed > "$HOME/limactl-start-fd8"; fi
    setsid sleep 60 </dev/null >/dev/null 2>&1 &
    echo $! > "$HOME/vm.pid"
fi
if [ "$1" = delete ] && [ ! -e "$HOME/limactl-delete-is-broken" ]; then rm -rf "$HOME/.lima/$3"; fi
"""


def _make_fake_box_home(home: Path) -> dict[str, str]:
    """Lay out a lima user's home with stub tooling; returns the env to run scripts under."""
    stub_bin = home / ".local" / "bin"
    stub_bin.mkdir(parents=True)
    for name, body in (
        ("s5cmd", _STUB_S5CMD),
        ("age", _STUB_AGE),
        ("zstd", _STUB_ZSTD),
        ("timeout", _STUB_TIMEOUT),
        ("limactl", _STUB_LIMACTL),
    ):
        (stub_bin / name).write_text(body)
        (stub_bin / name).chmod(0o755)
    (home / ".lima" / _INSTANCE).mkdir(parents=True)
    (home / ".lima" / "_disks" / _DISK).mkdir(parents=True)
    transfer = home / box_scripts.TRANSFER_DIR_ROOT / _INSTANCE
    transfer.mkdir(parents=True)
    (transfer / "env").write_text(
        box_scripts.render_transfer_env(
            box_scripts.TransferEnv(
                s3_endpoint="https://s3.example",
                s3_region="r",
                access_key_id="a",
                secret_access_key="s",
                bucket="b",
                key_prefix="k",
                instance_name=_INSTANCE,
                age_identity="AGE-SECRET-KEY-1TEST",
            )
        )
    )
    payload_sha = hashlib.sha256(b"payload").hexdigest()
    (transfer / "download.sh").write_text(
        box_scripts.render_download_script(
            _INSTANCE,
            _DISK,
            expected_sha_by_name={"DISK": payload_sha, "DATADISK": payload_sha, "META": "cc33"},
            vm_ssh_port=23000,
            container_ssh_port=23001,
        )
    )
    return {"HOME": str(home), "PATH": os.environ["PATH"]}


def _transfer_status(home: Path) -> dict[str, str]:
    status_file = home / box_scripts.TRANSFER_DIR_ROOT / _INSTANCE / "status"
    return box_scripts.parse_status_text(status_file.read_text()) if status_file.exists() else {}


def _is_lock_free(cg: ConcurrencyGroup, env: dict[str, str]) -> bool:
    lock = f"{env['HOME']}/{box_scripts.DOWNLOAD_LOCK_RELPATH}"
    return cg.run_process_to_completion(["flock", "-n", lock, "true"], env=env, is_checked_after=False).returncode == 0


def _hold_lock(cg: ConcurrencyGroup, env: dict[str, str]) -> RunningProcess:
    """Take the box download lock from a single long-lived process (terminate() releases it)."""
    holder = cg.run_process_in_background(
        ["bash", "-c", f'exec 8> "$HOME/{box_scripts.DOWNLOAD_LOCK_RELPATH}"; flock 8; exec sleep 60'],
        env=env,
        is_checked_by_group=False,
    )
    assert poll_until(lambda: not _is_lock_free(cg, env)), "lock holder never took the lock"
    return holder


def _is_process_running(pid: int) -> bool:
    status = Path("/proc") / str(pid) / "status"
    if not status.exists():
        return False
    return not any(line.startswith("State:") and "Z" in line for line in status.read_text().splitlines())


def _run_cleanup(cg: ConcurrencyGroup, env: dict[str, str]) -> tuple[int, str]:
    command = " && ".join(box_scripts.build_cleanup_reserved_restore_commands(_INSTANCE, _DISK))
    finished = cg.run_process_to_completion(["bash", "-c", command], env=env, is_checked_after=False)
    return finished.returncode or 0, finished.stderr


@_linux_only
def test_download_script_releases_the_box_lock_before_booting_the_vm(tmp_path: Path) -> None:
    """The lock must not reach ``limactl start``: the hostagent and qemu inherit
    every open descriptor and would hold the box's download lock for the life
    of the VM, stalling every later restore on the box."""
    env = _make_fake_box_home(tmp_path)
    transfer = tmp_path / box_scripts.TRANSFER_DIR_ROOT / _INSTANCE
    with ConcurrencyGroup(name="restore") as cg:
        finished = cg.run_process_to_completion(["bash", "download.sh"], cwd=transfer, env=env, is_checked_after=False)
        assert finished.returncode == 0, finished.stderr
        assert _transfer_status(tmp_path) == {"STAGE": "started", "FINISHED": "1"}
        assert (tmp_path / "limactl-start-fd8").read_text().strip() == "fd8=closed"
        vm_pid = int((tmp_path / "vm.pid").read_text())
        try:
            assert _is_process_running(vm_pid)
            assert _is_lock_free(cg, env)
        finally:
            os.kill(vm_pid, 9)


@_linux_only
def test_download_script_queues_behind_the_box_lock_and_reports_it(tmp_path: Path) -> None:
    """A restore waiting on another restore's download publishes
    ``waiting-for-lock`` (so a queued transfer is distinguishable from one that
    never started) and proceeds once the lock is released."""
    env = _make_fake_box_home(tmp_path)
    transfer = tmp_path / box_scripts.TRANSFER_DIR_ROOT / _INSTANCE
    with ConcurrencyGroup(name="restore") as cg:
        holder = _hold_lock(cg, env)
        script = cg.run_process_in_background(["bash", "download.sh"], cwd=transfer, env=env)
        assert poll_until(lambda: _transfer_status(tmp_path).get("STAGE") == "waiting-for-lock"), (
            "never queued on the lock"
        )
        assert script.poll() is None
        assert not (transfer / "DISK.sha").exists()
        holder.terminate()
        assert script.wait(timeout=5) == 0
        assert _transfer_status(tmp_path) == {"STAGE": "started", "FINISHED": "1"}
        os.kill(int((tmp_path / "vm.pid").read_text()), 9)


@_linux_only
def test_cleanup_commands_kill_the_queued_transfer_and_roll_back_the_slot(tmp_path: Path) -> None:
    """Rolling back a restore that timed out waiting for the lock must take the
    detached ``download.sh`` + ``flock`` pair with it (an abandoned waiter
    would otherwise sit on the lock queue forever) and then drop the claimed
    instance, disk, and transfer dirs."""
    env = _make_fake_box_home(tmp_path)
    (tmp_path / ".lima" / _INSTANCE / "lima.yaml").write_text("vmType: qemu\n")
    transfer = tmp_path / box_scripts.TRANSFER_DIR_ROOT / _INSTANCE
    with ConcurrencyGroup(name="rollback") as cg:
        holder = _hold_lock(cg, env)
        launch = box_scripts.build_launch_detached_command(_INSTANCE, "download.sh")
        cg.run_process_to_completion(["bash", "-c", launch], env=env)
        transfer_pid = int((transfer / "pid").read_text())
        assert poll_until(lambda: _transfer_status(tmp_path).get("STAGE") == "waiting-for-lock"), (
            "never queued on the lock"
        )

        returncode, stderr = _run_cleanup(cg, env)

        assert returncode == 0, stderr
        assert box_scripts.CLEANUP_DELETE_FAILED_MARKER not in stderr
        assert poll_until(lambda: not _is_process_running(transfer_pid)), "transfer still running after rollback"
        assert not (tmp_path / ".lima" / _INSTANCE).exists()
        assert not (tmp_path / ".lima" / "_disks" / _DISK).exists()
        assert not transfer.exists()
        holder.terminate()


@_linux_only
def test_cleanup_commands_keep_the_dirs_when_the_vm_survives_limactl_delete(tmp_path: Path) -> None:
    """When the instance config outlives ``limactl delete`` the VM may still be
    running off the instance dir: deleting it would leave a ghost qemu on
    unlinked inodes (holding the box's lock, ports, and RAM), so the dirs are
    kept and the survivor is reported. The transfer dir (creds) goes anyway."""
    env = _make_fake_box_home(tmp_path)
    (tmp_path / ".lima" / _INSTANCE / "lima.yaml").write_text("vmType: qemu\n")
    (tmp_path / "limactl-delete-is-broken").touch()
    with ConcurrencyGroup(name="rollback") as cg:
        returncode, stderr = _run_cleanup(cg, env)
    assert returncode == 0, stderr
    assert box_scripts.CLEANUP_DELETE_FAILED_MARKER in stderr
    assert (tmp_path / ".lima" / _INSTANCE / "lima.yaml").exists()
    assert (tmp_path / ".lima" / "_disks" / _DISK).exists()
    assert not (tmp_path / box_scripts.TRANSFER_DIR_ROOT / _INSTANCE).exists()


@_linux_only
def test_cleanup_commands_never_group_kill_a_reused_pid(tmp_path: Path) -> None:
    """A stale pid file may name an unrelated process on the shared box; the
    rollback only signals a pid whose cwd is this instance's transfer dir."""
    env = _make_fake_box_home(tmp_path)
    transfer = tmp_path / box_scripts.TRANSFER_DIR_ROOT / _INSTANCE
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with ConcurrencyGroup(name="rollback") as cg:
        bystander = cg.run_process_in_background(
            ["bash", "-c", f'echo $$ > "{transfer}/pid"; exec sleep 60'], cwd=elsewhere, is_checked_by_group=False
        )
        assert poll_until(lambda: (transfer / "pid").exists() and (transfer / "pid").read_text().strip() != "")

        returncode, stderr = _run_cleanup(cg, env)

        assert returncode == 0, stderr
        assert bystander.poll() is None
        bystander.terminate()
