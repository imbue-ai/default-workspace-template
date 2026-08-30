Marked `test_init_sentry_reports_warning_logs_as_events_and_info_logs_as_breadcrumbs_only`
as flaky so offload retries it. It times out against this package's 10s limit
in loaded CI runs; the underlying cost is being tracked separately.
