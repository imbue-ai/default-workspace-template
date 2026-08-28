"""R2 key-metadata and cleanup-grant stores (Neon-backed).

Tracks the *existence* of each bucket-scoped key (access key id, owner,
bucket, scope, alias) so the connector can list + revoke them. The secret
(sha256 of the token value) is never persisted -- only the non-secret access
key id is stored.

A cleanup grant temporarily restores an over-quota account's downgraded
bucket keys to readwrite so client-side restic cleanup (forget + prune,
which needs full write -- prune repacks) can run. The grant settles at an
explicit recheck or, as a fallback, when the sweep finds it expired; a
grant that settles without any usage decrease counts against a rolling
failed-grant budget, so genuine cleanup is unlimited while write-under-
cover-of-cleanup abuse is bounded.
"""

import contextlib
import functools
import logging
from collections.abc import Iterator
from typing import Any
from typing import Final
from typing import Protocol

from fastapi import HTTPException

from imbue.remote_service_connector import db

logger = logging.getLogger(__name__)


_R2_KEY_COLUMNS = (
    "access_key_id, owner_user_id, bucket_name, access, alias, created_at, enforced_access, suspension_access"
)


def _r2_key_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "access_key_id": row[0],
        "owner_user_id": row[1],
        "bucket_name": row[2],
        "access": row[3],
        "alias": row[4],
        "created_at": str(row[5]) if row[5] is not None else "",
        "enforced_access": row[6],
        # What account suspension did to this key ('read' = policy flipped
        # read-only, 'disabled' = token status disabled, None = untouched).
        # Distinct from the quota sweep's enforced_access so the sweep can
        # never "restore" a suspended key.
        "suspension_access": row[7],
    }


class KeyStore(Protocol):
    """Abstraction over the r2_keys table so endpoints are unit-testable."""

    def add_key(
        self, access_key_id: str, owner_user_id: str, bucket_name: str, access: str, alias: str | None
    ) -> None: ...
    def list_keys(self, owner_user_id: str, bucket_name: str | None = None) -> list[dict[str, Any]]: ...
    def list_all_keys(self) -> list[dict[str, Any]]: ...
    def get_key(self, access_key_id: str) -> dict[str, Any] | None: ...
    def delete_key(self, access_key_id: str) -> None: ...
    def delete_keys_for_bucket(self, owner_user_id: str, bucket_name: str) -> list[dict[str, Any]]: ...
    def set_enforced_access(self, access_key_id: str, enforced_access: str | None) -> None: ...
    def set_suspension_access(self, access_key_id: str, suspension_access: str | None) -> None: ...


class PostgresKeyStore:
    """KeyStore backed by the connector's existing Neon DB (same DB as pool_hosts)."""

    def add_key(
        self, access_key_id: str, owner_user_id: str, bucket_name: str, access: str, alias: str | None
    ) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO r2_keys (access_key_id, owner_user_id, bucket_name, access, alias) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (access_key_id, owner_user_id, bucket_name, access, alias),
                    )

    def list_keys(self, owner_user_id: str, bucket_name: str | None = None) -> list[dict[str, Any]]:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                if bucket_name is None:
                    cur.execute(
                        f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys WHERE owner_user_id = %s ORDER BY created_at",
                        (owner_user_id,),
                    )
                else:
                    cur.execute(
                        f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys "
                        "WHERE owner_user_id = %s AND bucket_name = %s ORDER BY created_at",
                        (owner_user_id, bucket_name),
                    )
                rows = cur.fetchall()
        return [_r2_key_row_to_dict(row) for row in rows]

    def list_all_keys(self) -> list[dict[str, Any]]:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys ORDER BY owner_user_id, bucket_name, created_at")
                rows = cur.fetchall()
        return [_r2_key_row_to_dict(row) for row in rows]

    def get_key(self, access_key_id: str) -> dict[str, Any] | None:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys WHERE access_key_id = %s", (access_key_id,))
                row = cur.fetchone()
        return _r2_key_row_to_dict(row) if row is not None else None

    def set_enforced_access(self, access_key_id: str, enforced_access: str | None) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE r2_keys SET enforced_access = %s WHERE access_key_id = %s",
                        (enforced_access, access_key_id),
                    )

    def set_suspension_access(self, access_key_id: str, suspension_access: str | None) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE r2_keys SET suspension_access = %s WHERE access_key_id = %s",
                        (suspension_access, access_key_id),
                    )

    def delete_key(self, access_key_id: str) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM r2_keys WHERE access_key_id = %s", (access_key_id,))

    def delete_keys_for_bucket(self, owner_user_id: str, bucket_name: str) -> list[dict[str, Any]]:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM r2_keys WHERE owner_user_id = %s AND bucket_name = %s RETURNING {_R2_KEY_COLUMNS}",
                        (owner_user_id, bucket_name),
                    )
                    rows = cur.fetchall()
        return [_r2_key_row_to_dict(row) for row in rows]


@functools.cache
def get_key_store() -> KeyStore:
    return PostgresKeyStore()


