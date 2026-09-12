"""Shared non-fixture test utilities for the slices package."""

import json
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_admin.slices.cutover_types import CutoverStage
from imbue.minds_admin.slices.cutover_types import CutoverWorkspaceState
from imbue.minds_admin.slices.cutover_types import HarvestedKeys
from imbue.minds_admin.slices.cutover_types import SavedProductArtifact
from imbue.minds_admin.slices.operator_identity import ManagementIdentityResolver


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
