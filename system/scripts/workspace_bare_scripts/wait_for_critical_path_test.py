"""Tests for wait_for_critical_path.py: the wait on the critical-path apps, and the exec that follows it.

Each app is stood in for by a real listening socket on loopback, registered in a registry under
the test's own directory.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from app_registration import forward_port
from oom_priority.supervisord_testing import supervisord_commands

from workspace_bare_scripts import wait_for_critical_path

_STUB = Path(__file__).resolve().parents[1] / "wait_for_critical_path.py"
_SUPERVISORD_CONF = Path(__file__).resolve().parents[2] / "supervisord.conf"


def _write_registry(registry: Path, url_by_name: dict[str, str]) -> None:
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        forward_port.dump_registry(
            [{"name": name, "url": url} for name, url in url_by_name.items()]
        )
    )


@pytest.fixture
def listening_url() -> Iterator[str]:
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"


@pytest.fixture
def closed_url() -> str:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_returns_at_once_when_every_app_is_serving(
    tmp_path: Path, listening_url: str
) -> None:
    registry = tmp_path / "apps.toml"
    _write_registry(
        registry, {"system_interface": listening_url, "chat": listening_url}
    )

    started = time.monotonic()
    waiting_for = wait_for_critical_path.wait_for_apps(
        registry, ("system_interface", "chat"), 10.0, 0.05
    )

    assert waiting_for == []
    assert time.monotonic() - started < 5.0


def test_names_the_apps_still_down_at_the_deadline(
    tmp_path: Path, listening_url: str, closed_url: str
) -> None:
    registry = tmp_path / "apps.toml"
    _write_registry(registry, {"system_interface": listening_url, "chat": closed_url})

    waiting_for = wait_for_critical_path.wait_for_apps(
        registry, ("system_interface", "chat"), 0.3, 0.05
    )

    assert waiting_for == ["chat"]


def test_an_app_that_has_accepted_is_not_probed_again(
    tmp_path: Path, closed_url: str
) -> None:
    with socket.create_server(("127.0.0.1", 0)) as serving:
        registry = tmp_path / "apps.toml"
        _write_registry(
            registry,
            {
                "system_interface": f"http://127.0.0.1:{serving.getsockname()[1]}",
                "chat": closed_url,
            },
        )

        waiting_for = wait_for_critical_path.wait_for_apps(
            registry, ("system_interface", "chat"), 0.3, 0.05
        )

        serving.setblocking(False)
        accepted: list[socket.socket] = []
        try:
            while True:
                accepted.append(serving.accept()[0])
        except BlockingIOError:
            pass
        finally:
            for connection in accepted:
                connection.close()

    assert waiting_for == ["chat"]
    assert len(accepted) == 1


def test_an_unregistered_app_and_a_missing_registry_are_waited_for(
    tmp_path: Path, listening_url: str
) -> None:
    missing = tmp_path / "missing" / "apps.toml"
    assert wait_for_critical_path.wait_for_apps(
        missing, ("system_interface", "chat"), 0.2, 0.05
    ) == ["system_interface", "chat"]

    registry = tmp_path / "apps.toml"
    _write_registry(registry, {"system_interface": listening_url})
    assert wait_for_critical_path.wait_for_apps(
        registry, ("system_interface", "chat"), 0.2, 0.05
    ) == ["chat"]


def test_an_unparseable_registry_is_waited_on_rather_than_raised(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text("[[apps]\nname = ")

    assert wait_for_critical_path.wait_for_apps(
        registry, ("system_interface",), 0.2, 0.05
    ) == ["system_interface"]
    problem = wait_for_critical_path._registry_problem(registry)
    assert problem is not None and "not valid TOML" in problem


def test_only_an_unreadable_registry_is_reported_as_a_problem(
    tmp_path: Path, listening_url: str
) -> None:
    assert wait_for_critical_path._registry_problem(tmp_path / "missing.toml") is None

    healthy = tmp_path / "apps.toml"
    _write_registry(healthy, {"system_interface": listening_url})
    assert wait_for_critical_path._registry_problem(healthy) is None

    not_an_array = tmp_path / "not_an_array.toml"
    not_an_array.write_text('apps = "system_interface"\n')
    problem = wait_for_critical_path._registry_problem(not_an_array)
    assert problem is not None and "not an array" in problem


def test_the_deadline_line_names_the_apps_down_and_an_unreadable_registry(
    tmp_path: Path, listening_url: str
) -> None:
    healthy = tmp_path / "apps.toml"
    _write_registry(healthy, {"system_interface": listening_url})
    assert (
        wait_for_critical_path._deadline_message(healthy, ["chat"], 12.0)
        == "wait_for_critical_path: chat not serving after 12s; starting anyway"
    )

    unparseable = tmp_path / "unparseable.toml"
    unparseable.write_text("[[apps]\nname = ")
    message = wait_for_critical_path._deadline_message(
        unparseable, ["system_interface", "chat"], 30.0
    )
    assert message.startswith(
        "wait_for_critical_path: system_interface, chat not serving after 30s ("
    )
    assert "not valid TOML" in message
    assert message.endswith("); starting anyway")


def test_a_url_with_an_unparseable_port_is_waited_on_rather_than_raised(
    tmp_path: Path, listening_url: str
) -> None:
    registry = tmp_path / "apps.toml"
    _write_registry(
        registry,
        {"system_interface": listening_url, "chat": "http://127.0.0.1:notaport"},
    )

    assert wait_for_critical_path.wait_for_apps(
        registry, ("system_interface", "chat"), 0.2, 0.05
    ) == ["chat"]


def test_returns_once_a_late_app_registers_and_listens(
    tmp_path: Path, listening_url: str
) -> None:
    registry = tmp_path / "apps.toml"
    _write_registry(registry, {"system_interface": listening_url})
    late_servers: list[socket.socket] = []

    def _listen_and_register() -> None:
        late_server = socket.create_server(("127.0.0.1", 0))
        late_servers.append(late_server)
        late_url = f"http://127.0.0.1:{late_server.getsockname()[1]}"
        _write_registry(registry, {"system_interface": listening_url, "chat": late_url})

    registered = threading.Timer(0.3, _listen_and_register)
    registered.start()
    try:
        waiting_for = wait_for_critical_path.wait_for_apps(
            registry, ("system_interface", "chat"), 10.0, 0.05
        )
    finally:
        registered.join()
        for late_server in late_servers:
            late_server.close()

    assert waiting_for == []
    assert len(late_servers) == 1


def test_the_stub_execs_its_command_under_a_bare_python3(
    tmp_path: Path, listening_url: str
) -> None:
    registry = tmp_path / "apps.toml"
    _write_registry(
        registry, {"system_interface": listening_url, "chat": listening_url}
    )
    marker = tmp_path / "ran"

    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-s",
            str(_STUB),
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').write('ran')",
        ],
        env={**os.environ, "MINDS_APPS_FILE": str(registry)},
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert marker.read_text() == "ran"


def test_the_stub_refuses_to_run_without_a_command() -> None:
    completed = subprocess.run(
        [sys.executable, str(_STUB)], capture_output=True, text=True, timeout=20
    )

    assert completed.returncode == 2
    assert "usage" in completed.stderr


def test_no_critical_path_program_waits_on_the_critical_path() -> None:
    # Prefixed, a critical-path program would wait for itself until the deadline on every start.
    commands = supervisord_commands(_SUPERVISORD_CONF, ("program", "eventlistener"))
    for name in wait_for_critical_path.CRITICAL_PATH_APPS:
        assert name in commands, name
        assert "wait_for_critical_path.py" not in commands[name], name
