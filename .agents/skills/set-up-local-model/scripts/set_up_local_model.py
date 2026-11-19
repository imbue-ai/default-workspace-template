"""Ask Imbue Studio to set up a model on the user's own computer, and show that this workspace can use it.

Imbue Studio does the installing. Once the user has allowed this workspace to use
local models on their computer, the app installs Ollama there when no model server
is running, downloads a model that suits the computer's memory, and says where
that stands. This script asks and reads through the latchkey gateway's
``minds-api-proxy`` as the calling agent (the gateway injects the app's API key;
this script never holds one), and for its last step talks to the model server
itself.

The permission request is not filed here: the chat builds the user's approval card
from the request's own command line and echoed result, so a request run from inside
a script would leave them no button to press. SKILL.md has the command.

Usage, from the repo root:

    python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py check
    python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py start [--model ID]
    python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py wait [--timeout SECONDS]
    python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py verify [--context TOKENS] [--model ID]

Every command takes ``--device ID`` to name one of the user's computers (the
``devices`` skill lists them); without it the gateway asks the one it saw most
recently.

Each command prints a summary in plain language and then, on its last line, one
JSON object whose ``state`` is one of:

    NOT_GRANTED         the user has not allowed this workspace yet
    NOT_STARTED         allowed, with nothing set up and nothing under way
    INSTALLING_OLLAMA   the app is installing the program that runs models
    DOWNLOADING_MODEL   the app is downloading the model
    READY               an Ollama answers on the computer and has the model
    FAILED              the last attempt failed; ``detail`` says why
    UNAVAILABLE         it cannot be set up on this computer; ``detail`` says why
    STUDIO_UNREACHABLE  the app did not answer: the computer is asleep or offline,
                        the app is closed, or this workspace has no way to it
    STUDIO_TOO_OLD      the app is from before it could do this
    STUDIO_ERROR        the app answered something else; ``detail`` says what

The first seven are the app's own answer, passed on whole with what it says of the
computer, the download and the models; the rest are this script's reading of a
call that brought back no status.

- ``check`` reads where the setup stands.
- ``start`` asks the app to start the setup, or to try again: for the model it
  picks for the computer, or for ``--model``.
- ``wait`` polls until the state differs from the first one it saw. It is meant
  for ``run_in_background.py --waiter``, whose report then starts the agent's turn.
- ``verify`` needs READY. It makes the model server answer inside this workspace,
  makes the copy of the model that runs with ``--context`` tokens when the server
  has none, times one short completion, and writes where refresh code reaches the
  model to ``data/.state/local_model/endpoint.json``.

Exit codes:
    0  -- the state it ended on is one to carry on from
    1  -- FAILED, UNAVAILABLE or a STUDIO_ state, or the model could not be verified
    3  -- ``wait`` ran out of time with the state unchanged

It imports only the standard library, so it runs under the system ``python3`` with
no venv.

Environment:
    MNGR_AGENT_ID               The calling agent's id, registered with the gateway.
    LATCHKEY_GATEWAY,           Gateway address + password mngr injects into the
    LATCHKEY_GATEWAY_PASSWORD   agent environment. Both must be present.
    LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
                                The per-agent authorization JWT, forwarded when
                                set (a desktop-hosted gateway needs it; a
                                VPS-hosted one never injects it).
    MINDS_LOCAL_MODEL_ENDPOINT_FILE
                                Where ``verify`` writes its record (default
                                data/.state/local_model/endpoint.json under the
                                repo root).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENV_AGENT_ID = "MNGR_AGENT_ID"
ENV_GATEWAY = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
ENV_ENDPOINT_FILE = "MINDS_LOCAL_MODEL_ENDPOINT_FILE"

# This script sits four directories below the repo root
# (.agents/skills/set-up-local-model/scripts/).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_ENDPOINT_FILE = (
    _REPO_ROOT / "data" / ".state" / "local_model" / "endpoint.json"
)

NOT_GRANTED = "NOT_GRANTED"
INSTALLING_OLLAMA = "INSTALLING_OLLAMA"
DOWNLOADING_MODEL = "DOWNLOADING_MODEL"
READY = "READY"
FAILED = "FAILED"
UNAVAILABLE = "UNAVAILABLE"
STUDIO_UNREACHABLE = "STUDIO_UNREACHABLE"
STUDIO_TOO_OLD = "STUDIO_TOO_OLD"
STUDIO_ERROR = "STUDIO_ERROR"

_STATES_TO_STOP_ON = frozenset(
    {FAILED, UNAVAILABLE, STUDIO_UNREACHABLE, STUDIO_TOO_OLD, STUDIO_ERROR}
)
_STATES_WAITING_CANNOT_CHANGE = frozenset({READY, FAILED, UNAVAILABLE, STUDIO_TOO_OLD})
_STATES_WITHOUT_A_STATUS = frozenset({STUDIO_UNREACHABLE, STUDIO_ERROR})

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_TIMED_OUT = 3

# The app's error code for a workspace the user has not allowed, and the opening of its
# answer for an agent it has not registered yet: a 404 that is not the route missing.
_NOT_GRANTED_CODE = "local_model_not_granted"
_UNKNOWN_AGENT_PREFIX = "Unknown agent"

# The app looks at each known port of the computer before it answers.
_STUDIO_TIMEOUT_SECONDS = 20.0
# Opening the tunnel may have to open a connection into the workspace first.
_TUNNEL_TIMEOUT_SECONDS = 45.0
_PROBE_TIMEOUT_SECONDS = 5.0
_OLLAMA_TIMEOUT_SECONDS = 5.0
# Making a copy writes one small manifest; the weights are shared.
_CREATE_TIMEOUT_SECONDS = 60.0
# A model that is not loaded yet is read off disk before it answers.
_COMPLETION_TIMEOUT_SECONDS = 300.0

_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_WAIT_TIMEOUT_SECONDS = 1800.0
# The user's computer drops off the network for moments at a time, and every exit of
# `wait` costs the agent a turn.
_SILENCE_GRACE_SECONDS = 60.0

# Ollama's /v1 API takes no context length with a request and loads a plain model at
# 4,096 tokens, so a refresh calls a copy of the model that names its size.
DEFAULT_CONTEXT_WINDOW = 16_384
_OLLAMA_CONTEXT_PARAMETER = "num_ctx"
_NAMED_CONTEXT_PATTERN = re.compile(
    rf"^{_OLLAMA_CONTEXT_PARAMETER}\s+(\d+)\s*$", re.MULTILINE
)
_OLLAMA_SERVER_NAME = "Ollama"
_OLLAMA_PORT = 11434
# The one phase of an install in which bytes arrive. An Ollama that is already on the
# computer's disk is only started, and reports nothing done of a download it never begins.
_INSTALL_PHASE_DOWNLOADING = "DOWNLOADING"
_THINKING_CAPABILITY = "thinking"
# The one `reasoning_effort` Ollama stops a model's reasoning for.
_OLLAMA_REASONING_OFF_EFFORT = "none"

# Where the computer's Ollama answers a workspace that runs on that computer: the
# workspace's own loopback, then the names Docker and Lima give the machine that runs
# the container. The chat app looks in the same places.
DIRECT_BASE_URLS = tuple(
    f"http://{host}:{_OLLAMA_PORT}/v1"
    for host in ("127.0.0.1", "host.docker.internal", "host.lima.internal")
)

ROUTE_TUNNEL = "TUNNEL"
ROUTE_DIRECT = "DIRECT"

_TEST_SYSTEM = "You answer in two plain sentences."
_TEST_PROMPT = "What is a daily news digest for?"
_TEST_MAX_TOKENS = 120


class VerifyError(Exception):
    """The model could not be shown to answer from this workspace; the message says why, for a person."""


@dataclass(frozen=True)
class HttpAnswer:
    """What one HTTP call came to. ``status`` is None when nothing answered, and ``text`` then says why."""

    status: int | None
    text: str

    def json(self) -> Any:
        """The body as JSON, or None when it is not JSON."""
        try:
            return json.loads(self.text)
        except ValueError:
            return None


def http_call(
    method: str,
    url: str,
    body: Mapping[str, Any] | None,
    headers: Mapping[str, str],
    timeout: float,
) -> HttpAnswer:
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent = (
        {**headers, "Content-Type": "application/json"} if body is not None else headers
    )
    request = urllib.request.Request(url, data=data, headers=dict(sent), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpAnswer(
                int(response.status), response.read().decode("utf-8", "replace")
            )
    except urllib.error.HTTPError as exc:
        return HttpAnswer(int(exc.code), exc.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return HttpAnswer(None, str(exc.reason))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        return HttpAnswer(None, str(exc))


@dataclass(frozen=True)
class StudioAccess:
    """How this agent reaches Imbue Studio: the gateway's address, what it asks of a caller, and who is calling."""

    gateway: str
    headers: Mapping[str, str]
    agent_id: str

    def route(self, name: str) -> str:
        return f"{self.gateway}/minds-api-proxy/api/v1/agents/{self.agent_id}/local-model/{name}"


