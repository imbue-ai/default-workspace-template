"""Copyable helper for calling the user's local model from a service or a routine.

COPY THIS FILE into your service and adapt it -- it is a self-contained reference
snippet, not something to import from the skill's package. It needs nothing but
the standard library.

A workspace can run a completion on a model on the user's own computer. Where
that model answers is read from one of two places, in this order:

- **The record the ``set-up-local-model`` skill writes**,
  ``data/.state/local_model/endpoint.json`` in the workspace: the address the
  model answers on from inside this workspace, the model to ask for, its window,
  and what stops it reasoning. This is what a refresh runs on, and it depends on
  no chat.
- **A chat's Local model connection**, in a workspace whose user signed a chat
  in to the "Local model" provider: an account under ``~/.minds/accounts/<id>/``
  on the ``local-model`` lane. ``models.json`` holds the server's address and the
  models it offers a chat, ``local_models.json`` which of them have passed the
  chat's tool-call check, and ``auth.json`` the server's key when it asks for one.

So code that should run on the local model names no address of its own: the
address differs per workspace, and changes when the model is set up again.
``read_local_model_endpoint`` does the read; ``local_model_completion`` makes one
completion with it.

With neither, the model has not been set up. Follow the ``set-up-local-model``
skill, which asks the user for one approval in Imbue Studio; never send them to
a chat's provider menu to make code work.

Three things differ from calling a hosted model, and a caller has to plan for
each:

- **The model may not be there.** It runs on the user's computer, so it answers
  only while that computer is awake, the model server is running, and (for a
  workspace elsewhere) Imbue Studio is open. A scheduled job will meet nights
  when it does not. ``local_model_completion`` looks before it calls: when
  nothing answers at the recorded address it asks Imbue Studio once to connect
  the workspace to the computer again, and raises ``LocalModelUnreachableError``
  when that does not bring the model back. Skip the run and say so, rather than
  failing loudly or falling back to a paid model the user did not ask for.
- **It answers one request at a time, slowly.** A local server queues what it
  is sent, so a thread pool buys nothing; send items one after another, and
  expect seconds to minutes each. The first request after a pause also waits
  for the model to load.
- **Its window is small.** ``LocalModelEndpoint.context_window`` is the whole
  budget for the prompt and the answer together: 16,384 tokens as the setup
  leaves it, 40,960 or more on a chat's connection. Send one item per call, not
  a batch.

There is no cost to measure: the model runs on the user's own hardware.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

# The record lives with the workspace's machine state, at the workspace's fixed root.
_ENDPOINT_FILE_ENV_VAR = "MINDS_LOCAL_MODEL_ENDPOINT_FILE"
_DEFAULT_ENDPOINT_FILE = "/home/user/workspace/data/.state/local_model/endpoint.json"

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
# Room for a round trip to the user's home through the tunnel, and short beside the
# call it comes before.
_PROBE_TIMEOUT_SECONDS = 5.0
# Opening the connection may have to reach into the workspace first.
_RECONNECT_TIMEOUT_SECONDS = 45.0
# The port Ollama listens on, on the user's computer: the server a record is of.
_OLLAMA_PORT = 11434

_SET_UP_INSTRUCTION = (
    "Follow the set-up-local-model skill: it asks the user to allow it in Imbue Studio, which installs "
    "what is missing on their computer, and records where the model answers."
)


class LocalModelError(RuntimeError):
    """A call to the local model could not be made, or came back unusable."""


class LocalModelNotConnectedError(LocalModelError):
    """The workspace has no local model set up, or not the one asked for."""


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


def _other_model_instruction(model: str) -> str:
    return (
        f"To call {model} from code, set it up with the set-up-local-model skill (its `start --model {model}`, "
        f"then `verify --model {model}`)."
    )


def _read_record() -> dict[str, Any] | None:
    """What the set-up-local-model skill recorded, or None when it has recorded nothing to call."""
    record = _read_json(
        os.environ.get(_ENDPOINT_FILE_ENV_VAR) or _DEFAULT_ENDPOINT_FILE
    )
    if not isinstance(record, dict):
        return None
    is_callable = all(
        isinstance(record.get(name), str) and record[name]
        for name in ("base_url", "model")
    )
    return record if is_callable else None


def _recorded_endpoint(record: Mapping[str, Any]) -> LocalModelEndpoint:
    context_window = record.get("context_window")
    reasoning_off_effort = record.get("reasoning_off_effort")
    return LocalModelEndpoint(
        base_url=str(record["base_url"]).rstrip("/"),
        api_key="",
        model=str(record["model"]),
        context_window=context_window if isinstance(context_window, int) else 0,
        reasoning_off_effort=reasoning_off_effort
        if isinstance(reasoning_off_effort, str)
        else None,
    )


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
            f"No local model is set up for this workspace. {_SET_UP_INSTRUCTION}"
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


def _account_endpoint(model: str | None) -> LocalModelEndpoint:
    """Where a chat's Local model connection answers: the model it was signed in on, or ``model``.

    Only a model that has passed the chat's tool-call check is returned: one that has
    not been used in a chat may not exist on the server yet.
    """
    account_dir = _local_model_account_dir()
    config = _read_json(os.path.join(account_dir, "models.json"))
    providers = config.get("providers") if isinstance(config, dict) else None
    provider = providers.get(_PROVIDER_ID) if isinstance(providers, dict) else None
    base_url = provider.get("baseUrl") if isinstance(provider, dict) else None
    if not isinstance(provider, dict) or not isinstance(base_url, str) or not base_url:
        raise LocalModelNotConnectedError(
            f"The workspace's local model connection names no server. {_SET_UP_INSTRUCTION}"
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
            f"The workspace's local model connection has no model ready to use. {_SET_UP_INSTRUCTION}"
            if model is None
            else f"The workspace's local model connection has no {model} ready to use. It offers: "
            + (
                ", ".join(
                    sorted({str(each.get("base_id") or each["id"]) for each in usable})
                )
                or "nothing"
            )
            + f". {_other_model_instruction(model)}"
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


def _resolve_endpoint(model: str | None) -> tuple[LocalModelEndpoint, bool]:
    """The endpoint for ``model``, and whether it is the one the set-up-local-model skill recorded."""
    record = _read_record()
    if record is not None and model in (
        None,
        record["model"],
        record.get("base_model"),
    ):
        return _recorded_endpoint(record), True
    try:
        return _account_endpoint(model), False
    except LocalModelNotConnectedError as e:
        if record is None or model is None:
            raise
        raise LocalModelNotConnectedError(
            f"This workspace's local model is {record.get('base_model') or record['model']}, and {model} is "
            f"not set up. {_other_model_instruction(model)}"
        ) from e


def read_local_model_endpoint(model: str | None = None) -> LocalModelEndpoint:
    """Where the workspace's local model answers.

    With no ``model``, the one the ``set-up-local-model`` skill recorded, else the
    one a chat's Local model connection was signed in on. With one, that model by
    its id or by the model it is a context size of (``"qwen3:8b"`` finds the
    recorded ``qwen3:8b-ctx16384``), from the record when it holds it, else from a
    chat's connection, where only a model a chat has run on is offered.

    Raises ``LocalModelNotConnectedError`` when the workspace has no local model
    set up, or not that one; the message says how to set one up.
    """
    return _resolve_endpoint(model)[0]


def _why_unanswered(base_url: str) -> str | None:
    """Why nothing answers HTTP at ``base_url`` right now; None when something does.

    Any answer counts, a refusal included: the question is whether a request would
    reach a server. A tunnel to a computer that is asleep takes a request and never
    answers it, so a completion sent into one waits out its whole timeout.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"{base_url}/models", timeout=_PROBE_TIMEOUT_SECONDS
        ):
            return None
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return str(e)


