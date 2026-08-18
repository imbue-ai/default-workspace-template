#!/usr/bin/env python3
"""Fully delete Imbue Cloud user accounts and their associated resources.

Operator tool that runs **locally against a tier's live backends** -- no
connector deploy required. It removes, for each account:

- every connector-database row keyed to the account (``account_entitlements``,
  ``workspace_records``, ``account_key_bundles``, ``r2_cleanup_grants``, and the
  ``shares`` / ``relay_tokens`` keyed by the account's 32-hex share label);
- the account's LiteLLM internal user (best-effort -- a missing user is fine);
- the SuperTokens identity itself (``POST {core}/user/remove``), last, so a
  mid-run failure leaves a still-resolvable account that a re-run finishes.

R2 backup buckets are intentionally NOT deleted here: removing an account's
``workspace_records`` orphans its ``host-<hex>`` backup buckets, and the
connector's existing backup-retention reaper empties, deletes, and de-keys
those orphans on its schedule (force it sooner with
``mngr imbue_cloud admin sweep`` if needed). Leaving them to that purpose-built
path keeps this tool from duplicating bounded S3 object deletion.

The tool is **dry-run by default**: it prints exactly what it would delete and
changes nothing until ``--execute`` is passed. Credentials are resolved, per
value, from an explicit flag, else the matching environment variable, else the
tier's HCP Vault entries (via the ``vault`` CLI -- run ``vault login`` first).

Usage:
    export VAULT_TOKEN=...              # or a prior `vault login`
    # Dry run (default): show the plan for every account in the CSV.
    uv run python scripts/delete_accounts.py \
        --tier production --accounts-file /path/to/accounts.csv
    # Actually delete:
    uv run python scripts/delete_accounts.py \
        --tier production --accounts-file /path/to/accounts.csv --execute

The accounts file is a CSV with a header row containing at least a ``user_id``
column (an ``email`` column, when present, is used only for reporting). The
``invalid_accounts.csv`` shape (``email,user_id,...``) is accepted directly.
"""

import csv
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from typing import Final

import click
import httpx
import psycopg2
from loguru import logger

# Vault addr / namespace default to the imbue HCP cluster so the tool works in a
# shell that only ran `vault login` (no VAULT_ADDR export). An operator override
# via the environment still wins.
_DEFAULT_VAULT_ADDR: Final[str] = "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
_DEFAULT_VAULT_NAMESPACE: Final[str] = "admin"
_VAULT_TIMEOUT_SECONDS: Final[int] = 30
_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

# SuperTokens core deletion is a stable CDI call; pin a version the core
# advertises so a newer default can never change the request shape under us.
_SUPERTOKENS_CDI_VERSION: Final[str] = "5.1"

# Connector tables keyed by the FULL SuperTokens user id (the hyphenated UUID).
_TABLES_KEYED_BY_USER_ID: Final[tuple[str, ...]] = (
    "account_entitlements",
    "workspace_records",
    "account_key_bundles",
    "r2_cleanup_grants",
)
# Connector tables keyed by the 32-hex share label (hyphens stripped). Delete
# the child (relay_tokens) before the parent (shares): relay_tokens has an
# ON DELETE CASCADE FK onto shares, so deleting shares first would cascade the
# tokens away and make the explicit relay_tokens delete report a count of 0.
_TABLES_KEYED_BY_SHARE_LABEL: Final[tuple[str, ...]] = (
    "relay_tokens",
    "shares",
)


class AccountDeletionError(RuntimeError):
    """Raised when an account could not be fully deleted."""


