"""Workspace sync: records + account key bundles.

Per-account workspace records: one row per workspace, keyed by the workspace
id (the workspace's system-services agent id); the machine it runs on
(``host_id``) is a mutable attribute. Rows hold plaintext metadata (name,
color, provider, location, lifecycle state) plus an opaque,
client-side-encrypted secrets blob the server can never read. Writes are
compare-and-swap on a per-row revision counter, addressed by workspace id
(the ``/sync/records/by-workspace/{workspace_id}`` routes); the host-keyed
routes survive as compat shims -- the DELETE resolves the row through its
host_id column, while the PUT checks the path against the body's host_id and
then addresses the row by the body's agent_id. The account key bundle holds the argon2id inputs and the
password-wrapped data-encryption key (also opaque). All endpoints require
user (SuperTokens) auth but are NOT paid-gated -- sync is a free feature.
"""

import base64
import binascii
import functools
import logging
from collections.abc import Set as AbstractSet
from datetime import datetime
from enum import Enum
from typing import Any
from typing import Protocol

import psycopg2
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.entitlements as entitlements_module
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.entitlements import raise_quota_exceeded
from imbue.remote_service_connector.errors import ConnectorError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.r2.naming import bucket_owner_prefix

logger = logging.getLogger(__name__)

router = APIRouter()


# Hard caps on what one sync row may carry. These exist to bound a row's size
# (the server can never read the blobs, so it cannot validate their contents)
# -- not to police the payload's shape. Today's payload uses a small fraction
# of each, and the headroom is deliberate: the secrets blob is an opaque,
# client-versioned envelope, so adding another secret to it later must not
# require a connector deploy to raise a limit.
#
# Client-encrypted secrets blob, decoded bytes. Today: an SSH private key +
# known_hosts + a canonical restic env (a few KiB).
_MAX_ENCRYPTED_SECRETS_BYTES = 2560 * 1024
# Each binary key-bundle field: the password-wrapped DEK (a 32-byte key +
# nonce + tag) and the argon2id salt. Today: under 100 bytes each.
_MAX_KEY_BUNDLE_FIELD_BYTES = 40960
# Each plaintext metadata field (names, ids, device labels).
_MAX_SYNC_TEXT_FIELD_LENGTH = 5120


class WorkspaceRecordState(str, Enum):
    """Lifecycle state of a synced workspace record (lowercase wire/DB values)."""

    ACTIVE = "active"
    DESTROYED = "destroyed"


