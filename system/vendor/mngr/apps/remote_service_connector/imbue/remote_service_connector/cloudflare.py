"""Cloudflare API client: pure request helpers plus the ops Protocol used by the app.

The ``cf_*`` functions are thin, stateless wrappers over the Cloudflare v4
API. ``CloudflareOps`` is the seam the rest of the app depends on;
``HttpCloudflareOps`` is its real implementation (tests provide fakes).
"""

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
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import R2BucketNotEmptyError
from imbue.remote_service_connector.errors import R2BucketNotFoundError
from imbue.remote_service_connector.errors import R2StorageResultTruncatedError

logger = logging.getLogger(__name__)

_CF_BASE_URL = "https://api.cloudflare.com/client/v4"
KV_NAMESPACE_TITLE = "cloudflare-forwarding-defaults"


def cf_check(response: httpx.Response) -> dict[str, Any]:
    data: dict[str, Any] = response.json()
    if not data.get("success", False):
        raise CloudflareApiError(
            status_code=response.status_code,
            errors=data.get("errors", [{"message": "Unknown error"}]),
        )
    return data


def cf_list_all_pages(client: httpx.Client, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    page = 1
    while True:
        paginated = {**params, "page": str(page), "per_page": "100"}
        response = client.get(url, params=paginated)
        data = cf_check(response)
        results: list[dict[str, Any]] = data["result"]
        all_results.extend(results)
        total_count = data.get("result_info", {}).get("total_count", len(results))
        if len(all_results) >= total_count:
            break
        page += 1
    return all_results


# --- Tunnel operations ---


# Env var the deployed connector reads at startup to identify which
# minds env it belongs to. The value is pushed by ``minds env deploy``
# into the per-tier ``litellm-connector-<tier>`` Modal Secret. For
# dev-tier deploys this is the per-developer dev env name (e.g.
# ``josh-3``); for tier deploys it's the tier itself (``staging`` /
# ``production``). Used to tag every Cloudflare tunnel the connector
# creates so the destroy-side can enumerate + delete only the tunnels
# belonging to a specific minds env -- without it, deleting tunnels
# would have to walk every tunnel on the dev-tier CF account
# (potentially clobbering other devs' tunnels).
MINDS_ENV_NAME_VAR = "MINDS_ENV_NAME"


def current_minds_env_name() -> str:
    """Return the value of ``MINDS_ENV_NAME`` or empty string.

    Empty when the deploy didn't push one (e.g. a pre-this-branch
    deploy). Callers must treat the empty case as "no env tag" -- the
    tunnel will still be creatable, just without env-aware destroy
    cleanup metadata.
    """
    return os.environ.get(MINDS_ENV_NAME_VAR, "")


def cf_create_tunnel(client: httpx.Client, account_id: str, name: str) -> dict[str, Any]:
    """Create a Cloudflare tunnel + tag it with the minds env name in metadata.

    The ``metadata`` field on ``cfd_tunnel`` POST accepts arbitrary
    string-keyed values; we shove ``{"env": "<minds-env-name>"}`` in so
    ``minds env destroy`` can later filter the tier's tunnels by env.
    Empty env_name still creates the tunnel (back-compat with older
    connector deploys); destroy then filters by exact match, so empty
    means "doesn't match any env" -- the operator can clean those up
    manually.
    """
    body: dict[str, Any] = {"name": name, "config_src": "cloudflare"}
    env_name = current_minds_env_name()
    if env_name:
        body["metadata"] = {"env": env_name}
    response = client.post(f"/accounts/{account_id}/cfd_tunnel", json=body)
    return cf_check(response)["result"]


def cf_list_tunnels(client: httpx.Client, account_id: str, include_prefix: str = "") -> list[dict[str, Any]]:
    params: dict[str, str] = {"is_deleted": "false"}
    if include_prefix:
        params["include_prefix"] = include_prefix
    return cf_list_all_pages(client, f"/accounts/{account_id}/cfd_tunnel", params)


def cf_get_tunnel_by_name(client: httpx.Client, account_id: str, name: str) -> dict[str, Any] | None:
    params: dict[str, str] = {"is_deleted": "false", "name": name}
    response = client.get(f"/accounts/{account_id}/cfd_tunnel", params=params)
    for tunnel in cf_check(response)["result"]:
        if tunnel["name"] == name:
            return tunnel
    return None


def cf_get_tunnel_by_id(client: httpx.Client, account_id: str, tunnel_id: str) -> dict[str, Any] | None:
    response = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}")
    try:
        data = cf_check(response)
        return data["result"]
    except CloudflareApiError as exc:
        if exc.status_code == 404:
            return None
        raise


