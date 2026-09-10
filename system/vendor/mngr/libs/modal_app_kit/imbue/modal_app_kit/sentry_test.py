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

# These tests usually run in well under a second, but on cold offload sandboxes
# (base image still building, I/O saturated) the sentry SDK's first-use setup has
# blown the global 10s pytest-timeout in CI -- on both the first attempt and the
# flaky retry -- so the module gets a wider 30s bound in addition to opting into
# offload's automatic flaky retry.
pytestmark = [pytest.mark.flaky, pytest.mark.timeout(30)]


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
    # Kill the real HttpTransport the init created before swapping it out:
    # abandoning it leaves its background worker thread alive for the rest of
    # the process, and those accumulate across tests in one worker.
    replaced_transport = client.transport
    client.transport = transport
    if replaced_transport is not None:
        replaced_transport.kill()
    return transport.captured_envelopes


def _captured_events(captured_envelopes: list[Any]) -> list[Any]:
    return [item.payload.json for envelope in captured_envelopes for item in envelope.items]


def _event_own_message(event: Any) -> str:
    """The event's own message -- never its breadcrumbs, so an info line riding along as
    a breadcrumb on a warning event does not count as an event of its own.
    """
    logentry = event.get("logentry")
    if isinstance(logentry, dict) and isinstance(logentry.get("message"), str):
        return logentry["message"]
    message = event.get("message")
    if isinstance(message, str):
        return message
    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list) and values and isinstance(values[-1].get("value"), str):
            return values[-1]["value"]
    return ""


def _events_matching(captured_envelopes: list[Any], probe: str) -> list[Any]:
    """Captured events whose own message carries ``probe`` (a per-test uuid).

    The SDK's LoggingIntegration is process-global: every WARNING-or-worse record
    anywhere in the process becomes an event on whichever client is active, so this
    capturing transport is a shared sink that unrelated, concurrent warnings also
    land in. Selecting by the test's own probe keeps each assertion counting only the
    events it generated, rather than an exact total that a stray warning inflates
    (the MIND-228 flake).
    """
    return [event for event in _captured_events(captured_envelopes) if probe in _event_own_message(event)]


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

    probe = uuid4().hex
    sentry_sdk.capture_message(f"tag probe {probe}")

    (event,) = _events_matching(captured_envelopes, probe)
    assert event["tags"]["service"].startswith("test-service-")
    assert event["server_name"].startswith("test-service-")


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

    # The info line stayed a breadcrumb on the warning event rather than minting its own.
    assert _events_matching(captured_envelopes, info_probe) == []
    (event,) = _events_matching(captured_envelopes, warning_probe)
    assert event["level"] == "warning"
    breadcrumb_messages = [crumb.get("message", "") for crumb in event.get("breadcrumbs", {}).get("values", [])]
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

    assert event_id is not None
    matching = [event for event in _captured_events(captured_envelopes) if event.get("event_id") == event_id]
    assert len(matching) == 1
    assert matching[0]["exception"]["values"][0]["value"] == "unexpected failure 9917"


def test_capture_and_reraise_reraises_the_original_exception(isolated_sentry_client: None) -> None:
    try:
        with capture_and_reraise():
            raise _ExampleError("cron failure 3178")
    except _ExampleError as exc:
        assert "cron failure 3178" in str(exc)
    else:
        raise AssertionError("capture_and_reraise swallowed the exception")


def test_init_sentry_never_reports_modal_client_logger_records(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    """Modal's own runtime warnings (variable-content messages) must not mint Bugsink issues."""
    captured_envelopes = _init_sentry_with_capturing_transport(monkeypatch)

    modal_probe = f"Detected 6 background thread(s) probe {uuid4().hex}"
    our_probe = f"our warning probe {uuid4().hex}"
    logging.getLogger("modal-client").warning(modal_probe)
    logging.getLogger(f"modal_app_kit_sentry_test_{uuid4().hex}").warning(our_probe)

    assert _events_matching(captured_envelopes, modal_probe) == []
    (event,) = _events_matching(captured_envelopes, our_probe)
    assert our_probe in event["logentry"]["message"]


def test_capturing_assertions_isolate_a_probe_from_unrelated_concurrent_events(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    """Pins MIND-228: an unrelated concurrent WARNING also reaches the shared sink (see
    ``_events_matching``), but selecting by the test's own probe still isolates exactly
    the intended event. Asserting an exact event total was the original flake.
    """
    captured_envelopes = _init_sentry_with_capturing_transport(monkeypatch)

    our_probe = f"intended probe {uuid4().hex}"
    unrelated_probe = f"unrelated churn {uuid4().hex}"
    logging.getLogger(f"modal_app_kit_sentry_test_{uuid4().hex}").warning(our_probe)
    logging.getLogger(f"unrelated_component_{uuid4().hex}").warning(unrelated_probe)

    assert len(_events_matching(captured_envelopes, unrelated_probe)) == 1
    assert len(_events_matching(captured_envelopes, our_probe)) == 1


def test_capture_and_reraise_logs_one_error_record_and_reports_one_event(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None, caplog: pytest.LogCaptureFixture
) -> None:
    captured_envelopes = _init_sentry_with_capturing_transport(monkeypatch)

    cron_failure = f"cron failure {uuid4().hex}"
    with caplog.at_level(logging.ERROR, logger="imbue.modal_app_kit.sentry"):
        with pytest.raises(_ExampleError):
            with capture_and_reraise():
                raise _ExampleError(cron_failure)

    error_records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
    assert isinstance(error_records[0].exc_info[1], _ExampleError)
    assert cron_failure in str(error_records[0].exc_info[1])
    # The logging integration would report the ERROR record as a second event of
    # our own were the SDK not deduping it against the explicit capture.
    matching = _events_matching(captured_envelopes, cron_failure)
    assert len(matching) == 1
    assert cron_failure in matching[0]["exception"]["values"][0]["value"]
