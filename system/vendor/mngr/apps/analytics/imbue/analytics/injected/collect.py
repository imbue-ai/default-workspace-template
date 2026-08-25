# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pydantic==2.13.4",
#     "presidio-analyzer==2.2.363",
#     "en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl",
# ]
# ///
"""The injected collection entrypoint (runs INSIDE an explorer workspace).

The collection runner writes this file (with the ``imbue.analytics.injected``
modules beside it) into ``data/.imbue/analytics/`` on every run and executes
it there via ``uv run --script`` -- the script's dependency environment is
resolved from the header above (cached in the workspace after the first run),
never from the monorepo, so the file you can read in the workspace is exactly
the code that ran.

The script's stdout is the one output channel: a multiplexed JSONL stream of
``{"source": ..., "record": ...}`` lines ending in a ``run_summary`` line.
Diagnostics go to stderr only; no record payload is ever written to stderr.
All transcript redaction (structural strip, secret scanning, Presidio PII
removal) happens here, inside the workspace, before anything is emitted.
"""

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from imbue.analytics.injected import workspace_feeds
from imbue.analytics.injected import workspace_redaction

logger = logging.getLogger("analytics_collect")

# Entity types scrubbed from message text, per the redaction contract
# (LOCATION covers physical addresses; NRP and the other defaults are out of
# scope in v1). Replacements read ``[REDACTED_<ENTITY_TYPE>]``.
_PII_ENTITIES = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "PERSON",
    "LOCATION",
    "IP_ADDRESS",
    "CREDIT_CARD",
)

# The small English model is pinned as a wheel in the script header, so the
# first run's cost is the uv environment resolution, not a model download.
_SPACY_MODEL_NAME = "en_core_web_sm"