def cf_get_tunnel_token(client: httpx.Client, account_id: str, tunnel_id: str) -> str:
    response = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
    return cf_check(response)["result"]


def cf_delete_tunnel(client: httpx.Client, account_id: str, tunnel_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}"))


def cf_get_tunnel_config(client: httpx.Client, account_id: str, tunnel_id: str) -> dict[str, Any]:
    response = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")
    return cf_check(response)["result"]


def cf_put_tunnel_config(client: httpx.Client, account_id: str, tunnel_id: str, config: dict[str, Any]) -> None:
    cf_check(client.put(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", json=config))


# --- DNS operations ---


def cf_create_cname(client: httpx.Client, zone_id: str, name: str, target: str) -> dict[str, Any]:
    response = client.post(
        f"/zones/{zone_id}/dns_records",
        json={"type": "CNAME", "name": name, "content": target, "proxied": True, "ttl": 1},
    )
    return cf_check(response)["result"]


def cf_list_dns_records(client: httpx.Client, zone_id: str, name: str = "") -> list[dict[str, Any]]:
    params: dict[str, str] = {"type": "CNAME"}
    if name:
        params["name"] = name
    return cf_list_all_pages(client, f"/zones/{zone_id}/dns_records", params)


def cf_delete_dns_record(client: httpx.Client, zone_id: str, record_id: str) -> None:
    cf_check(client.delete(f"/zones/{zone_id}/dns_records/{record_id}"))


# --- Access operations ---


def _is_transient_cloudflare_access_error(exc: BaseException) -> bool:
    """Whether a Cloudflare Access failure is worth retrying after a short wait.

    Cloudflare's Access control plane is eventually consistent around
    application deletion: recreating (or mutating) an app for a hostname whose
    previous app was deleted seconds earlier intermittently makes the API
    itself fail with its generic ``access.api.error.internal_server_error``
    (code 10001). Those 5xx responses are transient -- the same call succeeds
    once the teardown settles -- so the Access operations retry them.
    """
    return isinstance(exc, CloudflareApiError) and exc.status_code >= 500


_retry_transient_access_errors = retry(
    retry=retry_if_exception(_is_transient_cloudflare_access_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)


@_retry_transient_access_errors
def cf_create_access_app(
    client: httpx.Client,
    account_id: str,
    hostname: str,
    app_name: str,
    allowed_idps: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": app_name,
        "domain": hostname,
        "type": "self_hosted",
        "session_duration": "24h",
    }
    if allowed_idps is not None:
        body["allowed_idps"] = allowed_idps
    response = client.post(
        f"/accounts/{account_id}/access/apps",
        json=body,
    )
    return cf_check(response)["result"]


@_retry_transient_access_errors
def cf_delete_access_app(client: httpx.Client, account_id: str, app_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/access/apps/{app_id}"))


@_retry_transient_access_errors
def cf_get_access_app_by_domain(client: httpx.Client, account_id: str, hostname: str) -> dict[str, Any] | None:
    response = client.get(f"/accounts/{account_id}/access/apps")
    data = cf_check(response)
    for app_item in data["result"]:
        if app_item.get("domain") == hostname:
            return app_item
    return None


@_retry_transient_access_errors
def cf_list_access_policies(client: httpx.Client, account_id: str, app_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/access/apps/{app_id}/policies")
    return cf_check(response)["result"]


@_retry_transient_access_errors
def cf_create_access_policy(
    client: httpx.Client, account_id: str, app_id: str, policy: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/access/apps/{app_id}/policies", json=policy)
    return cf_check(response)["result"]


@_retry_transient_access_errors
def cf_update_access_policy(
    client: httpx.Client, account_id: str, app_id: str, policy_id: str, policy: dict[str, Any]
) -> dict[str, Any]:
    response = client.put(f"/accounts/{account_id}/access/apps/{app_id}/policies/{policy_id}", json=policy)
    return cf_check(response)["result"]


@_retry_transient_access_errors
def cf_delete_access_policy(client: httpx.Client, account_id: str, app_id: str, policy_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/access/apps/{app_id}/policies/{policy_id}"))


# --- Service token operations ---


def cf_create_service_token(
    client: httpx.Client, account_id: str, name: str, duration: str = "8760h"
) -> dict[str, Any]:
    response = client.post(
        f"/accounts/{account_id}/access/service_tokens",
        json={"name": name, "duration": duration},
    )
    return cf_check(response)["result"]


def cf_list_service_tokens(client: httpx.Client, account_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/access/service_tokens")
    return cf_check(response)["result"]


def cf_delete_service_token(client: httpx.Client, account_id: str, token_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/access/service_tokens/{token_id}"))


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
        if exc.status_code == 404:
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
        if exc.status_code == 404:
            raise R2BucketNotFoundError(bucket_name) from exc
        raise
    return [str(obj["key"]) for obj in result if isinstance(obj, dict) and "key" in obj]


def cf_delete_bucket_object(client: httpx.Client, account_id: str, bucket_name: str, key: str) -> None:
    """Delete one object from a bucket (missing objects are treated as already gone)."""
    response = client.delete(f"/accounts/{account_id}/r2/buckets/{bucket_name}/objects/{quote(key, safe='')}")
    try:
        cf_check(response)
    except CloudflareApiError as exc:
        if exc.status_code == 404:
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


# --- Workers KV operations ---


def cf_kv_list_namespaces(client: httpx.Client, account_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/storage/kv/namespaces")
    return cf_check(response)["result"]


def cf_kv_create_namespace(client: httpx.Client, account_id: str, title: str) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/storage/kv/namespaces", json={"title": title})
    return cf_check(response)["result"]


def cf_kv_get(client: httpx.Client, account_id: str, namespace_id: str, key: str) -> str | None:
    response = client.get(f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def cf_kv_put(client: httpx.Client, account_id: str, namespace_id: str, key: str, value: str) -> None:
    response = client.put(
        f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}",
        content=value,
        headers={"Content-Type": "text/plain"},
    )
    cf_check(response)


def cf_kv_delete(client: httpx.Client, account_id: str, namespace_id: str, key: str) -> None:
    response = client.delete(f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}")
    cf_check(response)


def cf_kv_ensure_namespace(client: httpx.Client, account_id: str, title: str) -> str:
    """Find or create a KV namespace by title. Returns the namespace ID."""
    namespaces = cf_kv_list_namespaces(client, account_id)
    for ns in namespaces:
        if ns["title"] == title:
            return ns["id"]
    result = cf_kv_create_namespace(client, account_id, title)
    return result["id"]


# ---------------------------------------------------------------------------


class CloudflareOps(Protocol):
    """Abstraction over Cloudflare API calls used by ForwardingCtx."""

    def create_tunnel(self, name: str) -> dict[str, Any]: ...
    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]: ...
    def get_tunnel_by_name(self, name: str) -> dict[str, Any] | None: ...
    def get_tunnel_by_id(self, tunnel_id: str) -> dict[str, Any] | None: ...
    def get_tunnel_token(self, tunnel_id: str) -> str: ...
    def delete_tunnel(self, tunnel_id: str) -> None: ...
    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]: ...
    def put_tunnel_config(self, tunnel_id: str, config: dict[str, Any]) -> None: ...
    def create_cname(self, name: str, target: str) -> dict[str, Any]: ...
    def list_dns_records(self, name: str = "") -> list[dict[str, Any]]: ...
    def delete_dns_record(self, record_id: str) -> None: ...
    def create_access_app(
        self, hostname: str, app_name: str, allowed_idps: list[str] | None = None
    ) -> dict[str, Any]: ...
    def delete_access_app(self, app_id: str) -> None: ...
    def get_access_app_by_domain(self, hostname: str) -> dict[str, Any] | None: ...
    def list_access_policies(self, app_id: str) -> list[dict[str, Any]]: ...
    def create_access_policy(self, app_id: str, policy: dict[str, Any]) -> dict[str, Any]: ...
    def update_access_policy(self, app_id: str, policy_id: str, policy: dict[str, Any]) -> dict[str, Any]: ...
    def delete_access_policy(self, app_id: str, policy_id: str) -> None: ...
    def kv_get(self, key: str) -> str | None: ...
    def kv_put(self, key: str, value: str) -> None: ...
    def kv_delete(self, key: str) -> None: ...
    def create_service_token(self, name: str) -> dict[str, Any]: ...
    def list_service_tokens(self) -> list[dict[str, Any]]: ...
    def delete_service_token(self, token_id: str) -> None: ...

    # R2 bucket + bucket-scoped-token operations. These are folded into the
    # CloudflareOps surface (rather than a parallel R2Ops abstraction) because
    # they are just more Cloudflare REST calls sharing the same authenticated
    # client + account_id; the genuinely-different concern (the key-metadata DB)
    # lives behind the separate KeyStore abstraction below.
    account_id: str

    def create_bucket(self, name: str) -> dict[str, Any]: ...
    def list_buckets(self, name_contains: str = "") -> list[dict[str, Any]]: ...
    def delete_bucket(self, name: str) -> None: ...
    def list_bucket_object_keys(self, bucket_name: str, limit: int) -> list[str]: ...
    def delete_bucket_object(self, bucket_name: str, key: str) -> None: ...
    def create_bucket_token(self, bucket_name: str, access: str, token_name: str) -> dict[str, Any]: ...
    def delete_bucket_token(self, token_id: str) -> None: ...
    def update_bucket_token_access(self, token_id: str, bucket_name: str, access: str, token_name: str) -> None: ...
    def roll_bucket_token_value(self, token_id: str) -> dict[str, Any]: ...
    def get_bucket_usage_bytes(self, bucket_name: str) -> int: ...
    def query_r2_storage_by_bucket(self) -> dict[str, int]: ...


class HttpCloudflareOps:
    """CloudflareOps implementation backed by real Cloudflare HTTP API calls."""

    def __init__(self, api_token: str, account_id: str, zone_id: str) -> None:
        self.client = httpx.Client(
            base_url=_CF_BASE_URL,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30.0,
        )
        self.account_id = account_id
        self.zone_id = zone_id
        self._kv_namespace_id: str | None = None
        # Per-container cache of R2 permission-group UUIDs, looked up lazily.
        # Looked up at runtime (not hard-coded) because the connector runs
        # against different Cloudflare accounts across deploy environments.
        self._r2_permission_group_id_by_access: dict[str, str] = {}

    def _ensure_kv_namespace(self) -> str:
        if self._kv_namespace_id is None:
            self._kv_namespace_id = cf_kv_ensure_namespace(self.client, self.account_id, KV_NAMESPACE_TITLE)
        return self._kv_namespace_id

    def create_tunnel(self, name: str) -> dict[str, Any]:
        return cf_create_tunnel(self.client, self.account_id, name)

    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]:
        return cf_list_tunnels(self.client, self.account_id, include_prefix=include_prefix)

    def get_tunnel_by_name(self, name: str) -> dict[str, Any] | None:
        return cf_get_tunnel_by_name(self.client, self.account_id, name)

    def get_tunnel_by_id(self, tunnel_id: str) -> dict[str, Any] | None:
        return cf_get_tunnel_by_id(self.client, self.account_id, tunnel_id)

    def get_tunnel_token(self, tunnel_id: str) -> str:
        return cf_get_tunnel_token(self.client, self.account_id, tunnel_id)

    def delete_tunnel(self, tunnel_id: str) -> None:
        cf_delete_tunnel(self.client, self.account_id, tunnel_id)

    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]:
        return cf_get_tunnel_config(self.client, self.account_id, tunnel_id)

    def put_tunnel_config(self, tunnel_id: str, config: dict[str, Any]) -> None:
        cf_put_tunnel_config(self.client, self.account_id, tunnel_id, config)

    def create_cname(self, name: str, target: str) -> dict[str, Any]:
        return cf_create_cname(self.client, self.zone_id, name, target)

    def list_dns_records(self, name: str = "") -> list[dict[str, Any]]:
        return cf_list_dns_records(self.client, self.zone_id, name=name)

    def delete_dns_record(self, record_id: str) -> None:
        cf_delete_dns_record(self.client, self.zone_id, record_id)

    def create_access_app(self, hostname: str, app_name: str, allowed_idps: list[str] | None = None) -> dict[str, Any]:
        return cf_create_access_app(self.client, self.account_id, hostname, app_name, allowed_idps=allowed_idps)

    def delete_access_app(self, app_id: str) -> None:
        cf_delete_access_app(self.client, self.account_id, app_id)

    def get_access_app_by_domain(self, hostname: str) -> dict[str, Any] | None:
        return cf_get_access_app_by_domain(self.client, self.account_id, hostname)

    def list_access_policies(self, app_id: str) -> list[dict[str, Any]]:
        return cf_list_access_policies(self.client, self.account_id, app_id)

    def create_access_policy(self, app_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        return cf_create_access_policy(self.client, self.account_id, app_id, policy)

    def update_access_policy(self, app_id: str, policy_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        return cf_update_access_policy(self.client, self.account_id, app_id, policy_id, policy)

    def delete_access_policy(self, app_id: str, policy_id: str) -> None:
        cf_delete_access_policy(self.client, self.account_id, app_id, policy_id)

    def kv_get(self, key: str) -> str | None:
        ns_id = self._ensure_kv_namespace()
        return cf_kv_get(self.client, self.account_id, ns_id, key)

    def kv_put(self, key: str, value: str) -> None:
        ns_id = self._ensure_kv_namespace()
        cf_kv_put(self.client, self.account_id, ns_id, key, value)

    def kv_delete(self, key: str) -> None:
        ns_id = self._ensure_kv_namespace()
        cf_kv_delete(self.client, self.account_id, ns_id, key)

    def create_service_token(self, name: str) -> dict[str, Any]:
        return cf_create_service_token(self.client, self.account_id, name)

    def list_service_tokens(self) -> list[dict[str, Any]]:
        return cf_list_service_tokens(self.client, self.account_id)

    def delete_service_token(self, token_id: str) -> None:
        cf_delete_service_token(self.client, self.account_id, token_id)

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

    def roll_bucket_token_value(self, token_id: str) -> dict[str, Any]:
        return {"value": cf_roll_account_token_value(self.client, self.account_id, token_id)}

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        usage = cf_get_bucket_usage(self.client, self.account_id, bucket_name)
        return int(usage.get("payloadSize") or 0) + int(usage.get("metadataSize") or 0)

    def query_r2_storage_by_bucket(self) -> dict[str, int]:
        return cf_query_r2_storage_by_bucket(self.client, self.account_id)
