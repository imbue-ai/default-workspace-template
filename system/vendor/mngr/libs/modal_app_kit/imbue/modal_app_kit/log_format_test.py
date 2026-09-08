import io
import json
import logging
import sys

import pytest
from inline_snapshot import snapshot

from imbue.modal_app_kit.errors import StructuredRecordMessageError
from imbue.modal_app_kit.log_format import DEFAULT_IMBUE_LOG_LEVEL_NAME
from imbue.modal_app_kit.log_format import IMBUE_LOGGER_NAME
from imbue.modal_app_kit.log_format import JsonLogFormatter
from imbue.modal_app_kit.log_format import LOG_LEVEL_ENV_VAR
from imbue.modal_app_kit.log_format import StructuredRecordJsonLogFormatter
from imbue.modal_app_kit.log_format import configure_logging
from imbue.modal_app_kit.log_format import format_utc_timestamp
from imbue.modal_app_kit.log_format import install_json_logging
from imbue.modal_app_kit.log_format import parse_log_level_name
from imbue.modal_app_kit.log_format import resolve_imbue_log_level_name


def _record(message: str, *args: object, level: int = logging.INFO, name: str = "imbue.example") -> logging.LogRecord:
    return logging.LogRecord(name, level, "example.py", 1, message, args, None)


def _format(record: logging.LogRecord) -> dict[str, object]:
    line = JsonLogFormatter().format(record)
    assert "\n" not in line
    return json.loads(line)


def _format_structured(record: logging.LogRecord) -> dict[str, object]:
    line = StructuredRecordJsonLogFormatter().format(record)
    assert "\n" not in line
    return json.loads(line)


def test_format_utc_timestamp_is_iso_8601_utc_with_microseconds() -> None:
    assert format_utc_timestamp(1_787_000_000.123456) == snapshot("2026-08-17T20:53:20.123456Z")


def test_plain_text_line_carries_level_logger_type_and_message(no_minds_env: None) -> None:
    record = _record("Slice reconcile done: slice_divergences=%d", 3, level=logging.WARNING, name="imbue.rsc.hosts")
    record.created = 1_787_000_000.5

    assert _format(record) == snapshot(
        {
            "timestamp": "2026-08-17T20:53:20.500000Z",
            "level": "WARNING",
            "logger": "imbue.rsc.hosts",
            "type": "log",
            "message": "Slice reconcile done: slice_divergences=3",
        }
    )


def test_structured_record_message_is_flattened_into_the_envelope(no_minds_env: None) -> None:
    access_record = {"type": "http_request", "method": "GET", "path": "/account", "status": 200, "duration_ms": 1.5}

    line = _format_structured(_record("%s", json.dumps(access_record)))

    assert line["level"] == "INFO"
    assert line["type"] == "http_request"
    assert line["method"] == "GET"
    assert line["status"] == 200
    assert "message" not in line


def test_structured_record_cannot_override_the_envelope_keys(no_minds_env: None) -> None:
    hostile_record = {"type": "metric", "level": "ERROR", "logger": "forged", "timestamp": "1970-01-01T00:00:00Z"}

    line = _format_structured(_record("%s", json.dumps(hostile_record), level=logging.INFO, name="imbue.metrics"))

    assert line["level"] == "INFO"
    assert line["logger"] == "imbue.metrics"
    assert line["timestamp"] != "1970-01-01T00:00:00Z"
    assert line["type"] == "metric"


@pytest.mark.parametrize(
    "message",
    ['["a", "json", "list"]', '"a json string"', "plain text mentioning {braces} inside"],
)
def test_structured_record_formatter_rejects_a_message_that_is_not_a_json_object(message: str) -> None:
    with pytest.raises(StructuredRecordMessageError):
        StructuredRecordJsonLogFormatter().format(_record(message))


@pytest.mark.parametrize(
    "message",
    [
        "{not json at all",
        '["a", "json", "list"]',
        "plain text mentioning {braces} inside",
        json.dumps({"type": "http_request", "method": "GET", "path": "/forged", "status": 200}),
    ],
)
def test_plain_text_formatter_never_flattens_the_message_even_when_it_is_a_json_object(
    no_minds_env: None, message: str
) -> None:
    # Only the dedicated structured-record handlers flatten; a log call whose
    # whole text is client-controlled JSON cannot forge an http_request record
    # through the root handler.
    line = _format(_record(message))

    assert line["type"] == "log"
    assert line["message"] == message
    assert "method" not in line