def studio_access(
    environ: Mapping[str, str], device: str | None
) -> StudioAccess | None:
    """The way to Imbue Studio the environment gives this agent, or None when it gives none."""
    gateway = environ.get(ENV_GATEWAY, "")
    password = environ.get(ENV_GATEWAY_PASSWORD, "")
    agent_id = environ.get(ENV_AGENT_ID, "")
    if not gateway or not password or not agent_id:
        return None
    headers = {"X-Latchkey-Gateway-Password": password}
    # Forwarded only when set: a desktop-hosted gateway needs it as the per-agent
    # authorization JWT, a VPS-hosted one never injects it.
    permissions = environ.get(ENV_GATEWAY_PERMISSIONS, "")
    if permissions:
        headers["X-Latchkey-Gateway-Permissions-Override"] = permissions
    if device:
        headers["X-Latchkey-Device"] = device
    return StudioAccess(gateway.rstrip("/"), headers, agent_id)


def _own_status(state: str, detail: str) -> dict[str, Any]:
    return {"state": state, "detail": detail}


def _error_of(payload: Any) -> tuple[str, str]:
    """The message and the code of an error body; each "" when it has none.

    The app words an error as ``{"error": message, "code": code}``; a model server's
    OpenAI-compatible root puts the message one level deeper.
    """
    if not isinstance(payload, dict):
        return "", ""
    message, code = payload.get("error"), payload.get("code")
    if isinstance(message, dict):
        message = message.get("message")
    return (
        message if isinstance(message, str) else "",
        code if isinstance(code, str) else "",
    )


