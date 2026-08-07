"""The owner-exec HTTP surface: run commands and read/write files, SSH-equivalently.

The hosted minds chrome (and the desktop, over the local forward channel)
drive a web-created workspace through this service instead of an SSH session:
it runs a command (streaming stdout/stderr + the exit code as newline-delimited
JSON), reads a file, or writes one. Every request is authenticated by an
Ed25519 signature over the request envelope (see :mod:`owner_exec.signing`),
so authorization equals possession of a key in the workspace's
``authorized_keys`` -- exactly SSH's model. A dedicated ``/grants`` endpoint
is the single writer of the sharing grants document.
"""

import base64
import json
import queue
import subprocess
import tempfile
import threading
import tomllib
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

from flask import Flask
from flask import Response
from flask import request

from owner_exec.signing import ExecAuthError
from owner_exec.signing import NonceCache
from owner_exec.signing import current_unix_time
from owner_exec.signing import parse_authorized_ed25519_keys
from owner_exec.signing import verify_exec_envelope

# Header names carrying the signed envelope.
_SIGNATURE_HEADER = "X-Exec-Signature"
_PUBLIC_KEY_HEADER = "X-Exec-Public-Key"
_TIMESTAMP_HEADER = "X-Exec-Timestamp"
_NONCE_HEADER = "X-Exec-Nonce"

# The grants document this service is the single writer of.
_GRANTS_RELATIVE_PATH = "data/.secrets/share_grants.toml"

# Cap a single command's runtime so a wedged process cannot pin a worker
# thread forever. The caller may request a shorter timeout.
_DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0
_MAX_COMMAND_TIMEOUT_SECONDS = 3600.0


class OwnerExecConfig:
    """Everything the owner-exec app needs to serve, resolved once at startup."""

    def __init__(
        self,
        audience_resolver: Callable[[], str],
        authorized_keys_path: Path,
        repo_root: Path,
        nonce_cache: NonceCache,
        now: Callable[[], float],
    ) -> None:
        # Returns the workspace's own share domain (an envelope must bind to
        # it), read fresh so enabling/disabling sharing needs no restart; "" when
        # the workspace is not shared, which disables exec.
        self.audience_resolver = audience_resolver
        self.authorized_keys_path = authorized_keys_path
        self.repo_root = repo_root
        self.nonce_cache = nonce_cache
        self.now = now


def build_owner_exec_app(config: OwnerExecConfig) -> Flask:
    app = Flask(__name__)

    def _authenticate() -> None:
        """Verify the request's signed envelope, raising ExecAuthError on failure."""
        audience = config.audience_resolver()
        if not audience:
            raise ExecAuthError("workspace is not shared, so exec is unavailable")
        try:
            authorized_text = config.authorized_keys_path.read_text()
        except OSError as exc:
            raise ExecAuthError(f"could not read authorized_keys: {exc}") from exc
        verify_exec_envelope(
            method=request.method,
            path=request.path,
            body=request.get_data(),
            audience=audience,
            signature_b64=request.headers.get(_SIGNATURE_HEADER, ""),
            public_key_ssh=request.headers.get(_PUBLIC_KEY_HEADER, ""),
            timestamp=request.headers.get(_TIMESTAMP_HEADER, ""),
            nonce=request.headers.get(_NONCE_HEADER, ""),
            authorized_keys=parse_authorized_ed25519_keys(authorized_text),
            nonce_cache=config.nonce_cache,
            now=config.now(),
        )

    @app.errorhandler(ExecAuthError)
    def _handle_auth_error(exc: ExecAuthError) -> Response:
        return app.response_class(
            response=json.dumps({"error": str(exc)}), status=401, mimetype="application/json"
        )

    @app.get("/_alive")
    def alive() -> Response:
        # Unauthenticated loopback liveness for supervisord / the forward probe.
        return Response(status=204)

    @app.post("/run")
    def run() -> Response:
        _authenticate()
        payload = request.get_json(silent=True) or {}
        command = payload.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command) or not command:
            return _bad_request("command must be a non-empty list of strings")
        cwd = _resolve_cwd(config.repo_root, payload.get("cwd"))
        timeout_seconds = _resolve_timeout(payload.get("timeout_seconds"))
        return Response(
            _stream_command(command, cwd=cwd, timeout_seconds=timeout_seconds),
            mimetype="application/x-ndjson",
        )

    @app.post("/read-file")
    def read_file() -> Response:
        _authenticate()
        payload = request.get_json(silent=True) or {}
        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _bad_request("path must be a non-empty string")
        target = _resolve_path(config.repo_root, raw_path)
        try:
            content = target.read_bytes()
        except FileNotFoundError:
            return app.response_class(
                response=json.dumps({"exists": False}), status=404, mimetype="application/json"
            )
        except OSError as exc:
            return _bad_request(f"could not read {raw_path}: {exc}")
        return app.response_class(
            response=json.dumps({"exists": True, "content_b64": base64.b64encode(content).decode("ascii")}),
            mimetype="application/json",
        )

    @app.post("/write-file")
    def write_file() -> Response:
        _authenticate()
        payload = request.get_json(silent=True) or {}
        raw_path = payload.get("path")
        content_b64 = payload.get("content_b64")
        if not isinstance(raw_path, str) or not raw_path:
            return _bad_request("path must be a non-empty string")
        if not isinstance(content_b64, str):
            return _bad_request("content_b64 must be a base64 string")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError):
            return _bad_request("content_b64 is not valid base64")
        mode = _resolve_mode(payload.get("mode"))
        target = _resolve_path(config.repo_root, raw_path)
        try:
            _atomic_write(target, content, mode)
        except OSError as exc:
            return _bad_request(f"could not write {raw_path}: {exc}")
        return app.response_class(response=json.dumps({"written": True}), mimetype="application/json")

    @app.get("/grants")
    def get_grants() -> Response:
        _authenticate()
        grants_path = config.repo_root / _GRANTS_RELATIVE_PATH
        try:
            text = grants_path.read_text()
        except FileNotFoundError:
            return app.response_class(response=json.dumps({"grants_toml": ""}), mimetype="application/json")
        except OSError as exc:
            return _bad_request(f"could not read grants: {exc}")
        return app.response_class(response=json.dumps({"grants_toml": text}), mimetype="application/json")

    @app.put("/grants")
    def put_grants() -> Response:
        _authenticate()
        payload = request.get_json(silent=True) or {}
        grants_toml = payload.get("grants_toml")
        if not isinstance(grants_toml, str):
            return _bad_request("grants_toml must be a string")
        try:
            tomllib.loads(grants_toml)
        except tomllib.TOMLDecodeError as exc:
            # Reject malformed TOML before writing: a bad grants file fails
            # closed at the gateway, which would lock the owner out.
            return _bad_request(f"grants_toml is not valid TOML: {exc}")
        grants_path = config.repo_root / _GRANTS_RELATIVE_PATH
        try:
            _atomic_write(grants_path, grants_toml.encode("utf-8"), 0o600)
        except OSError as exc:
            return _bad_request(f"could not write grants: {exc}")
        return app.response_class(response=json.dumps({"written": True}), mimetype="application/json")

    def _bad_request(message: str) -> Response:
        return app.response_class(response=json.dumps({"error": message}), status=400, mimetype="application/json")

    return app


