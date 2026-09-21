"""Shared non-fixture test utilities for the slices package."""

import base64
import json
import shlex
import stat
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_admin.slices.archive_types import ArchiveStreamSummary
from imbue.minds_admin.slices.archive_types import WorkspaceArchiveManifest
from imbue.minds_admin.slices.cutover_db import CutoverPoolRow
from imbue.minds_admin.slices.cutover_types import CutoverStage
from imbue.minds_admin.slices.cutover_types import CutoverWorkspaceState
from imbue.minds_admin.slices.cutover_types import HarvestedFile
from imbue.minds_admin.slices.cutover_types import HarvestedKeys
from imbue.minds_admin.slices.cutover_types import HarvestedLatchkeyState
from imbue.minds_admin.slices.cutover_types import LatchkeyReplayPlan
from imbue.minds_admin.slices.cutover_types import SavedProductArtifact
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_DIR
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_SUPERVISOR_CONF_DIR
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_TMPFS_DIR
from imbue.minds_admin.slices.operator_identity import ManagementIdentityResolver
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY


class RecordingCursor:
    """A psycopg2 cursor stand-in: returns scripted rows, reports a scripted rowcount, records every statement.

    ``error_by_param`` scripts a failure: a statement whose bind parameters contain
    one of its keys raises the mapped error instead of being recorded (e.g. a
    unique-index violation for one row's service name).
    """

    def __init__(
        self, rows: list[tuple[Any, ...]], rowcount: int, error_by_param: Mapping[Any, Exception] | None = None
    ) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self._error_by_param = dict(error_by_param) if error_by_param else {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        for param, error in self._error_by_param.items():
            if param in params:
                raise error
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    @property
    def executed_params(self) -> tuple[Any, ...] | None:
        """The bind parameters of the last executed statement (None before any)."""
        return self.executed[-1][1] if self.executed else None


class RecordingConnection:
    """A psycopg2 connection stand-in yielding one ``RecordingCursor`` and counting commits and rollbacks (no real DB)."""

    def __init__(
        self, rows: list[tuple[Any, ...]], rowcount: int, error_by_param: Mapping[Any, Exception] | None = None
    ) -> None:
        self.recording_cursor = RecordingCursor(rows, rowcount, error_by_param)
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self) -> RecordingCursor:
        return self.recording_cursor

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


def make_cutover_workspace_state(
    host_db_id: str, origin_server_id: str, target_server_id: str = "target-box"
) -> CutoverWorkspaceState:
    """A freshly harvested gen-1 workspace record (a 28 GiB data disk stamped for a 44 GiB gen-2 row)."""
    return CutoverWorkspaceState(
        host_db_id=host_db_id,
        host_id=f"host-{uuid4().hex}",
        agent_id=f"agent-{uuid4().hex}",
        host_name="slice-x",
        leased_to_user="0123456789abcdef",
        origin_server_id=origin_server_id,
        origin_public_address="15.204.1.2",
        origin_vm_ssh_port=22010,
        origin_container_ssh_port=22011,
        target_server_id=target_server_id,
        is_host_key_rotated=True,
        slice_instance_name="mngr-slice-dev-x-" + "a" * 16,
        slice_disk_name="mngr-slice-dev-x-" + "a" * 16 + "-data",
        version_tag="minds-v0.4.2",
        gen1_data_disk_virtual_gib=28,
        gen1_data_disk_format="qcow2",
        migrated_data_disk_gib=44,
        memory_units=8,
        stage=CutoverStage.HARVESTED,
    )


def make_saved_product_artifact() -> SavedProductArtifact:
    """A saved product stop artifact whose manifest already points at the rollback copy."""
    return SavedProductArtifact(
        generation=3,
        key_prefix="dev-x/cutover/host-abc/rollback",
        age_recipient="age1recipient",
        datadisk_sha256="cc" * 32,
        wrapped_dek="d2VkZWs=",
        manifest_json={
            "generation": 3,
            "key_prefix": "dev-x/cutover/host-abc/rollback",
            "age_recipient": "age1recipient",
            "object_by_name": {"DATADISK": {"sha256": "cc" * 32, "size_bytes": 5}},
        },
    )


def make_harvested_keys() -> HarvestedKeys:
    """The trust material harvested off one gen-1 workspace (the VM's authorized_keys carries a comment and a blank line)."""
    return HarvestedKeys(
        vm_host_private_key=SecretStr("-----BEGIN OPENSSH PRIVATE KEY-----\nvm\n-----END OPENSSH PRIVATE KEY-----\n"),
        vm_host_public_key="ssh-ed25519 AAAAVM vm-host\n",
        vm_authorized_keys="# the pool key\nssh-ed25519 AAAAPOOL pool\n\nssh-ed25519 AAAAOWNER owner\n",
        container_host_private_key=SecretStr(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nc\n-----END OPENSSH PRIVATE KEY-----\n"
        ),
        container_host_public_key="ssh-ed25519 AAAAC container\n",
        container_authorized_keys="ssh-ed25519 AAAAOWNER owner\n",
    )


