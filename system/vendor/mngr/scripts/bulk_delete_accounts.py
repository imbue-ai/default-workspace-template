#!/usr/bin/env python3
"""Bulk-revoke sessions and bulk-delete Imbue Cloud accounts (spam-cleanup scale).

The existing ``delete_accounts.py`` runs one account at a time (per-account DB
queries plus serial HTTP calls), which is right for a handful of accounts and
hopeless for hundreds of thousands. This tool handles the incident-scale case:

- **Connector-DB cleanup is set-based**: the whole accounts file is loaded into
  a temp table over one connection (``COPY``), leases are checked with one
  join, and each connector table is cleaned with a single ``DELETE ... USING``
  statement -- a fixed number of statements regardless of account count.
- **SuperTokens calls are parallelized**: the core's CDI has no bulk endpoint
  for regular users (``bulk-import/users/remove`` only covers its import
  staging area), so per-user ``POST /user/remove`` / ``POST
  /recipe/session/remove`` calls are fanned out across a worker pool with
  retry + exponential backoff on transient failures (429/5xx/transport).
- **Progress is durable and resumable**: every completed user id is appended to
  a progress JSONL; re-running skips them, so an interrupted run continues
  where it stopped instead of re-issuing 200k calls.

Phases (pass ``--phase``):

- ``revoke``: revoke every SuperTokens session for each listed account
  (``POST /recipe/session/remove`` with ``revokeAcrossAllTenants``). Use this
  for a fast lockout of a high-risk subset before the slower deletion pass.
- ``delete``: connector-DB cleanup (set-based, with a hard abort if any listed
  account still holds a pool host), then parallel SuperTokens
  ``POST /user/remove`` (which also removes the user's sessions and all other
  core data).

LiteLLM internal users are deliberately NOT touched here: verify separately
that no listed account has one (``SELECT`` against the LiteLLM DB), and use
``delete_accounts.py`` for the stragglers if any exist. Baking a best-effort
per-user LiteLLM call into this tool would re-introduce the serial-HTTP shape
this exists to avoid.

The tool is **dry-run by default**: it prints the full plan (row counts, call
counts, lease check) and changes nothing until ``--execute`` is passed.
Credentials resolve like ``delete_accounts.py``: explicit flag, else
environment variable, else the tier's HCP Vault entries (via the ``vault``
CLI -- run ``vault login`` or export ``VAULT_TOKEN`` first).

Usage:
    export VAULT_TOKEN=...
    # Dry run (default): show the plan.
    uv run python scripts/bulk_delete_accounts.py \
        --tier production --accounts-file spam.csv --phase delete
    # Actually delete:
    uv run python scripts/bulk_delete_accounts.py \
        --tier production --accounts-file spam.csv --phase delete --execute

The accounts file is a CSV with a header row containing a ``user_id`` column
of hyphenated SuperTokens UUIDs (an ``email`` column, when present, is
tolerated and ignored, so the same CSVs work for ``delete_accounts.py`` too).
"""

import csv
import io
import json
import os
import re
import subprocess
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import TextIO
from typing import assert_never

import click
import httpx
import psycopg2
from loguru import logger
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.enums import UpperCaseStrEnum

# Vault addr / namespace default to the imbue HCP cluster so the tool works in a
# shell that only ran `vault login` (no VAULT_ADDR export). An operator override
# via the environment still wins. (Kept in sync with delete_accounts.py.)
_DEFAULT_VAULT_ADDR: Final[str] = "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
_DEFAULT_VAULT_NAMESPACE: Final[str] = "admin"
_VAULT_TIMEOUT_SECONDS: Final[int] = 30
_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

# SuperTokens CDI version pin, matching delete_accounts.py: both endpoints this
# tool calls are stable CDI calls, and pinning keeps the request shape fixed.
_SUPERTOKENS_CDI_VERSION: Final[str] = "5.1"

# How often (in processed accounts, successes and failures alike) to log
# throughput while a phase runs.
_PROGRESS_LOG_INTERVAL: Final[int] = 2000

