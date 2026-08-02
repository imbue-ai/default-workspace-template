"""Exception types for the remote service connector."""


class CloudflareApiError(RuntimeError):
    """Raised when the Cloudflare API returns an error response."""

    def __init__(self, status_code: int, errors: list[dict[str, object]]) -> None:
        self.status_code = status_code
        self.cf_errors = errors
        messages = "; ".join(str(e.get("message", e)) for e in errors)
        super().__init__(f"Cloudflare API error ({status_code}): {messages}")


class TunnelNotFoundError(KeyError):
    def __init__(self, tunnel_name: str) -> None:
        self.tunnel_name = tunnel_name
        super().__init__(f"Tunnel not found: {tunnel_name}")


class TunnelOwnershipError(PermissionError):
    def __init__(self, tunnel_name: str, user_id_prefix: str) -> None:
        self.tunnel_name = tunnel_name
        self.user_id_prefix = user_id_prefix
        super().__init__(f"User '{user_id_prefix}' does not own tunnel '{tunnel_name}'")


class ServiceNotFoundError(KeyError):
    def __init__(self, service_name: str, tunnel_name: str) -> None:
        self.service_name = service_name
        self.tunnel_name = tunnel_name
        super().__init__(f"Service '{service_name}' not found on tunnel '{tunnel_name}'")


class InvalidTunnelComponentError(ValueError):
    def __init__(self, component_name: str, value: str, forbidden: str) -> None:
        self.component_name = component_name
        self.value = value
        self.forbidden = forbidden
        super().__init__(
            f"{component_name} '{value}' must not contain '{forbidden}' (used as the tunnel name separator)"
        )


class TunnelComponentTooLongError(ValueError):
    """Raised when a tunnel component exceeds the maximum length."""

    def __init__(self, component_name: str, value: str, max_length: int) -> None:
        self.component_name = component_name
        self.value = value
        self.max_length = max_length
        super().__init__(f"{component_name} '{value}' exceeds maximum length of {max_length}")


class InvalidHostNameError(ValueError):
    """Raised when a host_name fails the SafeName regex on the lease request."""

    def __init__(self, value: object) -> None:
        self.value = value
        super().__init__(f"host_name must be alphanumeric (with dashes/underscores allowed in the middle): {value!r}")


class InvalidPaidListEntryError(ValueError):
    """Raised when a paid-list domain or email entry is malformed."""

    def __init__(self, value: object, reason: str) -> None:
        self.value = value
        super().__init__(f"Invalid paid-list entry {value!r}: {reason}")


class InvalidR2BucketNameError(ValueError):
    """Raised when a derived R2 bucket name violates Cloudflare's naming rules."""

    def __init__(self, value: object) -> None:
        self.value = value
        super().__init__(
            f"R2 bucket name must be 3-63 lowercase alphanumeric/hyphen chars with no leading/trailing hyphen: {value!r}"
        )


class InvalidR2AccessError(ValueError):
    """Raised when a key access scope is neither 'read' nor 'readwrite'."""

    def __init__(self, value: object) -> None:
        self.value = value
        super().__init__(f"access must be 'read' or 'readwrite', got {value!r}")


class R2BucketExistsError(RuntimeError):
    """Raised when creating a bucket whose derived name already exists for the user."""

    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name
        super().__init__(f"Bucket already exists: {bucket_name}")


class R2BucketNotFoundError(KeyError):
    """Raised when a bucket the caller referenced does not exist (or is not theirs)."""

    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name
        super().__init__(f"Bucket not found: {bucket_name}")


class R2BucketNotEmptyError(RuntimeError):
    """Raised when destroying a bucket that still has objects in it."""

    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name
        super().__init__(f"Bucket is not empty: {bucket_name}. Empty it before destroying.")


class R2BucketOwnershipError(PermissionError):
    """Raised when a bucket name does not carry the caller's ownership prefix."""

    def __init__(self, bucket_name: str, user_id_prefix: str) -> None:
        self.bucket_name = bucket_name
        self.user_id_prefix = user_id_prefix
        super().__init__(f"User '{user_id_prefix}' does not own bucket '{bucket_name}'")


