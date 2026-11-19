"""Unit tests for the set-up-local-model script.

Imbue Studio and the model server are real HTTP servers on this machine's loopback: one
answers the gateway's per-agent local-model routes the way the app does, the other
answers the way an Ollama does. No test names a real gateway, or a model server's
usual port.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import set_up_local_model
from imbue.chat.harnesses.pi_coding.local_model_catalog import (
    context_model_id as chat_context_model_id,
)

_AGENT_ID = "agent-1"
_SETUP_PATH = f"/minds-api-proxy/api/v1/agents/{_AGENT_ID}/local-model/setup"
_SERVERS_PATH = f"/minds-api-proxy/api/v1/agents/{_AGENT_ID}/local-model/servers"
_TUNNEL_PATH = f"/minds-api-proxy/api/v1/agents/{_AGENT_ID}/local-model/tunnel"
_COPY_ID = "qwen3:8b-ctx16384"


@dataclass(frozen=True)
class _Request:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


class _Server:
    """A loopback HTTP server that answers each request with what ``answer`` gives for it, keeping what it was sent."""

    def __init__(self) -> None:
        self.requests: list[_Request] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                request = _Request(
                    self.command,
                    self.path,
                    {name.lower(): value for name, value in self.headers.items()},
                    json.loads(raw) if raw else None,
                )
                owner.requests.append(request)
                status, payload = owner.answer(request)
                body = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _serve  # noqa: N815
            do_POST = _serve  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self._http = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._http.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        # Closing waits out one poll, and every test closes its servers.
        self._thread = threading.Thread(
            target=self._http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def answer(self, request: _Request) -> tuple[int, Any]:
        """The status and the body to answer ``request`` with; a server that is only ever closed needs none."""
        return 404, {"error": "not found"}

    def sent(self, method: str, path: str) -> list[_Request]:
        return [
            request
            for request in self.requests
            if (request.method, request.path) == (method, path)
        ]

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._thread.join()


def _status(state: str, **overrides: Any) -> dict[str, Any]:
    """A setup status as Imbue Studio words one."""
    status: dict[str, Any] = {
        "state": state,
        "detail": f"The setup is {state.lower().replace('_', ' ')}.",
        "model_id": "qwen3:8b",
        "computer": {
            "hostname": "studio-mac",
            "system": "Darwin",
            "machine": "arm64",
            "memory_bytes": 34_359_738_368,
        },
        "ollama": {
            "is_installed": True,
            "is_running": True,
            "is_started_by_studio": True,
            "version": "0.40.16",
            "install_phase": "IDLE",
            "install_bytes_done": 0,
            "install_bytes_total": 186_116_182,
            "install_error": None,
        },
        "model_download": None,
        "models": [
            {
                "model_id": "qwen3:8b",
                "size_bytes": 5_225_387_677,
                "context_windows": [40_960],
            }
        ],
        "recommended": [
            {
                "model_id": "qwen3:4b",
                "download_bytes": 2_497_293_444,
                "min_memory_bytes": 8_589_934_592,
                "is_suggested": False,
                "is_installed": True,
            }
        ],
    }
    return {**status, **overrides}


class _Studio(_Server):
    """Answers the per-agent local-model routes the way Imbue Studio does behind the gateway."""

    def __init__(self) -> None:
        super().__init__()
        # Served in turn; the last one repeats.
        self.setup_answers: list[tuple[int, Any]] = [(200, _status("READY"))]
        self.start_answer: tuple[int, Any] = (200, _status("DOWNLOADING_MODEL"))
        self.servers_answer: tuple[int, Any] = (200, {"servers": []})
        self.tunnel_answer: tuple[int, Any] = (502, {"error": "no tunnel"})

    def answer(self, request: _Request) -> tuple[int, Any]:
        if request.path == _SETUP_PATH and request.method == "GET":
            if len(self.setup_answers) > 1:
                return self.setup_answers.pop(0)
            return self.setup_answers[0]
        if request.path == _SETUP_PATH:
            return self.start_answer
        if request.path == _SERVERS_PATH:
            return self.servers_answer
        if request.path == _TUNNEL_PATH:
            return self.tunnel_answer
        return 404, b"<html>Not Found</html>"


class _Ollama(_Server):
    """Answers the way an Ollama does: its OpenAI-compatible root under /v1, its own API under /api."""

    def __init__(self) -> None:
        super().__init__()
        self.capabilities: list[str] = ["completion", "tools", "thinking"]
        # Each model's id and the context length it names for itself, None when it names none.
        self.models: dict[str, int | None] = {"qwen3:8b": None}
        # The models this server fails to describe, though it has them.
        self.undescribed: set[str] = set()
        self.loaded: dict[str, int] = {}
        self.is_saying_what_is_loaded = True
        # The longest context the model can be loaded with, whatever a copy of it names.
        self.context_limit = 262_144
        self.create_status = 200
        self.completion_answer: tuple[int, Any] = (
            200,
            {
                "choices": [{"message": {"content": "It gathers the day's news."}}],
                "usage": {"prompt_tokens": 24, "completion_tokens": 30},
            },
        )

    def answer(self, request: _Request) -> tuple[int, Any]:
        if request.path == "/v1/models":
            return 200, {"data": [{"id": model_id} for model_id in self.models]}
        if request.path == "/api/show":
            model_id = request.body["model"]
            if model_id not in self.models:
                return 404, {"error": f"model '{model_id}' not found"}
            if model_id in self.undescribed:
                return 500, {"error": "busy"}
            named = self.models[model_id]
            return 200, {
                "capabilities": self.capabilities,
                "parameters": f"num_ctx                        {named}\nstop <end>"
                if named is not None
                else "stop <end>",
            }
        if request.path == "/api/create":
            if self.create_status != 200:
                return self.create_status, {"error": "cannot create"}
            self.models[request.body["model"]] = request.body["parameters"]["num_ctx"]
            return 200, {"status": "success"}
        if request.path == "/api/ps" and self.is_saying_what_is_loaded:
            return 200, {
                "models": [
                    {"name": model_id, "model": model_id, "context_length": length}
                    for model_id, length in self.loaded.items()
                ]
            }
        if request.path == "/v1/chat/completions":
            status, payload = self.completion_answer
            if status == 200:
                model_id = request.body["model"]
                self.loaded[model_id] = min(
                    self.models.get(model_id) or 4_096, self.context_limit
                )
            return status, payload
        return 404, {"error": "not found"}

    def serve_through_studio(self, studio: _Studio) -> None:
        """Have ``studio`` list this server as the computer's Ollama and open its tunnel onto it."""
        studio.servers_answer = (
            200,
            {
                "servers": [
                    {"port": 1234, "name": "LM Studio", "models": ["qwen/qwen3-8b"]},
                    {
                        "port": 11434,
                        "name": "Ollama",
                        "models": list(self.models),
                        "described_models": None,
                    },
                ]
            },
        )
        studio.tunnel_answer = (200, {"workspace_port": self.port})