# SuperTokens user ids are hyphenated UUIDs. Enforcing the shape up front keeps
# arbitrary CSV bytes out of the COPY text stream and guarantees the share-label
# and lease-prefix derivations are meaningful.
_USER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Connector tables keyed by the FULL SuperTokens user id (the hyphenated UUID).
# Mirrored in delete_accounts.py and pinned to it by a test in
# delete_accounts_test.py: both tools must clear the same connector-DB rows.
# (Outside the connector DB they diverge on purpose -- only delete_accounts.py
# touches LiteLLM users and analytics transcripts.)
_TABLES_KEYED_BY_USER_ID: Final[tuple[str, ...]] = (
    "account_entitlements",
    "workspace_records",
    "account_key_bundles",
    "r2_cleanup_grants",
    "account_attribution",
)
# The pool-host guard deliberately does NOT enumerate "held" statuses. A full
# release DELETEs the pool_hosts row (apps/remote_service_connector/.../hosts.py),
# and an available (never-leased) row carries leased_to_user = NULL, so the only
# way a row still names a user is a release that has not completed -- in ANY
# lifecycle status. Matching on leased_to_user alone is therefore both sufficient
# and fail-safe: a newly-added status can only ever be more-held, and this guard
# already covers it, whereas a status allowlist would silently under-match it
# (exactly the bug that let the old two-status guard miss stopping/starting/
# stopped/crashed). Over-matching a not-really-held row (e.g. if operator tooling
# ever returned a host to the pool without clearing leased_to_user) only refuses a
# delete -- the safe direction for a destructive operation.

# Connector tables keyed by the 32-hex share label (hyphens stripped). Delete
# the child (relay_tokens) before the parent (shares) so the explicit delete
# reports real counts rather than being emptied by the FK cascade first.
_TABLES_KEYED_BY_SHARE_LABEL: Final[tuple[str, ...]] = (
    "relay_tokens",
    "shares",
)


class BulkAccountDeletionError(RuntimeError):
    """Raised when the bulk revoke/delete run cannot proceed."""


class RetryableCoreError(BulkAccountDeletionError):
    """Raised for SuperTokens core responses worth retrying (429 / 5xx)."""


class TakedownPhase(UpperCaseStrEnum):
    """Which bulk operation to run against the listed accounts."""

    REVOKE = auto()
    DELETE = auto()


def _read_vault_value(tier: str, service: str, key: str) -> str:
    """Read a single Vault leaf value (``secrets/minds/<tier>/<service>/<key>``) via the vault CLI."""
    vault_env = dict(os.environ)
    vault_env.setdefault("VAULT_ADDR", _DEFAULT_VAULT_ADDR)
    vault_env.setdefault("VAULT_NAMESPACE", _DEFAULT_VAULT_NAMESPACE)
    path = f"minds/{tier}/{service}/{key}"
    result = subprocess.run(
        ["vault", "kv", "get", "-mount=secrets", "-field=value", path],
        capture_output=True,
        text=True,
        timeout=_VAULT_TIMEOUT_SECONDS,
        env=vault_env,
    )
    if result.returncode != 0:
        raise BulkAccountDeletionError(
            f"Could not read secrets/{path} from Vault (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}. Run `vault login` first."
        )
    return result.stdout.strip()


def _resolve_secret(explicit: str | None, env_var: str, tier: str, service: str, key: str) -> str:
    """Resolve one required secret: explicit flag > environment variable > tier Vault entry."""
    if explicit:
        return explicit
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    return _read_vault_value(tier, service, key)


def _share_label_for_user_id(user_id: str) -> str:
    """The 32-hex share/relay label for a SuperTokens user id (hyphens stripped)."""
    return user_id.replace("-", "").lower()


def _user_id_prefix(user_id: str) -> str:
    """The 16-hex prefix that namespaces a user's leases (matches the connector's convention)."""
    return user_id.replace("-", "")[:16]


