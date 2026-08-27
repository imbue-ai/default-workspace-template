import logging
import time
from typing import Any
from uuid import uuid4

import pytest
import sentry_sdk
import sentry_sdk.transport
from sentry_sdk.types import Event

from imbue.modal_app_kit.sentry import EventRateLimiter
from imbue.modal_app_kit.sentry import SENTRY_DISABLED_ENV_VAR
from imbue.modal_app_kit.sentry import capture_and_reraise
from imbue.modal_app_kit.sentry import capture_unexpected_exception
from imbue.modal_app_kit.sentry import drop_interrupt_events
from imbue.modal_app_kit.sentry import init_sentry
from imbue.modal_app_kit.sentry import resolve_sentry_dsn
from imbue.modal_app_kit.sentry import resolve_sentry_environment


class _ExampleError(Exception):
    """Distinct exception type for rate-limiter keying tests."""


def _exception_hint(message: str) -> dict[str, Any]:
    try:
        raise _ExampleError(message)
    except _ExampleError as exc:
        return {"exc_info": (type(exc), exc, exc.__traceback__)}


class _CapturingTransport(sentry_sdk.transport.Transport):
    """Collects envelopes in memory so assertions need no network."""

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.captured_envelopes: list[Any] = []

    def capture_envelope(self, envelope: Any) -> None:
        self.captured_envelopes.append(envelope)


