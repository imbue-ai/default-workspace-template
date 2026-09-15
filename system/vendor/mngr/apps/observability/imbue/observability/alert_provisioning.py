"""Provisioning the per-tier OpenObserve alerting for gen-2 box telemetry signals.

Implements the alert-first telemetry design of specs/slice-fleet-gen2: the box
collector evaluates every tier-1 condition locally and emits marker lines (see
:mod:`imbue.observability.box_signals`), and this module provisions one
OpenObserve alert rule per signal over the ``box_logs`` stream, delivering to
a direct GitHub-issue webhook (and optionally SMTP email).

Alert payloads are deliberately payload-free (the telemetry spec's condition
for having destinations at all): the templates carry only the alert name, the
stream name, the tier, and the trigger time -- never row content. Everything
is idempotent get-or-update, mirroring ``ensure_sender_credentials``.
"""

import json
from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.observability.box_signals import BOX_TELEMETRY_SYSLOG_IDENTIFIER
from imbue.observability.box_signals import BoxTelemetrySignal
from imbue.observability.box_signals import box_signal_line_prefix
from imbue.observability.openobserve_api import OpenObserveApiInterface
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import LOG_STREAM_NAME_BY_COLLECTOR_ROLE
from imbue.observability.primitives import ObservabilityTierName

# Names of the provisioned OpenObserve objects. snake_case throughout: the v2
# alerts API refuses names with spaces or special characters, and keeping the
# templates/destinations in the same style makes ours easy to spot in the UI.
GITHUB_ISSUE_TEMPLATE_NAME: Final[str] = "mngr_github_issue"
EMAIL_TEMPLATE_NAME: Final[str] = "mngr_email_alert"
GITHUB_DESTINATION_NAME: Final[str] = "mngr_github_issues"
EMAIL_DESTINATION_NAME: Final[str] = "mngr_infra_email"

# Every box-signal alert reads the stream the box collectors' journald
# pipeline ships into.
_BOX_LOG_STREAM: Final[str] = LOG_STREAM_NAME_BY_COLLECTOR_ROLE[CollectorRole.BOX]

# The stream columns the signal-matching SQL uses. OpenObserve flattens the
# journald receiver's entry map into per-field columns (there is no single
# ``body`` string): the line text lands in ``body_message`` and the unit's
# SyslogIdentifier in ``body_syslog_identifier``.
_BOX_LOG_MESSAGE_FIELD: Final[str] = "body_message"
_BOX_LOG_IDENTIFIER_FIELD: Final[str] = "body_syslog_identifier"

# Evaluation cadence: scan the last 10 minutes every 10 minutes, fire at the
# first matching line, then stay silent for 4 hours -- the dedupe knob: one
# GitHub issue per signal per tier per silence window, not one per line.
# The schedule is a cron expression on purpose: the interval-based
# ``frequency`` field's unit is ambiguous in practice (the v0.92.2 source
# comments say seconds, but the live scheduler rescheduled a 600 as ~10
# hours), and cron sidesteps the ambiguity entirely. Six fields: the
# server's cron parser has a leading seconds field.
_ALERT_PERIOD_MINUTES: Final[int] = 10
_ALERT_EVALUATION_CRON: Final[str] = "0 */10 * * * *"
_ALERT_SILENCE_MINUTES: Final[int] = 240
_ALERT_THRESHOLD_ROWS: Final[int] = 1

_BOX_SIGNAL_DESCRIPTION_BY_SIGNAL: Final[dict[BoxTelemetrySignal, str]] = {
    BoxTelemetrySignal.SMTP_BLOCKED: (
        "A slice VM attempted direct-to-MX SMTP (outbound TCP 25); the per-VM nftables block dropped it."
    ),
    BoxTelemetrySignal.NEW_CONNECTION_RATE: (
        "A slice VM sustained a new-connection rate above the alert threshold for a whole collection interval."
    ),
    BoxTelemetrySignal.EGRESS_RATE: (
        "A slice VM's egress rate exceeded the alerting share of the box's declared uplink."
    ),
    BoxTelemetrySignal.CONNTRACK_PRESSURE: ("The box's conntrack table is close to its ceiling."),
    BoxTelemetrySignal.LINK_SPEED_MISMATCH: (
        "The uplink's negotiated speed disagrees with the declared uplink_mbps used for fair-share shaping."
    ),
    BoxTelemetrySignal.PREP_ARTIFACT_DRIFT: (
        "A root-owned gen-2 prep artifact no longer matches the bytes prep installed."
    ),
    BoxTelemetrySignal.QEMU_CHILD_PROCESSES: (
        "A slice qemu process has child processes; a healthy qemu spawns nothing after boot."
    ),
    BoxTelemetrySignal.MANAGEMENT_SSH_ANOMALY: (
        "The management sshd saw a login pattern a locked-down gen-2 box never legitimately produces."
    ),
    BoxTelemetrySignal.SUDO_ANOMALY: ("A sudo invocation outside the scoped gen-2 grants was observed."),
    BoxTelemetrySignal.SLICE_UNIT_OOM_KILLED: (
        "systemd OOM-killed a slice VM's unit: the hardened unit's MemoryMax backstop fired, which must never happen."
    ),
    BoxTelemetrySignal.STORAGE_VOLUME_LOCKED: (
        "The box's storage root is not mounted from its LUKS mapper: the TPM unlock failed at boot (its slices are "
        "down until `minds-admin server unlock`), or the box was never encrypted."
    ),
}


