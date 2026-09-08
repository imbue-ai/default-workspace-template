"""One-line JSON log records for our Modal apps, so every line is severity-queryable.

Modal's OpenTelemetry exporter stamps severity at capture time (every
function-log line lands in the tier's OpenObserve ``modal_logs`` stream as
``level: INFO``) and does not parse it from the line content. The only way a
line's real level reaches the log store is inside the line itself, so every
line our apps emit is one JSON object carrying an explicit ``level`` --
queryable with OpenObserve's ``spath(body, 'level')``.

Two kinds of lines share one envelope (``timestamp``, ``level``, ``logger``,
plus ``minds_env`` when deployed and the record did not stamp it itself), and
the HANDLER decides which kind it carries -- a message is never sniffed for
JSON, so a log call whose text happens to be a JSON object cannot forge a
structured record:

- Plain text lines (``logger.info("Slice reconcile done: ...")``), rendered
  by ``JsonLogFormatter`` on the root handler, become ``{"timestamp": ...,
  "level": "INFO", "logger": ..., "type": "log", "message": "Slice reconcile
  done: ..."}``.
- Structured records (the ``http_request`` / ``metric`` /
  ``share_visit_authorized`` lines, whose message is a JSON object) flow
  only through the dedicated loggers ``ensure_info_log_handler`` bootstraps,
  whose handler renders with ``StructuredRecordJsonLogFormatter``: the
  record is FLATTENED into the envelope, so ``type`` and its fields stay
  top-level exactly as consumers (the analytics log views) already read
  them. ``timestamp`` / ``level`` / ``logger`` are reserved -- a record never
  emits them, and the envelope wins on a collision.

Tracebacks are folded into a single ``exception`` string: Modal turns every
stdout/stderr line into its own log record, so a multi-line traceback would
otherwise arrive as N unrelated INFO lines. ``json.dumps`` keeps every value
escaped inside its JSON string, so the output is always exactly one line.

Stdlib only: this module ships into every consumer's container as source
(see ``test_project_ratchets.py``). The envelope deliberately mirrors the
house event-envelope field names (``timestamp`` / ``type`` / ``level`` /
``message``, cf. ``imbue.imbue_common.logging``), which cannot be imported
here for the same reason ``sentry.py`` re-implements its rate limiter.
"""

import functools
import json
import logging
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final
from typing import TextIO

from imbue.modal_app_kit.errors import StructuredRecordMessageError

logger = logging.getLogger(__name__)

# The deployed env's name, pushed into the app's Modal Secret set by
# ``minds-admin env deploy`` (dev envs: the env name; shared tiers: the tier
# name). Stamped into every log line so downstream consumers of the shared
# per-tier log store (the analytics aggregation's log views) can filter one
# env's lines out of the mix, and used by the connector to scope env-owned
# maintenance (the slice-box reconcile) on shared infrastructure. Empty when
# the container predates the stamping or runs outside a minds deploy; log
# lines omit the field and env-scoped maintenance skips.
_MINDS_ENV_NAME_ENV_VAR: Final[str] = "MINDS_ENV_NAME"

# Deploy-time knob for the level our own packages log at (the third-party
# floor stays at WARNING regardless). Threaded into the container by the
# deploy metadata secret when the deployer exports it, e.g.
# ``MINDS_LOG_LEVEL=DEBUG uv run minds-admin env deploy`` on a dev env.
LOG_LEVEL_ENV_VAR: Final[str] = "MINDS_LOG_LEVEL"
DEFAULT_IMBUE_LOG_LEVEL: Final[int] = logging.INFO
DEFAULT_IMBUE_LOG_LEVEL_NAME: Final[str] = logging.getLevelName(DEFAULT_IMBUE_LOG_LEVEL)

# The logger subtree the level knob applies to: every shipped package is an
# ``imbue.*`` module, so this one logger's level covers all of our lines
# while root stays at WARNING for third-party libraries (httpx logs every
# request URL at INFO; botocore, paramiko, and supertokens are chatty too).
IMBUE_LOGGER_NAME: Final[str] = "imbue"

# The ``type`` of a line whose message was plain text (not a structured
# record); the discriminator queries filter on.
PLAIN_TEXT_RECORD_TYPE: Final[str] = "log"

# Envelope keys a structured record can never override.
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"timestamp", "level", "logger"})


def deployed_minds_env_name() -> str:
    """The deployed env's name from MINDS_ENV_NAME ('' when not deployed via minds)."""
    return os.environ.get(_MINDS_ENV_NAME_ENV_VAR, "")


