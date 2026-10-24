"""``usage-collector``: the supervised process that owns the workspace's usage store.

About once a second it reads what was appended to every usage event file in
the workspace (see ``event_files``); hourly it prunes raw events past
retention from the store (they stay in the files). It serves the query API on
127.0.0.1.
"""

import itertools
import os
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from loguru import logger
from werkzeug.serving import make_server
from workspace_usage.context import read_workspace_id
from workspace_usage.emit import events_dir, usage_data_dir

from usage_collector.event_files import PassResult, event_files, follow_event_files
from usage_collector.feed import UsageFeed
from usage_collector.ingest import Ingestor
from usage_collector.server import create_app
from usage_collector.status import IngestStatus
from usage_collector.store import UsageStore

COLLECTOR_PORT: Final[int] = 8795
DATABASE_FILENAME: Final[str] = "usage.db"
HOST_DIR_ENV_VAR: Final[str] = "MNGR_HOST_DIR"
_PASS_SECONDS: Final[float] = 1.0
_PRUNE_EVERY_PASSES: Final[int] = 3600


def host_dir(environ: Mapping[str, str]) -> Path | None:
    configured = environ.get(HOST_DIR_ENV_VAR, "").strip()
    return Path(configured) if configured else None


def run_pass(
    ingestor: Ingestor, store: UsageStore, environ: Mapping[str, str]
) -> PassResult:
    """One round of reading what every event file gained."""
    paths = event_files(host_dir(environ), events_dir(environ))
    return follow_event_files(
        paths,
        store,
        ingestor.ingest,
        read_workspace_id(environ),
        ingestor.observe_limits,
    )


def run_maintenance(
    ingestor: Ingestor,
    store: UsageStore,
    environ: Mapping[str, str],
    status: IngestStatus,
    stop: threading.Event,
) -> None:
    """Run a pass about once a second, and prune hourly, until ``stop`` is set.

    A pass that fails on the store, the disk or a bad value (a full disk, a
    corrupt store, an unreadable timestamp) counts as incomplete, so
    ``/health`` stops reporting the store as caught up, and the next pass
    retries. A lasting failure is logged once, and again only after it clears.
    """
    last_failure: str | None = None
    for passes in itertools.count():
        try:
            status.record_pass(run_pass(ingestor, store, environ).is_complete)
            if passes % _PRUNE_EVERY_PASSES == 0:
                pruned = store.prune(datetime.now(timezone.utc))
                if pruned:
                    logger.info(
                        "Pruned {} raw events past retention from the store", pruned
                    )
            last_failure = None
        except (sqlite3.Error, OSError, ValueError) as e:
            status.record_pass(False)
            failure = f"{type(e).__name__}: {e}"
            if failure != last_failure:
                logger.exception("A usage-collector pass failed; retrying each pass")
            last_failure = failure
        if stop.wait(_PASS_SECONDS):
            return


def main() -> None:
    environ = dict(os.environ)
    store = UsageStore(usage_data_dir(environ) / DATABASE_FILENAME)
    feed = UsageFeed()
    ingestor = Ingestor(store, feed)
    status = IngestStatus()
    app = create_app(store, feed, status)
    stop = threading.Event()
    maintenance = threading.Thread(
        target=run_maintenance,
        args=(ingestor, store, environ, status, stop),
        name="usage-maintenance",
        daemon=True,
    )
    maintenance.start()
    server = make_server("127.0.0.1", COLLECTOR_PORT, app, threaded=True)
    logger.info("usage-collector listening on 127.0.0.1:{}", COLLECTOR_PORT)
    try:
        server.serve_forever()
    finally:
        stop.set()
        feed.close()
        maintenance.join(timeout=10)
        store.close()