# How long a cleanup grant stays active before the sweep settles it as the
# fallback (the client's recheck normally settles it much sooner).
R2_CLEANUP_GRANT_EXPIRY_MINUTES: Final = 60
# How many settled-without-decrease grants an account may burn per window.
R2_CLEANUP_GRANT_FAILED_BUDGET: Final = 5
R2_CLEANUP_GRANT_WINDOW_HOURS: Final = 24

_R2_GRANT_COLUMNS = "grant_id, user_id, user_id_prefix, baseline_bytes, granted_at, expires_at, settled_at, settled_bytes, is_decreased"


def _r2_grant_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "grant_id": int(row[0]),
        "user_id": row[1],
        "user_id_prefix": row[2],
        "baseline_bytes": int(row[3]),
        "granted_at": str(row[4]),
        "expires_at": str(row[5]),
        "settled_at": str(row[6]) if row[6] is not None else None,
        "settled_bytes": int(row[7]) if row[7] is not None else None,
        "is_decreased": row[8],
    }


class GrantStore(Protocol):
    """Abstraction over the r2_cleanup_grants table so endpoints are unit-testable."""

    def create_grant(
        self, user_id: str, user_id_prefix: str, baseline_bytes: int, expiry_minutes: int
    ) -> dict[str, Any]: ...
    def get_active_grant(self, user_id: str) -> dict[str, Any] | None: ...
    def list_unsettled_grants(self, user_id: str) -> list[dict[str, Any]]: ...
    def list_expired_unsettled_grants(self) -> list[dict[str, Any]]: ...
    def settle_grant(self, grant_id: int, settled_bytes: int, is_decreased: bool) -> None: ...
    def count_failed_grants_in_window(self, user_id: str, window_hours: int) -> int: ...


class PostgresGrantStore:
    """GrantStore backed by the connector's existing Neon DB (all timestamps are DB NOW())."""

    def create_grant(
        self, user_id: str, user_id_prefix: str, baseline_bytes: int, expiry_minutes: int
    ) -> dict[str, Any]:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO r2_cleanup_grants (user_id, user_id_prefix, baseline_bytes, expires_at) "
                        f"VALUES (%s, %s, %s, NOW() + make_interval(mins => %s)) RETURNING {_R2_GRANT_COLUMNS}",
                        (user_id, user_id_prefix, baseline_bytes, expiry_minutes),
                    )
                    row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="Failed to record the cleanup grant")
        return _r2_grant_row_to_dict(row)

    def get_active_grant(self, user_id: str) -> dict[str, Any] | None:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_R2_GRANT_COLUMNS} FROM r2_cleanup_grants "
                    "WHERE user_id = %s AND settled_at IS NULL AND expires_at > NOW() "
                    "ORDER BY granted_at DESC LIMIT 1",
                    (user_id,),
                )
                row = cur.fetchone()
        return _r2_grant_row_to_dict(row) if row is not None else None

    def list_unsettled_grants(self, user_id: str) -> list[dict[str, Any]]:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_R2_GRANT_COLUMNS} FROM r2_cleanup_grants "
                    "WHERE user_id = %s AND settled_at IS NULL ORDER BY granted_at",
                    (user_id,),
                )
                rows = cur.fetchall()
        return [_r2_grant_row_to_dict(row) for row in rows]

    def list_expired_unsettled_grants(self) -> list[dict[str, Any]]:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_R2_GRANT_COLUMNS} FROM r2_cleanup_grants "
                    "WHERE settled_at IS NULL AND expires_at <= NOW() ORDER BY granted_at",
                )
                rows = cur.fetchall()
        return [_r2_grant_row_to_dict(row) for row in rows]

    def settle_grant(self, grant_id: int, settled_bytes: int, is_decreased: bool) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE r2_cleanup_grants SET settled_at = NOW(), settled_bytes = %s, is_decreased = %s "
                        "WHERE grant_id = %s AND settled_at IS NULL",
                        (settled_bytes, is_decreased, grant_id),
                    )

    def count_failed_grants_in_window(self, user_id: str, window_hours: int) -> int:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM r2_cleanup_grants "
                    "WHERE user_id = %s AND settled_at IS NOT NULL AND is_decreased = FALSE "
                    "AND granted_at > NOW() - make_interval(hours => %s)",
                    (user_id, window_hours),
                )
                row = cur.fetchone()
        return int(row[0]) if row is not None else 0


@functools.cache
def get_grant_store() -> GrantStore:
    return PostgresGrantStore()


@contextlib.contextmanager
def r2_enforcement_lock(owner_user_id: str) -> Iterator[None]:
    """Hold a per-owner advisory lock while flipping bucket-key token policies.

    Serializes the sweep, cleanup grants, and rechecks for one owner so
    overlapping runs cannot interleave Cloudflare policy writes with the
    ``enforced_access`` bookkeeping (same xact-lock pattern as the lease
    path's per-user serialization).
    """
    with db.pooled_db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"r2-enforce:{owner_user_id}",))
            yield
