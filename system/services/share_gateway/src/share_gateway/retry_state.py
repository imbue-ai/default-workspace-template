"""Backoff for bringing the share stack up, and the status file that reports it.

Starting the stack needs the connector twice (the certificate, then the relay
assignment), and either can fail: the connector is briefly down, a CA is
slow, the share is being throttled. Retrying every watch tick (10 seconds)
would hammer the connector and, through it, the certificate authorities, so
failures are spaced out on a fixed schedule -- tight at first for the
transient cases, capped at 15 minutes -- and a refusal the connector marks as
permanent halts retries until the share materials change (a re-share).

Every outcome is written to ``data/.state/share_gateway/status.json`` so the
minds desktop client can tell the owner why a share is not live yet instead
of showing a spinner forever.
"""

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

# Seconds between consecutive failed attempts; the last value repeats. A
# connector ``Retry-After`` longer than the scheduled delay wins.
RETRY_DELAYS_SECONDS = (15, 30, 60, 120, 480, 900)

_MAX_STATUS_ERROR_CHARS = 500

STATUS_UP = "up"
STATUS_RETRYING = "retrying"
STATUS_HALTED = "halted"


class ShareStackStartError(RuntimeError):
    """One failed attempt to bring the share stack up, with how it should be retried."""

    def __init__(self, message: str, is_retryable: bool, retry_after_seconds: int | None) -> None:
        super().__init__(message)
        self.is_retryable = is_retryable
        self.retry_after_seconds = retry_after_seconds


def delay_for_failed_attempt(failed_attempt_count: int, retry_after_seconds: int | None) -> int:
    """Seconds to wait after the N-th consecutive failure (N >= 1), honoring a longer Retry-After."""
    scheduled = RETRY_DELAYS_SECONDS[min(failed_attempt_count, len(RETRY_DELAYS_SECONDS)) - 1]
    if retry_after_seconds is not None and retry_after_seconds > scheduled:
        return retry_after_seconds
    return scheduled


class ProvisioningRetryState:
    """Consecutive-failure bookkeeping for one share's stack bring-up."""

    def __init__(self) -> None:
        self.failed_attempt_count = 0
        self.last_error = ""
        # Monotonic time before which no attempt is made; None means "now".
        self.next_attempt_at: float | None = None
        self.is_halted = False

    def is_attempt_due(self, now: float) -> bool:
        if self.is_halted:
            return False
        return self.next_attempt_at is None or now >= self.next_attempt_at

    def record_failure(self, error: ShareStackStartError, now: float) -> int | None:
        """Note a failed attempt; returns the delay until the next one, or None when halted."""
        self.failed_attempt_count += 1
        self.last_error = str(error)
        if not error.is_retryable:
            self.is_halted = True
            self.next_attempt_at = None
            return None
        delay = delay_for_failed_attempt(self.failed_attempt_count, error.retry_after_seconds)
        self.next_attempt_at = now + delay
        return delay

    def reset(self) -> None:
        """Forget every failure: the stack came up, or the share materials changed."""
        self.failed_attempt_count = 0
        self.last_error = ""
        self.next_attempt_at = None
        self.is_halted = False


def write_gateway_status(
    path: Path,
    state: str,
    workspace_domain: str,
    failed_attempt_count: int,
    last_error: str,
    # Seconds until the next attempt; None when up or halted.
    next_retry_in_seconds: int | None,
    now: datetime,
) -> None:
    """Atomically write the status document the minds desktop client reads."""
    next_retry_at = now + timedelta(seconds=next_retry_in_seconds) if next_retry_in_seconds is not None else None
    payload = {
        "state": state,
        "workspace_domain": workspace_domain,
        "failed_attempt_count": failed_attempt_count,
        "last_error": last_error[:_MAX_STATUS_ERROR_CHARS],
        "next_retry_at": next_retry_at.isoformat() if next_retry_at is not None else None,
        "updated_at": now.isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)


def remove_gateway_status(path: Path) -> None:
    path.unlink(missing_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