def _said(answer: HttpAnswer) -> str:
    """What an answer that is no success said, short enough for one sentence."""
    message, _ = _error_of(answer.json())
    return message or " ".join(answer.text.split())[:200] or "nothing readable"


def _shortfall(answer: HttpAnswer) -> str:
    """How a call fell short of an answer to use, for the inside of a sentence."""
    if answer.status is None:
        return f"it did not answer: {answer.text}"
    return f"it answered {answer.status}: {_said(answer)}"


def status_of(answer: HttpAnswer) -> dict[str, Any]:
    """What the app's answer to a setup call says: its own status when it gave one, else this script's reading."""
    if answer.status is None:
        return _own_status(
            STUDIO_UNREACHABLE,
            f"Imbue Studio did not answer ({answer.text}). The user's computer may be asleep or offline, "
            "or Imbue Studio may be closed on it.",
        )
    if answer.status in (502, 503):
        return _own_status(
            STUDIO_UNREACHABLE,
            "No computer running Imbue Studio answered this workspace. The user's computer may be asleep "
            "or offline, or Imbue Studio may be closed on it.",
        )
    payload = answer.json()
    if answer.status == 200 and isinstance(payload, dict):
        state = payload.get("state")
        if isinstance(state, str) and state:
            return payload
    message, code = _error_of(payload)
    if answer.status == 403 and code == _NOT_GRANTED_CODE:
        return _own_status(
            NOT_GRANTED,
            "The user has not yet allowed this workspace to use local models on their computer.",
        )
    if answer.status == 404 and message.startswith(_UNKNOWN_AGENT_PREFIX):
        return _own_status(
            STUDIO_ERROR,
            "Imbue Studio does not know this agent yet, which can take a minute after a chat starts. "
            "Check again shortly.",
        )
    if answer.status == 404:
        return _own_status(
            STUDIO_TOO_OLD,
            "The Imbue Studio on the user's computer is from before it could set up a local model for a "
            "workspace. It needs updating.",
        )
    return _own_status(
        STUDIO_ERROR, f"Imbue Studio answered {answer.status}: {_said(answer)}"
    )


