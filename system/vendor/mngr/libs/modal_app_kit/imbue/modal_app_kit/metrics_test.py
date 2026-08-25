import json
import logging

import pytest
from inline_snapshot import snapshot

from imbue.modal_app_kit.metrics import emit_metric
from imbue.modal_app_kit.metrics import format_metric_record


def test_format_metric_record_produces_one_json_line_with_the_metric_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINDS_ENV_NAME", raising=False)
    line = format_metric_record("cloudflare_usage_read_failed", 1, {"operation": "bucket_usage"})

    assert line == snapshot(
        '{"type":"metric","name":"cloudflare_usage_read_failed","value":1,"tags":{"operation":"bucket_usage"}}'
    )
    assert "\n" not in line


def test_format_metric_record_stamps_the_minds_env_when_deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_ENV_NAME", "dev-someone-1")
    line = format_metric_record("acme_ca_issuance_failed", 1, {"ca": "letsencrypt"})

    parsed = json.loads(line)
    assert parsed["minds_env"] == "dev-someone-1"


def test_format_metric_record_keeps_hostile_tag_values_on_one_escaped_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINDS_ENV_NAME", raising=False)
    line = format_metric_record("attribution_cookie_rejected", 1, {"reason": 'bad"\nvalue'})

    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["tags"]["reason"] == 'bad"\nvalue'


def test_emit_metric_logs_the_record_at_info_level(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("MINDS_ENV_NAME", raising=False)
    # The module installed its dedicated non-propagating handler at import;
    # flip propagate so caplog's root handler sees the record under test,
    # then restore the production setting.
    metric_logger = logging.getLogger("imbue.modal_app_kit.metrics")
    original_propagate = metric_logger.propagate
    metric_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="imbue.modal_app_kit.metrics"):
            emit_metric("supertokens_user_fetch_failed", 1, {"caller": "entitlements"})
    finally:
        metric_logger.propagate = original_propagate

    matching = [record for record in caplog.records if "supertokens_user_fetch_failed" in record.getMessage()]
    assert len(matching) == 1
    assert matching[0].levelno == logging.INFO
    parsed = json.loads(matching[0].getMessage())
    assert parsed == {
        "type": "metric",
        "name": "supertokens_user_fetch_failed",
        "value": 1,
        "tags": {"caller": "entitlements"},
    }
