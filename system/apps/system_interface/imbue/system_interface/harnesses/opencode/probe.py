"""Probe a running opencode agent's server for the model it started with.

opencode is a client-server harness: ``opencode serve`` (headless) resolves the
model from ``opencode.json`` / the authenticated provider default, and mngr's launch
script records the bound port at ``<agent_state_dir>/opencode_server_port``. Because
mngr pins no model (opencode is many-provider / many-auth), the startup model is not
in a static config file we can read -- it is whatever the server resolved. So we ask
the server directly, before any turn:

    GET /config             -> {"model": "provider/model", ...}   (only when pinned)
    GET /config/providers   -> {"default": {provider: model}, ...}

``config.model`` wins when present; otherwise the provider default is used (the user
must be authed with at least one provider, so ``default`` is non-empty). Returns None
when the server is not reachable -- not started yet, stopped, or on a remote host.

This is the pre-turn-1 source for the model bar. The live selection after a turn
comes from ``opencode_model_state.json`` (written by the lifecycle plugin), which
also covers a model/variant the user switches mid-session.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from loguru import logger

_PORT_MARKER = "opencode_server_port"
_TIMEOUT_SECONDS = 3.0


def read_server_port(agent_state_dir: Path) -> int | None:
    """The opencode server's bound port from the marker, or None when absent/blank/bad."""
    try:
        raw = (agent_state_dir / _PORT_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def startup_model_from_config(config: dict, providers: dict) -> str | None:
    """The ``provider/model`` the server starts with, from the two config responses.

    ``config.model`` (a pinned model) wins; otherwise the first entry of the
    ``providers.default`` map (provider -> model). Pure, so it is testable without a
    live server. Returns None when neither resolves.
    """
    model = config.get("model")
    if isinstance(model, str) and model:
        return model
    default = providers.get("default")
    if isinstance(default, dict):
        for provider, model_id in default.items():
            if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
                return f"{provider}/{model_id}"
    return None


def _get_json(port: int, path: str) -> dict | None:
    """GET one JSON object from the local opencode server, or None on any failure."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 -- fixed localhost URL
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # URLError / HTTPError / socket timeout are all OSError subclasses.
        logger.debug("opencode probe {} failed: {}", url, exc)
        return None
    return payload if isinstance(payload, dict) else None


def probe_startup_model(agent_state_dir: Path) -> str | None:
    """The ``provider/model`` a running opencode agent started with, or None when the
    server is not reachable (not started, stopped, or remote)."""
    port = read_server_port(agent_state_dir)
    if port is None:
        return None
    config = _get_json(port, "/config")
    providers = _get_json(port, "/config/providers")
    if config is None and providers is None:
        return None
    return startup_model_from_config(config or {}, providers or {})
