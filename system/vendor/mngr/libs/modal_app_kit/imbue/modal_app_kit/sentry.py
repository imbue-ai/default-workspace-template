"""Shared sentry-sdk initialization for our Modal apps (reporting to self-hosted Bugsink).

Every reporting service (remote_service_connector, modal_litellm,
oauth_redirector) calls :func:`init_sentry` at the top of its Modal
functions. The DSN points at the tier's self-hosted Bugsink instance and
arrives via the per-deploy ``sentry-<tier>-<deploy_id>`` Modal Secret; a
missing/empty DSN (a tier whose ``sentry`` Vault entry is not yet
provisioned) or ``MINDS_SENTRY_DISABLED=1`` makes the whole thing a no-op,
so consumers never depend on Bugsink being up.

Unlike the rest of this package, this module imports ``sentry_sdk`` -- so
any app that imports it MUST pin ``sentry-sdk`` in its image dependency
group (see the per-module allowance in ``test_project_ratchets.py``).

The event rate limiter below is a deliberately slim, stdlib-only
reimplementation of ``imbue.imbue_common.sentry.core._SentryEventRateLimiter``
(which cannot be used here: the containers ship only the app package plus
this one, and the original is coupled to loguru/pydantic). It is the
crash-storm control: a tight error loop is the same exception repeating,
and per-key dedup collapses it client-side before it hits the wire.
"""

import functools
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event
from sentry_sdk.types import Hint

# Kill switch: set to "1" at deploy time (or in a test run's env) to disable
# reporting entirely without editing any Vault entry.
SENTRY_DISABLED_ENV_VAR: Final[str] = "MINDS_SENTRY_DISABLED"

# Environment / release sources, all baked into the deployed function spec by
# ``minds-admin env deploy``: MINDS_ENV_NAME names the concrete env (e.g.
# ``dev-josh-1``; from the litellm-connector secret, connector only),
# MNGR_DEPLOY_ENV names the tier, and MINDS_DEPLOY_ID is the per-deploy
# timestamp (see imbue.modal_app_kit.deploy).
_ENV_NAME_ENV_VAR: Final[str] = "MINDS_ENV_NAME"
_DEPLOY_ENV_ENV_VAR: Final[str] = "MNGR_DEPLOY_ENV"
_DEPLOY_ID_ENV_VAR: Final[str] = "MINDS_DEPLOY_ID"

# Rate limiter defaults, mirroring the imbue_common original: the first
# ``_GRACE_REPORT_COUNT`` occurrences of a distinct error always pass, after
# which each further report requires a gap of ``_TIMEOUT_BASE_SECONDS *
# (sent_count - _GRACE_REPORT_COUNT + 1)`` seconds since the last one -- i.e.
# the gap grows by ``_TIMEOUT_BASE_SECONDS`` per report past the grace count.
_GRACE_REPORT_COUNT: Final[int] = 2
_TIMEOUT_BASE_SECONDS: Final[float] = 60.0
# Past this many distinct tracked errors, stop tracking and pass everything
# through (a container hitting this has a worse problem than event volume).
_MAX_TRACKED_KEYS: Final[int] = 10_000