_NO_ACCESS_STATUS_DETAIL = (
    "This workspace has no connection to Imbue Studio: the gateway's address, its password or this "
    "agent's id is missing from the environment."
)


def read_setup(access: StudioAccess | None) -> dict[str, Any]:
    """Where the setup stands on the user's computer."""
    if access is None:
        return _own_status(STUDIO_UNREACHABLE, _NO_ACCESS_STATUS_DETAIL)
    return status_of(
        http_call(
            "GET", access.route("setup"), None, access.headers, _STUDIO_TIMEOUT_SECONDS
        )
    )


def start_setup(access: StudioAccess | None, model_id: str | None) -> dict[str, Any]:
    """Ask the app to start the setup, or to try again, and say where that leaves it."""
    if access is None:
        return _own_status(STUDIO_UNREACHABLE, _NO_ACCESS_STATUS_DETAIL)
    body = {} if model_id is None else {"model_id": model_id}
    return status_of(
        http_call(
            "POST", access.route("setup"), body, access.headers, _STUDIO_TIMEOUT_SECONDS
        )
    )


def exit_code_of(state: Any) -> int:
    return EXIT_STOPPED if state in _STATES_TO_STOP_ON else EXIT_OK


def format_bytes(count: int) -> str:
    """A download's size as people read one: megabytes under a gigabyte, gigabytes above."""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f} GB"
    return f"{count / 1_000_000:.0f} MB"


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _progress(done: Any, total: Any) -> str | None:
    """How far a download has got ("1.2 GB of 5.2 GB (23%)"); None when there is nothing to count yet."""
    if not _is_count(done):
        return None
    if _is_count(total) and total > 0:
        percent = min(100, done * 100 // total)
        return f"{format_bytes(done)} of {format_bytes(total)} ({percent}%)"
    return f"{format_bytes(done)} so far" if done > 0 else None


def describe(status: Mapping[str, Any]) -> str:
    """The status in plain language: its state, the app's own sentence, and how far a download has got."""
    state = status.get("state")
    lines = [f"{state}: {status.get('detail') or 'Imbue Studio gave no detail.'}"]
    ollama = status.get("ollama")
    if (
        state == INSTALLING_OLLAMA
        and isinstance(ollama, dict)
        and ollama.get("install_phase") == _INSTALL_PHASE_DOWNLOADING
    ):
        progress = _progress(
            ollama.get("install_bytes_done"), ollama.get("install_bytes_total")
        )
        if progress is not None:
            lines.append(f"Ollama download: {progress}.")
    download = status.get("model_download")
    if state == DOWNLOADING_MODEL and isinstance(download, dict):
        progress = _progress(download.get("bytes_done"), download.get("bytes_total"))
        if progress is not None:
            lines.append(f"{download.get('model_id') or 'Model'} download: {progress}.")
    computer = status.get("computer")
    if isinstance(computer, dict) and isinstance(computer.get("hostname"), str):
        memory = computer.get("memory_bytes")
        lines.append(
            f"Computer: {computer['hostname']}"
            + (f", {memory / 2**30:.0f} GB of memory." if _is_count(memory) else ".")
        )
    return "\n".join(lines)


def wait_for_change(
    read: Callable[[], dict[str, Any]],
    timeout_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any], bool]:
    """Poll until the setup's state differs from the first one read: the last status, and whether time ran out.

    A state that waiting cannot change is returned at once. A poll that brings back no
    status is not a change until the app has given none for a minute.
    """
    first = read()
    if first.get("state") in _STATES_WAITING_CANNOT_CHANGE:
        return first, False
    is_first_without_a_status = first.get("state") in _STATES_WITHOUT_A_STATUS
    deadline = clock() + timeout_seconds
    silent_since: float | None = None
    latest = first
    while clock() < deadline:
        sleep(_POLL_INTERVAL_SECONDS)
        latest = read()
        state = latest.get("state")
        if state in _STATES_WITHOUT_A_STATUS:
            if is_first_without_a_status:
                continue
            silent_since = clock() if silent_since is None else silent_since
            if clock() - silent_since >= _SILENCE_GRACE_SECONDS:
                return latest, False
            continue
        silent_since = None
        if state != first.get("state"):
            return latest, False
    return latest, True