class WorkspaceRecordModel(BaseModel):
    """Wire form of one synced workspace record (also the PUT body)."""

    host_id: str = Field(min_length=1, max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Host the workspace is on")
    agent_id: str = Field(min_length=1, max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Logical workspace id")
    display_name: str = Field(max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Workspace display name")
    color: str | None = Field(default=None, max_length=64, description="Workspace accent color (#rrggbb)")
    provider_kind: str = Field(
        max_length=_MAX_SYNC_TEXT_FIELD_LENGTH,
        description="mngr provider backend kind; empty when not yet known (create-path seed records)",
    )
    hosting_device_id: str | None = Field(
        default=None,
        max_length=_MAX_SYNC_TEXT_FIELD_LENGTH,
        description="Install that hosts a local workspace (None for cloud rows)",
    )
    device_label: str = Field(
        default="", max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Human-readable device name"
    )
    state: WorkspaceRecordState = Field(description="Lifecycle state; 'destroyed' is a tombstone")
    restored_from_host_id: str | None = Field(
        default=None, max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Lineage link for restored workspaces"
    )
    backup_bucket: str | None = Field(
        default=None,
        max_length=_MAX_SYNC_TEXT_FIELD_LENGTH,
        description=(
            "Full R2 bucket name holding this workspace's backups. Stored and consumed server-side "
            "(the retention reaper prefers it over deriving a name from the host id); omitted from "
            "wire responses until the pre-tolerant strict client fleet is out of the support window."
        ),
    )
    encrypted_secrets: str | None = Field(
        default=None, description="Base64 of the client-encrypted secrets blob (opaque to the server)"
    )
    revision: int = Field(ge=1, description="Per-row monotonic revision; PUT is CAS on this")
    record_format: int = Field(
        default=1,
        ge=1,
        description=(
            "Semantic format of the record (missing = 1, which every pre-format client implicitly "
            "pushes). A push whose value is below the stored row's is rejected with a structured 409 "
            "(code: record_format_too_new) so an old client can never half-rewrite newer semantics."
        ),
    )
    created_at: str = Field(default="", description="Server timestamp (response only)")
    updated_at: str = Field(default="", description="Server timestamp (response only)")
    destroyed_at: str | None = Field(
        default=None,
        description=(
            "Server tombstone stamp (response only): set on the transition to 'destroyed', kept across "
            "destroyed-state updates, cleared on resurrection. Client-sent values are ignored -- the "
            "store derives it from state so the backup reapers age against the server's clock."
        ),
    )


class AccountKeyBundleModel(BaseModel):
    """Wire form of the per-account password-wrapped data key (also the PUT body)."""

    kdf_salt: str = Field(min_length=1, description="Base64 argon2id salt")
    kdf_time_cost: int = Field(gt=0, description="argon2id iteration count")
    kdf_memory_kib: int = Field(gt=0, description="argon2id memory (KiB)")
    kdf_parallelism: int = Field(gt=0, description="argon2id lane count")
    wrapped_dek: str = Field(min_length=1, description="Base64 password-wrapped DEK (opaque to the server)")
    key_epoch: int = Field(ge=1, description="Bumped only on compromise recovery")
    updated_at: str = Field(default="", description="Server timestamp (response only)")


class SyncRevisionConflictError(ConnectorError):
    """CAS failure: the stored revision does not precede the pushed one."""

    def __init__(self, stored_record: dict[str, Any]) -> None:
        super().__init__("workspace record revision conflict")
        self.stored_record = stored_record


class SyncRecordFormatTooNewError(ConnectorError):
    """A push carried a record_format below the stored row's (a client too old for this record)."""

    def __init__(self, stored_record: dict[str, Any]) -> None:
        super().__init__("record_format_too_new")
        self.stored_record = stored_record


class SyncStoreConsistencyError(ConnectorError, RuntimeError):
    """The store violated one of its own invariants (e.g. a write returned no row)."""


_WORKSPACE_RECORD_COLUMNS = (
    "host_id, agent_id, display_name, color, provider_kind, hosting_device_id, device_label, "
    "state, restored_from_host_id, encrypted_secrets, revision, created_at, updated_at, destroyed_at, "
    "record_format, backup_bucket"
)

# Columns a PUT may modify, in a fixed whitelist so the preserve-on-absent
# UPDATE below can never write a column the request did not name. A field
# absent from the push keeps its stored value; an explicitly sent null clears
# it. (agent_id -- the workspace id -- is the row key; host_id is the
# workspace's current machine and IS mutable; revision/updated_at/destroyed_at
# are managed by the store itself.)
UPDATABLE_RECORD_COLUMNS = (
    "host_id",
    "backup_bucket",
    "display_name",
    "color",
    "provider_kind",
    "hosting_device_id",
    "device_label",
    "state",
    "restored_from_host_id",
    "encrypted_secrets",
    "record_format",
)


def _workspace_record_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    encrypted_secrets = row[9]
    return {
        "host_id": row[0],
        "agent_id": row[1],
        "display_name": row[2],
        "color": row[3],
        "provider_kind": row[4],
        "hosting_device_id": row[5],
        "device_label": row[6],
        "state": row[7],
        "restored_from_host_id": row[8],
        "encrypted_secrets": (
            base64.b64encode(bytes(encrypted_secrets)).decode("ascii") if encrypted_secrets is not None else None
        ),
        "revision": row[10],
        "created_at": str(row[11]) if row[11] is not None else "",
        "updated_at": str(row[12]) if row[12] is not None else "",
        "destroyed_at": str(row[13]) if row[13] is not None else None,
        "record_format": row[14],
        "backup_bucket": row[15],
    }


class SyncStore(Protocol):
    """Abstraction over the workspace_records + account_key_bundles tables."""

    def list_records(self, user_id: str) -> list[dict[str, Any]]: ...

    def put_record(self, user_id: str, record: dict[str, Any], sent_fields: AbstractSet[str]) -> dict[str, Any]:
        """Insert or CAS-update one record; ``sent_fields`` names the fields the push actually carried."""
        ...

    def delete_record(self, user_id: str, host_id: str) -> None:
        """Delete the record whose host_id column matches (the host-keyed compat shim)."""
        ...

    def delete_record_by_workspace(self, user_id: str, workspace_id: str) -> None:
        """Delete the record for one workspace id (the row key)."""
        ...

    def scrub_secrets(self, user_id: str) -> int: ...
    def get_bundle(self, user_id: str) -> dict[str, Any] | None: ...
    def put_bundle(self, user_id: str, bundle: dict[str, Any]) -> None: ...

    def put_bundle_if_absent(self, user_id: str, bundle: dict[str, Any]) -> bool:
        """Store the bundle only when none exists yet; False when one already does (atomic)."""
        ...

    def delete_bundle(self, user_id: str) -> None: ...
    def list_destroyed_records_before(self, cutoff: datetime) -> list[dict[str, Any]]:
        """List destroyed records whose destroyed_at is before ``cutoff`` (the reaper's candidates)."""
        ...

    def any_record_references_backup_bucket(
        self, user_id_prefix: str, bucket_name: str, short_name: str, excluding_workspace_id: str | None = None
    ) -> bool:
        """Whether any record of a user with this prefix references the bucket.

        A record references a bucket when its explicit ``backup_bucket`` equals
        the full ``bucket_name``, or its ``host_id`` / ``agent_id`` equals the
        bucket's ``short_name`` (the legacy name-derived association).
        ``excluding_workspace_id`` leaves one record (by its agent id) out of
        the count -- the retention reaper's "does anyone ELSE still reference
        this record's bucket" question.
        """
        ...


class PostgresSyncStore:
    """SyncStore backed by the connector's existing Neon DB (same DB as pool_hosts)."""

    def list_records(self, user_id: str) -> list[dict[str, Any]]:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_WORKSPACE_RECORD_COLUMNS} FROM workspace_records "
                    "WHERE user_id = %s ORDER BY created_at",
                    (user_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_workspace_record_row_to_dict(row) for row in rows]

    def put_record(self, user_id: str, record: dict[str, Any], sent_fields: AbstractSet[str]) -> dict[str, Any]:
        """Insert or CAS-update one record; returns the stored row after the write.

        The update is preserve-on-absent: only the ``UPDATABLE_RECORD_COLUMNS``
        named by ``sent_fields`` are written, so a field this client version
        does not know about keeps its stored value (an explicitly sent null
        clears it). A push whose ``record_format`` is below the stored row's
        raises :class:`SyncRecordFormatTooNewError` before any write. An
        update requires ``record["revision"] == stored revision + 1``;
        otherwise :class:`SyncRevisionConflictError` carries the stored row so
        the client can merge and retry. Rows are addressed by the workspace id
        (``agent_id``, the primary key with the user). Two concurrent *first*
        pushes of the same workspace both pass the FOR UPDATE probe and the
        loser's INSERT hits the primary key; by then the winner's row is
        committed, so one retry reports that race through the regular CAS
        path (409 + stored row).
        """
        try:
            return self._put_record_once(user_id, record, sent_fields)
        except psycopg2.errors.UniqueViolation:
            return self._put_record_once(user_id, record, sent_fields)

    def _put_record_once(self, user_id: str, record: dict[str, Any], sent_fields: AbstractSet[str]) -> dict[str, Any]:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {_WORKSPACE_RECORD_COLUMNS} FROM workspace_records "
                        "WHERE user_id = %s AND agent_id = %s FOR UPDATE",
                        (user_id, record["agent_id"]),
                    )
                    existing = cur.fetchone()
                    encrypted = record["encrypted_secrets"]
                    encrypted_bytes = psycopg2.Binary(encrypted) if encrypted is not None else None
                    try:
                        # The server stamps destroyed_at itself (authoritative
                        # clock): set on the transition into 'destroyed', kept
                        # across destroyed-state updates, cleared on
                        # resurrection to 'active'. Clients never send it.
                        if existing is None:
                            cur.execute(
                                "INSERT INTO workspace_records (user_id, host_id, agent_id, display_name, color, "
                                "provider_kind, hosting_device_id, device_label, state, restored_from_host_id, "
                                "backup_bucket, encrypted_secrets, revision, record_format, destroyed_at) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                                "CASE WHEN %s = 'destroyed' THEN NOW() END) "
                                f"RETURNING {_WORKSPACE_RECORD_COLUMNS}",
                                (
                                    user_id,
                                    record["host_id"],
                                    record["agent_id"],
                                    record["display_name"],
                                    record["color"],
                                    record["provider_kind"],
                                    record["hosting_device_id"],
                                    record["device_label"],
                                    record["state"],
                                    record["restored_from_host_id"],
                                    record.get("backup_bucket"),
                                    encrypted_bytes,
                                    record["revision"],
                                    record["record_format"],
                                    record["state"],
                                ),
                            )
                        else:
                            stored = _workspace_record_row_to_dict(existing)
                            # The write-lock outranks the CAS: a client that
                            # cannot read this record's semantics must never
                            # write it, so it gets the terminal refusal even
                            # when its revision also happens to be stale.
                            if record["record_format"] < stored["record_format"]:
                                raise SyncRecordFormatTooNewError(stored)
                            if record["revision"] != stored["revision"] + 1:
                                raise SyncRevisionConflictError(stored)
                            # Preserve-on-absent: write only the whitelisted
                            # columns this push actually named, so a field a
                            # future client added survives an older client's
                            # pushes instead of being reset to its default.
                            set_clauses = []
                            update_params: list[Any] = []
                            for column in UPDATABLE_RECORD_COLUMNS:
                                if column not in sent_fields:
                                    continue
                                set_clauses.append(f"{column} = %s")
                                update_params.append(
                                    encrypted_bytes if column == "encrypted_secrets" else record.get(column)
                                )
                            set_clauses.append("revision = %s")
                            update_params.append(record["revision"])
                            set_clauses.append("updated_at = NOW()")
                            set_clauses.append(
                                "destroyed_at = CASE WHEN %s = 'destroyed' THEN COALESCE(destroyed_at, NOW()) END"
                            )
                            update_params.append(record["state"])
                            update_params.extend([user_id, record["agent_id"]])
                            cur.execute(
                                f"UPDATE workspace_records SET {', '.join(set_clauses)} "
                                "WHERE user_id = %s AND agent_id = %s "
                                f"RETURNING {_WORKSPACE_RECORD_COLUMNS}",
                                tuple(update_params),
                            )
                        written = cur.fetchone()
                    except psycopg2.errors.UniqueViolation:
                        # A unique violation here is the primary key: a
                        # concurrent insert of the same workspace won the race.
                        # The caller retries once; the retry finds the winner's
                        # committed row and reports through the CAS path.
                        raise
        finally:
            conn.close()
        if written is None:
            # INSERT/UPDATE ... RETURNING on a locked, existing row always
            # yields a row; reaching here means the store broke its own
            # invariant, which must surface as a server error -- not as a 409
            # whose "stored" row would be the pushed record (whose secrets are
            # raw bytes at this point, not wire-shaped base64).
            raise SyncStoreConsistencyError(
                f"workspace record write for workspace {record['agent_id']} returned no row"
            )
        return _workspace_record_row_to_dict(written)

    def delete_record(self, user_id: str, host_id: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM workspace_records WHERE user_id = %s AND host_id = %s",
                        (user_id, host_id),
                    )
        finally:
            conn.close()

    def delete_record_by_workspace(self, user_id: str, workspace_id: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM workspace_records WHERE user_id = %s AND agent_id = %s",
                        (user_id, workspace_id),
                    )
        finally:
            conn.close()

    def scrub_secrets(self, user_id: str) -> int:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE workspace_records SET encrypted_secrets = NULL, updated_at = NOW() "
                        "WHERE user_id = %s AND encrypted_secrets IS NOT NULL",
                        (user_id,),
                    )
                    scrubbed = cur.rowcount
        finally:
            conn.close()
        return scrubbed

    def get_bundle(self, user_id: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT kdf_salt, kdf_time_cost, kdf_memory_kib, kdf_parallelism, wrapped_dek, key_epoch, "
                    "updated_at FROM account_key_bundles WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "kdf_salt": base64.b64encode(bytes(row[0])).decode("ascii"),
            "kdf_time_cost": row[1],
            "kdf_memory_kib": row[2],
            "kdf_parallelism": row[3],
            "wrapped_dek": base64.b64encode(bytes(row[4])).decode("ascii"),
            "key_epoch": row[5],
            "updated_at": str(row[6]) if row[6] is not None else "",
        }

    def put_bundle(self, user_id: str, bundle: dict[str, Any]) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO account_key_bundles (user_id, kdf_salt, kdf_time_cost, kdf_memory_kib, "
                        "kdf_parallelism, wrapped_dek, key_epoch) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (user_id) DO UPDATE SET kdf_salt = EXCLUDED.kdf_salt, "
                        "kdf_time_cost = EXCLUDED.kdf_time_cost, kdf_memory_kib = EXCLUDED.kdf_memory_kib, "
                        "kdf_parallelism = EXCLUDED.kdf_parallelism, wrapped_dek = EXCLUDED.wrapped_dek, "
                        "key_epoch = EXCLUDED.key_epoch, updated_at = NOW()",
                        (
                            user_id,
                            psycopg2.Binary(bundle["kdf_salt"]),
                            bundle["kdf_time_cost"],
                            bundle["kdf_memory_kib"],
                            bundle["kdf_parallelism"],
                            psycopg2.Binary(bundle["wrapped_dek"]),
                            bundle["key_epoch"],
                        ),
                    )
        finally:
            conn.close()

    def put_bundle_if_absent(self, user_id: str, bundle: dict[str, Any]) -> bool:
        # ON CONFLICT DO NOTHING makes the existence check and the insert one
        # atomic statement, so two racing first-time setups cannot both win.
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO account_key_bundles (user_id, kdf_salt, kdf_time_cost, kdf_memory_kib, "
                        "kdf_parallelism, wrapped_dek, key_epoch) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (user_id) DO NOTHING",
                        (
                            user_id,
                            psycopg2.Binary(bundle["kdf_salt"]),
                            bundle["kdf_time_cost"],
                            bundle["kdf_memory_kib"],
                            bundle["kdf_parallelism"],
                            psycopg2.Binary(bundle["wrapped_dek"]),
                            bundle["key_epoch"],
                        ),
                    )
                    return cur.rowcount == 1
        finally:
            conn.close()

    def delete_bundle(self, user_id: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM account_key_bundles WHERE user_id = %s", (user_id,))
        finally:
            conn.close()

    def list_destroyed_records_before(self, cutoff: datetime) -> list[dict[str, Any]]:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, host_id, agent_id, backup_bucket, destroyed_at FROM workspace_records "
                    "WHERE state = 'destroyed' AND destroyed_at IS NOT NULL AND destroyed_at < %s "
                    "ORDER BY destroyed_at",
                    (cutoff,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {
                "user_id": row[0],
                "host_id": row[1],
                "agent_id": row[2],
                "backup_bucket": row[3],
                "destroyed_at": row[4],
            }
            for row in rows
        ]

    def any_record_references_backup_bucket(
        self, user_id_prefix: str, bucket_name: str, short_name: str, excluding_workspace_id: str | None = None
    ) -> bool:
        # The bucket name carries only the 16-hex user-id prefix, so the match
        # re-derives the prefix from user_id exactly as derive_user_id_prefix does.
        query = (
            "SELECT 1 FROM workspace_records "
            "WHERE (backup_bucket = %s OR host_id = %s OR agent_id = %s) "
            "AND SUBSTRING(REPLACE(user_id, '-', ''), 1, 16) = %s"
        )
        params: tuple[Any, ...] = (bucket_name, short_name, short_name, user_id_prefix)
        if excluding_workspace_id is not None:
            query += " AND agent_id <> %s"
            params = params + (excluding_workspace_id,)
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(query + " LIMIT 1", params)
                return cur.fetchone() is not None
        finally:
            conn.close()


class OrphanBucketStore(Protocol):
    """First-seen stamps for workspace-backup buckets no record references (the reaper's orphan clock)."""

    def get_first_seen(self, bucket_name: str) -> datetime | None: ...
    def get_or_record_first_seen(self, bucket_name: str) -> datetime: ...
    def delete_stamp(self, bucket_name: str) -> None: ...


class PostgresOrphanBucketStore:
    """OrphanBucketStore backed by the connector's Neon DB (orphan_backup_buckets table)."""

    def get_first_seen(self, bucket_name: str) -> datetime | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT first_seen_orphaned_at FROM orphan_backup_buckets WHERE bucket_name = %s",
                    (bucket_name,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row is not None else None

    def get_or_record_first_seen(self, bucket_name: str) -> datetime:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO orphan_backup_buckets (bucket_name) VALUES (%s) "
                        "ON CONFLICT (bucket_name) DO UPDATE SET bucket_name = EXCLUDED.bucket_name "
                        "RETURNING first_seen_orphaned_at",
                        (bucket_name,),
                    )
                    row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            raise SyncStoreConsistencyError(f"orphan stamp upsert for {bucket_name} returned no row")
        return row[0]

    def delete_stamp(self, bucket_name: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM orphan_backup_buckets WHERE bucket_name = %s", (bucket_name,))
        finally:
            conn.close()


@functools.cache
def get_sync_store() -> SyncStore:
    return PostgresSyncStore()


@functools.cache
def get_orphan_bucket_store() -> OrphanBucketStore:
    return PostgresOrphanBucketStore()


def _decode_size_capped_base64(field_name: str, encoded: str, max_bytes: int) -> bytes:
    """Decode a base64 request field, 400ing on malformed input or an oversized payload."""
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} is not valid base64") from exc
    if len(decoded) > max_bytes:
        raise HTTPException(status_code=400, detail=f"{field_name} exceeds the {max_bytes}-byte limit")
    return decoded


def _record_wire_response(record: dict[str, Any]) -> dict[str, object]:
    """Serialize one stored record for the wire, omitting a format-1 record_format.

    CLEANUP: serve record_format unconditionally once the pre-tolerant desktop
    fleet (minds <= 0.3.17, whose extra="forbid" SyncWorkspaceRecord rejects
    any new response field) has left the support window per the access log's
    imbue_client field. Format-1 records are exactly the ones those clients
    may still read and push, so the field is omitted where it carries no
    information (absent means 1 to every tolerant client); records with a
    genuinely newer format were never parseable by that fleet anyway.
    """
    dump = WorkspaceRecordModel(**record).model_dump()
    if dump.get("record_format") == 1:
        del dump["record_format"]
    # CLEANUP: serve backup_bucket once the same pre-tolerant strict fleet is
    # out of the support window. Until then the column is server-consumed only
    # (the retention reaper reads it straight from the store): a strict
    # extra="forbid" client that saw the field would drop the whole row from
    # its listing, which its absence-tombstoning would misread as destruction.
    dump.pop("backup_bucket", None)
    return dump


def _sync_caller(request: Request) -> tuple[UserAuth, str]:
    """Authenticate a sync endpoint call; returns (user auth, full user_id)."""
    return accounts_web_module.resolve_web_user_identity(request)


def _sync_caller_user_id(request: Request) -> str:
    """Authenticate a sync endpoint call and return the caller's full user_id."""
    return _sync_caller(request)[1]


@router.get("/sync/records")
def list_workspace_records_endpoint(request: Request) -> dict[str, object]:
    """List all of the caller's workspace records (metadata + opaque secrets)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        records = get_sync_store().list_records(user_id)
        return {"records": [_record_wire_response(record) for record in records]}


def _put_workspace_record(request: Request, body: WorkspaceRecordModel) -> dict[str, object]:
    """Insert or CAS-update one workspace record; 409 (with the stored row) on conflict.

    Rows are addressed by the workspace id (``body.agent_id``). Enforces the
    active-synced-workspaces quota: a push that would create a *new* ACTIVE
    record (a fresh row, or an existing non-active row flipping to active) is
    refused at the cap. Updates to already-active rows and tombstoning are
    always allowed.
    """
    user, user_id = _sync_caller(request)
    if body.backup_bucket is not None and not body.backup_bucket.startswith(bucket_owner_prefix(user.user_id_prefix)):
        # The retention reaper acts on this name, so a record may only ever
        # point it at a bucket in the caller's own namespace.
        raise HTTPException(status_code=400, detail="backup_bucket must be one of the caller's own buckets")
    if body.state == WorkspaceRecordState.ACTIVE:
        existing_records = get_sync_store().list_records(user_id)
        existing_row = next((r for r in existing_records if r["agent_id"] == body.agent_id), None)
        is_new_active = existing_row is None or existing_row["state"] != WorkspaceRecordState.ACTIVE.value
        if is_new_active:
            # Verified-only email: the backfill's paid-list check is
            # authorized by domain ownership.
            entitlements = entitlements_module.ensure_account_entitlements(
                user_id=user_id, user_id_prefix=user.user_id_prefix, email=user.verified_email or ""
            )
            active_count = sum(1 for r in existing_records if r["state"] == WorkspaceRecordState.ACTIVE.value)
            if active_count >= entitlements.max_active_synced_workspaces:
                raise_quota_exceeded(
                    "max_active_synced_workspaces",
                    entitlements.max_active_synced_workspaces,
                    active_count,
                    "active synced workspaces",
                )
    record = body.model_dump(mode="json")
    record["encrypted_secrets"] = (
        _decode_size_capped_base64("encrypted_secrets", body.encrypted_secrets, _MAX_ENCRYPTED_SECRETS_BYTES)
        if body.encrypted_secrets is not None
        else None
    )
    try:
        stored = get_sync_store().put_record(user_id, record, body.model_fields_set)
    except SyncRecordFormatTooNewError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "record_format_too_new",
                "message": (
                    "This record was written by a newer client "
                    f"(record_format {exc.stored_record.get('record_format')}); update the app to modify it."
                ),
                "stored": _record_wire_response(exc.stored_record),
            },
        ) from exc
    except SyncRevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "revision conflict",
                "stored": _record_wire_response(exc.stored_record),
            },
        ) from exc
    return _record_wire_response(stored)


@router.put("/sync/records/by-workspace/{workspace_id}")
def put_workspace_record_by_workspace_endpoint(
    request: Request, workspace_id: str, body: WorkspaceRecordModel
) -> dict[str, object]:
    """Insert or CAS-update one workspace record, addressed by its workspace id."""
    with handle_endpoint_errors():
        if body.agent_id != workspace_id:
            raise HTTPException(status_code=400, detail="workspace id in the path and body must match")
        return _put_workspace_record(request, body)


# CLEANUP: retire the host-keyed PUT/DELETE routes below once no in-window
# client release still calls them (clients newer than the workspace-keyed
# routes use /sync/records/by-workspace/...).
@router.put("/sync/records/{host_id}")
def put_workspace_record_endpoint(request: Request, host_id: str, body: WorkspaceRecordModel) -> dict[str, object]:
    """Insert or CAS-update one workspace record, addressed by its current host (compat shim)."""
    with handle_endpoint_errors():
        if body.host_id != host_id:
            raise HTTPException(status_code=400, detail="host_id in the path and body must match")
        return _put_workspace_record(request, body)


@router.delete("/sync/records/by-workspace/{workspace_id}")
def delete_workspace_record_by_workspace_endpoint(request: Request, workspace_id: str) -> dict[str, str]:
    """Remove one workspace record outright by workspace id (disassociation; idempotent)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        get_sync_store().delete_record_by_workspace(user_id, workspace_id)
        return {"status": "deleted"}


@router.delete("/sync/records/{host_id}")
def delete_workspace_record_endpoint(request: Request, host_id: str) -> dict[str, str]:
    """Remove one workspace record by its current host (compat shim; idempotent)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        get_sync_store().delete_record(user_id, host_id)
        return {"status": "deleted"}


@router.post("/sync/scrub-secrets")
def scrub_sync_secrets_endpoint(request: Request) -> dict[str, object]:
    """Strip encrypted_secrets from all the caller's records (the clear-password flow)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        return {"scrubbed": get_sync_store().scrub_secrets(user_id)}


@router.get("/sync/bundle")
def get_key_bundle_endpoint(request: Request) -> dict[str, object]:
    """Fetch the caller's password-wrapped key bundle (404 when none is stored)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        bundle = get_sync_store().get_bundle(user_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="No key bundle stored for this account")
        return AccountKeyBundleModel(**bundle).model_dump()


@router.put("/sync/bundle")
def put_key_bundle_endpoint(
    request: Request,
    body: AccountKeyBundleModel,
    # First-time setup passes if_absent=true so two clients racing to mint the
    # account's first DEK cannot silently clobber each other: exactly one
    # wins; the loser gets a 409 and unlocks with the winner's password
    # instead of holding a DEK the stored bundle can never recover.
    if_absent: bool = False,
) -> dict[str, str]:
    """Store the caller's password-wrapped key bundle (replace, or create-only with ``if_absent``)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        bundle = body.model_dump()
        bundle["kdf_salt"] = _decode_size_capped_base64("kdf_salt", body.kdf_salt, _MAX_KEY_BUNDLE_FIELD_BYTES)
        bundle["wrapped_dek"] = _decode_size_capped_base64(
            "wrapped_dek", body.wrapped_dek, _MAX_KEY_BUNDLE_FIELD_BYTES
        )
        if if_absent:
            if not get_sync_store().put_bundle_if_absent(user_id, bundle):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "bundle_exists",
                        "message": "A key bundle is already stored for this account.",
                    },
                )
        else:
            get_sync_store().put_bundle(user_id, bundle)
        return {"status": "ok"}


@router.delete("/sync/bundle")
def delete_key_bundle_endpoint(request: Request) -> dict[str, str]:
    """Delete the caller's key bundle (idempotent; part of the clear-password flow)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        get_sync_store().delete_bundle(user_id)
        return {"status": "deleted"}
