"""Cloudflare API client for R2: pure request helpers plus the ops Protocol used by the app.

The ``cf_*`` functions are thin, stateless wrappers over the Cloudflare v4
API (R2 buckets, bucket-scoped account tokens, and the storage-analytics
GraphQL query). ``CloudflareOps`` is the seam the rest of the app depends on;
``HttpCloudflareOps`` is its real implementation (tests provide fakes).
``CloudflareCtx`` / ``get_cloudflare_ctx`` hold the per-container ops
instance shared by the endpoints and crons.
"""

import functools
import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final
from typing import Protocol
from urllib.parse import quote

import httpx

from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import R2BucketNotEmptyError
from imbue.remote_service_connector.errors import R2BucketNotFoundError
from imbue.remote_service_connector.errors import R2StorageResultTruncatedError

logger = logging.getLogger(__name__)

CF_BASE_URL = "https://api.cloudflare.com/client/v4"


def cf_check(response: httpx.Response) -> dict[str, Any]:
    data: dict[str, Any] = response.json()
    if not data.get("success", False):
        raise CloudflareApiError(
            status_code=response.status_code,
            errors=data.get("errors", [{"message": "Unknown error"}]),
        )
    return data


# --- R2 bucket + account-token operations ---


_R2_READ_PERMISSION_GROUP_NAME = "Workers R2 Storage Bucket Item Read"
_R2_WRITE_PERMISSION_GROUP_NAME = "Workers R2 Storage Bucket Item Write"


def _is_bucket_not_empty_error(exc: CloudflareApiError) -> bool:
    """Detect Cloudflare's 'bucket not empty' rejection from a delete error."""
    for err in exc.cf_errors:
        if "not empty" in str(err.get("message", "")).lower():
            return True
        if err.get("code") == 10040:
            return True
    return False


def _is_bucket_not_found_error(exc: CloudflareApiError) -> bool:
    """Detect Cloudflare's 'bucket does not exist' rejection from a bucket-op error.

    R2 reports a missing bucket either as a plain HTTP 404 or as error code
    10007 ("The specified key does not exist.") carried on a non-404 status,
    so a status check alone misses the latter -- which made the backup reap
    sweep hard-fail on a bucket another pass had already deleted.
    """
    if exc.status_code == 404:
        return True
    for err in exc.cf_errors:
        if err.get("code") == 10007:
            return True
    return False


def cf_create_bucket(client: httpx.Client, account_id: str, name: str) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/r2/buckets", json={"name": name})
    return cf_check(response)["result"]


def cf_list_buckets(client: httpx.Client, account_id: str, name_contains: str = "") -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    cursor = ""
    is_more_pages = True
    while is_more_pages:
        params: dict[str, str] = {"per_page": "1000"}
        if name_contains:
            params["name_contains"] = name_contains
        if cursor:
            params["cursor"] = cursor
        response = client.get(f"/accounts/{account_id}/r2/buckets", params=params)
        data = cf_check(response)
        result = data["result"]
        buckets = result.get("buckets", []) if isinstance(result, dict) else result
        all_results.extend(buckets)
        result_info = data.get("result_info")
        cursor = result_info.get("cursor", "") if isinstance(result_info, dict) else ""
        is_more_pages = bool(cursor)
    return all_results


def cf_delete_bucket(client: httpx.Client, account_id: str, name: str) -> None:
    """Delete an R2 bucket. Raises R2BucketNotEmptyError / R2BucketNotFoundError on the matching CF errors."""
    response = client.delete(f"/accounts/{account_id}/r2/buckets/{name}")
    try:
        cf_check(response)
    except CloudflareApiError as exc:
        if _is_bucket_not_found_error(exc):
            raise R2BucketNotFoundError(name) from exc
        if _is_bucket_not_empty_error(exc):
            raise R2BucketNotEmptyError(name) from exc
        raise


def cf_list_bucket_object_keys(client: httpx.Client, account_id: str, bucket_name: str, limit: int) -> list[str]:
    """Return up to ``limit`` object keys from a bucket (one page; the reaper deletes and re-lists)."""
    response = client.get(
        f"/accounts/{account_id}/r2/buckets/{bucket_name}/objects",
        params={"per_page": str(limit)},
    )
    try:
        result = cf_check(response)["result"]
    except CloudflareApiError as exc:
        if _is_bucket_not_found_error(exc):
            raise R2BucketNotFoundError(bucket_name) from exc
        raise
    return [str(obj["key"]) for obj in result if isinstance(obj, dict) and "key" in obj]