class _PresidioScrubber(BaseModel):
    """Holds a loaded Presidio analyzer and scrubs one text at a time."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    analyzer: AnalyzerEngine

    def scrub_text(self, text: str) -> str:
        results = self.analyzer.analyze(text=text, entities=list(_PII_ENTITIES), language="en")
        spans = [(result.start, result.end, result.entity_type) for result in results]
        return workspace_redaction.replace_pii_spans(text, spans)


def build_presidio_scrubber() -> Callable[[str], str]:
    """Build the per-text PII scrubber; the engine load is timed and logged (stderr)."""
    load_started = time.monotonic()
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": _SPACY_MODEL_NAME}],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    logger.info("Loaded the Presidio analyzer in %.1fs", time.monotonic() - load_started)
    return _PresidioScrubber(analyzer=analyzer).scrub_text


def emit_protocol_line(line_payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(line_payload, sort_keys=True) + "\n")


class _RunState(BaseModel):
    """Mutable per-run accumulators shared by the feed loop."""

    record_count_by_source: dict[str, int] = Field(default_factory=dict)
    cursor_by_source: dict[str, str] = Field(default_factory=dict)
    error_by_source: dict[str, str] = Field(default_factory=dict)
    read_bytes: int = Field(default=0)


def _emit_feed(
    state: _RunState,
    source: str,
    records: Sequence[dict[str, Any]],
    cursor: dict[str, Any],
    read_bytes: int,
    emit_line: Callable[[dict[str, Any]], None],
) -> None:
    for record in records:
        emit_line({"source": source, "record": record})
    state.record_count_by_source[source] = len(records)
    state.cursor_by_source[source] = json.dumps(cursor, sort_keys=True)
    state.read_bytes += read_bytes


def run_collection(
    workspace_root: Path,
    host_dir: Path,
    run_id: str,
    script_version: str,
    cursor_by_source: dict[str, dict[str, Any]],
    budget_bytes: int,
    # Batch secret scanner + per-text PII scrubber, injected so tests never
    # need the real binaries or model. Production: workspace_redaction.
    # scan_texts_for_secret_lines and build_presidio_scrubber().
    scan_texts: Callable[[Sequence[str]], list[set[int]]],
    scrub_pii: Callable[[str], str],
    emit_line: Callable[[dict[str, Any]], None],
) -> _RunState:
    """Read every feed, redact transcripts, and emit the multiplexed stream.

    A failing feed is recorded in the summary and skipped (its cursor is not
    advanced); the other feeds still run. The budget drains in feed order,
    transcripts first.
    """
    state = _RunState()
    remaining_budget = budget_bytes

    # Transcripts: read raw, redact in full, and only then emit. If redaction
    # cannot be guaranteed (scanner missing/broken), nothing is emitted and
    # the cursor stays put.
    try:
        transcript_output = workspace_feeds.read_transcript_feed(
            host_dir, cursor_by_source.get(workspace_feeds.TRANSCRIPTS_SOURCE, {}), remaining_budget
        )
        redacted = workspace_redaction.redact_transcript_records(
            list(transcript_output.records), scan_texts, scrub_pii
        )
        _emit_feed(
            state,
            workspace_feeds.TRANSCRIPTS_SOURCE,
            redacted.records,
            transcript_output.cursor,
            transcript_output.read_bytes,
            emit_line,
        )
        remaining_budget -= transcript_output.read_bytes
        if redacted.dropped_record_count:
            logger.info("Dropped %d transcript records with unknown shapes", redacted.dropped_record_count)
    except (workspace_feeds.WorkspaceFeedError, workspace_redaction.RedactionError, OSError) as e:
        state.error_by_source[workspace_feeds.TRANSCRIPTS_SOURCE] = str(e)[:500]
        logger.warning("Transcript feed failed (nothing emitted, cursor unchanged): %s", e)

    # The remaining feeds carry no message content and need no redaction
    # beyond their own source-side field drops.
    plain_feeds: list[tuple[str, Callable[[dict[str, Any], int], workspace_feeds.FeedOutput]]] = [
        (
            workspace_feeds.CLIENT_ACTIVITY_SOURCE,
            lambda cursor, budget: workspace_feeds.read_client_activity_feed(host_dir, cursor, budget),
        ),
        (
            workspace_feeds.SERVICES_SOURCE,
            lambda cursor, budget: workspace_feeds.read_registration_feed(host_dir, "services", cursor, budget),
        ),
        (
            workspace_feeds.SERVERS_SOURCE,
            lambda cursor, budget: workspace_feeds.read_registration_feed(host_dir, "servers", cursor, budget),
        ),
        (
            workspace_feeds.GIT_NUMSTAT_SOURCE,
            lambda cursor, budget: workspace_feeds.read_git_numstat_feed(workspace_root, cursor, budget),
        ),
        (
            workspace_feeds.WORKSPACE_STATE_SOURCE,
            lambda cursor, budget: workspace_feeds.read_workspace_state_snapshot(workspace_root, host_dir, run_id),
        ),
    ]
    for source, read_feed in plain_feeds:
        try:
            feed_output = read_feed(cursor_by_source.get(source, {}), remaining_budget)
        except (workspace_feeds.WorkspaceFeedError, OSError) as e:
            state.error_by_source[source] = str(e)[:500]
            logger.warning("Feed %s failed (cursor unchanged): %s", source, e)
            continue
        _emit_feed(state, source, feed_output.records, feed_output.cursor, feed_output.read_bytes, emit_line)
        remaining_budget -= feed_output.read_bytes

    emit_line(
        {
            "source": workspace_feeds.RUN_SUMMARY_SOURCE,
            "record_count_by_source": state.record_count_by_source,
            "cursor_by_source": state.cursor_by_source,
            "error_by_source": state.error_by_source,
            "read_bytes": state.read_bytes,
            "is_budget_exhausted": remaining_budget <= 0,
            "script_version": script_version,
        }
    )
    return state


def _load_cursors(cursors_file: Path) -> dict[str, dict[str, Any]]:
    if not cursors_file.is_file():
        return {}
    try:
        parsed = json.loads(cursors_file.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Cursors file is unreadable; starting every feed from scratch")
        return {}
    if not isinstance(parsed, dict):
        return {}
    cursors: dict[str, dict[str, Any]] = {}
    for source, raw_cursor in parsed.items():
        if isinstance(raw_cursor, dict):
            cursors[str(source)] = raw_cursor
        else:
            pass
    return cursors


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Imbue analytics in-workspace collection")
    parser.add_argument("--run-id", required=True, help="Server-minted id for this collection run")
    parser.add_argument("--script-version", required=True, help="Content hash the runner stamped for this script")
    parser.add_argument("--workspace-root", required=True, help="The workspace repo root (holds data/)")
    parser.add_argument("--host-dir", required=True, help="The mngr host dir (holds agents/)")
    parser.add_argument("--cursors-file", required=True, help="JSON file of per-feed cursors from the runner")
    parser.add_argument("--budget-bytes", required=True, type=int, help="Pre-redaction input budget for this run")
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root)
    host_dir = Path(args.host_dir)
    workspace_feeds.write_readme_if_absent(workspace_root)
    state = run_collection(
        workspace_root=workspace_root,
        host_dir=host_dir,
        run_id=args.run_id,
        script_version=args.script_version,
        cursor_by_source=_load_cursors(Path(args.cursors_file)),
        budget_bytes=args.budget_bytes,
        scan_texts=workspace_redaction.scan_texts_for_secret_lines,
        scrub_pii=build_presidio_scrubber(),
        emit_line=emit_protocol_line,
    )
    workspace_feeds.append_collections_audit_record(
        workspace_root=workspace_root,
        run_id=args.run_id,
        script_version=args.script_version,
        record_count_by_source=state.record_count_by_source,
        error_by_source=state.error_by_source,
        read_bytes=state.read_bytes,
    )


if __name__ == "__main__":
    main()