def _share_label_for_user_id(user_id: str) -> str:
    """The 32-hex share/relay label for a SuperTokens user id (hyphens stripped)."""
    return user_id.replace("-", "").lower()


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
        raise AccountDeletionError(
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


def _resolve_optional_secret(explicit: str | None, env_var: str, tier: str, service: str, key: str) -> str | None:
    """Resolve an optional secret; return None (rather than raising) when it is not configured anywhere.

    Used for the LiteLLM proxy URL + master key: LiteLLM user cleanup is
    best-effort, so a tier that does not surface these to the operator simply
    skips it rather than failing the whole run.
    """
    try:
        return _resolve_secret(explicit, env_var, tier, service, key)
    except AccountDeletionError as exc:
        logger.trace("Optional secret {} unavailable ({}); skipping.", env_var, exc)
        return None


class DeletionCredentials:
    """The live-backend credentials the deletion cascade needs for one tier."""

    def __init__(
        self,
        database_url: str,
        supertokens_uri: str,
        supertokens_api_key: str,
        litellm_url: str | None,
        litellm_key: str | None,
    ) -> None:
        self.database_url = database_url
        self.supertokens_uri = supertokens_uri.rstrip("/")
        self.supertokens_api_key = supertokens_api_key
        self.litellm_url = litellm_url.rstrip("/") if litellm_url else None
        self.litellm_key = litellm_key


def _load_accounts(accounts_file: Path) -> list[tuple[str, str]]:
    """Return ``(email, user_id)`` pairs from the CSV (email blank when absent)."""
    with accounts_file.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "user_id" not in reader.fieldnames:
            raise AccountDeletionError(
                f"{accounts_file} must have a header row with a 'user_id' column; got {reader.fieldnames}"
            )
        accounts: list[tuple[str, str]] = []
        for row in reader:
            user_id = (row.get("user_id") or "").strip()
            if not user_id:
                continue
            accounts.append(((row.get("email") or "").strip(), user_id))
    return accounts


def _existing_target_tables(connection: Any) -> set[str]:
    """Return which of the tool's target tables actually exist in this database.

    Tier databases differ (e.g. a host_pool DB that predates the sharing tables),
    so the cascade only deletes from tables that are present rather than aborting
    the whole run on the first missing one.
    """
    candidates = (*_TABLES_KEYED_BY_USER_ID, *_TABLES_KEYED_BY_SHARE_LABEL)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(candidates),),
        )
        return {row[0] for row in cursor.fetchall()}


def _delete_db_rows_for_user(connection: Any, user_id: str, existing_tables: set[str]) -> dict[str, int]:
    """Delete every connector-DB row keyed to ``user_id`` in tables that exist; return per-table counts."""
    share_label = _share_label_for_user_id(user_id)
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in _TABLES_KEYED_BY_USER_ID:
            if table not in existing_tables:
                continue
            cursor.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
            counts[table] = cursor.rowcount
        for table in _TABLES_KEYED_BY_SHARE_LABEL:
            if table not in existing_tables:
                continue
            cursor.execute(f"DELETE FROM {table} WHERE user_id = %s", (share_label,))
            counts[table] = cursor.rowcount
    return counts


def _count_leased_hosts_for_user(connection: Any, user_id: str) -> int:
    """Return how many pool hosts still name the account (a released host leaves no row).

    The guard deliberately matches on ``leased_to_user`` alone, with no status
    filter: a full release DELETEs the pool_hosts row and an available row
    carries ``leased_to_user = NULL``, so any surviving row naming the user means
    the release has not completed, in ANY lifecycle status. An earlier
    ``status IN ('leased', 'removing')`` filter silently under-matched the
    ``stopping``/``starting``/``stopped``/``crashed`` states added later, which
    would let a delete strand a row (and a live or restorable VM).
    """
    prefix = user_id.replace("-", "")[:16]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM pool_hosts WHERE leased_to_user = %s",
            (prefix,),
        )
        return int(cursor.fetchone()[0])