@pytest.fixture
def studio() -> Iterator[_Studio]:
    server = _Studio()
    yield server
    server.close()


@pytest.fixture
def ollama() -> Iterator[_Ollama]:
    server = _Ollama()
    yield server
    server.close()


class _Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _environ(studio: _Studio, tmp_path: Path) -> dict[str, str]:
    return {
        "LATCHKEY_GATEWAY": f"{studio.url}/",
        "LATCHKEY_GATEWAY_PASSWORD": "gateway-password",
        "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "agent-jwt",
        "MNGR_AGENT_ID": _AGENT_ID,
        "MINDS_LOCAL_MODEL_ENDPOINT_FILE": str(_record_path(tmp_path)),
    }


def _record_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "local_model" / "endpoint.json"


def _run(
    argv: list[str],
    environ: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    direct_base_urls: tuple[str, ...] = (),
    clock: _Clock | None = None,
) -> tuple[int, str, dict[str, Any]]:
    """Run one command: its exit code, its summary, and the object on its last line."""
    clock = clock or _Clock()
    exit_code = set_up_local_model.run(
        argv,
        environ,
        clock=clock,
        sleep=clock.sleep,
        direct_base_urls=direct_base_urls,
    )
    *summary, last = capsys.readouterr().out.strip().splitlines()
    return exit_code, "\n".join(summary), json.loads(last)