def with_tag(model_id: str) -> str:
    """The id with its tag spelled out, as Ollama lists a model."""
    name = model_id.rsplit("/", 1)[-1]
    return model_id if ":" in name else f"{model_id}:latest"


def context_model_id(base_id: str, context_window: int) -> str:
    """The id of ``base_id``'s copy at ``context_window`` tokens.

    The chat app names its copies by the same rule (``context_model_id`` in its
    ``local_model_catalog.py``), so a chat and a refresh at one size share a copy.
    """
    return f"{with_tag(base_id)}-ctx{context_window}"


def _lists_model(model_ids: Any, model_id: str) -> bool:
    if not isinstance(model_ids, list):
        return False
    return with_tag(model_id) in {
        with_tag(each) for each in model_ids if isinstance(each, str)
    }


def _ready_model(status: Mapping[str, Any], wanted: str | None) -> str:
    """The model to verify: the one the setup is READY with, or ``wanted`` once the computer's Ollama has it."""
    state = status.get("state")
    standing = f"{state}: {status.get('detail') or 'no detail'}"
    if wanted is None:
        model_id = status.get("model_id")
        if state != READY or not isinstance(model_id, str) or not model_id:
            raise VerifyError(f"The setup is not ready ({standing}).")
        return model_id
    ollama = status.get("ollama")
    if not isinstance(ollama, dict):
        # No status from the app, so no telling what the computer has: the advice below to
        # ask for the model would point away from what is wrong.
        raise VerifyError(f"The setup is not ready ({standing}).")
    installed = status.get("models")
    is_installed = (
        ollama.get("is_running") is True
        and isinstance(installed, list)
        and _lists_model(
            [each.get("model_id") for each in installed if isinstance(each, dict)],
            wanted,
        )
    )
    if not is_installed:
        raise VerifyError(
            f"The Ollama on the user's computer does not have {wanted} ({standing}). "
            f"Ask for it with `start --model {wanted}`."
        )
    return with_tag(wanted)


def _tunnel_base_url(access: StudioAccess, model_id: str) -> tuple[str | None, str]:
    """Have the app make the Ollama that holds ``model_id`` answer inside this workspace.

    Returns the address it then answers on here, or None with what stood in the way.
    """
    listed = http_call(
        "GET", access.route("servers"), None, access.headers, _STUDIO_TIMEOUT_SECONDS
    )
    listing = listed.json()
    entries = listing.get("servers") if isinstance(listing, dict) else None
    if listed.status != 200 or not isinstance(entries, list):
        return (
            None,
            f"Imbue Studio would not list the computer's model servers ({_shortfall(listed)})",
        )
    holding = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("port"), int)
        and _lists_model(entry.get("models"), model_id)
    ]
    if not holding:
        return None, "Imbue Studio lists no model server that has it"
    # Only an Ollama can make the context copy, should another server list a model of the same name.
    ollamas = [entry for entry in holding if entry.get("name") == _OLLAMA_SERVER_NAME]
    opened = http_call(
        "POST",
        access.route("tunnel"),
        {"port": (ollamas or holding)[0]["port"]},
        access.headers,
        _TUNNEL_TIMEOUT_SECONDS,
    )
    connection = opened.json()
    workspace_port = (
        connection.get("workspace_port") if isinstance(connection, dict) else None
    )
    if opened.status != 200 or not isinstance(workspace_port, int):
        return (
            None,
            f"Imbue Studio could not connect this workspace to that server ({_shortfall(opened)})",
        )
    return f"http://127.0.0.1:{workspace_port}/v1", ""