def _resolve_timeout(raw: object) -> float:
    if raw is None:
        return _DEFAULT_COMMAND_TIMEOUT_SECONDS
    try:
        requested = float(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return _DEFAULT_COMMAND_TIMEOUT_SECONDS
    return max(1.0, min(requested, _MAX_COMMAND_TIMEOUT_SECONDS))


def _resolve_mode(raw: object) -> int:
    if isinstance(raw, str) and raw:
        try:
            return int(raw, 8)
        except ValueError:
            return 0o600
    return 0o600


def _resolve_path(repo_root: Path, raw_path: str) -> Path:
    """Resolve a request path against the repo root (an absolute path is honored).

    The owner already has command-execution parity with SSH, so file access is
    deliberately not confined below the repo root; a relative path is simply
    anchored there for convenience.
    """
    candidate = Path(raw_path)
    return candidate if candidate.is_absolute() else (repo_root / candidate)


def _resolve_cwd(repo_root: Path, raw_cwd: object) -> Path:
    if isinstance(raw_cwd, str) and raw_cwd:
        return _resolve_path(repo_root, raw_cwd)
    return repo_root


def _atomic_write(target: Path, content: bytes, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "wb") as handle:
            handle.write(content)
        tmp_path.chmod(mode)
        tmp_path.rename(target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _stream_command(command: list[str], *, cwd: Path, timeout_seconds: float) -> Iterator[bytes]:
    """Run ``command`` and yield newline-delimited JSON events for its output + exit.

    Stdout and stderr are read on separate threads into one queue so their
    interleaving is preserved and neither pipe can deadlock. The final event
    carries the exit code (or a timeout marker).
    """
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    events: queue.Queue[tuple[str, str] | None] = queue.Queue()

    def _pump(stream: object, stream_name: str) -> None:
        for chunk in iter(stream.readline, b""):  # type: ignore[attr-defined]
            events.put((stream_name, chunk.decode("utf-8", errors="replace")))
        events.put(None)

    stdout_thread = threading.Thread(target=_pump, args=(process.stdout, "stdout"), daemon=True)
    stderr_thread = threading.Thread(target=_pump, args=(process.stderr, "stderr"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    finished_pumps = 0
    while finished_pumps < 2:
        event = events.get()
        if event is None:
            finished_pumps += 1
            continue
        stream_name, text = event
        yield (json.dumps({"type": stream_name, "data": text}) + "\n").encode("utf-8")

    try:
        exit_code = process.wait(timeout=timeout_seconds)
        yield (json.dumps({"type": "exit", "code": exit_code}) + "\n").encode("utf-8")
    except subprocess.TimeoutExpired:
        process.kill()
        yield (json.dumps({"type": "exit", "code": None, "timed_out": True}) + "\n").encode("utf-8")
