"""Fakes the tests share: a /proc tree, the chat app, supervisord's socket, and the command runner."""

import json
import shutil
import socketserver
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from xmlrpc.server import SimpleXMLRPCDispatcher
from xmlrpc.server import SimpleXMLRPCRequestHandler

import httpx


def write_fake_process(
    proc_dir: Path,
    pid: int,
    command_name: str,
    parent_pid: int,
    rss_kib: int | None,
    oom_score_adj: int,
    command_line: Sequence[str],
    is_gvisor: bool = False,
    anonymous_kib: Sequence[int] = (),
    swap_kib: int = 0,
) -> None:
    """One process directory as Linux (``RssAnon`` present) or gVisor (no ``RssAnon``; ``smaps`` holds the anonymous
    sizes) writes it. ``rss_kib`` None writes no ``VmRSS`` line, as for a kernel thread."""
    process_dir = proc_dir / str(pid)
    process_dir.mkdir(parents=True)
    status_lines = [f"Name:\t{command_name}", f"PPid:\t{parent_pid}", "Threads:\t1"]
    if rss_kib is not None:
        status_lines += [f"VmSize:\t{rss_kib * 4 + 1000} kB", f"VmRSS:\t{rss_kib} kB", f"VmSwap:\t{swap_kib} kB"]
        if not is_gvisor:
            status_lines.append(f"RssAnon:\t{rss_kib} kB")
    (process_dir / "status").write_text("\n".join(status_lines) + "\n")
    (process_dir / "comm").write_text(f"{command_name}\n")
    (process_dir / "oom_score_adj").write_text(f"{oom_score_adj}\n")
    (process_dir / "cmdline").write_bytes(b"\0".join(part.encode() for part in command_line) + b"\0")
    (process_dir / "smaps").write_text("".join(f"Anonymous:     {size} kB\n" for size in anonymous_kib))


def chat_snapshot(
    chat_id: str, title: str, status: str, agent_id: str, harness: str, earlier_agent_ids: Sequence[str] = ()
) -> dict[str, Any]:
    """One chat as the chat app's ``GET /api/chats`` lists it, with the fields this app reads; ``earlier_agent_ids``
    are agents it ran on before switching account."""
    return {
        "chat_id": chat_id,
        "title": title,
        "name": title.lower().replace(" ", "-"),
        "status": status,
        "agent_ids": [*earlier_agent_ids, agent_id],
        "last_messaged_at": 1_790_000_000.0,
        "active_agent": {"agent_id": agent_id, "harness": harness, "state": "RUNNING"},
    }


class FakeChatApp:
    """The chat app's list, stop and start routes, recording every action it is asked for."""

    def __init__(self, chats: Sequence[Mapping[str, Any]], list_status: int = 200) -> None:
        self.chats = list(chats)
        self.list_status = list_status
        self.actions: list[tuple[str, str]] = []
        self.hosts: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(f"{request.url.host}:{request.url.port}" if request.url.port else request.url.host)
        path = request.url.path
        if request.method == "GET" and path == "/api/chats":
            return httpx.Response(self.list_status, json={"chats": self.chats})
        parts = path.strip("/").split("/")
        if request.method == "POST" and len(parts) == 4 and parts[:2] == ["api", "chats"]:
            if not any(chat["chat_id"] == parts[2] for chat in self.chats):
                return httpx.Response(404, json={"detail": f"Chat '{parts[2]}' not found"})
            self.actions.append((parts[2], parts[3]))
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"detail": "no such route"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


def process_info(name: str, statename: str, pid: int) -> dict[str, Any]:
    """One ``getAllProcessInfo`` entry, as supervisord answers it (pid 0 for a program that is not running)."""
    return {"name": name, "group": name, "statename": statename, "pid": pid}


class FakeRunner:
    """A command runner answering each argv's first word from a table, recording what it ran."""

    def __init__(self, results_by_command: Mapping[str, subprocess.CompletedProcess[str] | Exception]) -> None:
        self.results_by_command = dict(results_by_command)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        result = self.results_by_command[argv[0]]
        if isinstance(result, Exception):
            raise result
        return result


def completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def write_registry(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """An app registry (data/.state/apps.toml) holding ``rows``, written as the TOML forward_port.py writes."""

    def toml_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return json.dumps(value)

    blocks = ["[[apps]]\n" + "".join(f"{key} = {toml_value(value)}\n" for key, value in row.items()) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks))


class _UnixXmlRpcRequestHandler(SimpleXMLRPCRequestHandler):
    # TCP_NODELAY is a TCP option; setting it on a unix socket fails.
    disable_nagle_algorithm = False


class _UnixXmlRpcServer(socketserver.UnixStreamServer, SimpleXMLRPCDispatcher):
    logRequests = False

    def __init__(self, socket_path: Path) -> None:
        socketserver.UnixStreamServer.__init__(self, str(socket_path), _UnixXmlRpcRequestHandler)
        SimpleXMLRPCDispatcher.__init__(self, allow_none=True, encoding=None)


@contextmanager
def fake_supervisor_socket(all_process_info: object) -> Iterator[Path]:
    """supervisord's XML-RPC socket, answering ``getAllProcessInfo`` with ``all_process_info``; yields its path.

    The socket lives in a short directory under /tmp: a unix socket's path is capped at about 104 bytes on macOS,
    which pytest's ``tmp_path`` there exceeds."""
    socket_dir = Path(tempfile.mkdtemp(prefix="activity-", dir="/tmp"))
    socket_path = socket_dir / "s.sock"
    server = _UnixXmlRpcServer(socket_path)
    server.register_function(lambda: all_process_info, "supervisor.getAllProcessInfo")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        shutil.rmtree(socket_dir, ignore_errors=True)
