Slice-fleet gen-2 phase 4 (telemetry and alerting):

- New `box_signals.py`: the shared vocabulary of gen-2 box telemetry signals (`MNGR_BOX_SIGNAL` marker lines emitted by the box collector and matched by the alert rules).

- New `alert_provisioning.py` + `observability provision-alerts`: idempotently provisions one payload-free OpenObserve alert rule per box signal over the `box_logs` stream, a direct GitHub-issue webhook destination (PAT via `OBSERVABILITY_ALERTS_GITHUB_TOKEN`), and an optional SMTP fallback destination (`--email-recipient`).

- `openobserve_api.py` gains the template / destination / v2-alert operations the provisioning needs.