def cf_delete_bucket_object(client: httpx.Client, account_id: str, bucket_name: str, key: str) -> None:
    """Delete one object from a bucket (missing objects are treated as already gone)."""
    response = client.delete(f"/accounts/{account_id}/r2/buckets/{bucket_name}/objects/{quote(key, safe='')}")
    try:
        cf_check(response)
    except CloudflareApiError as exc:
        if _is_bucket_not_found_error(exc):
            return
        raise


def cf_list_token_permission_groups(client: httpx.Client, account_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/tokens/permission_groups")
    return cf_check(response)["result"]


def cf_create_account_token(
    client: httpx.Client, account_id: str, name: str, policies: list[dict[str, Any]]
) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/tokens", json={"name": name, "policies": policies})
    return cf_check(response)["result"]


def cf_delete_account_token(client: httpx.Client, account_id: str, token_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/tokens/{token_id}"))


def cf_update_account_token_policies(
    client: httpx.Client, account_id: str, token_id: str, name: str, policies: list[dict[str, Any]]
) -> dict[str, Any]:
    """Replace an account token's policy list in place (the token value is unchanged)."""
    response = client.put(f"/accounts/{account_id}/tokens/{token_id}", json={"name": name, "policies": policies})
    return cf_check(response)["result"]


def cf_set_account_token_status(
    client: httpx.Client,
    account_id: str,
    token_id: str,
    name: str,
    policies: list[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    """Set an account token's status (``active`` / ``disabled``) in place.

    The update PUT requires the full token body (name + policies), so the
    caller supplies the token's current policy list; the token id and secret
    value are unchanged, which is what makes a disable fully reversible on
    the same S3 credentials.
    """
    response = client.put(
        f"/accounts/{account_id}/tokens/{token_id}",
        json={"name": name, "policies": policies, "status": status},
    )
    return cf_check(response)["result"]


def cf_roll_account_token_value(client: httpx.Client, account_id: str, token_id: str) -> str:
    """Regenerate an account token's secret value (same token id, same policies)."""
    response = client.put(f"/accounts/{account_id}/tokens/{token_id}/value", json={})
    return cf_check(response)["result"]


def cf_get_bucket_usage(client: httpx.Client, account_id: str, bucket_name: str) -> dict[str, Any]:
    """Return one bucket's live usage (payloadSize / metadataSize / objectCount / uploadCount)."""
    response = client.get(f"/accounts/{account_id}/r2/buckets/{bucket_name}/usage")
    return cf_check(response)["result"]


# Row budget for the sweep's GraphQL query. The query groups by bucketName
# alone, so one row is one bucket and this budget is effectively a
# bucket-count ceiling (Cloudflare accepts limits up to 10000; past that,
# shard the query into bucketName_in chunks). A response that fills the
# budget may be truncated and raises rather than enforcing from partial data.
_R2_STORAGE_GRAPHQL_ROW_LIMIT: Final = 5000

# GraphQL analytics query used by the storage-quota sweep: one request covers
# every bucket in the account, regardless of bucket count, so the sweep never
# scales its REST-API usage with the number of users. Grouping by bucketName
# only (no datetime dimension) yields exactly one row per bucket: the max
# snapshot inside the lookback window. That is the window *peak*, not the
# latest value -- peak >= live, so it can only delay a restore, never justify
# a downgrade on its own; downgrades are re-confirmed against the real-time
# per-bucket REST endpoint (which also serves the display path).
_R2_STORAGE_GRAPHQL_QUERY = (
    """
query R2StorageByBucket($accountTag: string!, $since: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      r2StorageAdaptiveGroups(
        limit: %d
        filter: {datetime_geq: $since}
      ) {
        max {
          payloadSize
          metadataSize
        }
        dimensions {
          bucketName
        }
      }
    }
  }
}
"""
    % _R2_STORAGE_GRAPHQL_ROW_LIMIT
)

# How far back the sweep's GraphQL query looks for storage snapshots. Only
# needs to contain at least one snapshot per bucket: measured production
# cadence is one snapshot per 10-70 minutes (median 30, newest-snapshot age
# up to ~76 min), so 3 hours holds comfortable margin. A longer window costs
# peak staleness (delayed automatic restores after a cleanup), not rows.
_R2_STORAGE_LOOKBACK_HOURS = 3


def parse_r2_storage_graphql_response(data: dict[str, Any]) -> dict[str, int]:
    """Extract {bucket_name: peak_bytes_in_window} from the r2StorageAdaptiveGroups response.

    One row per bucket (bucketName-only grouping); ``payloadSize`` +
    ``metadataSize`` together are the bucket's stored bytes. A response that
    fills the query's row budget may be truncated -- buckets past the limit
    would silently count as zero usage -- so that case raises
    :class:`R2StorageResultTruncatedError` and fails the sweep loudly instead
    of enforcing from partial data.
    """
    usage_by_bucket: dict[str, int] = {}
    row_count = 0
    accounts = data.get("data", {}).get("viewer", {}).get("accounts", []) if isinstance(data, dict) else []
    for account in accounts:
        for group in account.get("r2StorageAdaptiveGroups", []) or []:
            row_count += 1
            dimensions = group.get("dimensions", {})
            bucket_name = dimensions.get("bucketName")
            if not bucket_name:
                continue
            max_values = group.get("max", {}) or {}
            payload = int(max_values.get("payloadSize") or 0)
            metadata = int(max_values.get("metadataSize") or 0)
            usage_by_bucket[bucket_name] = max(usage_by_bucket.get(bucket_name, 0), payload + metadata)
    if row_count >= _R2_STORAGE_GRAPHQL_ROW_LIMIT:
        raise R2StorageResultTruncatedError(row_count=row_count, row_limit=_R2_STORAGE_GRAPHQL_ROW_LIMIT)
    return usage_by_bucket


def cf_query_r2_storage_by_bucket(client: httpx.Client, account_id: str) -> dict[str, int]:
    """Query the GraphQL analytics dataset for every bucket's peak stored bytes in the lookback window.

    Requires the API token to carry ``Account Analytics: Read``. Raises
    :class:`CloudflareApiError` when the GraphQL layer reports errors and
    :class:`R2StorageResultTruncatedError` when the response fills the row
    budget (possible truncation).
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=_R2_STORAGE_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = client.post(
        "/graphql",
        json={
            "query": _R2_STORAGE_GRAPHQL_QUERY,
            "variables": {"accountTag": account_id, "since": since},
        },
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    errors = data.get("errors")
    if errors:
        raise CloudflareApiError(status_code=response.status_code, errors=list(errors))
    return parse_r2_storage_graphql_response(data)


def build_r2_bucket_token_policies(
    account_id: str, bucket_name: str, permission_group_id: str
) -> list[dict[str, Any]]:
    """Build the account-token policy list scoping a token to one R2 bucket.

    The resource key mirrors Cloudflare's R2 bucket resource identifier. The
    ``default`` segment is the (default) jurisdiction; revisit if non-default
    jurisdictions are ever exposed.
    """
    resource_key = f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket_name}"
    return [
        {
            "effect": "allow",
            "permission_groups": [{"id": permission_group_id}],
            "resources": {resource_key: "*"},
        }
    ]


# ---------------------------------------------------------------------------


class CloudflareOps(Protocol):
    """Abstraction over the Cloudflare API calls used by the R2 bucket endpoints and sweeps.

    R2 bucket + bucket-scoped-token operations share one authenticated client
    + account_id; the genuinely-different concern (the key-metadata DB) lives
    behind the separate KeyStore abstraction.
    """

    account_id: str

    def create_bucket(self, name: str) -> dict[str, Any]: ...
    def list_buckets(self, name_contains: str = "") -> list[dict[str, Any]]: ...
    def delete_bucket(self, name: str) -> None: ...
    def list_bucket_object_keys(self, bucket_name: str, limit: int) -> list[str]: ...
    def delete_bucket_object(self, bucket_name: str, key: str) -> None: ...
    def create_bucket_token(self, bucket_name: str, access: str, token_name: str) -> dict[str, Any]: ...
    def delete_bucket_token(self, token_id: str) -> None: ...
    def update_bucket_token_access(self, token_id: str, bucket_name: str, access: str, token_name: str) -> None: ...
    def set_bucket_token_status(
        self, token_id: str, bucket_name: str, access: str, token_name: str, status: str
    ) -> None: ...
    def roll_bucket_token_value(self, token_id: str) -> dict[str, Any]: ...
    def get_bucket_usage_bytes(self, bucket_name: str) -> int: ...
    def query_r2_storage_by_bucket(self) -> dict[str, int]: ...


class HttpCloudflareOps:
    """CloudflareOps implementation backed by real Cloudflare HTTP API calls."""

    def __init__(self, api_token: str, account_id: str) -> None:
        self.client = httpx.Client(
            base_url=CF_BASE_URL,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30.0,
        )
        self.account_id = account_id
        # Per-container cache of R2 permission-group UUIDs, looked up lazily.
        # Looked up at runtime (not hard-coded) because the connector runs
        # against different Cloudflare accounts across deploy environments.
        self._r2_permission_group_id_by_access: dict[str, str] = {}

    def _r2_permission_group_id(self, access: str) -> str:
        if access not in self._r2_permission_group_id_by_access:
            wanted = _R2_WRITE_PERMISSION_GROUP_NAME if access == "readwrite" else _R2_READ_PERMISSION_GROUP_NAME
            groups = cf_list_token_permission_groups(self.client, self.account_id)
            for group in groups:
                if group.get("name") == wanted:
                    self._r2_permission_group_id_by_access[access] = group["id"]
                    break
            else:
                raise CloudflareApiError(500, [{"message": f"R2 permission group not found: {wanted}"}])
        return self._r2_permission_group_id_by_access[access]

    def create_bucket(self, name: str) -> dict[str, Any]:
        return cf_create_bucket(self.client, self.account_id, name)

    def list_buckets(self, name_contains: str = "") -> list[dict[str, Any]]:
        return cf_list_buckets(self.client, self.account_id, name_contains=name_contains)

    def delete_bucket(self, name: str) -> None:
        cf_delete_bucket(self.client, self.account_id, name)

    def list_bucket_object_keys(self, bucket_name: str, limit: int) -> list[str]:
        return cf_list_bucket_object_keys(self.client, self.account_id, bucket_name, limit)

    def delete_bucket_object(self, bucket_name: str, key: str) -> None:
        cf_delete_bucket_object(self.client, self.account_id, bucket_name, key)

    def create_bucket_token(self, bucket_name: str, access: str, token_name: str) -> dict[str, Any]:
        policies = build_r2_bucket_token_policies(self.account_id, bucket_name, self._r2_permission_group_id(access))
        return cf_create_account_token(self.client, self.account_id, token_name, policies)

    def delete_bucket_token(self, token_id: str) -> None:
        cf_delete_account_token(self.client, self.account_id, token_id)

    def update_bucket_token_access(self, token_id: str, bucket_name: str, access: str, token_name: str) -> None:
        policies = build_r2_bucket_token_policies(self.account_id, bucket_name, self._r2_permission_group_id(access))
        cf_update_account_token_policies(self.client, self.account_id, token_id, token_name, policies)

    def set_bucket_token_status(
        self, token_id: str, bucket_name: str, access: str, token_name: str, status: str
    ) -> None:
        # ``access`` names the token's current effective scope so the PUT's
        # required policy list re-asserts it unchanged.
        policies = build_r2_bucket_token_policies(self.account_id, bucket_name, self._r2_permission_group_id(access))
        cf_set_account_token_status(self.client, self.account_id, token_id, token_name, policies, status)

    def roll_bucket_token_value(self, token_id: str) -> dict[str, Any]:
        return {"value": cf_roll_account_token_value(self.client, self.account_id, token_id)}

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        usage = cf_get_bucket_usage(self.client, self.account_id, bucket_name)
        return int(usage.get("payloadSize") or 0) + int(usage.get("metadataSize") or 0)

    def query_r2_storage_by_bucket(self) -> dict[str, int]:
        return cf_query_r2_storage_by_bucket(self.client, self.account_id)


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------


class CloudflareCtx:
    """Thin holder of the Cloudflare ops abstraction. Created once per container."""

    def __init__(self, ops: CloudflareOps) -> None:
        self.ops = ops


@functools.cache
def get_cloudflare_ctx() -> CloudflareCtx:
    ops = HttpCloudflareOps(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
    )
    return CloudflareCtx(ops=ops)