def _serves_model(base_url: str, model_id: str) -> bool:
    """Whether an OpenAI-compatible server answers at ``base_url`` and lists ``model_id``."""
    answer = http_call("GET", f"{base_url}/models", None, {}, _PROBE_TIMEOUT_SECONDS)
    payload = answer.json() if answer.status == 200 else None
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return False
    return _lists_model(
        [entry.get("id") for entry in entries if isinstance(entry, dict)], model_id
    )


def reach_model_server(
    access: StudioAccess, model_id: str, direct_base_urls: Sequence[str]
) -> tuple[str, str]:
    """The address ``model_id`` answers on from this workspace, and how it is reached.

    The app's tunnel first, as the chat app asks for it. A workspace that runs on the
    user's computer may reach the server with no tunnel, which is the fallback.
    """
    tunnel, obstacle = _tunnel_base_url(access, model_id)
    candidates = [(tunnel, ROUTE_TUNNEL)] if tunnel is not None else []
    candidates += [(base_url, ROUTE_DIRECT) for base_url in direct_base_urls]
    for base_url, route in candidates:
        if _serves_model(base_url, model_id):
            return base_url, route
    if tunnel is not None:
        obstacle = (
            f"nothing with it answers at {tunnel}, the connection Imbue Studio opened, so the computer may "
            "have gone to sleep"
        )
    raise VerifyError(
        f"Imbue Studio says {model_id} is ready, but no model server with it answers from this workspace: "
        f"{obstacle}."
    )


def _named_context_window(shown: Any) -> int | None:
    """The context length a model names for itself, from Ollama's description of it."""
    parameters = shown.get("parameters") if isinstance(shown, dict) else None
    match = (
        _NAMED_CONTEXT_PATTERN.search(parameters)
        if isinstance(parameters, str)
        else None
    )
    return int(match.group(1)) if match is not None else None


def ensure_context_model(root_url: str, model_id: str, context_window: int) -> str:
    """The id of ``model_id``'s copy at ``context_window`` tokens on the Ollama at ``root_url``, made now if absent."""
    copy_id = context_model_id(model_id, context_window)
    shown = http_call(
        "POST", f"{root_url}/api/show", {"model": copy_id}, {}, _OLLAMA_TIMEOUT_SECONDS
    )
    if shown.status == 200 and _named_context_window(shown.json()) == context_window:
        return copy_id
    created = http_call(
        "POST",
        f"{root_url}/api/create",
        {
            "model": copy_id,
            "from": model_id,
            "parameters": {_OLLAMA_CONTEXT_PARAMETER: context_window},
            "stream": False,
        },
        {},
        _CREATE_TIMEOUT_SECONDS,
    )
    if created.status != 200:
        raise VerifyError(
            f"The model server would not set {model_id} up for a context of {context_window:,} tokens "
            f"({_shortfall(created)})."
        )
    return copy_id


def _reasoning_off_effort(root_url: str, model_id: str) -> str | None:
    """What stops ``model_id`` reasoning before it answers; None for a model Ollama does not list as reasoning."""
    described = http_call(
        "POST", f"{root_url}/api/show", {"model": model_id}, {}, _OLLAMA_TIMEOUT_SECONDS
    )
    if described.status != 200:
        # Read as "does not reason", a call that failed would be recorded as that, and every
        # later refresh would let a reasoning model think through its answers.
        raise VerifyError(
            f"The model server would not say whether {model_id} reasons before it answers "
            f"({_shortfall(described)})."
        )
    shown = described.json()
    capabilities = shown.get("capabilities") if isinstance(shown, dict) else None
    if isinstance(capabilities, list) and _THINKING_CAPABILITY in capabilities:
        return _OLLAMA_REASONING_OFF_EFFORT
    return None


