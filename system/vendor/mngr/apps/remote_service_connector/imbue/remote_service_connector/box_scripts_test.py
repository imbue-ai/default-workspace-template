from imbue.remote_service_connector import box_scripts

_INSTANCE = "mngr-slice-test-" + "a" * 32
_DISK = _INSTANCE + "-data"


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
    assert "flock 8" in script


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