def test_message_with_newlines_and_quotes_stays_one_escaped_line(no_minds_env: None) -> None:
    hostile = 'first line\nsecond "quoted" line\r\n{"type":"http_request"}'

    line = _format(_record(hostile))

    assert line["message"] == hostile
    assert line["type"] == "log"


def test_exception_traceback_is_folded_into_one_line(no_minds_env: None) -> None:
    try:
        raise ValueError("boom 48213")
    except ValueError:
        record = logging.LogRecord("imbue.example", logging.ERROR, "example.py", 1, "Unhandled", (), None)
        record.exc_info = sys.exc_info()

    line = _format(record)

    assert line["level"] == "ERROR"
    assert line["message"] == "Unhandled"
    assert isinstance(line["exception"], str)
    assert "ValueError: boom 48213" in line["exception"]
    assert "Traceback" in line["exception"]


def test_minds_env_is_stamped_on_plain_lines_when_deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_ENV_NAME", "dev-someone-7")

    assert _format(_record("hello"))["minds_env"] == "dev-someone-7"


def test_minds_env_from_a_structured_record_is_kept_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_ENV_NAME", "dev-someone-7")

    line = _format_structured(_record("%s", json.dumps({"type": "metric", "minds_env": "recorded-env"})))

    assert line["minds_env"] == "recorded-env"


def test_parse_log_level_name_accepts_any_case_and_rejects_unknown_names() -> None:
    assert parse_log_level_name("debug") == logging.DEBUG
    assert parse_log_level_name(" Warning ") == logging.WARNING
    assert parse_log_level_name("VERBOSE") is None


def test_resolve_imbue_log_level_name_defaults_to_info_and_honors_the_knob() -> None:
    assert resolve_imbue_log_level_name({}) == DEFAULT_IMBUE_LOG_LEVEL_NAME
    assert resolve_imbue_log_level_name({LOG_LEVEL_ENV_VAR: ""}) == DEFAULT_IMBUE_LOG_LEVEL_NAME
    assert resolve_imbue_log_level_name({LOG_LEVEL_ENV_VAR: "DEBUG"}) == "DEBUG"


def test_install_json_logging_emits_our_info_and_only_third_party_warnings(
    no_minds_env: None, throwaway_logger: logging.Logger
) -> None:
    # The throwaway logger stands in for root; children under it for our
    # packages and for a third-party lib.
    imbue_logger = logging.getLogger(f"{throwaway_logger.name}.imbue")
    our_logger = logging.getLogger(f"{throwaway_logger.name}.imbue.rsc")
    third_party_logger = logging.getLogger(f"{throwaway_logger.name}.httpx")
    stream = io.StringIO()

    install_json_logging(throwaway_logger, imbue_logger, stream, logging.INFO)
    our_logger.info("ours at info")
    our_logger.debug("ours at debug")
    third_party_logger.info("theirs at info")
    third_party_logger.warning("theirs at warning")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(line["level"], line["message"]) for line in lines] == [
        ("INFO", "ours at info"),
        ("WARNING", "theirs at warning"),
    ]


def test_configure_logging_installs_exactly_one_handler_per_process(
    monkeypatch: pytest.MonkeyPatch, restored_root_logger: None
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "DEBUG")
    root = logging.getLogger()
    handler_count_before = len(root.handlers)

    configure_logging()
    configure_logging()

    json_handlers = [handler for handler in root.handlers if isinstance(handler.formatter, JsonLogFormatter)]
    assert len(json_handlers) == 1
    assert len(root.handlers) == handler_count_before + 1
    assert root.level == logging.WARNING
    assert logging.getLogger(IMBUE_LOGGER_NAME).level == logging.DEBUG


def test_configure_logging_falls_back_to_info_and_warns_on_an_unknown_level_name(
    monkeypatch: pytest.MonkeyPatch, restored_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "VERBOSE")

    configure_logging()
    stderr_lines = capsys.readouterr().err.splitlines()

    assert logging.getLogger(IMBUE_LOGGER_NAME).level == logging.INFO
    # The typo must be visible in the log store, as a JSON line through the
    # handler just installed.
    assert len(stderr_lines) == 1
    warning = json.loads(stderr_lines[0])
    assert warning["level"] == "WARNING"
    assert LOG_LEVEL_ENV_VAR in warning["message"]
    assert "VERBOSE" in warning["message"]