def _loaded_models(root_url: str) -> dict[str, int | None] | None:
    """The models Ollama holds in memory, each with the context length it was loaded with when Ollama says it;
    None from a server that does not say what it holds."""
    payload = http_call(
        "GET", f"{root_url}/api/ps", None, {}, _OLLAMA_TIMEOUT_SECONDS
    ).json()
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    loaded: dict[str, int | None] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        context_length = entry.get("context_length")
        for name in (entry.get("name"), entry.get("model")):
            if isinstance(name, str):
                loaded[name] = context_length if _is_count(context_length) else None
    return loaded


def _timed_completion(
    base_url: str, model_id: str, reasoning_off_effort: str | None
) -> tuple[float, int | None]:
    """Ask the model one short question as a refresh would: the seconds it took, and the tokens it wrote."""
    body: dict[str, Any] = {
        "model": model_id,
        "stream": False,
        "max_tokens": _TEST_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": _TEST_SYSTEM},
            {"role": "user", "content": _TEST_PROMPT},
        ],
    }
    if reasoning_off_effort is not None:
        body["reasoning_effort"] = reasoning_off_effort
    started_at = time.monotonic()
    answer = http_call(
        "POST", f"{base_url}/chat/completions", body, {}, _COMPLETION_TIMEOUT_SECONDS
    )
    seconds = time.monotonic() - started_at
    if answer.status is None:
        raise VerifyError(
            f"The model server stopped answering during the test ({answer.text})."
        )
    if answer.status != 200:
        raise VerifyError(
            f"The model server answered {answer.status} to the test: {_said(answer)}"
        )
    payload = answer.json()
    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or text.strip() == "":
        raise VerifyError(f"{model_id} answered the test with no text.")
    usage = payload.get("usage") if isinstance(payload, dict) else None
    output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    return seconds, output_tokens if _is_count(output_tokens) else None


