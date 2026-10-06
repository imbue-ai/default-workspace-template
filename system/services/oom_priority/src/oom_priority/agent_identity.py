"""Resolve an agent's priority class -- worker, primary (services), a spare chat
agent, or a plain user-created chat -- from the host's agent records.

mngr writes one record per agent at ``$MNGR_HOST_DIR/agents/<id>/data.json``
carrying the agent ``name`` and a ``labels`` dict. The agent-creation paths label
worker creations ``agent_created=true`` and user-facing creations
``user_created=true``; the workspace's own services agent additionally carries
``is_primary=true``, and a spare the chat app starts ahead of the next new chat
``chat_spare=true`` until a chat takes it. This maps a name to the right priority
band.

An agent we cannot classify is *not* primary, *not* a chat, and *not* a worker,
so it falls through to the least-protected agent tier (the worker band): we must
not shield an agent we cannot identify. The primary (services) agent never runs
the launch wrapper -- its window-0 command is ``sleep infinity`` -- so this
fallback can never make the workspace's services agent expendable.

Stdlib-only (see ``paths``): imported by the agent-tagging Claude hook under a
plain ``python3``.
"""

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

# The labels the agent-creation paths stamp (see the module docstring).
PRIMARY_LABEL: Final[str] = "is_primary"
CHAT_LABEL: Final[str] = "user_created"
WORKER_LABEL: Final[str] = "agent_created"
SPARE_LABEL: Final[str] = "chat_spare"


def _record_for_agent(agent_name: str) -> dict | None:
    """Return the ``data.json`` record mngr keeps for ``agent_name``, or None when
    the host records are unavailable or the agent is not found."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if not host_dir:
        return None
    agents_dir = Path(host_dir) / "agents"
    if not agents_dir.is_dir():
        return None
    for agent_dir in agents_dir.iterdir():
        data_path = agent_dir / "data.json"
        if not data_path.exists():
            continue
        try:
            data = json.loads(data_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("name") == agent_name:
            return data
    return None


def _labels_for_agent(agent_name: str) -> dict | None:
    """Return the ``labels`` dict recorded for ``agent_name``, or None.

    None when the host records are unavailable, the agent is not found, or its
    record carries no ``labels`` dict -- callers treat that as "unclassified" and
    fall back to the protected user-agent band.
    """
    data = _record_for_agent(agent_name)
    labels = None if data is None else data.get("labels")
    return labels if isinstance(labels, dict) else None


def agent_created_at(agent_name: str) -> datetime | None:
    """When mngr created the agent now named ``agent_name`` (its ``create_time``).

    None when the record is unavailable, or carries no parseable time. Tells a
    record about an earlier agent of the same name from one about this agent.
    """
    data = _record_for_agent(agent_name)
    create_time = None if data is None else data.get("create_time")
    if not isinstance(create_time, str):
        return None
    try:
        return datetime.fromisoformat(create_time)
    except ValueError:
        return None


def is_label_true(labels: Mapping[str, object], label: str) -> bool:
    """Whether ``labels`` sets ``label`` to true (mngr stores label values as strings)."""
    return str(labels.get(label, "")).lower() == "true"


def _has_true_label(agent_name: str, label: str) -> bool:
    labels = _labels_for_agent(agent_name)
    if labels is None:
        return False
    return is_label_true(labels, label)


def is_worker_agent(agent_name: str) -> bool:
    """Whether ``agent_name`` carries the ``agent_created=true`` label.

    Returns False when the host records are unavailable or the agent is not
    found, so the caller falls back to the protected user-agent band.
    """
    return _has_true_label(agent_name, WORKER_LABEL)


def is_chat_agent(agent_name: str) -> bool:
    """Whether ``agent_name`` carries the ``user_created=true`` label.

    A chat is a user-facing agent (created through the UI or an equivalent
    user_created path). It launches at the most-expendable chat band and the
    system_interface prioritizer pulls it toward the protected floor as the user
    engages with it. Returns False when the record is unavailable or the agent is
    not found, so an unclassifiable agent is treated as least-protected rather
    than given a chat's engagement-based protection.
    """
    return _has_true_label(agent_name, CHAT_LABEL)


def is_spare_agent(agent_name: str) -> bool:
    """Whether ``agent_name`` carries the ``chat_spare=true`` label.

    A spare is a chat agent the chat app started ahead of the next new chat, which
    no one uses yet; the chat app sets the label to false when a chat takes it.
    Returns False when the record is unavailable or the agent is not found.
    """
    return _has_true_label(agent_name, SPARE_LABEL)


def is_primary_agent(agent_name: str) -> bool:
    """Whether ``agent_name`` is the workspace's primary (services) agent.

    The primary agent runs the workspace's supervised services; shedding it would
    tear those down and make the workspace report a broken state, so it is pinned
    to the never-shed primary band. Returns False when the record is unavailable
    or unclassified, so only an agent explicitly labelled ``is_primary=true`` is
    ever pinned.
    """
    return _has_true_label(agent_name, PRIMARY_LABEL)
