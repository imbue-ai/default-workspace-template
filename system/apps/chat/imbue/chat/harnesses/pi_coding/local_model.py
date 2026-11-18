"""A locally hosted model as a pi provider: finding its server, checking the model, and the file pi reads.

pi reaches an OpenAI-compatible server (Ollama, LM Studio, llama.cpp) through a custom provider in
`models.json`, beside `auth.json` in its config dir. The provider's key stays in `auth.json` like
every other pi lane's, under :data:`LOCAL_MODEL_PROVIDER_ID`, so `models.json` holds no secret.

Two things a server's own answers cannot be trusted on, both measured against Ollama 0.40 and
pi 0.87.1, are checked here before an account is committed:

* a model that lists tool support may still print its tool calls as text, which pi runs none of
  and reports as a finished turn;
* Ollama loads a model with its own context length (4096 unless configured), whatever window pi
  is told, and silently drops what does not fit -- less than a workspace's own instructions.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import urlsplit

import httpx
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# pi's id for the provider, so also the key in `auth.json` and the prefix of the model's tag.
LOCAL_MODEL_PROVIDER_ID: Final = "local-model"
MODELS_FILENAME: Final = "models.json"
# What `auth.json` holds for a server that asks for no key: pi lists a provider's models only
# once it has some credential for it.
PLACEHOLDER_API_KEY: Final = "local"

DEFAULT_CONTEXT_WINDOW: Final = 32_768
# A workspace's own instructions measure about 12,000 tokens before the first message.
MIN_CONTEXT_WINDOW: Final = 16_384
_MAX_OUTPUT_TOKENS: Final = 8_192

# Hosts a server on the user's own machine can answer on, as a workspace sees them: its own
# loopback, then the names Docker and Lima give the machine that runs the container.
_CANDIDATE_HOSTS: Final = ("127.0.0.1", "host.docker.internal", "host.lima.internal")
_KNOWN_SERVERS: Final = ((11434, "Ollama"), (1234, "LM Studio"), (8080, "llama.cpp"))
_SERVER_NAME_BY_PORT: Final = dict(_KNOWN_SERVERS)

_PROBE_TIMEOUT_SECONDS: Final = 2.0
# A model that is not loaded yet is read off disk before it answers.
_TOOL_CALL_TIMEOUT_SECONDS: Final = 180.0

_TOOL_CALL_REQUEST: Final[dict[str, Any]] = {
    "stream": False,
    "max_tokens": 200,
    "messages": [{"role": "user", "content": "Use the get_weather tool to look up the weather in Paris."}],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Look up the current weather in a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ],
}


class LocalModelError(Exception):
    """A server could not be reached, or its model cannot run a chat here. The message is shown to the user."""


class LocalModelServer(FrozenModel):
    """One server that answered, and the models it lists."""

    base_url: str = Field(description="The server's OpenAI-compatible root, ending in its version path")
    name: str = Field(description="What the server is, as shown: the product on a known port, else its address")
    models: tuple[str, ...] = Field(description="The ids of the models the server lists")


def normalize_base_url(raw: str) -> str:
    """The OpenAI-compatible root for an address as typed: `localhost:1234` means `http://localhost:1234/v1`."""
    text = raw.strip()
    if text == "":
        raise LocalModelError("Enter the server's address.")
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LocalModelError("That is not a server address. Use the form http://host:port.")
    path = parts.path.rstrip("/") or "/v1"
    return f"{parts.scheme}://{parts.netloc}{path}"


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key or PLACEHOLDER_API_KEY}"}


def list_models(client: httpx.Client, base_url: str, api_key: str) -> tuple[str, ...]:
    """The model ids the server at ``base_url`` lists."""
    try:
        response = client.get(f"{base_url}/models", headers=_auth_headers(api_key), timeout=_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise LocalModelError(f"No server answered at {base_url}.") from e
    if response.status_code in (401, 403):
        raise LocalModelError(f"The server at {base_url} refused the request. Check its API key.")
    if response.status_code != 200:
        raise LocalModelError(f"The server at {base_url} answered {response.status_code} when asked for its models.")
    try:
        payload = response.json()
    except ValueError as e:
        raise LocalModelError(f"The server at {base_url} did not answer like an OpenAI-compatible server.") from e
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise LocalModelError(f"The server at {base_url} did not answer like an OpenAI-compatible server.")
    return tuple(
        entry["id"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    )


def describe_server(client: httpx.Client, base_url: str, api_key: str) -> LocalModelServer:
    """The server at ``base_url`` with its models, named for the product on its port when that is a known one."""
    models = list_models(client, base_url, api_key)
    parts = urlsplit(base_url)
    name = _SERVER_NAME_BY_PORT.get(parts.port or 0, parts.netloc)
    return LocalModelServer(base_url=base_url, name=name, models=models)


def _probe_candidate(client: httpx.Client, base_url: str) -> LocalModelServer | None:
    try:
        return describe_server(client, base_url, "")
    except LocalModelError as e:
        logger.debug("No local model server at {}: {}", base_url, e)
        return None


def discover_servers(client: httpx.Client) -> tuple[LocalModelServer, ...]:
    """Every known server that answers on a host the user's own machine can be reached at.

    One server per port: a port that answers on the workspace's own loopback is the one a chat
    would reach, so the same port on a further host is not offered beside it.
    """
    candidates = [f"http://{host}:{port}/v1" for port, _ in _KNOWN_SERVERS for host in _CANDIDATE_HOSTS]
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        answers = list(pool.map(_probe_candidate, repeat(client), candidates))
    by_port: dict[int | None, LocalModelServer] = {}
    for server in answers:
        if server is not None:
            by_port.setdefault(urlsplit(server.base_url).port, server)
    return tuple(by_port.values())


def check_tool_call(client: httpx.Client, base_url: str, api_key: str, model_id: str) -> None:
    """Ask the model for one tool call, and raise unless it answers with a structured one."""
    try:
        response = client.post(
            f"{base_url}/chat/completions",
            json={**_TOOL_CALL_REQUEST, "model": model_id},
            headers=_auth_headers(api_key),
            timeout=_TOOL_CALL_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as e:
        raise LocalModelError(f"{model_id} took too long to answer. Check that the server can load it.") from e
    except httpx.HTTPError as e:
        raise LocalModelError(f"The server at {base_url} stopped answering.") from e
    try:
        payload = response.json()
    except ValueError as e:
        raise LocalModelError(f"The server at {base_url} did not answer like an OpenAI-compatible server.") from e
    if response.status_code != 200:
        raise LocalModelError(f"The server would not run {model_id}: {_error_text(payload, response.status_code)}")
    if not _has_tool_call(payload):
        raise LocalModelError(
            f"{model_id} answered without calling a tool. Chats here work by calling tools,"
            " so pick a model that supports tool calling."
        )


def _error_text(payload: Any, status_code: int) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    return message if isinstance(message, str) and message else f"it answered {status_code}"


def _has_tool_call(payload: Any) -> bool:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    return isinstance(tool_calls, list) and len(tool_calls) > 0


def read_loaded_context_window(client: httpx.Client, base_url: str, model_id: str) -> int | None:
    """The context length the server loaded ``model_id`` with, or None when it does not say.

    Only Ollama says (`/api/ps`, beside its OpenAI-compatible root), and only for a model that is
    loaded, which the tool-call check has just made true.
    """
    try:
        response = client.get(f"{base_url.removesuffix('/v1')}/api/ps", timeout=_PROBE_TIMEOUT_SECONDS)
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("No loaded-model report from {}: {}", base_url, e)
        return None
    loaded = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(loaded, list):
        return None
    for entry in loaded:
        if isinstance(entry, dict) and model_id in (entry.get("name"), entry.get("model")):
            context_length = entry.get("context_length")
            return context_length if isinstance(context_length, int) and context_length > 0 else None
    return None


def verify_model(client: httpx.Client, base_url: str, api_key: str, model_id: str, context_window: int) -> int:
    """Check that ``model_id`` can run a chat here, and return the context window pi is to be told.

    The window is the smaller of what was asked for and what the server loaded the model with:
    telling pi a larger one than the server keeps makes it send turns the server truncates.
    """
    if model_id.strip() == "":
        raise LocalModelError("Pick a model.")
    if context_window < MIN_CONTEXT_WINDOW:
        raise LocalModelError(
            f"A chat here needs a context window of at least {MIN_CONTEXT_WINDOW:,} tokens"
            " to hold the workspace's own instructions."
        )
    check_tool_call(client, base_url, api_key, model_id)
    loaded = read_loaded_context_window(client, base_url, model_id)
    if loaded is None:
        return context_window
    if loaded < MIN_CONTEXT_WINDOW:
        raise LocalModelError(
            f"The server loaded {model_id} with a context of {loaded:,} tokens, and a chat here needs at least"
            f" {MIN_CONTEXT_WINDOW:,} to hold the workspace's own instructions. For Ollama, set"
            f" OLLAMA_CONTEXT_LENGTH={DEFAULT_CONTEXT_WINDOW} where it runs, restart it, and try again."
        )
    return min(context_window, loaded)


def verify_local_model(base_url: str, api_key: str, model_id: str, context_window: int) -> int:
    """:func:`verify_model` over a client of its own, for the sign-in flow."""
    with httpx.Client() as client:
        return verify_model(client, base_url, api_key, model_id, context_window)


def write_models_file(account_dir: Path, base_url: str, model_id: str, context_window: int) -> None:
    """Write the `models.json` that makes ``model_id`` at ``base_url`` a pi provider of this account."""
    config = {
        "providers": {
            LOCAL_MODEL_PROVIDER_ID: {
                "baseUrl": base_url,
                "api": "openai-completions",
                "models": [
                    {
                        "id": model_id,
                        "contextWindow": context_window,
                        "maxTokens": min(_MAX_OUTPUT_TOKENS, context_window // 4),
                    }
                ],
            }
        }
    }
    path = account_dir / MODELS_FILENAME
    path.write_text(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)