class AlertProvisioningConfig(FrozenModel):
    """Everything needed to provision one tier's box-signal alerting."""

    tier: ObservabilityTierName = Field(description="Tier whose OpenObserve instance the alerts live on")
    github_repository: str = Field(
        description="GitHub 'owner/repo' the issue webhook posts to (e.g. imbue-ai/mngr-internal)"
    )
    github_token: SecretStr = Field(
        description="GitHub token with issue-write access to the repository (rides in the destination's headers)"
    )
    email_recipients: tuple[str, ...] = Field(
        description=(
            "Recipients of the SMTP fallback destination; empty skips it entirely (the instance also needs "
            "SMTP configured server-side before email delivery works)"
        )
    )


class AlertProvisioningReport(FrozenModel):
    """What one provisioning pass created versus converged in place."""

    created: tuple[str, ...] = Field(description="Names of objects created by this pass")
    updated: tuple[str, ...] = Field(description="Names of objects that already existed and were re-applied")


@pure
def box_signal_alert_name(signal: BoxTelemetrySignal) -> str:
    return f"mngr_box_{str(signal).lower()}"


@pure
def _alert_notification_title(tier: ObservabilityTierName) -> str:
    """The tier-prefixed subject every alert template shares (OpenObserve substitutes the {variables})."""
    return f"[{tier}] {{alert_name}} firing on {{stream_name}}"


@pure
def _payload_free_alert_body(tier: ObservabilityTierName) -> str:
    """The notification body every alert template shares: rule, stream, tier, trigger time -- never rows.

    Deliberately no log content (no ``{rows}``): an alert may say which rule
    fired on which stream and nothing more, per the telemetry spec's alerting
    constraint. The tier is baked in as a literal because each instance
    serves exactly one tier.
    """
    return (
        f"OpenObserve alert {{alert_name}} (type {{alert_type}}) fired on stream {{stream_name}} "
        f"in the {tier} tier at {{alert_trigger_time_str}}.\n\n"
        f"This alert is payload-free by design. To investigate, open the {tier} OpenObserve instance "
        f"over an SSH tunnel and search the stream for the matching MNGR_BOX_SIGNAL lines."
    )


@pure
def build_github_issue_template_payload(tier: ObservabilityTierName) -> dict[str, object]:
    """The GitHub-issue webhook template: a payload-free issue create request.

    The body is the JSON document OpenObserve POSTs to the GitHub issues API,
    with OpenObserve's template variables inline.
    """
    issue_body = (
        f"{_payload_free_alert_body(tier)}\n\n"
        f"Repeated firings within one silence window ({_ALERT_SILENCE_MINUTES} minutes) are suppressed "
        f"by OpenObserve; a recurrence after that files a new issue."
    )
    # json.dumps produces the exact JSON document OpenObserve POSTs; the
    # {variable} placeholders ride through it untouched for OpenObserve to
    # substitute at delivery time.
    body = json.dumps({"title": _alert_notification_title(tier), "body": issue_body})
    return {"name": GITHUB_ISSUE_TEMPLATE_NAME, "body": body, "type": "http", "title": ""}


@pure
def build_email_template_payload(tier: ObservabilityTierName) -> dict[str, object]:
    """The SMTP fallback template: the same payload-free content as the GitHub one."""
    return {
        "name": EMAIL_TEMPLATE_NAME,
        "body": _payload_free_alert_body(tier),
        "type": "email",
        "title": _alert_notification_title(tier),
    }


@pure
def build_github_destination_payload(config: AlertProvisioningConfig) -> dict[str, object]:
    """The direct GitHub-issue webhook destination (the PAT rides in a header)."""
    return {
        "name": GITHUB_DESTINATION_NAME,
        "type": "http",
        "url": f"https://api.github.com/repos/{config.github_repository}/issues",
        "method": "post",
        "skip_tls_verify": False,
        "template": GITHUB_ISSUE_TEMPLATE_NAME,
        "headers": {
            "Authorization": f"Bearer {config.github_token.get_secret_value()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            # GitHub's API rejects requests without a User-Agent; OpenObserve's
            # HTTP client does not set one on its own.
            "User-Agent": "mngr-openobserve-alerts",
            "Content-Type": "application/json",
        },
    }