def format_utc_timestamp(epoch_seconds: float) -> str:
    """ISO 8601 UTC with microsecond precision and a trailing Z (all the resolution a LogRecord has)."""
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class JsonLogFormatter(logging.Formatter):
    """Formats every LogRecord as one JSON line carrying the level; the message is plain text."""

    def _message_fields(self, message: str) -> dict[str, Any]:
        """The envelope fields the message contributes (plain text under a fixed discriminator)."""
        return {"type": PLAIN_TEXT_RECORD_TYPE, "message": message}

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "timestamp": format_utc_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
        }
        line.update(self._message_fields(record.getMessage()))
        env_name = deployed_minds_env_name()
        if env_name and "minds_env" not in line:
            line["minds_env"] = env_name
        # exc_text is the stdlib's per-record cache of the formatted
        # traceback, shared with any other handler on the same record.
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line["exception"] = record.exc_text
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, ensure_ascii=True, separators=(",", ":"), default=str)


class StructuredRecordJsonLogFormatter(JsonLogFormatter):
    """For the dedicated structured-record loggers: flattens the JSON-object message into the envelope.

    Only ever installed by ``ensure_info_log_handler`` on loggers whose every
    message is a structured record, so a message that is not a JSON object is
    a programming error, not input to fall back on: it raises
    ``StructuredRecordMessageError``, which the stdlib's
    ``Handler.handleError`` reports on stderr.
    """

    def _message_fields(self, message: str) -> dict[str, Any]:
        try:
            record_fields = json.loads(message)
        except json.JSONDecodeError as e:
            raise StructuredRecordMessageError(f"Structured record message is not JSON: {message!r}") from e
        if not isinstance(record_fields, dict):
            raise StructuredRecordMessageError(f"Structured record message is not a JSON object: {message!r}")
        return {key: value for key, value in record_fields.items() if key not in _ENVELOPE_KEYS}


def _build_json_log_handler(stream: TextIO) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    return handler


def parse_log_level_name(level_name: str) -> int | None:
    """The numeric stdlib level for a name like ``"debug"``; None for an unknown name."""
    return logging.getLevelNamesMapping().get(level_name.strip().upper())


def resolve_imbue_log_level_name(environ: Mapping[str, str]) -> str:
    """The configured level name for our packages (the deploy knob, else INFO)."""
    return environ.get(LOG_LEVEL_ENV_VAR, "") or DEFAULT_IMBUE_LOG_LEVEL_NAME


def install_json_logging(
    root_logger: logging.Logger,
    imbue_logger: logging.Logger,
    stream: TextIO,
    imbue_level: int,
) -> None:
    """Route the whole logger tree through one JSON handler: third-party at WARNING, ours at ``imbue_level``.

    Python's root logger defaults to WARNING with no handler, so without this
    a container drops every INFO line our packages log and prints WARNING+
    through the stdlib's last-resort handler as the bare message. The root
    level is pinned to WARNING explicitly (a library may have raised it) and
    our own subtree is opened up to the configured level.
    """
    root_logger.addHandler(_build_json_log_handler(stream))
    root_logger.setLevel(logging.WARNING)
    imbue_logger.setLevel(imbue_level)


# ``functools.cache`` makes repeated calls (every request / cron invocation in
# a warm container) a no-op after the first: one handler per container,
# mirroring ``sentry._init_sentry_once``.
@functools.cache
def _configure_logging_once(imbue_level_name: str) -> None:
    imbue_level = parse_log_level_name(imbue_level_name)
    install_json_logging(
        logging.getLogger(),
        logging.getLogger(IMBUE_LOGGER_NAME),
        sys.stderr,
        imbue_level if imbue_level is not None else DEFAULT_IMBUE_LOG_LEVEL,
    )
    if imbue_level is None:
        logger.warning(
            "Ignored an unknown %s value %r; logging imbue.* at %s",
            LOG_LEVEL_ENV_VAR,
            imbue_level_name,
            DEFAULT_IMBUE_LOG_LEVEL_NAME,
        )


def configure_logging() -> None:
    """Install the JSON log handler for this container, or no-op when already installed.

    Call at the top of every Modal function (web app and crons alike), next
    to ``init_sentry``. Deliberately NOT done at import time: the entrypoint
    modules are the only place a root handler belongs, since the shipped
    modules are also imported by unit tests, whose log capture a root
    handler would disturb.
    """
    _configure_logging_once(resolve_imbue_log_level_name(os.environ))