def _load_accounts(accounts_file: Path) -> list[str]:
    """Return the canonicalized user ids from the CSV (other columns, e.g. email, are ignored)."""
    with accounts_file.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "user_id" not in reader.fieldnames:
            raise BulkAccountDeletionError(
                f"{accounts_file} must have a header row with a 'user_id' column; got {reader.fieldnames}"
            )
        user_ids: list[str] = []
        for row in reader:
            user_id = (row.get("user_id") or "").strip()
            if not user_id:
                continue
            if _USER_ID_PATTERN.fullmatch(user_id) is None:
                raise BulkAccountDeletionError(
                    f"{accounts_file} contains a user_id that is not a hyphenated UUID: {user_id!r}"
                )
            # Lowercase is the canonical SuperTokens/DB form. Canonicalizing here keeps
            # every derived key meaningful (raw-id joins, the lease prefix, the share
            # label) even if the CSV was round-tripped through a tool that uppercased
            # the ids -- otherwise the lease guard and the deletes would silently miss.
            user_ids.append(user_id.lower())
    duplicate_count = len(user_ids) - len(set(user_ids))
    if duplicate_count > 0:
        raise BulkAccountDeletionError(f"{accounts_file} contains {duplicate_count} duplicate user_id rows")
    return user_ids


def _load_completed_user_ids(progress_file: Path, phase: TakedownPhase, tier: str) -> set[str]:
    """User ids the progress file already records as completed for this phase and tier.

    Tier-scoping matters because the default progress path is derived from the
    accounts file: running the same CSV against a second tier must not skip the
    accounts already completed on the first one.
    """
    if not progress_file.exists():
        return set()
    completed: set[str] = set()
    for line in progress_file.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping unparsable progress line: {}", line[:120])
            continue
        if record.get("phase") == phase.value and record.get("tier") == tier and record.get("ok") is True:
            user_id = record.get("user_id")
            if not isinstance(user_id, str) or not user_id:
                logger.warning("Skipping progress line without a user_id: {}", line[:120])
                continue
            completed.add(user_id)
    return completed


def _existing_target_tables(connection: Any) -> set[str]:
    """Which of the target tables actually exist in this database (tier DBs differ)."""
    candidates = (*_TABLES_KEYED_BY_USER_ID, *_TABLES_KEYED_BY_SHARE_LABEL)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(candidates),),
        )
        return {row[0] for row in cursor.fetchall()}


def _copy_user_ids_into_temp_table(connection: Any, user_ids: Sequence[str]) -> None:
    """Create the ``bulk_takedown_ids`` temp table and COPY every listed user id into it.

    The share label and lease prefix are derived in Python (one place for the
    convention) and stored as columns, so every later statement is a plain join.
    """
    buffer = io.StringIO(
        "".join(
            f"{user_id}\t{_share_label_for_user_id(user_id)}\t{_user_id_prefix(user_id)}\n" for user_id in user_ids
        )
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE bulk_takedown_ids"
            " (user_id text PRIMARY KEY, share_label text NOT NULL, lease_prefix text NOT NULL)"
            " ON COMMIT DROP"
        )
        cursor.copy_expert("COPY bulk_takedown_ids (user_id, share_label, lease_prefix) FROM STDIN", buffer)


