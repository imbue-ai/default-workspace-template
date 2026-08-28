"""One-line JSON metric records for our Modal apps.

Expected, routine anomalies (transient upstream errors, client-input junk)
are worth counting, not error-reporting: what matters is whether their RATE
changes, which is a query over the tier's OpenObserve log store, not a
Bugsink issue. Each call here emits one single-line JSON object
(``{"type": "metric", "name": ..., "value": ..., "tags": {...}}``) to the
container's stderr; Modal's workspace-level OTEL integration ships every
function-log line into the tier's OpenObserve ``modal_logs`` stream, where
the record is queryable with plain JSON extraction -- the same
one-line-JSON-with-a-type convention (and ``ensure_info_log_handler``
plumbing) as the request-logging middleware's ``http_request`` lines.

``json.dumps`` keeps every value escaped inside its JSON string, so the
output is always exactly one line regardless of what the caller passes.
Tag values must stay low-cardinality (operation names, reason codes --
never user ids or raw error text): each distinct tag combination is a
separate series to whoever queries the rates.
"""

import json
import logging
from collections.abc import Mapping
from typing import Final

from imbue.modal_app_kit.log_format import deployed_minds_env_name
from imbue.modal_app_kit.request_logging import ensure_info_log_handler

logger = logging.getLogger(__name__)
# At import (single-threaded under the import lock), so concurrent emitters
# never race the handler installation.
ensure_info_log_handler(logger)

# The `type` field of every record emitted here, the discriminator queries
# filter on.
METRIC_RECORD_TYPE: Final[str] = "metric"


def format_metric_record(metric_name: str, value: float, tags: Mapping[str, str]) -> str:
    record: dict[str, object] = {
        "type": METRIC_RECORD_TYPE,
        "name": metric_name,
        "value": value,
        "tags": dict(tags),
    }
    env_name = deployed_minds_env_name()
    if env_name:
        record["minds_env"] = env_name
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"))


def emit_metric(metric_name: str, value: float, tags: Mapping[str, str]) -> None:
    """Emit one metric record line (usually a counter increment of 1)."""
    logger.info("%s", format_metric_record(metric_name, value, tags))