def _delete_litellm_user(credentials: DeletionCredentials, user_id: str) -> bool:
    """Best-effort LiteLLM internal-user deletion. Returns False (logged) on any failure."""
    if not credentials.litellm_url or not credentials.litellm_key:
        return False
    try:
        response = httpx.post(
            f"{credentials.litellm_url}/user/delete",
            headers={"Authorization": f"Bearer {credentials.litellm_key}"},
            json={"user_ids": [user_id]},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("LiteLLM /user/delete for {} failed ({}); leaving it for later cleanup", user_id[:8], exc)
        return False
    return True


def _delete_supertokens_user(credentials: DeletionCredentials, user_id: str) -> None:
    """Delete the SuperTokens identity. Raises AccountDeletionError on a non-OK core response."""
    try:
        response = httpx.post(
            f"{credentials.supertokens_uri}/user/remove",
            headers={"api-key": credentials.supertokens_api_key, "cdi-version": _SUPERTOKENS_CDI_VERSION},
            json={"userId": user_id},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AccountDeletionError(f"SuperTokens /user/remove for {user_id} failed: {exc}") from exc
    try:
        status = response.json().get("status")
    except ValueError as exc:
        raise AccountDeletionError(f"SuperTokens /user/remove for {user_id} returned a non-JSON body: {exc}") from exc
    # "OK" on a real delete; the core also returns OK for an already-absent user.
    if status != "OK":
        raise AccountDeletionError(f"SuperTokens /user/remove for {user_id} returned status {status!r}")


def _delete_one_account(
    connection: Any,
    credentials: DeletionCredentials,
    email: str,
    user_id: str,
    existing_tables: set[str],
    is_execute: bool,
) -> dict[str, Any]:
    """Run (or, in dry-run, describe) the full deletion cascade for one account."""
    leased = _count_leased_hosts_for_user(connection, user_id)
    if leased > 0:
        return {
            "email": email,
            "user_id": user_id,
            "skipped": True,
            "reason": f"still holds {leased} leased pool host(s); release them first (mngr pool destroy)",
        }
    if not is_execute:
        would_delete_tables = sorted(
            t for t in (*_TABLES_KEYED_BY_USER_ID, *_TABLES_KEYED_BY_SHARE_LABEL) if t in existing_tables
        )
        would_delete_litellm = credentials.litellm_url is not None and credentials.litellm_key is not None
        return {
            "email": email,
            "user_id": user_id,
            "dry_run": True,
            "would_delete_tables": would_delete_tables,
            "would_delete_litellm_user": would_delete_litellm,
            "would_delete_supertokens_user": True,
        }
    # DB rows first, then LiteLLM, then the identity last (so a partial failure
    # is re-runnable and never orphans a running VM or a live identity's data).
    db_counts = _delete_db_rows_for_user(connection, user_id, existing_tables)
    connection.commit()
    litellm_deleted = _delete_litellm_user(credentials, user_id)
    _delete_supertokens_user(credentials, user_id)
    return {
        "email": email,
        "user_id": user_id,
        "db_rows_deleted": db_counts,
        "litellm_user_deleted": litellm_deleted,
        "supertokens_user_deleted": True,
    }


@click.command()
@click.option("--tier", default="production", show_default=True, help="Minds tier whose Vault entries hold the creds.")
@click.option(
    "--accounts-file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="CSV with a header row containing a 'user_id' column (email column optional, used only for reporting).",
)
@click.option(
    "--execute",
    "is_execute",
    is_flag=True,
    default=False,
    help="Actually delete. Omitted: dry-run (prints the plan, changes nothing).",
)
@click.option(
    "--database-url", default=None, help="Override the host_pool DSN (else env NEON_HOST_POOL_DSN, else Vault)."
)
@click.option("--supertokens-uri", default=None, help="Override the SuperTokens core URI (else env, else Vault).")
@click.option(
    "--supertokens-api-key", default=None, help="Override the SuperTokens core API key (else env, else Vault)."
)
@click.option("--litellm-url", default=None, help="Override the LiteLLM proxy URL (else env, else Vault).")
@click.option("--litellm-key", default=None, help="Override the LiteLLM master key (else env, else Vault).")
@click.option(
    "--report-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Write a per-account JSONL audit trail here (in addition to stdout).",
)
def delete_accounts(
    tier: str,
    accounts_file: Path,
    is_execute: bool,
    database_url: str | None,
    supertokens_uri: str | None,
    supertokens_api_key: str | None,
    litellm_url: str | None,
    litellm_key: str | None,
    report_file: Path | None,
) -> None:
    """Fully delete the accounts listed in ACCOUNTS-FILE from TIER's live backends."""
    accounts = _load_accounts(accounts_file)
    credentials = DeletionCredentials(
        database_url=_resolve_secret(database_url, "NEON_HOST_POOL_DSN", tier, "neon", "DATABASE_URL"),
        supertokens_uri=_resolve_secret(
            supertokens_uri, "SUPERTOKENS_CONNECTION_URI", tier, "supertokens", "SUPERTOKENS_CONNECTION_URI"
        ),
        supertokens_api_key=_resolve_secret(
            supertokens_api_key, "SUPERTOKENS_API_KEY", tier, "supertokens", "SUPERTOKENS_API_KEY"
        ),
        litellm_url=_resolve_optional_secret(litellm_url, "LITELLM_PROXY_URL", tier, "litellm", "LITELLM_PROXY_URL"),
        litellm_key=_resolve_optional_secret(litellm_key, "LITELLM_MASTER_KEY", tier, "litellm", "LITELLM_MASTER_KEY"),
    )
    if credentials.litellm_url is None or credentials.litellm_key is None:
        logger.warning(
            "LiteLLM proxy URL/key not configured for tier '{}'; skipping LiteLLM user cleanup "
            "(pass --litellm-url/--litellm-key to enable it).",
            tier,
        )
    mode = "EXECUTE" if is_execute else "DRY-RUN"
    logger.info("{}: deleting {} account(s) from tier '{}'", mode, len(accounts), tier)

    results: list[dict[str, Any]] = []
    deleted = 0
    skipped = 0
    failed = 0
    connection = psycopg2.connect(credentials.database_url)
    try:
        existing_tables = _existing_target_tables(connection)
        missing_tables = (set(_TABLES_KEYED_BY_USER_ID) | set(_TABLES_KEYED_BY_SHARE_LABEL)) - existing_tables
        if missing_tables:
            logger.info("Tables absent from this database (skipped): {}", ", ".join(sorted(missing_tables)))
        for email, user_id in accounts:
            try:
                result = _delete_one_account(connection, credentials, email, user_id, existing_tables, is_execute)
            except (AccountDeletionError, psycopg2.Error) as exc:
                # Roll back this account's aborted transaction and record it, but keep going: a
                # single bad account (an unexpected DB error, an unreachable SuperTokens core)
                # must not abort a bulk delete or lose the audit trail for the accounts already done.
                connection.rollback()
                failed += 1
                result = {"email": email, "user_id": user_id, "error": str(exc)}
                logger.error("FAILED {} ({}): {}", email or "<no-email>", user_id, exc)
            else:
                if result.get("skipped"):
                    skipped += 1
                    logger.warning("SKIPPED {} ({}): {}", email or "<no-email>", user_id, result["reason"])
                elif result.get("dry_run"):
                    logger.info(
                        "WOULD DELETE {} ({}): tables={} litellm_user={} supertokens_user=yes",
                        email or "<no-email>",
                        user_id,
                        result["would_delete_tables"] or "<none>",
                        "yes" if result["would_delete_litellm_user"] else "no",
                    )
                elif is_execute:
                    deleted += 1
                    logger.info("DELETED {} ({}) rows={}", email or "<no-email>", user_id, result["db_rows_deleted"])
            results.append(result)
    finally:
        connection.close()

    if report_file is not None:
        report_file.write_text("\n".join(json.dumps(r) for r in results) + "\n")
    logger.info(
        "{} complete: {} accounts | deleted={} skipped={} failed={}",
        mode,
        len(accounts),
        deleted,
        skipped,
        failed,
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    delete_accounts()
