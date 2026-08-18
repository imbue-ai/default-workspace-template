"""Frozen snapshot of the pre-tolerant minds clients' strict connector-response models.

This file pins what the PRE-TOLERANT shipped desktop clients can parse
(minds 0.3.16 and the identically-shaped 0.3.17, the last releases before
the tolerant WireModel parsing shipped): their wire models were
``extra="forbid"`` with strict enums, so ANY change to these response
shapes -- field added, field removed, enum value added -- breaks every such
install in the field. The golden compat test
(``wire_compat_test.py``) validates the connector's live responses against
every snapshot in this package, so a breaking change fails CI before it can
deploy.

Snapshot rules (each file here follows them):

- Self-contained: no imports from ``imbue.*`` -- the point is that later
  refactors of the live code can never silently loosen what a snapshot
  asserts. Only pydantic + stdlib.
- Never edited to match a server change. A snapshot changes only when it is
  PRUNED (its release left the support window -- see ``SUPPORT_ENDS``) or a
  new release's snapshot is added beside it.
- ``MODEL_BY_ENDPOINT`` maps each endpoint whose response this client parses
  STRICTLY to the model it used. Endpoints the client parsed tolerantly
  (hand-rolled ``.get()`` readers, raw dicts) cannot be broken by additive
  changes and are deliberately absent.

CLEANUP: delete this file (un-freezing the response shapes it pins) once the
access log's imbue_client field shows no in-window pre-tolerant clients
(minds 0.3.17 or older) after the forced desktop update -- tracked by ``SUPPORT_ENDS`` below, which
the compat test enforces with a prune-or-extend failure.
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

SNAPSHOT_NAME = "pre-tolerant minds releases (0.3.16 through 0.3.17, strict models)"

# When the covered client versions were current (the snapshot's provenance;
# the release date of the last covered release, 0.3.17).
RELEASE_DATE = date(2026, 8, 17)

# The date this snapshot stops being enforced: prune the file then (or extend
# this date deliberately, if the fleet still shows in-window pre-tolerant
# (0.3.17-or-older) clients).
# Set to the planned forced-update date plus the ~1 month support window;
# adjust when the forced-update date is fixed.
SUPPORT_ENDS = date(2026, 10, 16)


class _StrictModel(BaseModel):
    """The v0.3.16 FrozenModel semantics, inlined: frozen and extra='forbid'."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _WorkspaceStatus(str, Enum):
    """The strict five-value lifecycle enum; any other wire value raised in 0.3.16."""

    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STARTING = "starting"
    CRASHED = "crashed"


class _R2BucketAccess(str, Enum):
    """The strict access scope; any other wire value raised in 0.3.16."""

    READ = "read"
    READWRITE = "readwrite"


class AuthRawResponse(_StrictModel):
    """Parsed from /auth/signin, /auth/signup, and /auth/device/token."""

    status: str
    message: str | None = None
    user: dict[str, Any] | None = None
    tokens: dict[str, Any] | None = None
    needs_email_verification: bool = False


class LeaseResult(_StrictModel):
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


class WorkspaceInfo(_StrictModel):
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


class WorkspaceTransitionStatus(_StrictModel):
    """The 0.3.16 client read only ``status`` from stop/start, but through the strict enum."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: _WorkspaceStatus


class LeasedHostInfo(_StrictModel):
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


class LiteLLMKeyMaterial(_StrictModel):
    """Parsed from POST /keys/create."""

    key: SecretStr
    base_url: AnyUrl


class LiteLLMKeyInfo(_StrictModel):
    """Parsed from GET /keys (per entry) and GET /keys/{id}."""

    token: str
    key_alias: str | None = None
    key_name: str | None = None
    spend: Decimal = Decimal("0")
    max_budget: Decimal | None = None
    budget_duration: str | None = None
    user_id: str | None = None


class R2BucketInfo(_StrictModel):
    """Parsed from GET /buckets (per entry), GET /buckets/{name}, and inside bucket-create."""

    bucket_name: str
    s3_endpoint: AnyUrl


class R2KeyMaterial(_StrictModel):
    """Parsed from POST /buckets/{name}/roll-key and inside bucket-create."""

    access_key_id: str = Field(min_length=1)
    secret_access_key: SecretStr
    s3_endpoint: AnyUrl
    bucket_name: str
    access: _R2BucketAccess


class R2KeyInfo(_StrictModel):
    """Parsed from GET /buckets/{name}/keys and GET /bucket-keys (per entry)."""

    access_key_id: str = Field(min_length=1)
    bucket_name: str
    access: _R2BucketAccess
    alias: str | None = None
    created_at: str
    enforced_access: str | None = None


class R2BucketCreateResult(_StrictModel):
    """Parsed from POST /buckets."""

    bucket: R2BucketInfo
    key: R2KeyMaterial


class StorageCleanupGrant(_StrictModel):
    """Parsed from POST /account/storage-cleanup-grant."""

    status: str
    expires_at: str | None = None
    baseline_bytes: int | None = None
    keys: tuple[R2KeyInfo, ...] = ()


class StorageRecheckResult(_StrictModel):
    """Parsed from POST /account/storage-recheck."""

    usage_bytes: int
    limit_bytes: int
    is_over_quota: bool
    is_grant_settled: bool
    keys: tuple[R2KeyInfo, ...] = ()


class AccountEntitlementValues(_StrictModel):
    """Nested in GET /account. NOTE: the tunnel-era fields are REQUIRED-with-default here only
    because 0.3.16 shipped them with defaults; the connector still must SERVE them (see the
    serve-zeros shim in accounts.py) for even older clients that required them outright."""

    max_remote_workspaces: int
    max_total_workspaces: int
    max_buckets: int
    max_total_bucket_bytes: int
    monthly_llm_spend_usd: float
    max_active_synced_workspaces: int
    max_tunnels: int = 0
    max_services_per_tunnel: int = 0


class AccountUsageInfo(_StrictModel):
    """Nested in GET /account."""

    remote_workspaces: int
    total_workspaces: int
    buckets: int
    total_bucket_bytes: int
    llm_spend_usd_this_period: float
    llm_budget_resets_at: str | None = None
    active_synced_workspaces: int
    tunnels: int = 0


class AccountInfo(_StrictModel):
    """Parsed from GET /account."""

    user_id: str = Field(min_length=1)
    email: str
    plan_name: str
    entitlements: AccountEntitlementValues
    usage: AccountUsageInfo
    available_plans: tuple[str, ...] = ()


class SyncWorkspaceRecord(_StrictModel):
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
    created_at: str = ""
    updated_at: str = ""
    destroyed_at: str | None = None


class SyncKeyBundle(_StrictModel):
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
MODEL_BY_ENDPOINT: dict[str, type[BaseModel]] = {
    "POST /auth/signin": AuthRawResponse,
    "POST /auth/signup": AuthRawResponse,
    "POST /auth/device/token": AuthRawResponse,
    "POST /hosts/lease": LeaseResult,
    "GET /hosts [entry]": LeasedHostInfo,
    "GET /workspaces [entry]": WorkspaceInfo,
    "GET /workspaces/{host_db_id}": WorkspaceInfo,
    "POST /workspaces/{host_db_id}/stop": WorkspaceTransitionStatus,
    "POST /workspaces/{host_db_id}/start": WorkspaceTransitionStatus,
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
