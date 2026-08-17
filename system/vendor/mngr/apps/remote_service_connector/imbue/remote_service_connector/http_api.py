"""Mapping of domain exceptions onto FastAPI HTTP responses."""

import contextlib
import logging
from collections.abc import Iterator
from typing import NoReturn

from fastapi import HTTPException

from imbue.remote_service_connector.errors import AcmeIssuanceError
from imbue.remote_service_connector.errors import CleanupGrantBudgetExhaustedError
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import EmailNotVerifiedError
from imbue.remote_service_connector.errors import InvalidCsrError
from imbue.remote_service_connector.errors import InvalidPaidListEntryError
from imbue.remote_service_connector.errors import InvalidR2BucketNameError
from imbue.remote_service_connector.errors import InvalidRelayRecordError
from imbue.remote_service_connector.errors import InvalidShareCoordinateError
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.errors import MissingStorageConfigError
from imbue.remote_service_connector.errors import NoActiveRelaysError
from imbue.remote_service_connector.errors import PlanNotFoundError
from imbue.remote_service_connector.errors import PoolHostCleanupError
from imbue.remote_service_connector.errors import QuotaExceededError
from imbue.remote_service_connector.errors import R2BucketActiveWorkspaceError
from imbue.remote_service_connector.errors import R2BucketExistsError
from imbue.remote_service_connector.errors import R2BucketNotEmptyError
from imbue.remote_service_connector.errors import R2BucketNotFoundError
from imbue.remote_service_connector.errors import R2BucketOwnershipError
from imbue.remote_service_connector.errors import R2ReservedBucketNameError
from imbue.remote_service_connector.errors import R2StorageResultTruncatedError
from imbue.remote_service_connector.errors import RelayNotFoundError
from imbue.remote_service_connector.errors import ShareNotFoundError
from imbue.remote_service_connector.errors import ShareQuotaExceededError

logger = logging.getLogger(__name__)


def raise_as_http(exc: Exception) -> NoReturn:
    """Convert domain exceptions to HTTPException."""
    if isinstance(exc, CloudflareApiError):
        logger.warning("Cloudflare API error: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail={"errors": exc.cf_errors}) from exc
    if isinstance(exc, PoolHostCleanupError):
        # A release that could not finish its teardown -- surface as a server
        # error so the client retries rather than treating the lease as gone.
        logger.error("Pool host cleanup error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if isinstance(exc, InvalidPaidListEntryError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, InvalidR2BucketNameError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, R2BucketOwnershipError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, R2BucketNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, R2BucketNotEmptyError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, R2BucketExistsError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, R2ReservedBucketNameError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, R2BucketActiveWorkspaceError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, EmailNotVerifiedError):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_not_verified",
                "email": exc.email,
                # Whether the refusal itself sent the verification email
                # (null when no send was attempted in this context).
                "sent": exc.is_verification_email_sent,
                "message": str(exc),
            },
        ) from exc
    if isinstance(exc, QuotaExceededError):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "quota_exceeded",
                "entitlement": exc.entitlement,
                "limit": exc.limit,
                "current": exc.current,
                "message": exc.message,
            },
        ) from exc
    if isinstance(exc, CleanupGrantBudgetExhaustedError):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cleanup_grant_budget_exhausted",
                "limit": exc.limit,
                "current": exc.current,
                "window_hours": exc.window_hours,
                "message": str(exc),
            },
        ) from exc
    if isinstance(exc, R2StorageResultTruncatedError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if isinstance(exc, PlanNotFoundError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, InvalidShareCoordinateError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, InvalidRelayRecordError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, RelayNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, NoActiveRelaysError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, ShareNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ShareQuotaExceededError):
        # Same shape as QuotaExceededError so clients surface it uniformly.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "quota_exceeded",
                "entitlement": "max_shared_workspaces",
                "limit": exc.limit,
                "current": exc.current,
                "message": str(exc),
            },
        ) from exc
    if isinstance(exc, MissingShareConfigError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, MissingStorageConfigError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, InvalidCsrError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, AcmeIssuanceError):
        logger.error("ACME issuance failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.error("Unexpected error in endpoint handler", exc_info=exc)
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@contextlib.contextmanager
def handle_endpoint_errors() -> Iterator[None]:
    """Wrap endpoint logic: re-raise HTTPException, convert domain errors via raise_as_http."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        raise_as_http(exc)