def _count_held_pool_hosts_for_listed_accounts(connection: Any) -> int:
    """How many pool hosts still name any listed account (a released host leaves no row)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM pool_hosts p JOIN bulk_takedown_ids t ON p.leased_to_user = t.lease_prefix",
        )
        return int(cursor.fetchone()[0])


def _iter_tables_with_key_column(existing_tables: set[str]) -> Iterator[tuple[str, str]]:
    """Yield ``(table, temp-table key column)`` for each present target table, in deletion order.

    The single source of the table-to-join-key mapping, shared by the dry-run
    counts and the actual deletes so the two can never disagree about which
    rows are targeted.
    """
    for table in _TABLES_KEYED_BY_USER_ID:
        if table in existing_tables:
            yield table, "user_id"
    for table in _TABLES_KEYED_BY_SHARE_LABEL:
        if table in existing_tables:
            yield table, "share_label"


def _count_db_rows_for_listed_accounts(connection: Any, existing_tables: set[str]) -> dict[str, int]:
    """Per-table counts of rows keyed to any listed account (the dry-run view)."""
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table, key_column in _iter_tables_with_key_column(existing_tables):
            cursor.execute(f"SELECT COUNT(*) FROM {table} a JOIN bulk_takedown_ids t ON a.user_id = t.{key_column}")
            counts[table] = int(cursor.fetchone()[0])
    return counts


def _delete_db_rows_for_listed_accounts(connection: Any, existing_tables: set[str]) -> dict[str, int]:
    """Delete every connector-DB row keyed to any listed account; one statement per table."""
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table, key_column in _iter_tables_with_key_column(existing_tables):
            cursor.execute(f"DELETE FROM {table} a USING bulk_takedown_ids t WHERE a.user_id = t.{key_column}")
            counts[table] = cursor.rowcount
    return counts


class SupertokensCoreClientInterface(ABC):
    """The two per-user SuperTokens core calls that ``_PhaseRunner`` fans out."""

    @abstractmethod
    def revoke_all_sessions(self, user_id: str) -> int:
        """Revoke every session of the account; returns how many session handles were revoked."""

    @abstractmethod
    def remove_user(self, user_id: str) -> None:
        """Delete the user and all their core data (sessions included). OK for absent users."""


class SupertokensCoreClient(SupertokensCoreClientInterface):
    """Thread-safe client for the two per-user SuperTokens core calls this tool issues."""

    def __init__(self, core_uri: str, api_key: str, max_connections: int) -> None:
        self._core_uri = core_uri.rstrip("/")
        self._client = httpx.Client(
            headers={"api-key": api_key, "cdi-version": _SUPERTOKENS_CDI_VERSION},
            timeout=_HTTP_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((RetryableCoreError, httpx.TransportError)),
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=30),
        reraise=True,
    )
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to the core, retrying transport errors and 429/5xx responses."""
        response = self._client.post(f"{self._core_uri}{path}", json=body)
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableCoreError(f"Core answered {response.status_code} for {path}")
        if response.status_code != 200:
            raise BulkAccountDeletionError(f"Core answered {response.status_code} for {path}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise BulkAccountDeletionError(f"Core answered a non-JSON body for {path}: {exc}") from exc

    # the number of session handles revoked
    def revoke_all_sessions(self, user_id: str) -> int:
        result = self._post(
            "/recipe/session/remove",
            {"userId": user_id, "revokeAcrossAllTenants": True, "revokeSessionsForLinkedAccounts": True},
        )
        if result.get("status") != "OK":
            raise BulkAccountDeletionError(f"session/remove for {user_id} returned status {result.get('status')!r}")
        return len(result.get("sessionHandlesRevoked", []))

    def remove_user(self, user_id: str) -> None:
        """Delete the user and all their core data (sessions included). OK for absent users."""
        result = self._post("/user/remove", {"userId": user_id})
        if result.get("status") != "OK":
            raise BulkAccountDeletionError(f"user/remove for {user_id} returned status {result.get('status')!r}")


class _PhaseRunner:
    """Fans one per-user core call out across a worker pool with durable progress."""

    def __init__(
        self,
        core_client: SupertokensCoreClientInterface,
        phase: TakedownPhase,
        tier: str,
        progress_file: Path,
        worker_count: int,
    ) -> None:
        self._core_client = core_client
        self._phase = phase
        self._tier = tier
        self._progress_file = progress_file
        self._worker_count = worker_count
        self._lock = threading.Lock()
        self._completed_count = 0
        self._failed_count = 0
        self._revoked_session_count = 0
        self._started_at_monotonic = time.monotonic()

    def _run_one(self, user_id: str) -> None:
        match self._phase:
            case TakedownPhase.REVOKE:
                revoked = self._core_client.revoke_all_sessions(user_id)
            case TakedownPhase.DELETE:
                self._core_client.remove_user(user_id)
                revoked = 0
            case _ as unreachable:
                assert_never(unreachable)
        with self._lock:
            self._revoked_session_count += revoked

    def _record(self, user_id: str, is_ok: bool, error: str | None, progress_handle: TextIO) -> None:
        record: dict[str, Any] = {"user_id": user_id, "phase": self._phase.value, "tier": self._tier, "ok": is_ok}
        if error is not None:
            record["error"] = error
        with self._lock:
            progress_handle.write(json.dumps(record) + "\n")
            progress_handle.flush()
            if is_ok:
                self._completed_count += 1
            else:
                self._failed_count += 1
            done = self._completed_count + self._failed_count
            if done % _PROGRESS_LOG_INTERVAL == 0:
                elapsed = time.monotonic() - self._started_at_monotonic
                rate = done / elapsed if elapsed > 0 else 0.0
                logger.info(
                    "{}: {} done ({} failed) at {:.0f}/s",
                    self._phase.value,
                    done,
                    self._failed_count,
                    rate,
                )

    # (completed_count, failed_count, revoked_session_count)
    def run(self, user_ids: Sequence[str]) -> tuple[int, int, int]:
        with self._progress_file.open("a") as progress_handle:
            with ThreadPoolExecutor(max_workers=self._worker_count) as executor:

                def _task(user_id: str) -> None:
                    try:
                        self._run_one(user_id)
                    except (BulkAccountDeletionError, httpx.HTTPError) as exc:
                        logger.warning("{} failed for {}: {}", self._phase.value, user_id, exc)
                        self._record(user_id, False, str(exc), progress_handle)
                    else:
                        self._record(user_id, True, None, progress_handle)

                list(executor.map(_task, user_ids))
        return (self._completed_count, self._failed_count, self._revoked_session_count)


def _run_connector_db_cleanup(database_url: str, user_ids: Sequence[str], is_execute: bool) -> None:
    """Set-based held-host check + row cleanup over one connection/transaction.

    Raises when any pool_hosts row still names a listed account (any lifecycle
    status -- a released host leaves no row) -- release those first
    (``mngr pool destroy``); deleting the connector rows out from under a held
    host would orphan a live or restorable VM.
    """
    connection = psycopg2.connect(database_url)
    try:
        _copy_user_ids_into_temp_table(connection, user_ids)
        held_count = _count_held_pool_hosts_for_listed_accounts(connection)
        if held_count > 0:
            raise BulkAccountDeletionError(f"{held_count} pool host(s) still name listed accounts; release them first")
        logger.info("Pool-host hold check passed: no pool_hosts row still names a listed account")
        existing_tables = _existing_target_tables(connection)
        missing_tables = (set(_TABLES_KEYED_BY_USER_ID) | set(_TABLES_KEYED_BY_SHARE_LABEL)) - existing_tables
        if missing_tables:
            logger.info("Tables absent from this database (skipped): {}", ", ".join(sorted(missing_tables)))
        if is_execute:
            deleted_counts = _delete_db_rows_for_listed_accounts(connection, existing_tables)
            connection.commit()
            logger.info("Deleted connector-DB rows: {}", deleted_counts)
        else:
            row_counts = _count_db_rows_for_listed_accounts(connection, existing_tables)
            connection.rollback()
            logger.info("DRY-RUN: would delete connector-DB rows: {}", row_counts)
    finally:
        connection.close()


@click.command()
@click.option("--tier", default="production", show_default=True, help="Minds tier whose Vault entries hold the creds.")
@click.option(
    "--accounts-file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="CSV with a header row containing a 'user_id' column (email column optional and ignored).",
)
@click.option(
    "--phase",
    "phase_name",
    type=click.Choice([p.value.lower() for p in TakedownPhase]),
    required=True,
    help="revoke: revoke all sessions for each account. delete: connector-DB cleanup + SuperTokens user removal.",
)
@click.option(
    "--execute",
    "is_execute",
    is_flag=True,
    default=False,
    help="Actually run. Omitted: dry-run (prints the plan, changes nothing).",
)
@click.option(
    "--workers",
    default=24,
    show_default=True,
    type=click.IntRange(min=1),
    help="Concurrent SuperTokens core calls.",
)
@click.option(
    "--progress-file",
    type=click.Path(path_type=Path),
    default=None,
    help="JSONL progress log used to resume an interrupted run (default: <accounts-file>.progress.jsonl).",
)
@click.option(
    "--database-url", default=None, help="Override the host_pool DSN (else env NEON_HOST_POOL_DSN, else Vault)."
)
@click.option("--supertokens-uri", default=None, help="Override the SuperTokens core URI (else env, else Vault).")
@click.option(
    "--supertokens-api-key", default=None, help="Override the SuperTokens core API key (else env, else Vault)."
)
def bulk_delete_accounts(
    tier: str,
    accounts_file: Path,
    phase_name: str,
    is_execute: bool,
    workers: int,
    progress_file: Path | None,
    database_url: str | None,
    supertokens_uri: str | None,
    supertokens_api_key: str | None,
) -> None:
    """Run one bulk phase (revoke / delete) over every account in ACCOUNTS-FILE against TIER."""
    phase = TakedownPhase(phase_name.upper())
    listed_user_ids = _load_accounts(accounts_file)
    resolved_progress_file = (
        progress_file
        if progress_file is not None
        else accounts_file.with_suffix(accounts_file.suffix + ".progress.jsonl")
    )
    already_completed = _load_completed_user_ids(resolved_progress_file, phase, tier)
    remaining_user_ids = [user_id for user_id in listed_user_ids if user_id not in already_completed]

    mode = "EXECUTE" if is_execute else "DRY-RUN"
    logger.info(
        "{} phase={} tier={}: {} accounts listed, {} already completed, {} remaining (workers={})",
        mode,
        phase.value,
        tier,
        len(listed_user_ids),
        len(already_completed),
        len(remaining_user_ids),
        workers,
    )

    # The delete phase cleans the connector DB first (set-based), so a partial
    # SuperTokens pass never leaves connector rows pointing at removed users.
    if phase == TakedownPhase.DELETE:
        resolved_database_url = _resolve_secret(database_url, "NEON_HOST_POOL_DSN", tier, "neon", "DATABASE_URL")
        _run_connector_db_cleanup(resolved_database_url, listed_user_ids, is_execute)

    if not is_execute:
        logger.info(
            "DRY-RUN complete: would issue {} SuperTokens {} call(s). Re-run with --execute to proceed.",
            len(remaining_user_ids),
            "session/remove" if phase == TakedownPhase.REVOKE else "user/remove",
        )
        return

    core_client = SupertokensCoreClient(
        core_uri=_resolve_secret(
            supertokens_uri, "SUPERTOKENS_CONNECTION_URI", tier, "supertokens", "SUPERTOKENS_CONNECTION_URI"
        ),
        api_key=_resolve_secret(
            supertokens_api_key, "SUPERTOKENS_API_KEY", tier, "supertokens", "SUPERTOKENS_API_KEY"
        ),
        max_connections=workers,
    )
    try:
        runner = _PhaseRunner(
            core_client=core_client,
            phase=phase,
            tier=tier,
            progress_file=resolved_progress_file,
            worker_count=workers,
        )
        completed_count, failed_count, revoked_session_count = runner.run(remaining_user_ids)
    finally:
        core_client.close()

    if phase == TakedownPhase.REVOKE:
        logger.info(
            "revoke complete: {} accounts done, {} failed, {} sessions revoked",
            completed_count,
            failed_count,
            revoked_session_count,
        )
    else:
        logger.info("delete complete: {} accounts removed, {} failed", completed_count, failed_count)
    if failed_count > 0:
        raise BulkAccountDeletionError(
            f"{failed_count} account(s) failed; re-run the same command to retry just those "
            f"(progress file: {resolved_progress_file})"
        )


if __name__ == "__main__":
    bulk_delete_accounts()