def _reconnected_base_url() -> str | None:
    """Ask Imbue Studio to connect this workspace to the Ollama on the user's computer again.

    Returns the address the model then answers on here, or None when the app cannot be
    asked or will not. The app restores the connection by itself too, on a timer; asking
    saves a run that lands in between. The call goes through the workspace's gateway as
    the agent this process runs as, under the permission the setup was granted.
    """
    gateway = os.environ.get("LATCHKEY_GATEWAY", "")
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not gateway or not password or not agent_id:
        return None
    headers = {
        "Content-Type": "application/json",
        "X-Latchkey-Gateway-Password": password,
    }
    # A desktop-hosted gateway needs it as the per-agent authorization; another never sets it.
    permissions = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if permissions:
        headers["X-Latchkey-Gateway-Permissions-Override"] = permissions
    request = urllib.request.Request(
        f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/agents/{agent_id}/local-model/tunnel",
        data=json.dumps({"port": _OLLAMA_PORT}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=_RECONNECT_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        return None
    workspace_port = (
        payload.get("workspace_port") if isinstance(payload, dict) else None
    )
    if not isinstance(workspace_port, int):
        return None
    return f"http://127.0.0.1:{workspace_port}/v1"


def _unreachable(base_url: str, reason: object) -> LocalModelUnreachableError:
    return LocalModelUnreachableError(
        f"The model server at {base_url} did not answer ({reason}). The user's computer may be asleep or "
        "offline, the model server may not be installed or running, or Imbue Studio may be closed. An agent "
        "working with the user runs the set-up-local-model skill's check command before telling them anything: "
        "Imbue Studio can install and start the model server itself."
    )


def _answering_endpoint(
    endpoint: LocalModelEndpoint, is_recorded: bool
) -> LocalModelEndpoint:
    """``endpoint`` at an address that answers now; raises ``LocalModelUnreachableError`` when none does."""
    reason = _why_unanswered(endpoint.base_url)
    if reason is None:
        return endpoint
    # Only for the recorded model, which is an Ollama's: a chat's connection may lead to another
    # server, and this would point the workspace's one tunnel away from it.
    reconnected = _reconnected_base_url() if is_recorded else None
    if reconnected is not None and _why_unanswered(reconnected) is None:
        return replace(endpoint, base_url=reconnected)
    raise _unreachable(endpoint.base_url, reason)


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
    endpoint = _answering_endpoint(*_resolve_endpoint(model))
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
        raise _unreachable(endpoint.base_url, e) from e
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