@pure
def build_email_destination_payload(config: AlertProvisioningConfig) -> dict[str, object]:
    return {
        "name": EMAIL_DESTINATION_NAME,
        "type": "email",
        "emails": list(config.email_recipients),
        "template": EMAIL_TEMPLATE_NAME,
    }


@pure
def build_box_signal_alert_payload(
    signal: BoxTelemetrySignal,
    tier: ObservabilityTierName,
    destination_names: tuple[str, ...],
) -> dict[str, object]:
    """The v2 alerts API payload for one box signal: a trivial marker-line match.

    All the real condition logic (thresholds, allowlists, deltas) lives in the
    box collector, so the server-side rule is just "any line carrying this
    signal's marker in the window".
    """
    prefix = box_signal_line_prefix(signal)
    # Explicit columns: the v2 API refuses SELECT * in alert SQL. Row count
    # is what trips the threshold.
    sql = (
        f'SELECT _timestamp, host_name, {_BOX_LOG_MESSAGE_FIELD} FROM "{_BOX_LOG_STREAM}" '
        f"WHERE {_BOX_LOG_IDENTIFIER_FIELD} = '{BOX_TELEMETRY_SYSLOG_IDENTIFIER}' "
        f"AND str_match({_BOX_LOG_MESSAGE_FIELD}, '{prefix}')"
    )
    return {
        "name": box_signal_alert_name(signal),
        "stream_type": "logs",
        "stream_name": _BOX_LOG_STREAM,
        "is_real_time": False,
        "query_condition": {"type": "sql", "sql": sql},
        "trigger_condition": {
            "period": _ALERT_PERIOD_MINUTES,
            "operator": ">=",
            "threshold": _ALERT_THRESHOLD_ROWS,
            "frequency_type": "cron",
            "cron": _ALERT_EVALUATION_CRON,
            "timezone": "UTC",
            "silence": _ALERT_SILENCE_MINUTES,
        },
        "destinations": list(destination_names),
        "description": _BOX_SIGNAL_DESCRIPTION_BY_SIGNAL[signal],
        "enabled": True,
        "context_attributes": {"tier": str(tier)},
    }


def ensure_box_alerting(api: OpenObserveApiInterface, config: AlertProvisioningConfig) -> AlertProvisioningReport:
    """Create-or-converge the tier's templates, destinations, and box-signal alert rules.

    Re-applies the rendered payloads on every pass (unlike the sender
    credentials, nothing here is a one-way secret), so a changed template or
    a new signal reaches the instance by re-running provisioning.
    """
    created: list[str] = []
    updated: list[str] = []

    # Templates first (destinations reference them by name).
    existing_templates = set(api.list_template_names())
    template_payloads = [build_github_issue_template_payload(config.tier)]
    if config.email_recipients:
        template_payloads.append(build_email_template_payload(config.tier))
    for template_payload in template_payloads:
        template_name = str(template_payload["name"])
        if template_name in existing_templates:
            api.update_template(template_name, template_payload)
            updated.append(template_name)
        else:
            api.create_template(template_payload)
            created.append(template_name)

    # Destinations next (alerts reference them by name).
    existing_destinations = set(api.list_destination_names())
    destination_payloads = [build_github_destination_payload(config)]
    if config.email_recipients:
        destination_payloads.append(build_email_destination_payload(config))
    for destination_payload in destination_payloads:
        destination_name = str(destination_payload["name"])
        if destination_name in existing_destinations:
            api.update_destination(destination_name, destination_payload)
            updated.append(destination_name)
        else:
            api.create_destination(destination_payload)
            created.append(destination_name)
    destination_names = tuple(str(payload["name"]) for payload in destination_payloads)

    # One alert rule per box signal.
    alert_ids_by_name = api.list_alert_ids_by_name()
    for signal in BoxTelemetrySignal:
        alert_payload = build_box_signal_alert_payload(signal, config.tier, destination_names)
        alert_name = str(alert_payload["name"])
        existing_alert_id = alert_ids_by_name.get(alert_name)
        if existing_alert_id is not None:
            api.update_alert(existing_alert_id, alert_payload)
            updated.append(alert_name)
        else:
            api.create_alert(alert_payload)
            created.append(alert_name)

    return AlertProvisioningReport(created=tuple(created), updated=tuple(updated))
