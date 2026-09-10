import json

from pydantic import SecretStr

from imbue.observability.alert_provisioning import AlertProvisioningConfig
from imbue.observability.alert_provisioning import EMAIL_DESTINATION_NAME
from imbue.observability.alert_provisioning import EMAIL_TEMPLATE_NAME
from imbue.observability.alert_provisioning import GITHUB_DESTINATION_NAME
from imbue.observability.alert_provisioning import GITHUB_ISSUE_TEMPLATE_NAME
from imbue.observability.alert_provisioning import box_signal_alert_name
from imbue.observability.alert_provisioning import build_box_signal_alert_payload
from imbue.observability.alert_provisioning import build_github_destination_payload
from imbue.observability.alert_provisioning import build_github_issue_template_payload
from imbue.observability.alert_provisioning import ensure_box_alerting
from imbue.observability.box_signals import BoxTelemetrySignal
from imbue.observability.mock_openobserve_api_test import MockOpenObserveApi
from imbue.observability.primitives import ObservabilityTierName


def _config(email_recipients: tuple[str, ...] = ()) -> AlertProvisioningConfig:
    return AlertProvisioningConfig(
        tier=ObservabilityTierName("dev"),
        github_repository="imbue-ai/mngr-internal",
        github_token=SecretStr("ghp_test_token"),
        email_recipients=email_recipients,
    )


def test_github_issue_template_is_valid_json_and_payload_free() -> None:
    payload = build_github_issue_template_payload(ObservabilityTierName("dev"))
    document = json.loads(str(payload["body"]))
    assert set(document) == {"title", "body"}
    # Payload-free by design: the template may name the rule, stream, tier,
    # and time -- never row content ({rows} would leak log lines to GitHub).
    assert "{rows}" not in str(payload["body"])
    assert "{alert_name}" in document["title"]
    assert "[dev]" in document["title"]


def test_github_destination_carries_the_token_and_required_github_headers() -> None:
    payload = build_github_destination_payload(_config())
    assert payload["url"] == "https://api.github.com/repos/imbue-ai/mngr-internal/issues"
    # GitHub rejects requests without a User-Agent, and OpenObserve's client
    # does not set one on its own.
    assert payload["headers"] == {
        "Authorization": "Bearer ghp_test_token",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mngr-openobserve-alerts",
        "Content-Type": "application/json",
    }
    assert payload["template"] == GITHUB_ISSUE_TEMPLATE_NAME


def test_box_signal_alert_payload_matches_the_signal_marker_line() -> None:
    payload = build_box_signal_alert_payload(
        BoxTelemetrySignal.SMTP_BLOCKED, ObservabilityTierName("dev"), (GITHUB_DESTINATION_NAME,)
    )
    assert payload["name"] == "mngr_box_smtp_blocked"
    assert payload["stream_name"] == "box_logs"
    assert payload["query_condition"] == {
        "type": "sql",
        "sql": (
            'SELECT _timestamp, host_name, body_message FROM "box_logs" '
            "WHERE body_syslog_identifier = 'mngr-box-telemetry' "
            "AND str_match(body_message, 'MNGR_BOX_SIGNAL SMTP_BLOCKED')"
        ),
    }
    # A cron schedule, deliberately: the interval-based frequency field's
    # unit is ambiguous in practice (observed rescheduling 600 as ~10 hours),
    # while cron is exact. period and silence are minutes.
    assert payload["trigger_condition"] == {
        "period": 10,
        "operator": ">=",
        "threshold": 1,
        "frequency_type": "cron",
        "cron": "0 */10 * * * *",
        "timezone": "UTC",
        "silence": 240,
    }
    assert payload["destinations"] == [GITHUB_DESTINATION_NAME]


def test_ensure_box_alerting_creates_everything_on_a_fresh_instance() -> None:
    api = MockOpenObserveApi()

    report = ensure_box_alerting(api, _config())

    assert GITHUB_ISSUE_TEMPLATE_NAME in api.template_payload_by_name
    assert GITHUB_DESTINATION_NAME in api.destination_payload_by_name
    assert set(api.created_alert_names) == {box_signal_alert_name(signal) for signal in BoxTelemetrySignal}
    assert set(report.created) == {
        GITHUB_ISSUE_TEMPLATE_NAME,
        GITHUB_DESTINATION_NAME,
        *(box_signal_alert_name(signal) for signal in BoxTelemetrySignal),
    }
    assert report.updated == ()


def test_ensure_box_alerting_skips_email_objects_without_recipients() -> None:
    api = MockOpenObserveApi()

    ensure_box_alerting(api, _config())

    assert EMAIL_TEMPLATE_NAME not in api.template_payload_by_name
    assert EMAIL_DESTINATION_NAME not in api.destination_payload_by_name
    for payload in api.alert_payload_by_id.values():
        assert payload["destinations"] == [GITHUB_DESTINATION_NAME]


def test_ensure_box_alerting_provisions_email_fallback_when_recipients_given() -> None:
    api = MockOpenObserveApi()

    ensure_box_alerting(api, _config(email_recipients=("infra@imbue.com",)))

    assert api.destination_payload_by_name[EMAIL_DESTINATION_NAME]["emails"] == ["infra@imbue.com"]
    for payload in api.alert_payload_by_id.values():
        assert payload["destinations"] == [GITHUB_DESTINATION_NAME, EMAIL_DESTINATION_NAME]


def test_ensure_box_alerting_converges_an_already_provisioned_instance_in_place() -> None:
    api = MockOpenObserveApi()
    first_report = ensure_box_alerting(api, _config())
    alert_ids_after_first_pass = set(api.alert_payload_by_id)

    second_report = ensure_box_alerting(api, _config())

    # The second pass updates every object in place: no duplicates, same ids.
    assert second_report.created == ()
    assert set(second_report.updated) == set(first_report.created)
    assert set(api.alert_payload_by_id) == alert_ids_after_first_pass
    assert set(api.updated_alert_ids) == alert_ids_after_first_pass
