"""Copyable helper for calling the user's local model from a service or a routine.

COPY THIS FILE into your service and adapt it -- it is a self-contained reference
snippet, not something to import from the skill's package. It needs nothing but
the standard library.

A workspace can be connected to a model the user hosts on their own computer
(Ollama, LM Studio, llama.cpp): the "Local model" provider in the chat's provider
chooser. That connection is an account under ``~/.minds/accounts/<id>/`` on the
``local-model`` lane, and it holds everything a call needs:

- ``models.json``: the server's OpenAI-compatible address (for a workspace that
  is not on the user's computer, a loopback port the Imbue Studio app tunnels to
  the server) and the models it offers a chat;
- ``local_models.json``: which of those models has passed the tool-call check,
  and which model each is a context size of;
- ``auth.json``: the server's key, when it asks for one.

So code that should run on the local model reads the account, not an address of
its own: the address differs per workspace, and changes when the user signs in
again. ``read_local_model_endpoint`` does that read; ``local_model_completion``
makes one completion with it.

Three things differ from calling a hosted model, and a caller has to plan for
each:

- **The model may not be there.** It runs on the user's computer, so it answers
  only while that computer is awake, the model server is running, and (for a
  workspace elsewhere) Imbue Studio is open. A scheduled job will meet nights
  when it does not. ``LocalModelUnreachableError`` is that case: skip the run
  and say so, rather than failing loudly or falling back to a paid model the
  user did not ask for.
- **It answers one request at a time, slowly.** A local server queues what it
  is sent, so a thread pool buys nothing; send items one after another, and
  expect seconds to minutes each. The first request after a pause also waits
  for the model to load.
- **Its window is small.** ``LocalModelEndpoint.context_window`` is the whole
  budget for the prompt and the answer together, often 40,960 tokens. Send one
  item per call, not a batch.

There is no cost to measure: the model runs on the user's own hardware.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_LOCAL_MODEL_LANE = "local-model"
# pi's id for the provider: the key of its entry in models.json and in auth.json.
_PROVIDER_ID = "local-model"
# What auth.json holds for a server that asks for no key.
_PLACEHOLDER_API_KEY = "local"
_ACCOUNTS_ROOT_ENV_VAR = "MINDS_ACCOUNTS_ROOT"

# A model that is not loaded yet is read off disk before it answers, and a long
# answer on a laptop takes minutes.
_DEFAULT_TIMEOUT_SECONDS = 600.0
_DEFAULT_MAX_TOKENS = 1024


class LocalModelError(RuntimeError):
    """A call to the local model could not be made, or came back unusable."""


class LocalModelNotConnectedError(LocalModelError):
    """The workspace has no local model connected, or not the one asked for."""


class LocalModelUnreachableError(LocalModelError):
    """The model server did not answer: the user's computer is asleep or offline, or the server is not running."""


@dataclass(frozen=True)
class LocalModelEndpoint:
    """Where the workspace's local model answers, and what to ask it for."""

    base_url: str
    # "" for a server that asks for no key.
    api_key: str
    # The model's id on the server. On Ollama this names one context size of a model.
    model: str
    # The window the prompt and the answer share, in tokens.
    context_window: int
    # The `reasoning_effort` that stops a reasoning model thinking before it answers; None for a
    # model that does not reason, or a server not known to take one.
    reasoning_off_effort: str | None


@dataclass(frozen=True)
class LocalModelResult:
    """One completion from the local model."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_seconds: float


def _accounts_root() -> str:
    return os.environ.get(_ACCOUNTS_ROOT_ENV_VAR) or os.path.join(
        os.path.expanduser("~"), ".minds", "accounts"
    )


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _local_model_account_dir() -> str:
    """The folder of the workspace's local-model account: the pinned default, else the most recently used, else the oldest."""
    root = _accounts_root()
    index = _read_json(os.path.join(root, "index.json"))
    rows = index.get("accounts") if isinstance(index, dict) else None
    local = [
        row
        for row in rows or []
        if isinstance(row, dict) and row.get("lane") == _LOCAL_MODEL_LANE
    ]
    if not local:
        raise LocalModelNotConnectedError(
            "No local model is connected to this workspace. Ask the user to choose the Local model provider in a "
            "chat's provider menu first."
        )
    by_id = {row.get("id"): row for row in local}
    chosen = (
        by_id.get(index.get("default_account"))
        or by_id.get(index.get("mru"))
        or local[0]
    )
    return os.path.join(root, str(chosen.get("id")))


