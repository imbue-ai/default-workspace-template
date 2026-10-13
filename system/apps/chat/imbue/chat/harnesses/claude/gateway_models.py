"""The model list of a gateway an account points claude at, for the model picker.

An account whose settings.json sets ``ANTHROPIC_BASE_URL`` sends every claude request to that
address instead of Anthropic. A gateway like Concentrate AI serves many more models there than
the four the static catalog offers, and claude takes any of them by id (``/model kimi-k2-6``),
so the picker should offer what the gateway actually has rather than the catalog's Claude-only
set.

The list is fetched from the gateway's own ``GET /v1/models`` with the account's key and the
raw response is kept beside the account's settings, so the picker still has an answer while the
gateway is unreachable, and so the chip can name a model after a restart before anyone opens the
picker again. A gateway that does not serve a model list, or answers with something this does not
understand, offers nothing extra: the static catalog stays the offer.
"""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.chat.errors import ChatAppError
from imbue.chat.harnesses.claude.auth import ANTHROPIC_API_KEY_ENV_VAR
from imbue.chat.harnesses.claude.auth import ANTHROPIC_BASE_URL_ENV_VAR
from imbue.chat.harnesses.model import EffortChoice
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.model import match_option
from imbue.imbue_common.frozen_model import FrozenModel

# The raw `/v1/models` answer, kept in the account folder it was fetched for.
GATEWAY_MODELS_FILE_NAME: Final = "gateway_models.json"

_FETCH_TIMEOUT_SECONDS: Final = 10.0

# The cheapest real request: one token back from a small model.
_KEY_CHECK_MODEL: Final = "claude-haiku-4-5-20251001"
_REFUSED_STATUSES: Final = frozenset({401, 403})


class GatewayModelListError(ChatAppError, ValueError):
    """The gateway answered its model list with something that is not one."""


class Gateway(FrozenModel):
    """Where an account's claude requests go, and the key they carry."""

    base_url: str
    api_key: str


def read_gateway(account_dir: Path) -> Gateway | None:
    """The gateway the account at ``account_dir`` points claude at, or None for Anthropic itself."""
    settings_path = account_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        return None
    env = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(env, dict):
        return None
    base_url = env.get(ANTHROPIC_BASE_URL_ENV_VAR)
    api_key = env.get(ANTHROPIC_API_KEY_ENV_VAR)
    if not isinstance(base_url, str) or not base_url or not isinstance(api_key, str) or not api_key:
        return None
    return Gateway(base_url=base_url.rstrip("/"), api_key=api_key)


def fetch_gateway_models_raw(gateway: Gateway) -> dict[str, Any]:
    """The gateway's raw ``GET /v1/models`` answer. Raises ``OSError``/``ValueError`` on any failure."""
    request = urllib.request.Request(
        f"{gateway.base_url}/v1/models",
        headers={"x-api-key": gateway.api_key, "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        parsed = json.loads(response.read())
    if not isinstance(parsed, dict):
        raise GatewayModelListError("model list is not a JSON object")
    return parsed


def gateway_accepts_key(gateway: Gateway) -> bool | None:
    """Whether the gateway takes the key, or None when the check could not tell."""
    # The model list is public on the gateway, so only a message request tests the key.
    body = json.dumps(
        {"model": _KEY_CHECK_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    try:
        request = urllib.request.Request(
            f"{gateway.base_url}/v1/messages",
            data=body,
            headers={
                "x-api-key": gateway.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code in _REFUSED_STATUSES else None
    # A garbled answer (http.client's errors are not OSErrors) or an unusable address says
    # nothing about the key either, and raising would cost the user the sign-in for it.
    except (OSError, http.client.HTTPException, ValueError):
        return None


def _is_offered_on_claude_code(raw_model: Mapping[str, Any]) -> bool:
    """False only when the gateway says outright the model does not work with Claude Code.

    Concentrate tags each model with its Claude Code compatibility; a model it marks unsupported
    would give the user a chat that fails on its first turn. A gateway that tags nothing (or a
    model it has not tagged) is taken at its word that the model exists.
    """
    integrations = raw_model.get("integrations")
    if not isinstance(integrations, dict) or "claude_code" not in integrations:
        return True
    claude_code = integrations.get("claude_code")
    return not (isinstance(claude_code, dict) and claude_code.get("supported") is False)


def gateway_options(
    raw: Mapping[str, Any], catalog_options: tuple[ModelOption, ...], efforts: tuple[EffortChoice, ...]
) -> tuple[ModelOption, ...]:
    """The picker options a gateway's raw model list means: the whole catalog, then the gateway's own.

    The catalog's Claude models keep their place and their ``[1m]`` aliases, because the gateway
    serves Claude too and those aliases are what keep the 1M window. Its hidden models come along
    too (the picker does not show them), so a chip on one still names it. Every other model the
    gateway lists is offered by its own id, with ``efforts`` (claude's own set, since claude keeps
    and reports its effort setting whatever the model -- an option without one would never match
    a live read) and without fast mode, which is an Opus capability. A gateway id the catalog
    already matches (a Claude model, offered or hidden) is not offered twice.
    """
    data = raw.get("data")
    if not isinstance(data, list):
        return catalog_options
    extra: list[ModelOption] = []
    seen: set[str] = set()
    for raw_model in data:
        if not isinstance(raw_model, dict):
            continue
        model_id = raw_model.get("id") or raw_model.get("slug")
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        if not _is_offered_on_claude_code(raw_model):
            continue
        if match_option(ModelIdentity(model_id=model_id, effort=None, fast=False), catalog_options) is not None:
            continue
        seen.add(model_id)
        label = raw_model.get("display_name") or raw_model.get("name") or model_id
        extra.append(
            ModelOption(
                id=model_id,
                label=str(label),
                efforts=efforts,
                supports_fast=False,
                harness_reported_model_id=model_id,
            )
        )
    return catalog_options + tuple(extra)


def gateway_options_for_account(
    account_dir: Path,
    catalog_options: tuple[ModelOption, ...],
    efforts: tuple[EffortChoice, ...],
    fetch: Callable[[Gateway], dict[str, Any]] = fetch_gateway_models_raw,
) -> tuple[ModelOption, ...] | None:
    """The options an account's gateway offers: None for an account on Anthropic itself.

    Fetched fresh and written through to the account folder; a failed fetch falls back to the
    last list written there, and to the catalog when there is none, so the picker is never empty.
    """
    gateway = read_gateway(account_dir)
    if gateway is None:
        return None
    cache_path = account_dir / GATEWAY_MODELS_FILE_NAME
    try:
        raw = fetch(gateway)
    except (OSError, ValueError) as e:
        logger.warning("Could not fetch the model list from {}: {}", gateway.base_url, e)
        return cached_gateway_options(account_dir, catalog_options, efforts) or catalog_options
    try:
        cache_path.write_text(json.dumps(raw) + "\n")
    except OSError as e:
        logger.warning("Could not keep the model list at {}: {}", cache_path, e)
    return gateway_options(raw, catalog_options, efforts)


def cached_gateway_options(
    account_dir: Path, catalog_options: tuple[ModelOption, ...], efforts: tuple[EffortChoice, ...]
) -> tuple[ModelOption, ...]:
    """The options from the last model list kept for this account, or empty when there is none."""
    try:
        raw = json.loads((account_dir / GATEWAY_MODELS_FILE_NAME).read_text())
    except (OSError, ValueError):
        return ()
    return gateway_options(raw, catalog_options, efforts) if isinstance(raw, dict) else ()
