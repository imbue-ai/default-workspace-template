"""Frozen snapshot of the minds 0.4.0 client's tolerant connector-response models.

This file pins what the 0.4.0 desktop client can parse: the first release
whose wire models are tolerant ``WireModel``s (``extra="ignore"``, enums
coerced to ``UNKNOWN``), so additive server changes can no longer break it.
What CAN still break it -- and what this snapshot therefore pins -- is the
required-field surface: removing, renaming, or re-typing a required response
field fails these models exactly as it would fail the shipped client. The
golden compat test (``wire_compat_test.py``) validates the connector's live
responses against every snapshot in this package, so such a change fails CI
before it can deploy.

Snapshot rules (each file here follows them):

- Self-contained: no imports from ``imbue.*`` -- the point is that later
  refactors of the live code can never silently loosen what a snapshot
  asserts. Only pydantic + stdlib.
- Never edited to match a server change. A snapshot changes only when it is
  PRUNED (its release left the support window -- see ``SUPPORT_ENDS``) or a
  new release's snapshot is added beside it.
- ``MODEL_BY_ENDPOINT`` maps each endpoint whose response this client parses
  STRICTLY (``validate_wire`` / ``parse_wire_entries`` on a wire model) to
  the model it used. Endpoints the client parsed tolerantly (hand-rolled
  ``.get()`` readers, raw dicts, status-only enum coercions) cannot be
  broken by response-shape changes and are deliberately absent -- notably
  the workspace stop/start transitions, whose 0.4.0 reader coerces even a
  missing ``status`` to UNKNOWN.
"""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import AnyUrl
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

SNAPSHOT_NAME = "minds 0.4.0 (first tolerant WireModel release)"

# When the covered client version was released.
RELEASE_DATE = date(2026, 8, 18)

# The date this snapshot stops being enforced: prune the file then (or extend
# this date deliberately). Set to the release date + the ~1 month support
# window; later releases that share this strictly-parsed surface extend this
# date instead of adding a new snapshot (see docs:
# apps/minds/docs/deploy/release.md).
# Also covers minds 0.4.1 (released 2026-08-18): its strictly-parsed connector
# surface is identical to 0.4.0's (no wire_types or connector response changes
# between the two tags), and its support window ends the same date.
# Also covers minds 0.4.2 (released 2026-08-24): the strictly-parsed surface is
# still identical -- the only wire_types changes between the two tags are
# docstrings, field descriptions, and the additive operator-only
# AdminAccountInfo, which no strictly-parsed endpoint maps to.
# Also covers minds 0.4.3 (released 2026-08-29): the strictly-parsed surface is
# still identical -- the only wire_types changes between the two tags are field
# descriptions and the additive optional SyncWorkspaceRecord.backup_bucket
# field (default None), which adds no required field; no new strict-parse call
# sites were added client-side.
# Also covers minds 0.4.4 (released 2026-08-31): the strictly-parsed surface is
# still identical -- the only wire_types change between the two tags is the
# additive optional ShareInfo.chrome_origin field (default None), which adds no
# required field; no new strict-parse call sites were added client-side.
SUPPORT_ENDS = date(2026, 10, 1)