class R2ReservedBucketNameError(RuntimeError):
    """Raised when creating a `host-` prefixed bucket that no workspace record backs.

    The `host-<hex>` short-name shape is reserved for workspace-backup buckets
    (named by their workspace's host id); a generic user bucket must never be
    able to collide with one, or the backup reapers could not tell them apart.
    """

    def __init__(self, short_name: str) -> None:
        self.short_name = short_name
        super().__init__(
            f"Bucket names starting with 'host-' are reserved for workspace backups: '{short_name}'. "
            "Pick a different name."
        )


class R2BucketActiveWorkspaceError(RuntimeError):
    """Raised when destroying a workspace-backup bucket whose workspace record is still ACTIVE.

    Tombstone-first is enforced server-side so a live workspace's backups can
    never be deleted -- destroy the workspace (or remove its record) before
    destroying its backup bucket.
    """

    def __init__(self, bucket_name: str, host_id: str) -> None:
        self.bucket_name = bucket_name
        self.host_id = host_id
        super().__init__(
            f"Bucket '{bucket_name}' holds backups for workspace '{host_id}', which is still active. "
            "Destroy the workspace first."
        )


class QuotaExceededError(RuntimeError):
    """Raised when an operation would exceed one of the account's entitlements.

    Mapped to a structured 403 (``code: quota_exceeded`` plus the entitlement
    name, limit, and current usage) so clients can render "N of M used".
    """

    def __init__(self, entitlement: str, limit: float, current: float, message: str) -> None:
        self.entitlement = entitlement
        self.limit = limit
        self.current = current
        self.message = message
        super().__init__(message)


class R2StorageResultTruncatedError(RuntimeError):
    """Raised when the sweep's GraphQL usage response fills its row budget and may be truncated."""

    def __init__(self, row_count: int, row_limit: int) -> None:
        self.row_count = row_count
        self.row_limit = row_limit
        super().__init__(
            f"R2 storage GraphQL response returned {row_count} rows, filling the {row_limit}-row budget; "
            "the result may be truncated so the sweep must not enforce from it. The query returns one row "
            "per bucket -- shard it into bucketName_in chunks to raise the ceiling."
        )


class CleanupGrantBudgetExhaustedError(RuntimeError):
    """Raised when an account has burned its failed-cleanup-grant budget for the rolling window.

    Mapped to a structured 403 (``code: cleanup_grant_budget_exhausted``) so
    clients can message it separately from quota errors.
    """

    def __init__(self, limit: int, current: int, window_hours: int) -> None:
        self.limit = limit
        self.current = current
        self.window_hours = window_hours
        super().__init__(
            f"Cleanup-grant budget exhausted: {current} grants in the last {window_hours} hours ended "
            f"without any usage decrease (limit {limit}). The budget frees up as those grants age out "
            "of the window; grants that actually reduce usage never count against it."
        )


class PlanNotFoundError(KeyError):
    """Raised when a referenced plan has no row in the plans table."""

    def __init__(self, plan_name: str) -> None:
        self.plan_name = plan_name
        super().__init__(
            f"Plan '{plan_name}' is not seeded in the plans table; "
            "run `minds env deploy` (which writes the [plans] blocks from deploy.toml)."
        )


class InvalidAuthPolicyError(ValueError):
    """Raised when an auth policy has no identity constraint (would expose the service publicly)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Auth policy rejected: {reason}")


class UnknownEntitlementColumnError(ValueError):
    """Raised when an entitlements update names a column that does not exist."""

    def __init__(self, unknown_columns: list[str]) -> None:
        super().__init__(f"Unknown entitlement columns: {unknown_columns}")


class ServicePolicyMissingError(RuntimeError):
    """Raised when a service would be added with no Access policy available at all."""

    def __init__(self, tunnel_name: str) -> None:
        self.tunnel_name = tunnel_name
        super().__init__(
            f"Tunnel '{tunnel_name}' has no default auth policy and no owner policy could be derived; "
            "set a default auth policy on the tunnel before adding services."
        )


class PoolHostCleanupError(RuntimeError):
    """Raised when a pool-host release/teardown cannot destroy the slice's lima VM.

    Surfaced (rather than swallowed to a warning) so a release that fails to
    actually tear down the VM reports failure instead of a false success.
    """


class MissingAuthWebsiteDomainError(RuntimeError):
    """Raised when the required AUTH_WEBSITE_DOMAIN secret is not set."""