def write_record(path: Path, record: Mapping[str, Any]) -> None:
    """Write the record refresh code reads. A refresh may be reading it now, so it is swapped in whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(scratch, path)


def endpoint_file(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(ENV_ENDPOINT_FILE) or _DEFAULT_ENDPOINT_FILE)


def verify(
    access: StudioAccess | None,
    wanted_model: str | None,
    context_window: int,
    direct_base_urls: Sequence[str],
    record_path: Path,
) -> tuple[str, dict[str, Any], int]:
    """Show that the model answers from this workspace and record where: a summary, the result, and the exit code."""
    status = read_setup(access)
    try:
        if access is None:
            raise VerifyError(_NO_ACCESS_STATUS_DETAIL)
        model_id = _ready_model(status, wanted_model)
        base_url, route = reach_model_server(access, model_id, direct_base_urls)
        root_url = base_url.removesuffix("/v1")
        copy_id = ensure_context_model(root_url, model_id, context_window)
        reasoning_off_effort = _reasoning_off_effort(root_url, model_id)
        loaded_before = _loaded_models(root_url)
        seconds, output_tokens = _timed_completion(
            base_url, copy_id, reasoning_off_effort
        )
        loaded = (_loaded_models(root_url) or {}).get(copy_id)
    except VerifyError as exc:
        result = {
            "state": status.get("state"),
            "is_verified": False,
            "detail": str(exc),
        }
        return f"Not verified. {exc}", result, EXIT_STOPPED
    record = {
        "base_url": base_url,
        "model": copy_id,
        "base_model": model_id,
        # A model whose own limit is lower is loaded at that limit, whatever its copy names.
        "context_window": min(context_window, loaded) if loaded else context_window,
        "reasoning_off_effort": reasoning_off_effort,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_record(record_path, record)
    # None when the server does not say what it held: the seconds may or may not include a load.
    is_load_included = None if loaded_before is None else copy_id not in loaded_before
    tokens_per_second = (
        round(output_tokens / seconds, 1)
        if output_tokens is not None and seconds > 0
        else None
    )
    computer = status.get("computer")
    hostname = computer.get("hostname") if isinstance(computer, dict) else None
    where = hostname if isinstance(hostname, str) else "the user's computer"
    speed = (
        f"{seconds:.1f} seconds"
        if tokens_per_second is None
        else f"{seconds:.1f} seconds, at {tokens_per_second} tokens a second"
    )
    summary = "\n".join(
        [
            f"READY: {model_id} on {where} answers from this workspace.",
            f"It answered a short test in {speed}"
            + (
                ", which includes loading the model into memory; later answers start sooner."
                if is_load_included
                else "."
            ),
            f"Refresh code reaches it at {base_url} as {copy_id}, with a window of "
            f"{record['context_window']:,} tokens. That is recorded in {record_path}.",
        ]
    )
    result = {
        "state": READY,
        "is_verified": True,
        **record,
        "route": route,
        "seconds": round(seconds, 1),
        "output_tokens": output_tokens,
        "tokens_per_second": tokens_per_second,
        "is_load_included": is_load_included,
        "record_path": str(record_path),
    }
    return summary, result, EXIT_OK


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of tokens")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Set up a model on the user's own computer through Imbue Studio, and show that this "
        "workspace can use it."
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--device",
        default=None,
        metavar="ID",
        help="The user's computer to ask, by its device id; by default the one the gateway saw most recently.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", parents=[shared], help="Say where the setup stands.")
    start = commands.add_parser(
        "start",
        parents=[shared],
        help="Ask Imbue Studio to start the setup, or to try again.",
    )
    start.add_argument(
        "--model",
        default=None,
        metavar="ID",
        help="The model to set up, by its Ollama name, in place of the one Imbue Studio picks.",
    )
    wait = commands.add_parser(
        "wait",
        parents=[shared],
        help="Poll until the setup's state differs from the first one seen.",
    )
    wait.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="How long to wait for a change (default: %(default)s).",
    )
    verify_parser = commands.add_parser(
        "verify",
        parents=[shared],
        help="Show that the model answers from this workspace, and record where refresh code reaches it.",
    )
    verify_parser.add_argument(
        "--context",
        type=_positive_int,
        default=DEFAULT_CONTEXT_WINDOW,
        metavar="TOKENS",
        help="The window the prompt and the answer share (default: %(default)s).",
    )
    verify_parser.add_argument(
        "--model",
        default=None,
        metavar="ID",
        help="An installed model to verify in place of the one the setup is ready with.",
    )
    return parser


def _emit(summary: str, result: Mapping[str, Any]) -> None:
    print(summary)
    print(json.dumps(result))


def run(
    argv: Sequence[str] | None,
    environ: Mapping[str, str],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    direct_base_urls: Sequence[str] = DIRECT_BASE_URLS,
) -> int:
    args = _parser().parse_args(argv)
    access = studio_access(environ, args.device)
    if args.command == "verify":
        summary, result, exit_code = verify(
            access, args.model, args.context, direct_base_urls, endpoint_file(environ)
        )
        _emit(summary, result)
        return exit_code
    if args.command == "wait":
        status, is_timed_out = wait_for_change(
            lambda: read_setup(access), args.timeout, clock, sleep
        )
        if is_timed_out:
            _emit(
                f"No change in {args.timeout:.0f} seconds.\n{describe(status)}",
                {**status, "is_timed_out": True},
            )
            return EXIT_TIMED_OUT
    elif args.command == "start":
        status = start_setup(access, args.model)
    else:
        status = read_setup(access)
    _emit(describe(status), status)
    return exit_code_of(status.get("state"))


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv, dict(os.environ))


if __name__ == "__main__":
    sys.exit(main())
