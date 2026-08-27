"""Mapping of domain exceptions onto FastAPI HTTP responses.

Two layers: ``raise_as_http`` converts the EXPECTED domain exceptions (the
isinstance ladder) to their status codes, and anything unexpected propagates
to ``handle_unexpected_exception`` -- the app-level 500 handler registered in
``web.py`` -- which reports it to the tier's Bugsink instance at the highest
priority and answers with a generic ``internal_error`` body (the exception
text is included only on dev/ci tiers; production and staging clients get
the Bugsink event id instead of internals).
"""

import contextlib
import logging
from collections.abc import Iterator
from typing import Final
from typing import NoReturn

from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse

from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.sentry import capture_unexpected_exception
from imbue.remote_service_connector.errors import AccountSuspendedError
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
from imbue.remote_service_connector.errors import WorkspaceRecordLeaseActiveError

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
    if isinstance(exc, WorkspaceRecordLeaseActiveError):
        raise HTTPException(status_code=409, detail={"code": "lease_active", "message": str(exc)}) from exc
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
    if isinstance(exc, AccountSuspendedError):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_suspended",
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
    # Unexpected: propagate unchanged so handle_unexpected_exception (the
    # app-level 500 handler) reports it and renders the generic body.
    raise exc


@contextlib.contextmanager
def handle_endpoint_errors() -> Iterator[None]:
    """Wrap endpoint logic: re-raise HTTPException, convert domain errors via raise_as_http."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        raise_as_http(exc)


# What every client sees in an internal_error body; the specifics live in
# Bugsink under the event id, not in the response.
INTERNAL_ERROR_MESSAGE: Final[str] = "Something went wrong on our side. The error has been reported."


def is_exception_detail_exposed() -> bool:
    """Whether 500 bodies may carry the exception repr (dev/ci tiers only).

    Keyed off the tier baked into the container by the deploy metadata
    secret. Fails closed: an unset tier (a container missing its metadata)
    reads as production and exposes nothing.
    """
    return read_deploy_env() not in ("production", "staging")


def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    """App-level 500 handler: report to Bugsink at top priority, answer generically.

    Registered in ``web.py`` for bare ``Exception``, so it sees exactly the
    exceptions nothing expected -- neither the domain mapping above nor the
    route itself. The explicit capture supplies the event id for the body;
    the log line puts the traceback in the Modal function logs (the SDK
    dedupes it against the capture, so Bugsink still gets one event).
    Starlette re-raises after this handler responds, which is normal and
    keeps the failure visible to the ASGI server too.
    """
    event_id = capture_unexpected_exception(exc)
    logger.error(
        "Unhandled exception on %s %s (event_id=%s)", request.method, request.url.path, event_id, exc_info=exc
    )
    # Both optional-valued fields are always PRESENT (empty when unavailable):
    # a client shaped against dev/ci responses must never break in production
    # over a missing key.
    detail: dict[str, str] = {
        "code": "internal_error",
        "message": INTERNAL_ERROR_MESSAGE,
        "event_id": event_id if event_id is not None else "",
        "exception": repr(exc) if is_exception_detail_exposed() else "",
    }
    return JSONResponse(status_code=500, content={"detail": detail})