class EventRateLimiter:
    """Per-error-key dedup for outgoing Sentry events (a ``before_send`` hook).

    Keys on the exception type + message (or the log message for
    non-exception events). Thread-safe; one instance lives for the
    container's lifetime as the SDK's ``before_send`` hook.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (sent_count, last_sent_monotonic)
        self._history: dict[str, tuple[int, float]] = {}

    def _event_key(self, event: Event, hint: Hint) -> str | None:
        exc_info = hint.get("exc_info")
        if exc_info is not None:
            exc_type, exc_value, _ = exc_info
            if exc_type is not None:
                return f"{exc_type.__module__}.{exc_type.__qualname__}:{exc_value}"
        log_entry = event.get("logentry")
        if isinstance(log_entry, dict):
            message = log_entry.get("message")
            if isinstance(message, str) and message:
                return f"log:{message}"
        return None

    def before_send(self, event: Event, hint: Hint) -> Event | None:
        # Interrupts / clean shutdowns are dropped before they consume a
        # dedup slot; this method is the whole before_send chain.
        interrupt_filtered = drop_interrupt_events(event, hint)
        if interrupt_filtered is None:
            return None
        key = self._event_key(event, hint)
        if key is None:
            return event
        now = time.monotonic()
        with self._lock:
            history = self._history.get(key)
            if history is None:
                if len(self._history) >= _MAX_TRACKED_KEYS:
                    return event
                self._history[key] = (1, now)
                return event
            sent_count, last_sent = history
            if sent_count < _GRACE_REPORT_COUNT:
                self._history[key] = (sent_count + 1, now)
                return event
            required_gap = _TIMEOUT_BASE_SECONDS * (sent_count - _GRACE_REPORT_COUNT + 1)
            if now - last_sent >= required_gap:
                self._history[key] = (sent_count + 1, now)
                return event
            return None


def drop_interrupt_events(event: Event, hint: Hint) -> Event | None:
    """``before_send`` hook dropping interrupts / clean shutdowns (not real faults).

    ``KeyboardInterrupt`` is never an error; ``SystemExit`` only is for a
    non-zero code. Mirrors ``_drop_interrupt_events`` in
    ``imbue.imbue_common.sentry.core``.
    """
    exc_info = hint.get("exc_info")
    if exc_info is None:
        return event
    exc_type, exc_value, _ = exc_info
    if exc_type is None:
        return event
    if issubclass(exc_type, KeyboardInterrupt):
        return None
    if issubclass(exc_type, SystemExit):
        code = exc_value.code if isinstance(exc_value, SystemExit) else None
        if code is None or code == 0:
            return None
    return event


def resolve_sentry_environment(environ: dict[str, str]) -> str:
    """The Sentry ``environment`` label: the concrete env name when known, else the tier."""
    return environ.get(_ENV_NAME_ENV_VAR) or environ.get(_DEPLOY_ENV_ENV_VAR) or "unknown"


def resolve_sentry_dsn(environ: dict[str, str], dsn_env_var: str) -> str | None:
    """The DSN to report to, or None when reporting must be disabled.

    None when the DSN env var is unset/empty (the tier's ``sentry`` Vault
    entry has not been provisioned yet) or when the kill switch is set.
    """
    if environ.get(SENTRY_DISABLED_ENV_VAR) == "1":
        return None
    return environ.get(dsn_env_var) or None


# ``functools.cache`` makes repeated calls (every request / cron invocation in
# a warm container) a no-op after the first: one init per (service, dsn) per
# container, mirroring the connector's ``_init_supertokens_once`` pattern.
@functools.cache
def _init_sentry_once(service_name: str, dsn: str, environment: str, release: str) -> None:
    limiter = EventRateLimiter()
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        server_name=service_name,
        # Log-level reporting policy: WARNING and above become events (not
        # just ERROR, the SDK default). The stdlib levels ARE the priority
        # scheme -- ``logger.error`` (and unhandled exceptions) report at
        # error level for failures nothing tolerated, while ``logger.warning``
        # reports at warning level for exceptions we caught and continued
        # past for robustness. Expected, routine anomalies (transient
        # upstream errors, client-input junk) belong at info/debug plus a
        # metric line, not warning. INFO and up remain breadcrumbs.
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.WARNING)],
        # Error reporting only -- a transaction per HTTP request would be
        # high-volume noise for the tiny Bugsink instances.
        traces_sample_rate=0.0,
        sample_rate=1.0,
        send_default_pii=False,
        # Local variables can carry request payloads / secrets; events keep
        # the stack trace without them.
        include_local_variables=False,
        attach_stacktrace=True,
        max_value_length=10_000,
        before_send=limiter.before_send,
    )
    sentry_sdk.set_tag("service", service_name)


def init_sentry(service_name: str, dsn_env_var: str) -> None:
    """Initialize sentry-sdk for this container, or no-op when reporting is disabled.

    Reads the DSN from ``dsn_env_var``; a missing/empty value or
    ``MINDS_SENTRY_DISABLED=1`` disables reporting entirely. Idempotent per
    container. Safe to call at the top of every Modal function.
    """
    environ = dict(os.environ)
    dsn = resolve_sentry_dsn(environ, dsn_env_var)
    if dsn is None:
        return
    _init_sentry_once(
        service_name,
        dsn,
        resolve_sentry_environment(environ),
        environ.get(_DEPLOY_ID_ENV_VAR, "unknown"),
    )


def capture_unexpected_exception(exc: BaseException) -> str | None:
    """Report an unexpected exception to Sentry, returning the event id when one was sent.

    For app-level 500 handlers that embed the event id in their error
    response. Explicit capture (rather than waiting for the framework
    integration to see the exception propagate) is what makes the id
    available while building the response; the SDK's default Dedupe
    integration then drops the framework integration's later capture of the
    same exception instance, so nothing is double-reported. Returns None
    when reporting is disabled (no active client) or the event was dropped
    (e.g. by the rate limiter).
    """
    return sentry_sdk.capture_exception(exc)


@contextmanager
def capture_and_reraise() -> Iterator[None]:
    """Report any escaping exception to Sentry, then re-raise it.

    For Modal cron / spawned functions: Modal owns their top-level exception
    handling, so the SDK's excepthook integration never sees their failures.
    A no-op when :func:`init_sentry` never activated (capture on an inactive
    client is discarded by the SDK).
    """
    try:
        yield
    except Exception:
        sentry_sdk.capture_exception()
        sentry_sdk.flush(timeout=5)
        raise