def _candidate_models(
    account_dir: str, provider: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The account's models, the first being the one it was signed in on: each with its id, the model
    it is a size of, its window, whether it passed the tool-call check, and how its reasoning is stopped."""
    kept = _read_json(os.path.join(account_dir, "local_models.json"))
    records = kept.get("records") if isinstance(kept, dict) else None
    if isinstance(records, list) and records:
        return [
            record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("id"), str)
        ]
    # An account signed in before local_models.json was kept names its one model in models.json alone.
    listed = provider.get("models")
    models: list[Any] = listed if isinstance(listed, list) else []
    return [
        {
            "id": model["id"],
            "base_id": model["id"],
            "context_window": model.get("contextWindow"),
            "is_checked": True,
            "reasoning_off_effort": (model.get("thinkingLevelMap") or {}).get("off"),
        }
        for model in models
        if isinstance(model, dict) and isinstance(model.get("id"), str)
    ]


def read_local_model_endpoint(model: str | None = None) -> LocalModelEndpoint:
    """Where the workspace's local model answers.

    With no ``model``, the one the account was signed in on. With one, that model
    by its id or by the model it is a context size of (``"qwen3:8b"`` finds the
    account's ``qwen3:8b`` at whichever size has been used in a chat). Only a
    model that has passed the chat's tool-call check is returned: one that has
    not been used in a chat may not exist on the server yet.

    Raises ``LocalModelNotConnectedError`` when the workspace has no local model
    connected, or not that one.
    """
    account_dir = _local_model_account_dir()
    config = _read_json(os.path.join(account_dir, "models.json"))
    providers = config.get("providers") if isinstance(config, dict) else None
    provider = providers.get(_PROVIDER_ID) if isinstance(providers, dict) else None
    base_url = provider.get("baseUrl") if isinstance(provider, dict) else None
    if not isinstance(provider, dict) or not isinstance(base_url, str) or not base_url:
        raise LocalModelNotConnectedError(
            "The workspace's local model connection names no server."
        )
    usable = [
        each
        for each in _candidate_models(account_dir, provider)
        if each.get("is_checked")
    ]
    wanted = [
        each
        for each in usable
        if model is None or model in (each["id"], each.get("base_id"))
    ]
    if not wanted:
        raise LocalModelNotConnectedError(
            "The workspace's local model connection has no model ready to use."
            if model is None
            else f"The workspace's local model connection has no {model} ready to use. It offers: "
            + (
                ", ".join(
                    sorted({str(each.get("base_id") or each["id"]) for each in usable})
                )
                or "nothing"
            )
            + ". A model becomes ready once a chat has run on it."
        )
    chosen = wanted[0]
    auth = _read_json(os.path.join(account_dir, "auth.json"))
    entry = auth.get(_PROVIDER_ID) if isinstance(auth, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    context_window = chosen.get("context_window")
    reasoning_off_effort = chosen.get("reasoning_off_effort")
    return LocalModelEndpoint(
        base_url=base_url.rstrip("/"),
        api_key=key if isinstance(key, str) and key != _PLACEHOLDER_API_KEY else "",
        model=chosen["id"],
        context_window=context_window if isinstance(context_window, int) else 0,
        reasoning_off_effort=reasoning_off_effort
        if isinstance(reasoning_off_effort, str)
        else None,
    )


def _completion_body(
    endpoint: LocalModelEndpoint,
    prompt: str,
    system: str,
    max_tokens: int,
    is_reasoning_wanted: bool,
    response_format: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": endpoint.model,
        "stream": False,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    # A reasoning model thinks before every answer unless told not to, which for a
    # classify-or-summarize call is most of the wait and of the output budget.
    if not is_reasoning_wanted and endpoint.reasoning_off_effort is not None:
        body["reasoning_effort"] = endpoint.reasoning_off_effort
    if response_format is not None:
        body["response_format"] = dict(response_format)
    return body


def local_model_completion(
    prompt: str,
    *,
    system: str,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    is_reasoning_wanted: bool = False,
    response_format: Mapping[str, Any] | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> LocalModelResult:
    """One completion from the workspace's local model: one prompt, one answer, no tools.

    ``system`` is required: a small model follows a stated task far better than an
    implied one. ``response_format={"type": "json_object"}`` asks for JSON, which
    Ollama and LM Studio both honour. ``is_reasoning_wanted`` lets a reasoning
    model think first (slower; its thinking is not returned).

    Synchronous. Call it for one item at a time: a local server answers requests
    in turn. Raises ``LocalModelNotConnectedError`` when the workspace has no
    local model, ``LocalModelUnreachableError`` when its server does not answer,
    and ``LocalModelError`` for an answer that cannot be used.
    """
    endpoint = read_local_model_endpoint(model)
    body = _completion_body(
        endpoint, prompt, system, max_tokens, is_reasoning_wanted, response_format
    )
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    request = urllib.request.Request(
        f"{endpoint.base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    started_at = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.loads(response.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise LocalModelError(
            f"The model server answered {e.code} for {endpoint.model}: {detail}"
        ) from e
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        raise LocalModelUnreachableError(
            f"The model server at {endpoint.base_url} did not answer ({e}). The user's computer may be asleep or "
            "offline, the model server may not be running, or Imbue Studio may be closed."
        ) from e
    except ValueError as e:
        raise LocalModelError(
            f"The model server at {endpoint.base_url} answered something that is not JSON."
        ) from e
    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or text.strip() == "":
        raise LocalModelError(
            f"{endpoint.model} answered with no text. A reasoning model can spend its whole max_tokens thinking: "
            "raise max_tokens, or leave is_reasoning_wanted off."
        )
    usage = payload.get("usage")
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    completion_tokens = (
        usage.get("completion_tokens") if isinstance(usage, dict) else None
    )
    return LocalModelResult(
        text=text,
        model=endpoint.model,
        input_tokens=prompt_tokens if isinstance(prompt_tokens, int) else 0,
        output_tokens=completion_tokens if isinstance(completion_tokens, int) else 0,
        duration_seconds=time.monotonic() - started_at,
    )