def _init_sentry_with_capturing_transport(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Activate the SDK with a fake DSN and swap in a capturing transport.

    Returns the list the transport appends captured envelopes to.
    """
    dsn_env_var = f"MODAL_APP_KIT_TEST_DSN_{uuid4().hex.upper()}"
    monkeypatch.setenv(dsn_env_var, f"https://{uuid4().hex}@bugsink.invalid/1")
    init_sentry(f"test-service-{uuid4().hex}", dsn_env_var)
    client = sentry_sdk.get_client()
    assert client.is_active()
    transport = _CapturingTransport(client.options)
    client.transport = transport
    return transport.captured_envelopes


def _captured_events(captured_envelopes: list[Any]) -> list[Any]:
    return [item.payload.json for envelope in captured_envelopes for item in envelope.items]


def test_resolve_sentry_environment_prefers_env_name_over_tier() -> None:
    assert resolve_sentry_environment({"MINDS_ENV_NAME": "dev-someone-1", "MNGR_DEPLOY_ENV": "dev"}) == "dev-someone-1"
    assert resolve_sentry_environment({"MNGR_DEPLOY_ENV": "staging"}) == "staging"
    assert resolve_sentry_environment({}) == "unknown"


def test_resolve_sentry_dsn_returns_none_when_unset_empty_or_disabled() -> None:
    assert resolve_sentry_dsn({}, "RSC_SENTRY_DSN") is None
    assert resolve_sentry_dsn({"RSC_SENTRY_DSN": ""}, "RSC_SENTRY_DSN") is None
    assert (
        resolve_sentry_dsn({"RSC_SENTRY_DSN": "https://k@host/1", SENTRY_DISABLED_ENV_VAR: "1"}, "RSC_SENTRY_DSN")
        is None
    )
    assert resolve_sentry_dsn({"RSC_SENTRY_DSN": "https://k@host/1"}, "RSC_SENTRY_DSN") == "https://k@host/1"


def test_rate_limiter_allows_grace_reports_then_throttles_repeats() -> None:
    limiter = EventRateLimiter()
    hint = _exception_hint("repeated failure 7301")

    first = limiter.before_send({}, hint)
    second = limiter.before_send({}, hint)
    third = limiter.before_send({}, hint)

    assert first is not None
    assert second is not None
    assert third is None


def test_rate_limiter_keys_distinct_errors_independently() -> None:
    limiter = EventRateLimiter()
    for i in range(5):
        event = limiter.before_send({}, _exception_hint(f"distinct failure {i} 8113"))
        assert event is not None


def test_rate_limiter_allows_repeat_after_timeout_elapses() -> None:
    limiter = EventRateLimiter()
    hint = _exception_hint("timeout-elapse failure 9217")
    assert limiter.before_send({}, hint) is not None
    assert limiter.before_send({}, hint) is not None
    assert limiter.before_send({}, hint) is None

    # Age the last-sent stamp past the required gap instead of sleeping.
    key = next(iter(limiter._history))
    sent_count, last_sent = limiter._history[key]
    limiter._history[key] = (sent_count, time.monotonic() - 10_000.0)

    assert limiter.before_send({}, hint) is not None


def test_rate_limiter_dedups_message_events_by_log_message() -> None:
    limiter = EventRateLimiter()
    event: Event = {"logentry": {"message": "reconcile divergence 5521"}}

    assert limiter.before_send(event, {}) is not None
    assert limiter.before_send(event, {}) is not None
    assert limiter.before_send(event, {}) is None


def test_rate_limiter_passes_events_with_no_key_through() -> None:
    limiter = EventRateLimiter()
    for _ in range(5):
        assert limiter.before_send({}, {}) is not None


def test_drop_interrupt_events_drops_interrupts_and_clean_exits_only() -> None:
    def hint_for(exc: BaseException) -> dict[str, Any]:
        return {"exc_info": (type(exc), exc, None)}

    assert drop_interrupt_events({}, hint_for(KeyboardInterrupt())) is None
    assert drop_interrupt_events({}, hint_for(SystemExit(0))) is None
    assert drop_interrupt_events({}, hint_for(SystemExit())) is None
    assert drop_interrupt_events({}, hint_for(SystemExit(3))) is not None
    assert drop_interrupt_events({}, hint_for(_ExampleError("real 6412"))) is not None
    assert drop_interrupt_events({}, {}) is not None


def test_init_sentry_is_a_noop_without_a_dsn(monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None) -> None:
    monkeypatch.delenv("MODAL_APP_KIT_TEST_SENTRY_DSN", raising=False)

    init_sentry("test-service", "MODAL_APP_KIT_TEST_SENTRY_DSN")

    assert not sentry_sdk.get_client().is_active()


def test_init_sentry_activates_and_tags_service_with_a_dsn(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "dev")
    monkeypatch.setenv("MINDS_DEPLOY_ID", "20260817T000000Z")
    captured_envelopes = _init_sentry_with_capturing_transport(monkeypatch)

    client = sentry_sdk.get_client()
    assert client.options["environment"] == "dev"
    assert client.options["release"] == "20260817T000000Z"
    assert client.options["traces_sample_rate"] == 0.0
    assert client.options["send_default_pii"] is False

    sentry_sdk.capture_message(f"tag probe {uuid4().hex}")
    sentry_sdk.flush(timeout=5)

    assert len(captured_envelopes) == 1
    event = captured_envelopes[0].items[0].payload.json
    assert event["tags"]["service"].startswith("test-service-")
    assert event["server_name"].startswith("test-service-")


# Flakes on a >10s pytest-timeout in CI (offload run 32990804756). The body
# runs ~3.5s locally against this package's --timeout=10, most of it spent in
# init_sentry's real transport reaching for the deliberately unresolvable
# bugsink.invalid DSN before the capturing transport is swapped in -- a cost
# that varies with the resolver CI happens to have.
@pytest.mark.flaky
def test_init_sentry_reports_warning_logs_as_events_and_info_logs_as_breadcrumbs_only(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    captured_envelopes = _init_sentry_with_capturing_transport(monkeypatch)

    test_logger = logging.getLogger(f"modal_app_kit_sentry_test_{uuid4().hex}")
    # The ad-hoc logger inherits the root's WARNING level; the info probe
    # must pass the logger-level check to reach the SDK's breadcrumb hook.
    test_logger.setLevel(logging.INFO)
    info_probe = f"info probe {uuid4().hex}"
    warning_probe = f"tolerated failure probe {uuid4().hex}"
    test_logger.info(info_probe)
    test_logger.warning(warning_probe)
    sentry_sdk.flush(timeout=5)

    events = _captured_events(captured_envelopes)
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert warning_probe in events[0]["logentry"]["message"]
    # The info line rode along as a breadcrumb on the warning event, not as its own event.
    breadcrumb_messages = [crumb.get("message", "") for crumb in events[0].get("breadcrumbs", {}).get("values", [])]
    assert any(info_probe in message for message in breadcrumb_messages)


def test_capture_unexpected_exception_returns_none_without_an_active_client(isolated_sentry_client: None) -> None:
    assert capture_unexpected_exception(_ExampleError("unreported 8802")) is None


def test_capture_unexpected_exception_returns_the_event_id_when_reporting_is_active(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    captured_envelopes = _init_sentry_with_capturing_transport(monkeypatch)

    try:
        raise _ExampleError("unexpected failure 9917")
    except _ExampleError as exc:
        event_id = capture_unexpected_exception(exc)
    sentry_sdk.flush(timeout=5)

    assert event_id is not None
    events = _captured_events(captured_envelopes)
    assert len(events) == 1
    assert events[0]["event_id"] == event_id


def test_capture_and_reraise_reraises_the_original_exception(isolated_sentry_client: None) -> None:
    try:
        with capture_and_reraise():
            raise _ExampleError("cron failure 3178")
    except _ExampleError as exc:
        assert "cron failure 3178" in str(exc)
    else:
        raise AssertionError("capture_and_reraise swallowed the exception")