# The container sshd port the fixture tunnel drop-in dials (matches the
# ``22/tcp`` HostPort of the cutover scripts test's inspect fixture).
HARVESTED_TUNNEL_CONTAINER_PORT: Final[int] = 2222


def make_harvested_file(path: str, content: bytes, mode: str) -> HarvestedFile:
    return HarvestedFile(path=path, mode=mode, content_base64=SecretStr(base64.b64encode(content).decode("ascii")))


def make_harvested_latchkey_state(plan: LatchkeyReplayPlan) -> HarvestedLatchkeyState:
    """The latchkey state harvested off one gen-1 workspace VM, in one of the three shapes the replay handles.

    The credential store and the encryption key deliberately end without a
    newline (a cat-based harvest would have appended one).
    """
    if plan == LatchkeyReplayPlan.ABSENT:
        return HarvestedLatchkeyState(is_present=False, disk_files=(), supervisor_confs=(), tmpfs_files=())
    disk_files = (
        make_harvested_file(f"{VM_LATCHKEY_DIR}/credentials.json.enc", b'{"enc":"c2VjcmV0"}', "0600"),
        make_harvested_file(f"{VM_LATCHKEY_DIR}/config.json", b'{"hideBuiltinServices": true}\n', "0600"),
        make_harvested_file(f"{VM_LATCHKEY_DIR}/permissions.json", b'{"rules": []}\n', "0600"),
        make_harvested_file(f"{VM_LATCHKEY_DIR}/data-format-version", b"2\n", "0644"),
        make_harvested_file(
            f"{VM_LATCHKEY_DIR}/container_tunnel_key", b"-----BEGIN OPENSSH PRIVATE KEY-----\nt\n", "0600"
        ),
        make_harvested_file(
            f"{VM_LATCHKEY_DIR}/container_tunnel_key.pub", b"ssh-ed25519 AAAATUNNEL root@vm\n", "0644"
        ),
        make_harvested_file(f"{VM_LATCHKEY_DIR}/gateway_run.sh", b"#!/bin/sh\nexec latchkey gateway\n", "0700"),
        make_harvested_file(
            f"{VM_LATCHKEY_DIR}/extensions/desktop_gateway_proxy.mjs", b"export default {};\n", "0600"
        ),
    )
    tunnel_command = (
        "command=/usr/bin/ssh -N -T -o StrictHostKeyChecking=no -i /root/.latchkey/container_tunnel_key "
        f"-p {HARVESTED_TUNNEL_CONTAINER_PORT} -R 127.0.0.1:1989:127.0.0.1:1989 root@127.0.0.1\n"
    )
    supervisor_confs = (
        make_harvested_file(
            f"{VM_LATCHKEY_SUPERVISOR_CONF_DIR}/latchkey-gateway.conf",
            b"[program:latchkey-gateway]\ncommand=/bin/sh /root/.latchkey/gateway_run.sh\n",
            "0600",
        ),
        make_harvested_file(
            f"{VM_LATCHKEY_SUPERVISOR_CONF_DIR}/latchkey-tunnel.conf",
            b"[program:latchkey-tunnel]\n" + tunnel_command.encode("utf-8"),
            "0600",
        ),
    )
    desktop_pair = (
        make_harvested_file(f"{VM_LATCHKEY_TMPFS_DIR}/desktop_gateway_password", b"desktop-password", "0600"),
        make_harvested_file(f"{VM_LATCHKEY_TMPFS_DIR}/desktop_permissions_override", b"desktop-jwt", "0600"),
    )
    machine_pair = (
        make_harvested_file(
            f"{VM_LATCHKEY_TMPFS_DIR}/gateway_encryption_key", b"machine-key-0123456789abcdef0123456789ab", "0600"
        ),
        make_harvested_file(f"{VM_LATCHKEY_TMPFS_DIR}/gateway_listen_password", b"machine-password", "0600"),
    )
    tmpfs_files = (*machine_pair, *desktop_pair) if plan == LatchkeyReplayPlan.FULL else desktop_pair
    return HarvestedLatchkeyState(
        is_present=True, disk_files=disk_files, supervisor_confs=supervisor_confs, tmpfs_files=tmpfs_files
    )


def make_test_management_identities(
    tier: str | None, pool_private_key_pem: str, operator_key_path: Path | None = None
) -> ManagementIdentityResolver:
    """A resolver whose gen-1 key is the given PEM and whose gen-2 identity is a preset path (never Vault)."""
    return ManagementIdentityResolver(
        tier=tier,
        resolve_gen1_pool_private_key_pem=lambda: pool_private_key_pem,
        operator_key_path=operator_key_path,
    )


