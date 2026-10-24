"""Recording the model calls of a ``claude -p`` one-shot.

Claude Code writes every call it makes, sub-agents' included, to its session
log. A one-shot runs with a session id chosen up front (``--session-id``,
with session persistence on); once it exits, its calls are read out of that
log with mngr_usage's session-log reader -- the one the Claude agents' usage
poller runs -- and appended to the origin's event file. The log is then
deleted, so a one-shot leaves nothing behind in the account's ``projects/``
directory.

The reader is mngr_usage's standard-library core, so recording imports
nothing of mngr beyond those two modules.
"""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from imbue.mngr_usage.resources import claude_session_calls
from loguru import logger

from workspace_usage.context import (
    ORIGIN_KIND,
    ORIGIN_KIND_UNKNOWN,
    read_usage_context,
    read_workspace_id,
)
from workspace_usage.emit import record_model_call
from workspace_usage.turn import current_turn_id

CLAUDE_HARNESS = "claude"


def new_claude_session_id() -> str:
    return str(uuid4())


def claude_session_args(session_id: str) -> list[str]:
    """The ``claude -p`` arguments that make it log under ``session_id`` (in place of ``--no-session-persistence``)."""
    return ["--session-id", session_id]


def record_claude_one_shot(
    config_dir: Path,
    session_id: str,
    purpose: str | None = None,
    context: Mapping[str, str] | None = None,
    billing_mode: str | None = None,
    environ: Mapping[str, str] | None = None,
    delete_log: bool = True,
    turn_id: str | None = None,
) -> int:
    """Record a finished one-shot's calls; returns how many. Never raises.

    ``context`` defaults to this process's usage context, and ``billing_mode``
    to what the account under ``config_dir`` records (API key, subscription,
    or unknown). ``delete_log=False`` keeps a session the user may resume.
    ``turn_id`` defaults to that of the ``usage_turn`` the one-shot ran in,
    and replaces the turn the session log gives each call.
    """
    env = os.environ if environ is None else environ
    main_log = claude_session_calls.find_session_log(str(config_dir), session_id)
    if main_log is None:
        logger.debug(
            "No session log for the one-shot {} under {}; nothing to record",
            session_id,
            config_dir,
        )
        return 0
    inherited = read_usage_context(env) if context is None else context
    resolved_turn_id = turn_id or current_turn_id()
    attribution: dict[str, str] = {
        "workspace_id": read_workspace_id(env),
        ORIGIN_KIND: inherited.get(ORIGIN_KIND, ORIGIN_KIND_UNKNOWN),
        **{
            key: value
            for key, value in inherited.items()
            if key != ORIGIN_KIND and value
        },
        "harness": CLAUDE_HARNESS,
        **({"purpose": purpose} if purpose else {}),
    }
    mode = billing_mode or claude_session_calls.claude_billing_mode(
        {"CLAUDE_CONFIG_DIR": str(config_dir)}, str(config_dir)
    )
    try:
        events = claude_session_calls.session_log_events(main_log, attribution, mode)
    except OSError as e:
        logger.warning("Could not read the one-shot's session log {}: {}", main_log, e)
        return 0
    for event in events:
        if resolved_turn_id:
            event["attribution"]["turn_id"] = resolved_turn_id
        record_model_call(event, env)
    if delete_log:
        _delete_session_logs(Path(main_log))
    return len(events)


def _delete_session_logs(main_log: Path) -> None:
    try:
        main_log.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not delete the one-shot session log {}: {}", main_log, e)
    shutil.rmtree(main_log.with_suffix(""), ignore_errors=True)