class _TolerantModel(BaseModel):
    """The 0.4.0 WireModel semantics, inlined: frozen, unknown fields ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class _WorkspaceStatus(str, Enum):
    """The 0.4.0 tolerant lifecycle enum: unrecognized wire values coerce to UNKNOWN."""

    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STARTING = "starting"
    CRASHED = "crashed"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "_WorkspaceStatus":
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value == normalized:
                    return member
        return cls.UNKNOWN


class _R2BucketAccess(str, Enum):
    """The 0.4.0 tolerant access scope: unrecognized wire values coerce to UNKNOWN."""

    READ = "read"
    READWRITE = "readwrite"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "_R2BucketAccess":
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value == normalized:
                    return member
        return cls.UNKNOWN


class AuthRawResponse(_TolerantModel):
    """Parsed from /auth/signin, /auth/signup, and /auth/device/token."""

    status: str
    message: str | None = None
    user: dict[str, Any] | None = None
    tokens: dict[str, Any] | None = None
    needs_email_verification: bool = False


class LeaseResult(_TolerantModel):
    """Parsed from POST /hosts/lease."""

    host_db_id: str = Field(min_length=1)
    vps_address: str
    ssh_port: int
    ssh_user: str
    container_ssh_port: int
    agent_id: str
    host_id: str
    host_name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    outer_host_public_key: str | None = None
    container_host_public_key: str | None = None


class WorkspaceInfo(_TolerantModel):
    """Parsed from GET /workspaces (per entry) and GET /workspaces/{id}."""

    host_db_id: str = Field(min_length=1)
    status: _WorkspaceStatus
    vps_address: str | None = None
    ssh_port: int | None = None
    ssh_user: str = "root"
    container_ssh_port: int | None = None
    agent_id: str
    host_id: str
    host_name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    leased_at: str = ""
    stop_requested_at: str | None = None
    stopped_at: str | None = None
    transition_error: str | None = None
    outer_host_public_key: str | None = None
    container_host_public_key: str | None = None


class LeasedHostInfo(_TolerantModel):
    """Parsed from GET /hosts (per entry)."""

    host_db_id: str = Field(min_length=1)
    vps_address: str
    ssh_port: int
    ssh_user: str
    container_ssh_port: int
    agent_id: str
    host_id: str
    host_name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    leased_at: str
    outer_host_public_key: str | None = None
    container_host_public_key: str | None = None


class LiteLLMKeyMaterial(_TolerantModel):
    """Parsed from POST /keys/create."""

    key: SecretStr
    base_url: AnyUrl


class LiteLLMKeyInfo(_TolerantModel):
    """Parsed from GET /keys (per entry) and GET /keys/{id}."""

    token: str
    key_alias: str | None = None
    key_name: str | None = None
    spend: Decimal = Decimal("0")
    max_budget: Decimal | None = None
    budget_duration: str | None = None
    user_id: str | None = None


class R2BucketInfo(_TolerantModel):
    """Parsed from GET /buckets (per entry), GET /buckets/{name}, and inside bucket-create."""

    bucket_name: str
    s3_endpoint: AnyUrl


class R2KeyMaterial(_TolerantModel):
    """Parsed from POST /buckets/{name}/roll-key and inside bucket-create."""

    access_key_id: str = Field(min_length=1)
    secret_access_key: SecretStr
    s3_endpoint: AnyUrl
    bucket_name: str
    access: _R2BucketAccess


class R2KeyInfo(_TolerantModel):
    """Parsed from GET /buckets/{name}/keys and GET /bucket-keys (per entry)."""

    access_key_id: str = Field(min_length=1)
    bucket_name: str
    access: _R2BucketAccess
    alias: str | None = None
    created_at: str
    enforced_access: str | None = None


class R2BucketCreateResult(_TolerantModel):
    """Parsed from POST /buckets."""

    bucket: R2BucketInfo
    key: R2KeyMaterial


class StorageCleanupGrant(_TolerantModel):
    """Parsed from POST /account/storage-cleanup-grant."""

    status: str
    expires_at: str | None = None
    baseline_bytes: int | None = None
    keys: tuple[R2KeyInfo, ...] = ()


class StorageRecheckResult(_TolerantModel):
    """Parsed from POST /account/storage-recheck."""

    usage_bytes: int
    limit_bytes: int
    is_over_quota: bool
    is_grant_settled: bool
    keys: tuple[R2KeyInfo, ...] = ()


class AccountEntitlementValues(_TolerantModel):
    """Nested in GET /account. The tunnel-era fields are gone from the 0.4.0 client
    (a tolerant model simply ignores them if served)."""

    max_remote_workspaces: int
    max_total_workspaces: int
    max_buckets: int
    max_total_bucket_bytes: int
    monthly_llm_spend_usd: float
    max_active_synced_workspaces: int


class AccountUsageInfo(_TolerantModel):
    """Nested in GET /account."""

    remote_workspaces: int
    total_workspaces: int
    buckets: int
    total_bucket_bytes: int
    llm_spend_usd_this_period: float
    llm_budget_resets_at: str | None = None
    active_synced_workspaces: int


class AccountInfo(_TolerantModel):
    """Parsed from GET /account."""

    user_id: str = Field(min_length=1)
    email: str
    plan_name: str
    entitlements: AccountEntitlementValues
    usage: AccountUsageInfo
    available_plans: tuple[str, ...] = ()


class SyncWorkspaceRecord(_TolerantModel):
    """Parsed from GET /sync/records (per entry) and the PUT response."""

    host_id: str
    agent_id: str
    display_name: str = ""
    color: str | None = None
    provider_kind: str
    hosting_device_id: str | None = None
    device_label: str = ""
    state: str
    restored_from_host_id: str | None = None
    encrypted_secrets: str | None = None
    revision: int
    record_format: int = 1
    created_at: str = ""
    updated_at: str = ""
    destroyed_at: str | None = None


class SyncKeyBundle(_TolerantModel):
    """Parsed from GET /sync/bundle."""

    kdf_salt: str
    kdf_time_cost: int
    kdf_memory_kib: int
    kdf_parallelism: int
    wrapped_dek: str
    key_epoch: int
    updated_at: str = ""


# Endpoint -> the model this client version validated the response body with.
# LIST_OF_* keys mean the body (or named sub-list) is validated per entry.
# The workspace stop/start transitions are absent: the 0.4.0 client reads only
# `status` from them, tolerantly (missing/unknown coerces to UNKNOWN).
MODEL_BY_ENDPOINT: dict[str, type[BaseModel]] = {
    "POST /auth/signin": AuthRawResponse,
    "POST /auth/signup": AuthRawResponse,
    "POST /auth/device/token": AuthRawResponse,
    "POST /hosts/lease": LeaseResult,
    "GET /hosts [entry]": LeasedHostInfo,
    "GET /workspaces [entry]": WorkspaceInfo,
    "GET /workspaces/{host_db_id}": WorkspaceInfo,
    "POST /keys/create": LiteLLMKeyMaterial,
    "GET /keys [entry]": LiteLLMKeyInfo,
    "GET /keys/{key_id}": LiteLLMKeyInfo,
    "POST /buckets": R2BucketCreateResult,
    "GET /buckets [entry]": R2BucketInfo,
    "GET /buckets/{name}": R2BucketInfo,
    "POST /buckets/{name}/roll-key": R2KeyMaterial,
    "GET /buckets/{name}/keys [entry]": R2KeyInfo,
    "GET /bucket-keys [entry]": R2KeyInfo,
    "GET /account": AccountInfo,
    "POST /account/storage-cleanup-grant": StorageCleanupGrant,
    "POST /account/storage-recheck": StorageRecheckResult,
    "GET /sync/records [entry]": SyncWorkspaceRecord,
    "PUT /sync/records/{host_id}": SyncWorkspaceRecord,
    "GET /sync/bundle": SyncKeyBundle,
}