class FakeVaultSigner(FrozenModel):
    """A fake ``vault`` CLI whose ``write <mount>/sign/<role>`` answers a canned certificate and counts its calls.

    Tests reach it through the ``fake_vault_signer`` fixture, which puts
    ``binary_dir`` first on PATH; ``install`` rewrites the script with the
    behaviour a test needs (a refusal, or a pause that widens a race window).
    """

    binary_dir: Path = Field(description="The directory holding the fake ``vault`` executable")
    sign_count_path: Path = Field(description="File whose content is the number of sign invocations so far")
    signed_key_path: Path = Field(description="The ``-format=json`` sign response the script prints")

    @property
    def sign_count(self) -> int:
        return int(self.sign_count_path.read_text().strip())

    def install(self, *, is_sign_refused: bool = False, sleep_seconds: float = 0.0) -> None:
        """(Re)write the fake binary: pause ``sleep_seconds``, bump the count, then refuse (exit 2) or answer."""
        lines = ["#!/usr/bin/env bash"]
        if sleep_seconds > 0:
            lines.append(f"sleep {sleep_seconds}")
        lines.extend(
            [
                # Not atomic, but a read/increment race can only under-count
                # concurrent invocations, so a "signed exactly once" assertion
                # can only get stricter, never spuriously pass a double sign.
                f"count=$(cat {shlex.quote(str(self.sign_count_path))})",
                f"echo $((count + 1)) > {shlex.quote(str(self.sign_count_path))}",
            ]
        )
        if is_sign_refused:
            lines.append('echo "permission denied" >&2; exit 2')
        else:
            lines.append(f"cat {shlex.quote(str(self.signed_key_path))}")
        binary_path = self.binary_dir / "vault"
        binary_path.write_text("\n".join(lines) + "\n")
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_fake_vault_signer(root: Path) -> FakeVaultSigner:
    """A :class:`FakeVaultSigner` under ``root`` (created), installed in its default succeeding shape."""
    binary_dir = root / "bin"
    binary_dir.mkdir(parents=True)
    sign_count_path = root / "sign_count.txt"
    sign_count_path.write_text("0")
    signed_key_path = root / "signed_key.json"
    signed_key_path.write_text(json.dumps({"data": {"signed_key": "ssh-ed25519-cert-v01@openssh.com AAAAcert\n"}}))
    signer = FakeVaultSigner(binary_dir=binary_dir, sign_count_path=sign_count_path, signed_key_path=signed_key_path)
    signer.install()
    return signer


def make_workspace_archive_manifest(
    host_db_id: str,
    *,
    created_at: datetime,
    owner_email: str | None = "alice@example.com",
    host_name: str = "my-mind",
    host_id: str | None = None,
) -> WorkspaceArchiveManifest:
    """One archive's manifest, as ``minds-admin archives create`` records it."""
    resolved_host_id = host_id or f"host-{uuid4().hex}"
    prefix = f"dev-x/archives/{resolved_host_id}/{created_at.strftime('%Y%m%dT%H%M%SZ')}"
    return WorkspaceArchiveManifest(
        env_name="dev-x",
        host_db_id=host_db_id,
        host_id=resolved_host_id,
        agent_id=f"agent-{uuid4().hex}",
        host_name=host_name,
        owner_user_id_prefix="0123456789abcdef",
        owner_email=owner_email,
        created_at=created_at,
        workspace_status_before_archive="leased",
        box_generation=1,
        version_probe="minds-v0.3.9",
        baked_version="minds-v0.3.9",
        home_layout="legacy",
        volume_subvolume_path="/mngr-btrfs/0123abcd",
        container_layer_path="/var/lib/docker/overlay2/x/diff",
        bucket="bucket",
        zip_key=f"{prefix}/workspace.zip",
        manifest_key=f"{prefix}/manifest.json",
        zip_size_bytes=4096,
        zip_sha256="ab" * 32,
        excludes=("**/.venv",),
        summary=ArchiveStreamSummary(entry_count=3, entry_bytes=4000, skipped_count=0, skipped_paths=()),
    )


def make_ready_gen1_server() -> BareMetalServer:
    """A ready gen-1 box with a public address, as the cutover and the archive find their origin boxes."""
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId(str(uuid4())),
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.1.2",
        cpu_threads=32,
        ram_gb=128,
        disk_gb=1000,
        memory_per_slice_gb=8,
        cpu_overcommit_ratio=1.5,
        slot_count=15,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
        uplink_mbps=1000,
    )


def make_gen1_pool_row(**overrides: object) -> CutoverPoolRow:
    """A parked-shape gen-1 pool row (stopped, no placement, no artifact) unless overridden."""
    fields: dict[str, object] = dict(
        id=str(uuid4()),
        status="stopped",
        host_id=f"host-{uuid4().hex}",
        agent_id=None,
        host_name="slice-x",
        leased_to_user="0123456789abcdef",
        vps_address=None,
        ssh_port=None,
        container_ssh_port=None,
        bare_metal_server_id=None,
        slice_instance_name="mngr-slice-dev-x-" + "a" * 16,
        slice_disk_name="mngr-slice-dev-x-" + "a" * 16 + "-data",
        outer_host_public_key=None,
        container_host_public_key=None,
        box_generation=1,
        memory_units=8,
        disk_gb=44,
        attributes={},
        artifact_generation=3,
        region=None,
        artifact_manifest=None,
        wrapped_dek=None,
    )
    fields.update(overrides)
    return CutoverPoolRow.model_validate(fields)