def test_check_passes_on_the_status_imbue_studio_gives_as_the_calling_agent(
    studio: _Studio, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = _status("READY", detail="qwen3:8b is installed and Ollama is running.")
    studio.setup_answers = [(200, status)]

    exit_code, summary, result = _run(["check"], _environ(studio, tmp_path), capsys)

    assert (exit_code, result) == (0, status)
    assert summary == (
        "READY: qwen3:8b is installed and Ollama is running.\n"
        "Computer: studio-mac, 32 GB of memory."
    )
    (request,) = studio.requests
    assert (request.method, request.path, request.body) == ("GET", _SETUP_PATH, None)
    assert request.headers["x-latchkey-gateway-password"] == "gateway-password"
    assert request.headers["x-latchkey-gateway-permissions-override"] == "agent-jwt"
    assert "x-latchkey-device" not in request.headers


def test_a_command_can_name_one_of_the_user_s_computers_and_do_without_the_override(
    studio: _Studio, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ(studio, tmp_path)
    del environ["LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"]

    _run(["check", "--device", "device-7"], environ, capsys)

    (request,) = studio.requests
    assert request.headers["x-latchkey-device"] == "device-7"
    assert "x-latchkey-gateway-permissions-override" not in request.headers


@pytest.mark.parametrize(
    ("state", "overrides", "progress"),
    [
        (
            "DOWNLOADING_MODEL",
            {
                "model_download": {
                    "model_id": "qwen3:8b",
                    "bytes_done": 1_200_000_000,
                    "bytes_total": 5_225_387_677,
                    "error": None,
                }
            },
            "qwen3:8b download: 1.2 GB of 5.2 GB (22%).",
        ),
        (
            "DOWNLOADING_MODEL",
            {
                "model_download": {
                    "model_id": "qwen3:8b",
                    "bytes_done": 3_000_000,
                    "bytes_total": 0,
                    "error": None,
                }
            },
            "qwen3:8b download: 3 MB so far.",
        ),
        (
            "INSTALLING_OLLAMA",
            {
                "ollama": {
                    "version": "0.40.16",
                    "install_phase": "DOWNLOADING",
                    "install_bytes_done": 80_000_000,
                    "install_bytes_total": 186_116_182,
                }
            },
            "Ollama download: 80 MB of 186 MB (42%).",
        ),
    ],
)
def test_the_summary_of_a_download_says_how_far_it_has_got(
    state: str, overrides: dict[str, Any], progress: str
) -> None:
    summary = set_up_local_model.describe(_status(state, **overrides))

    assert progress in summary.splitlines()


@pytest.mark.parametrize(
    ("install_phase", "install_bytes_done"),
    [("STARTING", 0), ("IDLE", 0), ("UNPACKING", 186_116_182)],
)
def test_the_summary_counts_no_download_of_an_ollama_that_is_only_being_started(
    install_phase: str, install_bytes_done: int
) -> None:
    status = _status(
        "INSTALLING_OLLAMA",
        detail="Imbue Studio is starting Ollama.",
        ollama={
            "is_installed": True,
            "is_running": False,
            "install_phase": install_phase,
            "install_bytes_done": install_bytes_done,
            "install_bytes_total": 186_116_182,
        },
    )

    assert set_up_local_model.describe(status) == (
        "INSTALLING_OLLAMA: Imbue Studio is starting Ollama.\n"
        "Computer: studio-mac, 32 GB of memory."
    )


@pytest.mark.parametrize(
    ("answer", "state", "exit_code", "fragment"),
    [
        (
            (
                403,
                {"error": "This workspace may not.", "code": "local_model_not_granted"},
            ),
            "NOT_GRANTED",
            0,
            "has not yet allowed",
        ),
        ((502, {"error": "desktop gone"}), "STUDIO_UNREACHABLE", 1, "No computer"),
        ((503, {"error": "no desktop"}), "STUDIO_UNREACHABLE", 1, "No computer"),
        ((404, b"<html>Not Found</html>"), "STUDIO_TOO_OLD", 1, "needs updating"),
        (
            (404, {"error": f"Unknown agent {_AGENT_ID}"}),
            "STUDIO_ERROR",
            1,
            "does not know this agent yet",
        ),
        (
            (403, {"error": "Request not permitted by the user."}),
            "STUDIO_ERROR",
            1,
            "answered 403: Request not permitted by the user.",
        ),
        ((500, b"boom"), "STUDIO_ERROR", 1, "answered 500: boom"),
        ((200, {"servers": []}), "STUDIO_ERROR", 1, "answered 200"),
    ],
)
def test_an_answer_that_is_no_status_is_read_for_what_it_means(
    studio: _Studio,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    answer: tuple[int, Any],
    state: str,
    exit_code: int,
    fragment: str,
) -> None:
    studio.setup_answers = [answer]

    code, summary, result = _run(["check"], _environ(studio, tmp_path), capsys)

    assert (code, result["state"]) == (exit_code, state)
    assert fragment in result["detail"]
    assert summary == f"{state}: {result['detail']}"


def test_a_workspace_with_no_way_to_imbue_studio_cannot_reach_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in (["check"], ["start"], ["verify"]):
        exit_code, _summary, result = _run(command, {}, capsys)

        assert (exit_code, result["state"]) == (1, "STUDIO_UNREACHABLE")
        assert "no connection to Imbue Studio" in result["detail"]


def test_a_gateway_that_does_not_answer_is_imbue_studio_unreachable(
    studio: _Studio, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ(studio, tmp_path)
    studio.close()

    exit_code, _summary, result = _run(["check"], environ, capsys)

    assert (exit_code, result["state"]) == (1, "STUDIO_UNREACHABLE")
    assert "did not answer" in result["detail"]


@pytest.mark.parametrize(
    ("arguments", "body"),
    [([], {}), (["--model", "qwen3:14b"], {"model_id": "qwen3:14b"})],
)
def test_start_asks_for_the_setup_and_says_where_that_leaves_it(
    studio: _Studio,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    body: dict[str, str],
) -> None:
    exit_code, _summary, result = _run(
        ["start", *arguments], _environ(studio, tmp_path), capsys
    )

    assert (exit_code, result["state"]) == (0, "DOWNLOADING_MODEL")
    (request,) = studio.requests
    assert (request.method, request.path, request.body) == ("POST", _SETUP_PATH, body)
    assert request.headers["content-type"] == "application/json"


def test_a_start_imbue_studio_refuses_ends_on_what_it_said(
    studio: _Studio, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    studio.start_answer = (
        400,
        {"error": "'model_id' is not a model name", "field": "model_id"},
    )

    exit_code, _summary, result = _run(
        ["start", "--model", "not a model"], _environ(studio, tmp_path), capsys
    )

    assert (exit_code, result["state"]) == (1, "STUDIO_ERROR")
    assert (
        result["detail"] == "Imbue Studio answered 400: 'model_id' is not a model name"
    )


def _wait(
    states: list[str], timeout_seconds: float = 600.0
) -> tuple[str, bool, _Clock]:
    """Wait on a setup that reports ``states`` in turn, the last one for good."""
    remaining = list(states)
    clock = _Clock()

    def read() -> dict[str, Any]:
        return _status(remaining.pop(0) if len(remaining) > 1 else remaining[0])

    status, is_timed_out = set_up_local_model.wait_for_change(
        read, timeout_seconds, clock, clock.sleep
    )
    return status["state"], is_timed_out, clock


def test_wait_ends_on_the_first_state_that_differs_from_the_one_it_began_with() -> None:
    state, is_timed_out, clock = _wait(
        ["INSTALLING_OLLAMA", "INSTALLING_OLLAMA", "DOWNLOADING_MODEL", "READY"]
    )

    assert (state, is_timed_out, clock.sleeps) == (
        "DOWNLOADING_MODEL",
        False,
        [5.0, 5.0],
    )


@pytest.mark.parametrize("state", ["READY", "FAILED", "UNAVAILABLE", "STUDIO_TOO_OLD"])
def test_wait_does_not_wait_on_a_state_that_waiting_cannot_change(state: str) -> None:
    assert _wait([state, "DOWNLOADING_MODEL"])[:2] == (state, False)
    assert _wait([state])[2].sleeps == []


def test_wait_runs_out_of_time_on_a_state_that_never_changes() -> None:
    state, is_timed_out, clock = _wait(["DOWNLOADING_MODEL"], timeout_seconds=12.0)

    assert (state, is_timed_out, clock.sleeps) == (
        "DOWNLOADING_MODEL",
        True,
        [5.0, 5.0, 5.0],
    )


def test_wait_rides_out_a_moment_in_which_imbue_studio_does_not_answer() -> None:
    state, is_timed_out, _clock = _wait(
        ["DOWNLOADING_MODEL", *["STUDIO_UNREACHABLE"] * 5, "DOWNLOADING_MODEL", "READY"]
    )

    assert (state, is_timed_out) == ("READY", False)


def test_wait_ends_once_imbue_studio_has_not_answered_for_a_minute() -> None:
    state, is_timed_out, clock = _wait(["DOWNLOADING_MODEL", "STUDIO_UNREACHABLE"])

    assert (state, is_timed_out, clock.now) == ("STUDIO_UNREACHABLE", False, 65.0)


def test_wait_that_began_without_an_answer_ends_when_imbue_studio_gives_one() -> None:
    state, is_timed_out, _clock = _wait(
        ["STUDIO_UNREACHABLE", "STUDIO_ERROR", "STUDIO_UNREACHABLE", "NOT_STARTED"]
    )

    assert (state, is_timed_out) == ("NOT_STARTED", False)


def test_the_wait_command_reports_a_change_and_a_time_out_apart(
    studio: _Studio, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ(studio, tmp_path)
    studio.setup_answers = [
        (200, _status("DOWNLOADING_MODEL")),
        (200, _status("FAILED", detail="The download stopped.")),
    ]

    changed = _run(["wait"], environ, capsys)

    assert (changed[0], changed[2]["state"]) == (1, "FAILED")
    assert changed[1].startswith("FAILED: The download stopped.")

    studio.setup_answers = [(200, _status("DOWNLOADING_MODEL"))]

    timed_out = _run(["wait", "--timeout", "12"], environ, capsys)

    assert (timed_out[0], timed_out[2]["state"]) == (3, "DOWNLOADING_MODEL")
    assert timed_out[2]["is_timed_out"] is True
    assert timed_out[1].startswith("No change in 12 seconds.\nDOWNLOADING_MODEL: ")


def test_the_context_copy_is_named_as_the_chat_app_names_its_own() -> None:
    for base_id in ("qwen3:8b", "llama3.2", "hf.co/someone/model"):
        assert set_up_local_model.context_model_id(
            base_id, 16_384
        ) == chat_context_model_id(base_id, 16_384)


def test_verify_connects_the_model_makes_its_copy_times_an_answer_and_records_where(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.serve_through_studio(studio)
    base_url = f"{ollama.url}/v1"

    exit_code, summary, result = _run(["verify"], _environ(studio, tmp_path), capsys)

    assert exit_code == 0
    (opened,) = studio.sent("POST", _TUNNEL_PATH)
    assert opened.body == {"port": 11434}
    (created,) = ollama.sent("POST", "/api/create")
    assert created.body == {
        "model": _COPY_ID,
        "from": "qwen3:8b",
        "parameters": {"num_ctx": 16_384},
        "stream": False,
    }
    (completion,) = ollama.sent("POST", "/v1/chat/completions")
    assert completion.body == {
        "model": _COPY_ID,
        "stream": False,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": "You answer in two plain sentences."},
            {"role": "user", "content": "What is a daily news digest for?"},
        ],
        "reasoning_effort": "none",
    }
    record = json.loads(_record_path(tmp_path).read_text())
    assert datetime.fromisoformat(record.pop("updated_at")).utcoffset() is not None
    assert record == {
        "base_url": base_url,
        "model": _COPY_ID,
        "base_model": "qwen3:8b",
        "context_window": 16_384,
        "reasoning_off_effort": "none",
    }
    assert {name: result[name] for name in record} == record
    assert (result["state"], result["is_verified"], result["route"]) == (
        "READY",
        True,
        "TUNNEL",
    )
    assert (result["output_tokens"], result["is_load_included"]) == (30, True)
    assert result["seconds"] >= 0
    assert result["tokens_per_second"] > 0
    assert summary.startswith(
        "READY: qwen3:8b on studio-mac answers from this workspace.\n"
        "It answered a short test in "
    )
    assert "which includes loading the model into memory" in summary
    assert (
        f"Refresh code reaches it at {base_url} as {_COPY_ID}, with a window of 16,384 tokens."
        in summary
    )


def test_verify_keeps_a_copy_that_names_the_size_and_a_model_already_in_memory(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.models[_COPY_ID] = 16_384
    ollama.loaded[_COPY_ID] = 16_384
    ollama.serve_through_studio(studio)

    exit_code, summary, result = _run(["verify"], _environ(studio, tmp_path), capsys)

    assert (exit_code, result["is_load_included"]) == (0, False)
    assert ollama.sent("POST", "/api/create") == []
    assert "loading the model" not in summary


def test_verify_does_not_guess_about_loading_when_the_server_does_not_say(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.is_saying_what_is_loaded = False
    ollama.serve_through_studio(studio)

    exit_code, summary, result = _run(["verify"], _environ(studio, tmp_path), capsys)

    assert (exit_code, result["is_load_included"], result["context_window"]) == (
        0,
        None,
        16_384,
    )
    assert "loading the model" not in summary


def test_verify_makes_the_copy_again_when_one_of_its_name_has_another_size(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.models[_COPY_ID] = 4_096
    ollama.serve_through_studio(studio)

    _run(["verify"], _environ(studio, tmp_path), capsys)

    assert ollama.models[_COPY_ID] == 16_384


def test_verify_takes_another_window_and_records_the_one_the_server_loaded(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.context_limit = 8_192
    ollama.serve_through_studio(studio)

    exit_code, _summary, result = _run(
        ["verify", "--context", "32768"], _environ(studio, tmp_path), capsys
    )

    assert (exit_code, result["model"], result["context_window"]) == (
        0,
        "qwen3:8b-ctx32768",
        8_192,
    )
    assert json.loads(_record_path(tmp_path).read_text())["context_window"] == 8_192


def test_verify_sends_no_reasoning_effort_to_a_model_that_does_not_reason(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.capabilities = ["completion", "tools"]
    ollama.serve_through_studio(studio)

    _exit_code, _summary, result = _run(["verify"], _environ(studio, tmp_path), capsys)

    (completion,) = ollama.sent("POST", "/v1/chat/completions")
    assert "reasoning_effort" not in completion.body
    assert result["reasoning_off_effort"] is None
    assert (
        json.loads(_record_path(tmp_path).read_text())["reasoning_off_effort"] is None
    )


def test_verify_reaches_the_model_directly_when_no_tunnel_can_be_opened(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.serve_through_studio(studio)
    studio.tunnel_answer = (
        502,
        {"error": "no way in", "code": "local_model_workspace_unreachable"},
    )
    closed = _Server()
    closed.close()

    exit_code, _summary, result = _run(
        ["verify"],
        _environ(studio, tmp_path),
        capsys,
        direct_base_urls=(f"{closed.url}/v1", f"{ollama.url}/v1"),
    )

    assert (exit_code, result["route"], result["base_url"]) == (
        0,
        "DIRECT",
        f"{ollama.url}/v1",
    )


def test_verify_says_what_stood_in_the_way_of_reaching_the_model(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.serve_through_studio(studio)
    environ = _environ(studio, tmp_path)
    listing = studio.servers_answer
    studio.servers_answer = (
        403,
        {"error": "This workspace may not.", "code": "local_model_not_granted"},
    )

    unlisted = _run(["verify"], environ, capsys)

    assert unlisted[2]["detail"].endswith(
        "from this workspace: Imbue Studio would not list the computer's model servers "
        "(it answered 403: This workspace may not.)."
    )

    studio.servers_answer = listing
    studio.tunnel_answer = (
        502,
        {
            "error": "Imbue Studio could not open a connection into the workspace.",
            "code": "local_model_workspace_unreachable",
        },
    )

    unconnected = _run(["verify"], environ, capsys)

    assert unconnected[2]["detail"].endswith(
        "from this workspace: Imbue Studio could not connect this workspace to that server "
        "(it answered 502: Imbue Studio could not open a connection into the workspace.)."
    )

    closed = _Server()
    closed.close()
    studio.tunnel_answer = (200, {"workspace_port": closed.port})

    silent = _run(["verify"], environ, capsys)

    assert silent[2]["detail"].endswith(
        f"from this workspace: nothing with it answers at {closed.url}/v1, the connection Imbue Studio "
        "opened, so the computer may have gone to sleep."
    )
    for exit_code, _summary, result in (unlisted, unconnected, silent):
        assert (exit_code, result["state"], result["is_verified"]) == (
            1,
            "READY",
            False,
        )
    assert ollama.requests == []
    assert not _record_path(tmp_path).exists()


def test_verify_can_be_pointed_at_another_model_the_computer_has(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.models["llama3.2:latest"] = None
    ollama.serve_through_studio(studio)
    installed = [{"model_id": "qwen3:8b"}, {"model_id": "llama3.2:latest"}]
    studio.setup_answers = [(200, _status("READY", models=installed))]
    environ = _environ(studio, tmp_path)

    exit_code, _summary, result = _run(
        ["verify", "--model", "llama3.2"], environ, capsys
    )

    assert (exit_code, result["base_model"], result["model"]) == (
        0,
        "llama3.2:latest",
        "llama3.2:latest-ctx16384",
    )

    exit_code, summary, result = _run(["verify", "--model", "phi4"], environ, capsys)

    assert (exit_code, result["is_verified"]) == (1, False)
    assert "does not have phi4" in summary
    assert "`start --model phi4`" in summary


def test_verify_does_nothing_before_the_setup_is_ready(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.serve_through_studio(studio)
    studio.setup_answers = [
        (200, _status("DOWNLOADING_MODEL", detail="Downloading qwen3:8b."))
    ]

    exit_code, summary, result = _run(["verify"], _environ(studio, tmp_path), capsys)

    assert (exit_code, result) == (
        1,
        {
            "state": "DOWNLOADING_MODEL",
            "is_verified": False,
            "detail": "The setup is not ready (DOWNLOADING_MODEL: Downloading qwen3:8b.).",
        },
    )
    assert summary == f"Not verified. {result['detail']}"
    assert ollama.requests == []
    assert not _record_path(tmp_path).exists()


def test_verify_of_a_named_model_touches_no_server_for_a_workspace_not_yet_allowed(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.serve_through_studio(studio)
    studio.setup_answers = [
        (403, {"error": "This workspace may not.", "code": "local_model_not_granted"})
    ]

    exit_code, summary, result = _run(
        ["verify", "--model", "qwen3:8b"],
        _environ(studio, tmp_path),
        capsys,
        direct_base_urls=(f"{ollama.url}/v1",),
    )

    assert (exit_code, result["state"], result["is_verified"]) == (
        1,
        "NOT_GRANTED",
        False,
    )
    assert result["detail"].startswith("The setup is not ready (NOT_GRANTED: ")
    assert "start --model" not in summary
    assert [request.path for request in studio.requests] == [_SETUP_PATH]
    assert ollama.requests == []
    assert not _record_path(tmp_path).exists()


@pytest.mark.parametrize(
    ("break_it", "fragment"),
    [
        (
            "no_route",
            "no model server with it answers from this workspace: Imbue Studio lists no model server that has it.",
        ),
        ("no_copy", "would not set qwen3:8b up for a context of 16,384 tokens"),
        (
            "no_description",
            "would not say whether qwen3:8b reasons before it answers (it answered 500: busy)",
        ),
        ("no_answer", "answered 500 to the test: out of memory"),
        ("no_text", f"{_COPY_ID} answered the test with no text."),
    ],
)
def test_a_model_that_cannot_be_shown_to_answer_is_not_recorded(
    studio: _Studio,
    ollama: _Ollama,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    break_it: str,
    fragment: str,
) -> None:
    if break_it != "no_route":
        ollama.serve_through_studio(studio)
    ollama.create_status = 500 if break_it == "no_copy" else 200
    if break_it == "no_description":
        ollama.undescribed = {"qwen3:8b"}
    if break_it == "no_answer":
        ollama.completion_answer = (500, {"error": "out of memory"})
    if break_it == "no_text":
        ollama.completion_answer = (200, {"choices": [{"message": {"content": ""}}]})

    exit_code, _summary, result = _run(["verify"], _environ(studio, tmp_path), capsys)

    assert (exit_code, result["state"], result["is_verified"]) == (1, "READY", False)
    assert fragment in result["detail"]
    assert not _record_path(tmp_path).exists()
