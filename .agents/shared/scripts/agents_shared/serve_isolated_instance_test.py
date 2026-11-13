"""Tests for ``serve_isolated_instance.py``.

Run via: ``uv run --no-sync pytest .agents/shared/scripts/agents_shared/serve_isolated_instance_test.py``

Like the ``preview_app.py`` tests, these inject a recording
``Runner`` (so no real process group is signalled), a recording ``Registry`` (so
no real registry is written), a programmable
``HttpClient`` (so the health probe is deterministic), a fake ``Spawner`` (so no
throwaway server is launched), and a no-op sleeper. We assert on the exact env
overrides / commands the ``up`` motion produces and on the teardown-on-failure
control flow, which must never regress: a leaked server holds a port and a leaked
service registration routes the live UI at a dead port.
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest
from app_registration import forward_port
from workspace_layout.answers import InventoryClient, InventoryDocument
from workspace_layout.primitives import ClientId, DesktopId
from workspace_layout.shell_url import INVENTORY_ROUTE
from workspace_layout.testing import (
    FAKE_TIME,
    LoopbackShell,
    fake_desktop,
    fake_window,
)

from agents_shared import preview_wrapper_server as wrapper_mod
from agents_shared import serve_isolated_instance as mod
from agents_shared.preview_wrapper_server import FrameKind

_SCRIPT = Path(__file__).resolve().parent / "serve_isolated_instance.py"

_NAME = "demo"
_PORT_ENV = "MYSVC_PORT"
_LAUNCH = ["uv", "run", "my-service"]


@dataclass
class _RecordingRunner(mod.Runner):
    """Records every process-group signal; reports ``alive_pids`` as still running."""

    killed_pgroups: list[int] = field(default_factory=list)
    kill_signals: list[tuple[int, int]] = field(default_factory=list)
    # Pids reported as still-alive by ``process_group_alive`` (default: none, so a
    # just-killed process reads as gone immediately -- the common refresh path).
    alive_pids: set[int] = field(default_factory=set)
    # What ``process_start_time`` reports per pid (default: '', unknown, as on macOS).
    start_times: dict[int, str] = field(default_factory=dict)

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        self.killed_pgroups.append(pid)
        self.kill_signals.append((pid, sig))

    def killed_pgroup(self, pid: int) -> bool:
        return pid in self.killed_pgroups

    def signals_sent_to(self, pid: int) -> list[int]:
        return [sig for killed_pid, sig in self.kill_signals if killed_pid == pid]

    def process_group_alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def process_start_time(self, pid: int) -> str:
        return self.start_times.get(pid, "")


@dataclass
class _SigtermProofRunner(_RecordingRunner):
    """A process group that ignores SIGTERM and dies only once SIGKILLed."""

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        super().kill_process_group(pid, sig)
        if sig == signal.SIGKILL:
            self.alive_pids.discard(pid)


class _SignalRecordingRealRunner(mod.Runner):
    """The real process-group runner, recording which signals it sent."""

    def __init__(self) -> None:
        self.sent: list[int] = []

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        self.sent.append(sig)
        super().kill_process_group(pid, sig)


@dataclass(frozen=True)
class _Registration:
    service_name: str
    url: str
    internal: bool
    display_name: str | None


@dataclass
class _RecordingRegistry(mod.Registry):
    """Records registrations and removals; refuses names in ``refused``."""

    registered: list[_Registration] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refused: frozenset[str] = frozenset()

    def register(
        self,
        repo_root: Path,
        service_name: str,
        url: str,
        *,
        internal: bool,
        display_name: str | None,
        theming_mode: str | None = None,
    ) -> None:
        if service_name in self.refused:
            raise forward_port.RegistrationError(f"refusing {service_name}")
        self.registered.append(_Registration(service_name, url, internal, display_name))

    def remove(self, repo_root: Path, service_name: str) -> None:
        self.removed.append(service_name)

    def by_name(self) -> dict[str, _Registration]:
        return {entry.service_name: entry for entry in self.registered}


@dataclass
class _RecordingDesktops(mod.Desktops):
    """Reports ``open_windows`` showing the apps they name; records every close, refusing ids in ``unclosable``."""

    open_windows: list[mod.OpenWindow] = field(default_factory=list)
    unclosable: frozenset[str] = frozenset()
    is_unreadable: bool = False
    reads: int = 0
    closed: list[str] = field(default_factory=list)

    def windows_of(self, apps: Sequence[str]) -> list[mod.OpenWindow]:
        self.reads += 1
        if self.is_unreadable:
            raise mod.InstanceError("could not reach the shell")
        return [window for window in self.open_windows if window.app in apps]

    def close(self, window: mod.OpenWindow) -> None:
        if window.window_id in self.unclosable:
            raise mod.InstanceError(f"could not close window {window.window_id}")
        self.closed.append(window.window_id)


class _FakeHttp(mod.HttpClient):
    """Returns whatever ``responder(url)`` yields for GETs, with a canned body."""

    def __init__(self, responder: Callable[[str], int | None], body: str = "") -> None:
        self._responder = responder
        self._body = body
        self.get_urls: list[str] = []

    def get(self, url: str, timeout: float) -> mod.ProbeResult:
        self.get_urls.append(url)
        status = self._responder(url)
        return mod.ProbeResult(status, self._body if status is not None else "")


@dataclass
class _FakeSpawner(mod.Spawner):
    detached_spawns: list[list[str]] = field(default_factory=list)
    detached_envs: list[dict] = field(default_factory=list)
    detached_cwds: list[str] = field(default_factory=list)
    detached_pid: int = 4242
    detached_raises: BaseException | None = None
    detached_pids: list[int] = field(default_factory=list)
    # What the "spawned" process writes to its log. Appended after the caller's
    # boot marker, exactly as a real child's output would land.
    log_output: str = ""

    def spawn_detached(
        self, argv: Sequence[str], cwd: str, env: dict, log_path: str
    ) -> int:
        self.detached_spawns.append(list(argv))
        self.detached_envs.append(dict(env))
        self.detached_cwds.append(cwd)
        if self.log_output:
            with open(log_path, "a") as handle:
                handle.write(self.log_output)
        if self.detached_raises is not None:
            raise self.detached_raises
        pid = self.detached_pid + len(self.detached_pids)
        self.detached_pids.append(pid)
        return pid


@dataclass
class _RecordingVenvLinker(mod.VenvLinker):
    linked: list[Path] = field(default_factory=list)
    raises: OSError | None = None

    def link_project(self, project_dir: Path) -> None:
        if self.raises is not None:
            raise self.raises
        self.linked.append(project_dir)


def _all_healthy(_url: str) -> int:
    return 200


def _plenty_of_disk(_path: Path) -> int:
    return 1024**4


def _up(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    registry: _RecordingRegistry | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
    venv_linker: _RecordingVenvLinker | None = None,
    cwd: Path | None = None,
    **kwargs,
) -> int:
    return mod.up(
        _NAME,
        _LAUNCH,
        str(cwd if cwd is not None else tmp_path),
        tmp_path,
        port_env_by_name=kwargs.pop(
            "port_env_by_name", {mod.MAIN_PORT_NAME: _PORT_ENV}
        ),
        runner=runner or _RecordingRunner(),
        registry=registry or _RecordingRegistry(),
        http=http or _FakeHttp(_all_healthy),
        spawner=spawner or _FakeSpawner(),
        venv_linker=venv_linker or _RecordingVenvLinker(),
        sleeper=lambda _seconds: None,
        **kwargs,
    )


def _state_path(tmp_path: Path) -> Path:
    return mod._state_path(tmp_path, _NAME)


def test_the_self_hint_still_names_where_this_script_actually_lives() -> None:
    # ``up``'s boot-success hint prints ``uv run --no-sync <_SELF_HINT> refresh|down
    # --name ...`` as commands the agent copies, and the system-interface flow
    # learns its instance name from that hint -- so the spelling has to stay
    # runnable. A move or rename would leave the hardcoded constant naming a path
    # that no longer exists, which nothing else here would notice.
    assert _SCRIPT.is_file()
    assert str(_SCRIPT).endswith(mod._SELF_HINT)


# bare instance (own testing)


def test_up_boots_on_a_port_injects_port_env_and_reports_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spawner = _FakeSpawner()

    code = _up(tmp_path, spawner=spawner)

    assert code == 0
    # Launched the given command from the given cwd.
    assert spawner.detached_spawns[0] == _LAUNCH
    assert spawner.detached_cwds[0] == str(tmp_path)
    # The chosen free port was injected into the named env var.
    env = spawner.detached_envs[0]
    assert env[_PORT_ENV]
    injected_port = int(env[_PORT_ENV])
    # The loopback URL (with the injected port) is printed to stdout for capture.
    out = capsys.readouterr().out.strip()
    assert out == f"http://127.0.0.1:{injected_port}"
    # State records the single server and no services.
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["pids"] == spawner.detached_pids
    assert state["services"] == []
    assert state["inner_port"] == injected_port


def test_up_bare_instance_registers_no_service(tmp_path: Path) -> None:
    registry = _RecordingRegistry()

    code = _up(tmp_path, registry=registry)

    assert code == 0
    assert not registry.registered


def test_up_applies_env_overrides_and_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A var present in the ambient env is removed; an override is added.
    monkeypatch.setenv("MNGR_AGENT_ID", "live-agent")
    spawner = _FakeSpawner()

    code = _up(
        tmp_path,
        spawner=spawner,
        env_overrides={"MYSVC_DATA_DIR": "/tmp/scratch"},
        unset_env=["MNGR_AGENT_ID"],
        host_env="MYSVC_HOST",
    )

    assert code == 0
    env = spawner.detached_envs[0]
    assert "MNGR_AGENT_ID" not in env
    assert env["MYSVC_DATA_DIR"] == "/tmp/scratch"
    assert env["MYSVC_HOST"] == "127.0.0.1"


def test_up_rejects_a_missing_cwd(tmp_path: Path) -> None:
    spawner = _FakeSpawner()

    code = _up(tmp_path, spawner=spawner, cwd=tmp_path / "gone")

    assert code == 1
    assert not spawner.detached_spawns
    assert not _state_path(tmp_path).exists()


def test_up_tears_down_when_the_boot_raises(tmp_path: Path) -> None:
    registry = _RecordingRegistry()
    spawner = _FakeSpawner(detached_raises=FileNotFoundError("uv not found"))

    code = _up(tmp_path, registry=registry, spawner=spawner)

    assert code == 1
    assert not registry.registered
    assert not _state_path(tmp_path).exists()


def test_up_tears_down_when_the_instance_never_gets_healthy(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()
    http = _FakeHttp(lambda _url: None)  # never returns 200

    code = _up(tmp_path, runner=runner, registry=registry, http=http, spawner=spawner)

    assert code == 1
    assert spawner.detached_spawns  # it was booted
    assert runner.killed_pgroup(spawner.detached_pid)  # then killed
    assert not registry.registered
    assert not _state_path(tmp_path).exists()


def test_a_failed_boot_states_the_cause_the_health_body_carried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal's own words are the diagnosis; a bare status code is not.

    A system interface answering 503 names in its body exactly which precondition
    failed. Throwing that away leaves an agent with a wall of DEBUG discovery
    chatter and bare ``503`` access-log lines, which say nothing about what to fix.
    """
    diagnosis = "No 'mngr observe' process holds /tmp/fake/.mngr/observe_lock"
    http = _FakeHttp(lambda _url: 503, body=f'{{"detail":"{diagnosis}"}}')

    code = _up(tmp_path, http=http, spawner=_FakeSpawner())

    assert code == 1
    assert diagnosis in capsys.readouterr().err


def test_a_failed_boot_names_a_log_that_still_exists_afterwards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The teardown deletes the state dir, so the message must not point into it.

    An agent told "last 40 line(s) of <path>" goes and reads the rest of <path>;
    finding nothing there is worse than not being pointed at a file at all.
    """
    log_line = "Traceback: something went wrong during boot"
    inner_log = mod._state_dir(tmp_path, _NAME) / mod.INNER_LOG_FILENAME
    http = _FakeHttp(lambda _url: None)

    code = _up(tmp_path, http=http, spawner=_FakeSpawner(log_output=log_line + "\n"))

    assert code == 1
    assert not inner_log.exists(), "the state dir is still torn down"
    preserved = tmp_path / mod.STATE_ROOT / f"{_NAME}{mod.FAILED_LOG_SUFFIX}"
    assert preserved.exists()
    assert log_line in preserved.read_text()
    stderr = capsys.readouterr().err
    assert str(preserved) in stderr
    assert log_line in stderr


def test_a_boot_excerpt_never_quotes_an_earlier_boot(tmp_path: Path) -> None:
    """The inner log is append-only and reused across refreshes.

    Without a boundary, a reboot that hangs at import (writing nothing at all)
    shows the *previous* boot's traceback -- so the agent hunts a cause that is no
    longer in the source. An empty excerpt after the marker is the truthful signal.
    """
    log_path = tmp_path / mod.INNER_LOG_FILENAME
    mod._append_boot_marker(log_path, _NAME)
    with open(log_path, "a") as handle:
        handle.write("ModuleNotFoundError: No module named 'gone'\n")
    mod._append_boot_marker(log_path, _NAME)

    excerpt = mod._log_excerpt(log_path)

    assert "ModuleNotFoundError" not in excerpt
    assert "no output from this boot" in excerpt


def test_body_excerpt_marks_truncation_by_bytes_not_characters() -> None:
    """The read cap is in bytes, so truncation must be judged in bytes too.

    A multi-byte body can be over the byte budget while under it in characters;
    comparing the decoded character count would then silently drop the "..."
    marker and pass off a partial body as the whole diagnosis.
    """
    # Two bytes per character: over the byte budget, under it in characters.
    multibyte_body = ("é" * mod._HEALTH_BODY_READ_BYTES).encode("utf-8")

    excerpt = mod._read_body_excerpt(io.BytesIO(multibyte_body))

    assert excerpt.endswith("...")

    # A short body -- trailing newline and all -- comes back whole, unmarked.
    assert (
        mod._read_body_excerpt(io.BytesIO(b'{"detail":"fine"}\n'))
        == '{"detail":"fine"}'
    )


def _install_oom_tagger(repo_root: Path) -> Path:
    tagger = repo_root / mod._OOM_TAG_SCRIPT
    tagger.parent.mkdir(parents=True, exist_ok=True)
    tagger.write_text("#!/usr/bin/env python3\n")
    return tagger


def test_every_spawn_is_tagged_into_the_user_service_band(tmp_path: Path) -> None:
    """An instance launched directly is banded by nothing else.

    It would inherit the launching shell's band, and every Claude bash command
    self-tags AGENT_SUBPROCESS -- making the surface the user is looking at the
    first non-browser thing shed under memory pressure. That has to hold for the
    wrapper and for a refresh's relaunch too, not just the first inner boot.
    """
    tagger = _install_oom_tagger(tmp_path)
    up_spawner = _FakeSpawner()
    assert _up_preview(tmp_path, spawner=up_spawner) == 0

    refresh_spawner = _FakeSpawner(detached_pid=5555)
    assert _refresh(tmp_path, spawner=refresh_spawner) == 0

    for argv in [*up_spawner.detached_spawns, *refresh_spawner.detached_spawns]:
        assert argv[1:3] == [str(tagger), mod._OOM_SERVICE_KEY]
    assert up_spawner.detached_spawns[0][3:] == _LAUNCH


def test_up_clears_a_stale_instance_before_booting(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps({"pids": [999], "services": ["old-svc"]})
    )
    runner = _RecordingRunner()
    registry = _RecordingRegistry()

    code = _up(tmp_path, runner=runner, registry=registry)

    assert code == 0
    assert runner.killed_pgroup(999)  # old server killed
    assert registry.removed == ["old-svc"]
    assert json.loads(_state_path(tmp_path).read_text())["pids"][0] == 4242


# registered service (surfaced, no wrapper)


def test_up_with_service_name_registers_the_instance(tmp_path: Path) -> None:
    registry = _RecordingRegistry()

    code = _up(tmp_path, registry=registry, service_name="demo-app")

    assert code == 0
    port = json.loads(_state_path(tmp_path).read_text())["inner_port"]
    # Unwrapped, the instance's own origin is the surface: it stays an app to open.
    assert registry.registered == [
        _Registration("demo-app", f"http://localhost:{port}", False, None)
    ]
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["services"] == ["demo-app"]


# preview (surfaced, wrapped)


def _up_preview(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    registry: _RecordingRegistry | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
    **kwargs,
) -> int:
    return _up(
        tmp_path,
        runner=runner,
        registry=registry,
        http=http,
        spawner=spawner,
        service_name="demo-app",
        preview_service_name="demo-preview",
        preview_title="my change",
        **kwargs,
    )


def test_up_preview_boots_wrapper_registers_both_and_reports_tab(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()

    code = _up_preview(tmp_path, registry=registry, spawner=spawner)

    assert code == 0
    # Two detached servers: the instance, then the wrapper chrome page.
    assert spawner.detached_spawns[0] == _LAUNCH
    wrapper_argv = spawner.detached_spawns[1]
    assert wrapper_argv[1] == str(Path(wrapper_mod.__file__).resolve())
    assert "--inner-service" in wrapper_argv
    assert "demo-app" in wrapper_argv
    assert "my change" in wrapper_argv
    # Registered both the inner app and the user-facing wrapper.
    assert [entry.service_name for entry in registry.registered] == [
        "demo-app",
        "demo-preview",
    ]
    # The app to open (the wrapper's service name) is printed to stdout; its
    # browser origin depends on the workspace host, which is not knowable here.
    assert capsys.readouterr().out.strip() == "demo-preview"
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["pids"] == spawner.detached_pids
    assert state["services"] == ["demo-app", "demo-preview"]
    assert isinstance(state["wrapper_port"], int)


def test_up_preview_surfaces_only_the_frame_and_titles_it(tmp_path: Path) -> None:
    """One preview puts one app in the launcher and the window list, reading as the app
    it previews. The inner service keeps its origin (the frame reaches it by
    swapping the hostname label) but is not an app to open: its bare page is the
    same app with nothing marking it as a preview."""
    registry = _RecordingRegistry()

    code = _up_preview(tmp_path, registry=registry)

    assert code == 0
    by_name = registry.by_name()
    assert by_name["demo-app"].internal
    assert by_name["demo-app"].display_name is None
    assert not by_name["demo-preview"].internal
    assert by_name["demo-preview"].display_name == "my change"


def test_up_preview_requires_service_name(tmp_path: Path) -> None:
    # Preview flags without --service-name is a misconfiguration, not a preview.
    spawner = _FakeSpawner()

    code = _up(
        tmp_path,
        spawner=spawner,
        preview_service_name="demo-preview",
        preview_title="my change",
    )

    assert code == 1
    assert not spawner.detached_spawns  # bailed before booting anything


def test_up_preview_refuses_an_inner_path_without_a_leading_slash(
    tmp_path: Path,
) -> None:
    # The wrapper dies at once on such a path, so refusing it here spares the
    # inner server's boot and the wrapper's whole health budget.
    spawner = _FakeSpawner()

    code = _up(
        tmp_path,
        spawner=spawner,
        service_name="demo-app",
        preview_service_name="demo-preview",
        preview_title="my change",
        inner_path="agent-1",
    )

    assert code == 1
    assert not spawner.detached_spawns


def test_up_preview_tears_down_both_when_the_wrapper_never_gets_healthy(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()
    # Inner health passes; the wrapper root probe never does. The inner is probed
    # on its own port with the caller's health path (``/`` here); the wrapper is
    # probed at ``/``. Distinguish by which port each call targets.
    http = _FakeHttp(lambda url: 200 if _first_port(url, spawner) else None)

    code = _up_preview(
        tmp_path, runner=runner, registry=registry, http=http, spawner=spawner
    )

    assert code == 1
    assert len(spawner.detached_pids) == 2  # both booted
    for pid in spawner.detached_pids:
        assert runner.killed_pgroup(pid)  # both killed
    assert registry.removed == ["demo-app"]  # inner deregistered
    assert not _state_path(tmp_path).exists()


def test_up_preview_tears_down_when_a_registration_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _RecordingRunner()
    registry = _RecordingRegistry(refused=frozenset({"demo-preview"}))
    spawner = _FakeSpawner()

    code = _up_preview(tmp_path, runner=runner, registry=registry, spawner=spawner)

    assert code == 1
    assert "forward_port register (wrapper) failed: refusing demo-preview" in (
        capsys.readouterr().err
    )
    for pid in spawner.detached_pids:
        assert runner.killed_pgroup(pid)
    assert registry.removed == ["demo-app"]
    assert not _state_path(tmp_path).exists()


def test_the_registry_writes_the_repo_roots_registry_from_any_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(forward_port.ENV_APPS_FILE, raising=False)
    repo_root = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    registry = mod.Registry()

    registry.register(
        repo_root,
        "demo-preview",
        "http://localhost:9001",
        internal=False,
        display_name="my change",
    )

    apps = repo_root / forward_port.DEFAULT_APPS_FILE
    assert '"demo-preview"' in apps.read_text()
    assert not (elsewhere / forward_port.DEFAULT_APPS_FILE).exists()
    registry.remove(repo_root, "demo-preview")
    assert '"demo-preview"' not in apps.read_text()


def _first_port(url: str, spawner: _FakeSpawner) -> bool:
    """True iff ``url`` targets the inner instance's port (the first spawn)."""
    inner_port = spawner.detached_envs[0][_PORT_ENV] if spawner.detached_envs else None
    return inner_port is not None and f":{inner_port}" in url


# teardown


def test_down_tears_down_servers_and_services(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps({"pids": [4242, 4243], "services": ["demo-app", "demo-preview"]})
    )
    runner = _RecordingRunner()
    registry = _RecordingRegistry()

    code = mod.down(
        _NAME, tmp_path, runner=runner, registry=registry, desktops=_RecordingDesktops()
    )

    assert code == 0
    assert runner.killed_pgroup(4242)
    assert runner.killed_pgroup(4243)
    assert registry.removed == ["demo-app", "demo-preview"]
    assert not state_dir.exists()


def _write_state(tmp_path: Path, pids: list[int], services: list[str]) -> Path:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True, exist_ok=True)
    _state_path(tmp_path).write_text(json.dumps({"pids": pids, "services": services}))
    return state_dir


def test_down_escalates_to_sigkill_for_a_server_that_traps_sigterm(
    tmp_path: Path,
) -> None:
    """The situation you reach for ``down`` in is often precisely a wedged instance.

    Sending SIGTERM and never looking reports success while the process keeps
    serving on a port that stays bound -- and deletes the only record of its pid.
    """
    state_dir = _write_state(tmp_path, [4242], ["demo-app"])
    runner = _SigtermProofRunner(alive_pids={4242})

    code = mod.down(
        _NAME,
        tmp_path,
        runner=runner,
        registry=_RecordingRegistry(),
        desktops=_RecordingDesktops(),
        sleeper=lambda _seconds: None,
    )

    assert code == 0
    assert runner.signals_sent_to(4242) == [signal.SIGTERM, signal.SIGKILL]
    assert not state_dir.exists()


def test_down_keeps_the_state_and_fails_when_a_server_survives_sigkill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing could find the survivor again if its pid and port were deleted."""
    state_dir = _write_state(tmp_path, [4242], ["demo-app"])
    runner = _RecordingRunner(alive_pids={4242})

    code = mod.down(
        _NAME,
        tmp_path,
        runner=runner,
        registry=_RecordingRegistry(),
        desktops=_RecordingDesktops(),
        sleeper=lambda _seconds: None,
    )

    assert code == 1
    assert runner.signals_sent_to(4242) == [signal.SIGTERM, signal.SIGKILL]
    assert state_dir.exists()
    assert "4242" in capsys.readouterr().err


def _preview_window(window_id: str = "win-1") -> mod.OpenWindow:
    return mod.OpenWindow(window_id, "demo-preview", "home", "client-1")


def test_down_refuses_and_changes_nothing_while_a_window_shows_the_instance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Torn down under an open window, the user would be left looking at a dead server."""
    state_dir = _write_state(tmp_path, [4242], ["demo-app", "demo-preview"])
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    desktops = _RecordingDesktops(open_windows=[_preview_window()])

    code = mod.down(
        _NAME, tmp_path, runner=runner, registry=registry, desktops=desktops
    )

    assert code == 1
    assert not runner.killed_pgroups
    assert registry.removed == []
    assert desktops.closed == []
    assert state_dir.exists()
    err = capsys.readouterr().err
    assert "win-1" in err
    assert "--close-windows" in err


def test_down_with_close_windows_closes_them_then_tears_down(tmp_path: Path) -> None:
    state_dir = _write_state(tmp_path, [4242], ["demo-app", "demo-preview"])
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    desktops = _RecordingDesktops(
        open_windows=[_preview_window("win-1"), _preview_window("win-2")]
    )

    code = mod.down(
        _NAME,
        tmp_path,
        runner=runner,
        registry=registry,
        desktops=desktops,
        close_windows=True,
    )

    assert code == 0
    assert desktops.closed == ["win-1", "win-2"]
    assert runner.killed_pgroup(4242)
    assert registry.removed == ["demo-app", "demo-preview"]
    assert not state_dir.exists()


def test_down_tears_nothing_down_when_a_window_will_not_close(tmp_path: Path) -> None:
    state_dir = _write_state(tmp_path, [4242], ["demo-app", "demo-preview"])
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    desktops = _RecordingDesktops(
        open_windows=[_preview_window("win-1"), _preview_window("win-2")],
        unclosable=frozenset({"win-2"}),
    )

    code = mod.down(
        _NAME,
        tmp_path,
        runner=runner,
        registry=registry,
        desktops=desktops,
        close_windows=True,
    )

    assert code == 1
    assert desktops.closed == ["win-1"]
    assert not runner.killed_pgroups
    assert registry.removed == []
    assert state_dir.exists()


def test_down_tears_down_with_a_warning_when_the_desktops_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable shell must not make an instance impossible to tear down."""
    state_dir = _write_state(tmp_path, [4242], ["demo-app", "demo-preview"])
    runner = _RecordingRunner()

    code = mod.down(
        _NAME,
        tmp_path,
        runner=runner,
        registry=_RecordingRegistry(),
        desktops=_RecordingDesktops(is_unreadable=True),
    )

    assert code == 0
    assert runner.killed_pgroup(4242)
    assert not state_dir.exists()
    assert "could not check for windows" in capsys.readouterr().err


def test_down_of_an_instance_with_no_services_never_reads_the_desktops(
    tmp_path: Path,
) -> None:
    state_dir = _write_state(tmp_path, [4242], [])
    desktops = _RecordingDesktops()

    code = mod.down(
        _NAME,
        tmp_path,
        runner=_RecordingRunner(),
        registry=_RecordingRegistry(),
        desktops=desktops,
    )

    assert code == 0
    assert desktops.reads == 0
    assert not state_dir.exists()


def test_a_window_closes_through_a_client_already_on_its_desktop() -> None:
    """Naming a client on another desktop would switch that client's view to the window's."""
    seats = [
        mod.ClientSeat("elsewhere", "work", True),
        mod.ClientSeat("idle-here", "home", False),
        mod.ClientSeat("live-here", "home", True),
    ]

    assert mod.closing_client(seats, "home") == "live-here"
    assert mod.closing_client(seats[:2], "home") == "idle-here"
    assert mod.closing_client(seats, "unvisited") is None


def test_a_window_on_a_desktop_no_client_is_on_is_not_closed() -> None:
    window = mod.OpenWindow("win-1", "demo-preview", "unvisited", None)

    with pytest.raises(mod.InstanceError, match="no client is on"):
        mod.Desktops().close(window)


_WINDOW_1 = "win-0000000000000001"
_WINDOW_2 = "win-0000000000000002"
_WINDOW_3 = "win-0000000000000003"


def _inventory_answer() -> tuple[int, dict[str, object]]:
    """The shell's inventory: the preview on two desktops (one with a client on it), and another app."""
    document = InventoryDocument(
        is_preview=False,
        workspace_name="ws",
        desktops=(
            fake_desktop(
                "home",
                [
                    fake_window(_WINDOW_1, "demo-preview", "/"),
                    fake_window(_WINDOW_2, "notes", "/"),
                ],
            ),
            fake_desktop("work", [fake_window(_WINDOW_3, "demo-preview", "/")]),
        ),
        apps=(),
        clients=(
            InventoryClient(
                id=ClientId("client-home"),
                active_desktop=DesktopId("home"),
                last_seen=FAKE_TIME,
                is_connected=True,
            ),
        ),
    )
    return 200, document.model_dump(mode="json")


def test_desktops_find_an_apps_windows_and_close_one_through_the_client_on_its_desktop() -> (
    None
):
    with LoopbackShell(get_answers={INVENTORY_ROUTE: _inventory_answer()}) as shell:
        desktops = mod.Desktops(shell_url=shell.url)

        windows = desktops.windows_of(["demo-preview"])
        desktops.close(windows[0])

    assert windows == [
        mod.OpenWindow(_WINDOW_1, "demo-preview", "home", "client-home"),
        mod.OpenWindow(_WINDOW_3, "demo-preview", "work", None),
    ]
    assert shell.posted_ops() == [
        ("close", {"window": _WINDOW_1, "client": "client-home", "desktop": "home"})
    ]


def test_desktops_report_a_refused_close_and_an_unreachable_shell() -> None:
    window = mod.OpenWindow(_WINDOW_1, "demo-preview", "home", "client-home")
    with LoopbackShell(op_refusal=(400, {"detail": "pinned"})) as shell:
        shell_url = shell.url
        with pytest.raises(
            mod.InstanceError, match=f"could not close window {_WINDOW_1}"
        ):
            mod.Desktops(shell_url=shell_url).close(window)
    with pytest.raises(mod.InstanceError, match="could not read the desktops"):
        mod.Desktops(shell_url=shell_url).windows_of(["demo-preview"])


@pytest.mark.parametrize(
    "launch, has_exited_before_teardown",
    [(["sleep", "30"], False), (["sh", "-c", "exit 1"], True)],
    ids=["still-running", "crashed-during-boot"],
)
def test_a_server_this_process_spawned_reads_as_gone_once_killed(
    tmp_path: Path, launch: list[str], has_exited_before_teardown: bool
) -> None:
    """A failed ``up`` tears down its own children, which linger as zombies until reaped.

    Real processes, since the point is what the kernel answers for an exited but unreaped
    child: the group must read as gone after SIGTERM, not as a survivor of SIGKILL -- both
    for a server still running and for one that already crashed, the usual reason ``up``
    fails.
    """
    pid = mod.Spawner().spawn_detached(
        launch,
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        log_path=str(tmp_path / "server.log"),
    )
    if has_exited_before_teardown:
        # WNOWAIT observes the exit without reaping it, so the child stays a zombie.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    runner = _SignalRecordingRealRunner()
    try:
        is_gone = mod._kill_process_group_and_wait(runner, pid, sleeper=time.sleep)
    finally:
        mod.Runner().kill_process_group(pid, signal.SIGKILL)

    assert is_gone
    assert runner.sent == [signal.SIGTERM]


def test_up_refuses_to_boot_over_an_instance_it_could_not_clear(
    tmp_path: Path,
) -> None:
    """Booting anyway would leave the old server holding a port with no record of it."""
    _write_state(tmp_path, [4242], ["demo-app"])
    runner = _RecordingRunner(alive_pids={4242})
    spawner = _FakeSpawner()

    code = _up(tmp_path, runner=runner, spawner=spawner)

    assert code == 1
    assert not spawner.detached_spawns


# refresh (in-place inner reboot)


def _refresh(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
    venv_linker: _RecordingVenvLinker | None = None,
) -> int:
    return mod.refresh(
        _NAME,
        tmp_path,
        runner=runner or _RecordingRunner(),
        http=http or _FakeHttp(_all_healthy),
        spawner=spawner or _FakeSpawner(),
        venv_linker=venv_linker or _RecordingVenvLinker(),
        sleeper=lambda _seconds: None,
    )


def test_a_project_instance_links_its_venv_before_every_boot(tmp_path: Path) -> None:
    linker = _RecordingVenvLinker()
    up_spawner = _FakeSpawner()

    assert (
        _up(
            tmp_path,
            project="system/apps/widget",
            venv_linker=linker,
            spawner=up_spawner,
        )
        == 0
    )
    assert linker.linked == [tmp_path / "system" / "apps" / "widget"]
    [up_argv] = up_spawner.detached_spawns
    assert up_argv == [*mod.project_launcher("system/apps/widget"), *_LAUNCH]

    refresh_spawner = _FakeSpawner(detached_pid=5555)
    assert _refresh(tmp_path, spawner=refresh_spawner, venv_linker=linker) == 0
    assert linker.linked == [tmp_path / "system" / "apps" / "widget"] * 2
    assert refresh_spawner.detached_spawns == [up_argv]


def test_an_instance_of_no_project_links_nothing(tmp_path: Path) -> None:
    linker = _RecordingVenvLinker()

    assert _up(tmp_path, venv_linker=linker) == 0
    assert (
        _refresh(tmp_path, spawner=_FakeSpawner(detached_pid=5555), venv_linker=linker)
        == 0
    )

    assert linker.linked == []


def test_a_project_venv_that_cannot_be_linked_boots_nothing_and_keeps_the_running_server(
    tmp_path: Path,
) -> None:
    spawner = _FakeSpawner()
    unlinkable = _RecordingVenvLinker(raises=OSError("uv cache dir failed"))

    assert (
        _up(
            tmp_path,
            project="system/apps/widget",
            venv_linker=unlinkable,
            spawner=spawner,
        )
        == 1
    )
    assert spawner.detached_spawns == []
    assert not _state_path(tmp_path).exists()

    assert (
        _up(tmp_path, project="system/apps/widget", venv_linker=_RecordingVenvLinker())
        == 0
    )
    runner = _RecordingRunner()
    refresh_spawner = _FakeSpawner(detached_pid=5555)

    assert (
        _refresh(
            tmp_path, runner=runner, spawner=refresh_spawner, venv_linker=unlinkable
        )
        == 1
    )
    assert not runner.killed_pgroup(4242)
    assert refresh_spawner.detached_spawns == []


def test_refresh_reboots_inner_on_same_port_leaving_wrapper_and_services(
    tmp_path: Path,
) -> None:
    # Stand up a preview (inner pid 4242, wrapper pid 4243, two services), then
    # refresh it with a distinct spawner so the new inner pid (5555) is
    # distinguishable from the old.
    up_spawner = _FakeSpawner()
    registry = _RecordingRegistry()
    assert _up_preview(tmp_path, registry=registry, spawner=up_spawner) == 0
    inner_port = json.loads(_state_path(tmp_path).read_text())["inner_port"]

    runner = _RecordingRunner()
    spawner = _FakeSpawner(detached_pid=5555)
    code = _refresh(tmp_path, runner=runner, spawner=spawner)

    assert code == 0
    # The old inner server was killed; the wrapper (second spawn) was left alone.
    assert runner.killed_pgroup(4242)
    assert not runner.killed_pgroup(4243)
    # Exactly the inner command was relaunched -- on the SAME port, no new wrapper.
    assert spawner.detached_spawns == [_LAUNCH]
    assert spawner.detached_envs[0][_PORT_ENV] == str(inner_port)
    # Services were never deregistered (the window keeps routing to the same port).
    assert not registry.removed
    # State now points at the new inner pid; the wrapper pid is preserved.
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["pids"] == [5555, 4243]
    assert state["services"] == ["demo-app", "demo-preview"]


def test_refresh_without_state_is_an_error(tmp_path: Path) -> None:
    assert _refresh(tmp_path) == 1


def test_refresh_aborts_when_old_server_will_not_exit(tmp_path: Path) -> None:
    # The old inner server never dies, so its port stays held -- refresh must not
    # respawn (a bind clash), and must report failure.
    assert _up_preview(tmp_path) == 0
    runner = _RecordingRunner(alive_pids={4242})
    spawner = _FakeSpawner(detached_pid=5555)

    code = _refresh(tmp_path, runner=runner, spawner=spawner)

    assert code == 1
    assert runner.killed_pgroup(4242)
    assert not spawner.detached_spawns


def test_refresh_escalates_to_sigkill_for_a_server_that_ignores_sigterm(
    tmp_path: Path,
) -> None:
    """A server that traps SIGTERM would otherwise block every refresh of its preview."""
    assert _up_preview(tmp_path) == 0
    runner = _SigtermProofRunner(alive_pids={4242})
    spawner = _FakeSpawner(detached_pid=5555)

    code = _refresh(tmp_path, runner=runner, spawner=spawner)

    assert code == 0
    assert runner.signals_sent_to(4242) == [signal.SIGTERM, signal.SIGKILL]
    assert spawner.detached_spawns == [_LAUNCH]


def test_refresh_reports_unhealthy_reboot_but_records_new_pid(tmp_path: Path) -> None:
    # The rebooted inner never gets healthy: refresh fails, but the new pid is
    # recorded so a later ``down`` still kills it (no leaked server).
    assert _up_preview(tmp_path) == 0
    runner = _RecordingRunner()
    spawner = _FakeSpawner(detached_pid=5555)
    http = _FakeHttp(lambda _url: None)

    code = _refresh(tmp_path, runner=runner, http=http, spawner=spawner)

    assert code == 1
    assert json.loads(_state_path(tmp_path).read_text())["pids"][0] == 5555


def test_main_routes_refresh(tmp_path: Path) -> None:
    # No state -> refresh returns 1, but it routed through refresh() (exit 1, not
    # an argparse error).
    code = mod.main(["refresh", "--name", _NAME, "--repo-root", str(tmp_path)])
    assert code == 1


def test_down_without_state_is_a_noop_success(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = mod.down(
        _NAME,
        tmp_path,
        runner=runner,
        registry=_RecordingRegistry(),
        desktops=_RecordingDesktops(),
    )

    assert code == 0
    assert not runner.killed_pgroups


def test_down_reports_unreadable_state(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text("not json{")
    runner = _RecordingRunner()

    code = mod.down(
        _NAME,
        tmp_path,
        runner=runner,
        registry=_RecordingRegistry(),
        desktops=_RecordingDesktops(),
    )

    assert code == 1


# CLI + parsing


def test_parse_env_assignments_rejects_a_missing_equals() -> None:
    with pytest.raises(mod.InstanceError):
        mod.parse_env_assignments(["NOPE"])


def test_main_up_strips_the_leading_double_dash(tmp_path: Path) -> None:
    # ``argparse.REMAINDER`` keeps the ``--`` separator; ``main`` must drop it so
    # the launch argv is clean. A missing cwd makes ``up`` exit 1 without spawning,
    # which is enough to prove the wiring reached ``up`` with the right launch.
    code = mod.main(
        [
            "up",
            "--name",
            _NAME,
            "--cwd",
            str(tmp_path / "gone"),
            "--port-env",
            _PORT_ENV,
            "--repo-root",
            str(tmp_path),
            "--",
            "uv",
            "run",
            "my-service",
        ]
    )
    assert code == 1  # missing cwd, but it routed through up()


def test_named_ports_reach_the_env_and_the_argv(tmp_path: Path) -> None:
    # A second named port is allocated beside main, injected into its own env var,
    # and fills {port:<name>} wherever the launch argv or an --env value names it.
    spawner = _FakeSpawner()
    code = mod.up(
        _NAME,
        ["my-service", "--api", "http://{host}:{port:sidecar}", "--ui", "{port:main}"],
        str(tmp_path),
        tmp_path,
        port_env_by_name={"main": _PORT_ENV, "sidecar": "MYSVC_API_PORT"},
        env_overrides={"MYSVC_API_URL": "http://{host}:{port:sidecar}"},
        runner=_RecordingRunner(),
        registry=_RecordingRegistry(),
        http=_FakeHttp(_all_healthy),
        spawner=spawner,
        venv_linker=_RecordingVenvLinker(),
        sleeper=lambda _seconds: None,
    )

    assert code == 0
    state = json.loads(_state_path(tmp_path).read_text())
    main_port, api_port = state["ports"]["main"], state["ports"]["sidecar"]
    assert main_port != api_port
    env = spawner.detached_envs[0]
    assert env[_PORT_ENV] == str(main_port)
    assert env["MYSVC_API_PORT"] == str(api_port)
    assert env["MYSVC_API_URL"] == f"http://127.0.0.1:{api_port}"
    assert spawner.detached_spawns[0][1:] == [
        "--api",
        f"http://127.0.0.1:{api_port}",
        "--ui",
        str(main_port),
    ]


def test_copies_land_in_the_scratch_space_and_fill_their_placeholder(
    tmp_path: Path,
) -> None:
    # The source is copied whole before boot; the instance is pointed at the copy, never
    # the source, and {scratch} names a directory of the instance's own.
    source = tmp_path / "data" / ".apps" / "my-service"
    source.mkdir(parents=True)
    (source / "records.json").write_text("[1]")
    spawner = _FakeSpawner()
    code = mod.up(
        _NAME,
        [
            "my-service",
            "--store",
            "{copy:store}/records.json",
            "--state",
            "{scratch}/state",
        ],
        str(tmp_path),
        tmp_path,
        port_env_by_name={"main": _PORT_ENV},
        copy_sources={
            "store": "data/.apps/my-service",
            "missing": "data/.apps/never-written",
        },
        runner=_RecordingRunner(),
        registry=_RecordingRegistry(),
        http=_FakeHttp(_all_healthy),
        spawner=spawner,
        venv_linker=_RecordingVenvLinker(),
        sleeper=lambda _seconds: None,
        free_space=_plenty_of_disk,
    )

    assert code == 0
    state_dir = mod._state_dir(tmp_path, _NAME)
    copy_path = state_dir / mod.COPIES_DIRNAME / "store"
    assert (copy_path / "records.json").read_text() == "[1]"
    assert (state_dir / mod.COPIES_DIRNAME / "missing").is_dir()
    scratch = state_dir / mod.SCRATCH_DIRNAME
    assert scratch.is_dir()
    assert spawner.detached_spawns[0][1:] == [
        "--store",
        f"{copy_path}/records.json",
        "--state",
        f"{scratch}/state",
    ]
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["copies"]["store"] == str(copy_path)
    assert state["scratch"] == str(scratch)


def test_a_copy_that_would_not_fit_on_disk_fails_the_boot_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "data" / ".apps" / "my-service"
    source.mkdir(parents=True)
    (source / "records.json").write_text("[1]")
    spawner = _FakeSpawner()
    code = _up(
        tmp_path,
        spawner=spawner,
        copy_sources={"store": "data/.apps/my-service"},
        free_space=lambda _path: 0,
    )

    assert code == 1
    assert spawner.detached_spawns == []
    assert not mod._state_dir(tmp_path, _NAME).exists()
    assert "--copy store=data/.apps/my-service" in capsys.readouterr().err


def _seed_store(root: Path) -> Path:
    source = root / "store"
    (source / "repos" / "one").mkdir(parents=True)
    (source / "repos" / "one" / "pack.bin").write_bytes(b"x" * 3000)
    (source / "records.json").write_text("[1]")
    os.symlink("records.json", source / "latest.json")
    return source


def test_a_copys_size_counts_file_bytes_and_not_symlink_targets(tmp_path: Path) -> None:
    assert mod.tree_size_bytes(_seed_store(tmp_path)) == 3000 + len("[1]")


def test_a_copy_that_fits_is_made_whole_with_symlinks_kept(tmp_path: Path) -> None:
    destination = tmp_path / "copies" / "data"

    mod.copy_tree_checked(_seed_store(tmp_path), destination, _plenty_of_disk)

    assert (destination / "repos" / "one" / "pack.bin").read_bytes() == b"x" * 3000
    assert os.readlink(destination / "latest.json") == "records.json"


def test_a_copy_that_would_not_leave_the_reserve_is_refused_before_writing(
    tmp_path: Path,
) -> None:
    source = _seed_store(tmp_path)
    destination = tmp_path / "copies" / "data"
    probed: list[Path] = []

    def just_short(path: Path) -> int:
        probed.append(path)
        return mod.COPY_RESERVE_BYTES + mod.tree_size_bytes(source) - 1

    with pytest.raises(mod.InstanceError, match="Copy only what the test needs"):
        mod.copy_tree_checked(source, destination, just_short)

    assert not destination.parent.exists()
    # The probe asks about the disk the copy would land on, not the source's.
    assert probed == [tmp_path]


def test_a_copy_that_fails_part_way_is_removed(tmp_path: Path) -> None:
    # copytree copies everything else, then fails on the pipe it cannot copy.
    source = _seed_store(tmp_path)
    os.mkfifo(source / "repos" / "one" / "pipe")
    destination = tmp_path / "copies" / "data"

    with pytest.raises(mod.InstanceError, match="failed"):
        mod.copy_tree_checked(source, destination, _plenty_of_disk)

    assert not destination.exists()


def test_a_copy_never_lands_on_an_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "copies" / "data"
    destination.mkdir(parents=True)
    (destination / "kept.json").write_text("{}")

    with pytest.raises(mod.InstanceError, match="already exists"):
        mod.copy_tree_checked(_seed_store(tmp_path), destination, _plenty_of_disk)

    assert (destination / "kept.json").exists()


def test_a_port_with_no_env_var_reaches_the_argv_by_placeholder_alone(
    tmp_path: Path,
) -> None:
    spawner = _FakeSpawner()
    code = mod.up(
        _NAME,
        ["my-service", "--port", "{port:main}"],
        str(tmp_path),
        tmp_path,
        port_env_by_name={"main": None},
        env_overrides={"MYSVC_PORT_URL": "http://{host}:{port:main}"},
        runner=_RecordingRunner(),
        registry=_RecordingRegistry(),
        http=_FakeHttp(_all_healthy),
        spawner=spawner,
        venv_linker=_RecordingVenvLinker(),
        sleeper=lambda _seconds: None,
    )

    assert code == 0
    main_port = json.loads(_state_path(tmp_path).read_text())["ports"]["main"]
    assert spawner.detached_spawns[0][1:] == ["--port", str(main_port)]
    assert spawner.detached_envs[0]["MYSVC_PORT_URL"] == f"http://127.0.0.1:{main_port}"
    assert _PORT_ENV not in spawner.detached_envs[0]


def test_a_placeholder_naming_nothing_the_instance_was_given_fails_the_boot(
    tmp_path: Path,
) -> None:
    spawner = _FakeSpawner()
    code = mod.up(
        _NAME,
        ["my-service", "--store", "{copy:store}"],
        str(tmp_path),
        tmp_path,
        port_env_by_name={"main": _PORT_ENV},
        runner=_RecordingRunner(),
        registry=_RecordingRegistry(),
        http=_FakeHttp(_all_healthy),
        spawner=spawner,
        venv_linker=_RecordingVenvLinker(),
        sleeper=lambda _seconds: None,
    )

    assert code == 1
    assert spawner.detached_spawns == []
    assert not _state_path(tmp_path).exists()


def test_the_wrapper_is_told_the_inner_path_to_open(tmp_path: Path) -> None:
    spawner = _FakeSpawner()
    assert _up_preview(tmp_path, spawner=spawner, inner_path="/agent-123") == 0

    wrapper_argv = spawner.detached_spawns[1]
    assert wrapper_argv[wrapper_argv.index("--inner-path") + 1] == "/agent-123"
    assert json.loads(_state_path(tmp_path).read_text())["inner_path"] == "/agent-123"


def test_refresh_replays_the_named_ports_and_copies(tmp_path: Path) -> None:
    # A rebuilt instance comes back on every port it had, pointed at the same copies,
    # with nothing re-copied: the user's edits in the copy survive the refresh.
    source = tmp_path / "data" / ".apps" / "my-service"
    source.mkdir(parents=True)
    assert (
        mod.up(
            _NAME,
            ["my-service", "--store", "{copy:store}"],
            str(tmp_path),
            tmp_path,
            port_env_by_name={"main": _PORT_ENV, "sidecar": "MYSVC_API_PORT"},
            env_overrides={"MYSVC_STORE": "{copy:store}"},
            copy_sources={"store": "data/.apps/my-service"},
            runner=_RecordingRunner(),
            registry=_RecordingRegistry(),
            http=_FakeHttp(_all_healthy),
            spawner=_FakeSpawner(),
            venv_linker=_RecordingVenvLinker(),
            sleeper=lambda _seconds: None,
            free_space=_plenty_of_disk,
        )
        == 0
    )
    state = json.loads(_state_path(tmp_path).read_text())
    copy_path = state["copies"]["store"]
    (Path(copy_path) / "edited.json").write_text("{}")

    spawner = _FakeSpawner(detached_pid=5555)
    assert _refresh(tmp_path, spawner=spawner) == 0

    assert spawner.detached_spawns == [["my-service", "--store", copy_path]]
    env = spawner.detached_envs[0]
    assert env[_PORT_ENV] == str(state["ports"]["main"])
    assert env["MYSVC_API_PORT"] == str(state["ports"]["sidecar"])
    assert env["MYSVC_STORE"] == copy_path
    assert (Path(copy_path) / "edited.json").exists()


@pytest.mark.parametrize(
    ("port_envs", "problem"),
    [
        (["sidecar=MYSVC_API_PORT"], "main"),
        (["MYSVC_PORT", "MYSVC_OTHER"], "twice"),
        (["=MYSVC_PORT"], "expects"),
    ],
)
def test_port_envs_must_name_main_exactly_once(
    port_envs: list[str], problem: str
) -> None:
    with pytest.raises(mod.InstanceError, match=problem):
        mod.parse_port_envs(port_envs)


def test_a_bare_port_env_is_the_main_port_and_copies_parse_by_key() -> None:
    assert mod.parse_port_envs(["MYSVC_PORT", "sidecar=MYSVC_API_PORT"]) == {
        "main": "MYSVC_PORT",
        "sidecar": "MYSVC_API_PORT",
    }
    # A --port is a port no env var carries; it still counts as main when it is named so.
    assert mod.parse_port_envs([], ["main", "sidecar"]) == {
        "main": None,
        "sidecar": None,
    }
    with pytest.raises(mod.InstanceError, match="twice"):
        mod.parse_port_envs(["MYSVC_PORT"], ["main"])
    assert mod.parse_copy_assignments(["data=data/.apps/x"]) == {"data": "data/.apps/x"}
    with pytest.raises(mod.InstanceError, match="KEY=SOURCE"):
        mod.parse_copy_assignments(["data"])


def test_main_up_takes_named_ports_and_copies(tmp_path: Path) -> None:
    # The flags reach ``up``: a missing cwd makes it exit 1 after parsing everything.
    code = mod.main(
        [
            "up",
            "--name",
            _NAME,
            "--cwd",
            str(tmp_path / "gone"),
            "--port-env",
            _PORT_ENV,
            "--port-env",
            "sidecar=MYSVC_API_PORT",
            "--copy",
            "data=data/.apps/x",
            "--inner-path",
            "/x",
            "--repo-root",
            str(tmp_path),
            "--",
            "my-service",
        ]
    )
    assert code == 1


def test_main_routes_down(tmp_path: Path) -> None:
    code = mod.main(["down", "--name", _NAME, "--repo-root", str(tmp_path)])
    assert code == 0  # no state -> idempotent no-op


# wrapper page


def test_wrapper_page_derives_the_inner_origin_from_location_host() -> None:
    # Every service is served at its own browser origin (a sibling hostname of
    # the wrapper's own), and the workspace host is not knowable server-side --
    # so the page must derive the inner iframe's origin from ``location.host``
    # in JavaScript rather than bake in any static URL. The old path-prefix
    # scheme (``/service/<name>/``) no longer exists.
    html = wrapper_mod.build_wrapper_html(inner_service="demo-app", title="my change")

    assert '"demo-app"' in html
    assert "location.host" in html
    assert "/service/" not in html
    # No static src pointing anywhere: the iframe src is assigned in JS only.
    assert 'src="' not in html


def test_wrapper_page_opens_the_inner_service_at_the_given_path() -> None:
    page = wrapper_mod.build_wrapper_html(
        "chat-preview-app", "t", inner_path="/agent-1"
    )

    assert 'var previewPath = "/agent-1";' in page
    assert "innerHost + previewPath" in page
    with pytest.raises(ValueError, match="start with"):
        wrapper_mod.build_wrapper_html("chat-preview-app", "t", inner_path="agent-1")


def test_wrapper_page_escapes_the_title() -> None:
    html = wrapper_mod.build_wrapper_html(inner_service="svc", title='<b>x</b> & "y"')
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_a_mock_wrapper_page_says_it_is_a_mock_and_shows_the_note_escaped() -> None:
    page = wrapper_mod.build_wrapper_html(
        "inbox",
        "Inbox",
        kind=FrameKind.MOCK,
        note="<b>Refresh</b> is not wired up",
    )
    preview_page = wrapper_mod.build_wrapper_html("inbox", "Inbox")

    assert '<span class="tag">UI Sketch</span>' in page
    assert "&lt;b&gt;Refresh&lt;/b&gt; is not wired up" in page
    assert "<b>Refresh</b>" not in page
    assert '<span class="tag">Preview</span>' in preview_page
    assert 'class="preview-note"' not in preview_page


def test_a_boot_whose_health_says_the_frontend_is_not_built_is_not_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shell or chat whose bundle is missing still answers its health route 200: the
    process booted. A preview of it shows the 'not built' placeholder, which is nothing
    the user could judge, so the probe reads the route's own verdict on that."""
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    http = _FakeHttp(
        lambda _url: 200, body='{"status": "ok", "is_frontend_built": false}'
    )

    code = _up(tmp_path, runner=runner, http=http, spawner=spawner)

    assert code == 1
    assert runner.killed_pgroup(spawner.detached_pid)
    assert not _state_path(tmp_path).exists()
    assert "is_frontend_built: false" in capsys.readouterr().err

    built = mod.ProbeResult(200, '{"status": "ok", "is_frontend_built": true}')
    assert built.is_healthy
    # A health route that says nothing about a frontend (a bare service) is judged on its status alone.
    assert mod.ProbeResult(200, '{"status": "ok"}').is_healthy
    assert mod.ProbeResult(200, "not json").is_healthy


def test_a_project_launch_syncs_the_project_then_becomes_its_own_script(
    tmp_path: Path,
) -> None:
    """Once linked, a standalone project boots as its program does: synced from its own
    lock, and the bare script name resolved from its own .venv/bin, with no uv process
    left as the parent."""
    cwd = tmp_path / "workspace"
    project = "system/apps/widget"
    bin_dir = cwd / project / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "widget-app"
    script.write_text('#!/bin/sh\necho "$$ $VIRTUAL_ENV $*"\n')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(f'#!/bin/sh\necho "$*" > {tmp_path / "uv-args"}\n')
    for executable in (script, uv):
        executable.chmod(0o755)

    process = subprocess.Popen(
        [*mod.project_launcher(project), "widget-app", "--port", "1"],
        cwd=cwd,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        stdout=subprocess.PIPE,
        text=True,
    )
    stdout, _ = process.communicate(timeout=30)

    assert process.returncode == 0
    assert (tmp_path / "uv-args").read_text().split() == [
        "sync",
        "--locked",
        "--project",
        project,
    ]
    assert stdout.split() == [
        str(process.pid),
        str(cwd / project / ".venv"),
        "--port",
        "1",
    ]


# frame only (a mock around an app something else runs)

_FRAME_NAME = "inbox-sketch"


def _frame(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    registry: _RecordingRegistry | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
    note: str | None = "Sample data; refresh does not work yet.",
) -> int:
    return mod.frame(
        _FRAME_NAME,
        tmp_path,
        inner_service="inbox",
        frame_service_name=_FRAME_NAME,
        title="Inbox",
        display_name="Inbox (UI sketch)",
        kind=FrameKind.MOCK,
        note=note,
        runner=runner or _RecordingRunner(),
        registry=registry or _RecordingRegistry(),
        http=http or _FakeHttp(_all_healthy),
        spawner=spawner or _FakeSpawner(),
        sleeper=lambda _seconds: None,
    )


def _wrapper_arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_frame_boots_only_a_mock_wrapper_and_registers_only_the_frame(
    tmp_path: Path,
) -> None:
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()

    code = _frame(tmp_path, registry=registry, spawner=spawner)

    assert code == 0
    [wrapper_argv] = spawner.detached_spawns
    assert wrapper_argv[1] == str(Path(wrapper_mod.__file__).resolve())
    assert _wrapper_arg(wrapper_argv, "--inner-service") == "inbox"
    assert _wrapper_arg(wrapper_argv, "--kind") == "mock"
    assert (
        _wrapper_arg(wrapper_argv, "--note")
        == "Sample data; refresh does not work yet."
    )
    # The framed app's own registration is its program's business; only the frame is added.
    assert registry.registered == [
        _Registration(
            _FRAME_NAME,
            f"http://localhost:{_wrapper_arg(wrapper_argv, '--port')}",
            internal=False,
            display_name="Inbox (UI sketch)",
        )
    ]
    state = json.loads(mod._state_path(tmp_path, _FRAME_NAME).read_text())
    assert state["pids"] == spawner.detached_pids
    assert state["services"] == [_FRAME_NAME]


def test_reframing_reboots_the_wrapper_on_its_port_without_deregistering_it(
    tmp_path: Path,
) -> None:
    """A changed note re-boots the wrapper, and an open mock window keeps working: the
    frame's row is never removed (which would mint it a new origin), and it keeps
    routing to the same port."""
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()
    assert _frame(tmp_path, runner=runner, registry=registry, spawner=spawner) == 0
    first_pid = spawner.detached_pids[0]

    code = _frame(
        tmp_path,
        runner=runner,
        registry=registry,
        spawner=spawner,
        note="Now reading your inbox live; sending is not wired up yet.",
    )

    assert code == 0
    assert runner.killed_pgroup(first_pid)
    first_argv, second_argv = spawner.detached_spawns
    assert _wrapper_arg(second_argv, "--port") == _wrapper_arg(first_argv, "--port")
    assert _wrapper_arg(second_argv, "--note") == (
        "Now reading your inbox live; sending is not wired up yet."
    )
    assert registry.removed == []
    state = json.loads(mod._state_path(tmp_path, _FRAME_NAME).read_text())
    assert state["pids"] == [spawner.detached_pids[1]]


def test_a_reframe_whose_port_another_server_took_moves_to_a_fresh_one(
    tmp_path: Path,
) -> None:
    """After a reboot the dead wrapper's port can be another server's, which would answer
    the health probe for a wrapper that could not bind it; the frame moves instead, its
    row re-pointed rather than removed."""
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()
    assert _frame(tmp_path, registry=registry, spawner=spawner) == 0
    taken_port = int(_wrapper_arg(spawner.detached_spawns[0], "--port"))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind((mod.LOOPBACK_HOST, taken_port))
        squatter.listen()
        assert _frame(tmp_path, registry=registry, spawner=spawner) == 0

    new_port = int(_wrapper_arg(spawner.detached_spawns[1], "--port"))
    assert new_port != taken_port
    assert registry.removed == []
    assert registry.registered[-1].url == f"http://localhost:{new_port}"


def test_a_frame_never_signals_a_pid_a_restart_handed_to_another_process(
    tmp_path: Path,
) -> None:
    """After a reboot the recorded wrapper pid can lead an unrelated process group; the
    re-frame and ``down`` signal only the wrapper whose start time they recorded."""
    spawner = _FakeSpawner()
    first_pid = spawner.detached_pid
    second_pid = first_pid + 1
    runner = _RecordingRunner(start_times={first_pid: "100"})
    registry = _RecordingRegistry()
    assert _frame(tmp_path, runner=runner, registry=registry, spawner=spawner) == 0
    runner.start_times = {first_pid: "7", second_pid: "8"}

    assert _frame(tmp_path, runner=runner, registry=registry, spawner=spawner) == 0
    assert (
        mod.down(
            _FRAME_NAME,
            tmp_path,
            runner=runner,
            registry=registry,
            desktops=_RecordingDesktops(),
        )
        == 0
    )

    assert not runner.killed_pgroup(first_pid)
    assert runner.killed_pgroup(second_pid)


@pytest.mark.skipif(sys.platform != "linux", reason="start times are read from /proc")
def test_a_real_process_start_time_holds_for_its_life_and_follows_its_parents(
    tmp_path: Path,
) -> None:
    """The field ``process_start_time`` reads must be the kernel's start time: fixed while
    the process runs (or a live wrapper would read as not ours), no earlier than the
    parent that spawned it, and gone once the process is."""
    runner = mod.Runner()
    pid = mod.Spawner().spawn_detached(
        ["sleep", "30"],
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        log_path=str(tmp_path / "sleep.log"),
    )
    try:
        started = runner.process_start_time(pid)
        assert started.isdigit()
        assert int(runner.process_start_time(os.getpid())) <= int(started)
        assert runner.process_start_time(pid) == started
    finally:
        runner.kill_process_group(pid, signal.SIGKILL)
        os.waitpid(pid, 0)

    assert runner.process_start_time(pid) == ""


def test_a_failed_reframe_deregisters_the_frame_but_keeps_its_record_for_a_retry(
    tmp_path: Path,
) -> None:
    """The row must not route to a dead port, but the record is all a caller that never
    saw the note has to boot the frame again with, so it survives the failure."""
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()
    assert _frame(tmp_path, registry=registry, spawner=spawner) == 0
    recorded = mod.read_frame(_FRAME_NAME, tmp_path)
    assert recorded is not None

    code = _frame(
        tmp_path,
        registry=registry,
        spawner=spawner,
        http=_FakeHttp(lambda _url: None),
    )

    assert code == 1
    assert registry.removed == [_FRAME_NAME]
    assert mod.read_frame(_FRAME_NAME, tmp_path) == recorded

    retry_spawner = _FakeSpawner()
    assert _frame(tmp_path, registry=registry, spawner=retry_spawner) == 0
    [retry_argv] = retry_spawner.detached_spawns
    # Registered afresh (the failure removed the row), and never removed again.
    assert registry.removed == [_FRAME_NAME]
    assert registry.registered[-1] == _Registration(
        _FRAME_NAME,
        f"http://localhost:{_wrapper_arg(retry_argv, '--port')}",
        internal=False,
        display_name="Inbox (UI sketch)",
    )
    assert _wrapper_arg(retry_argv, "--note") == recorded.note
    assert (
        mod.down(
            _FRAME_NAME,
            tmp_path,
            runner=_RecordingRunner(),
            registry=registry,
            desktops=_RecordingDesktops(),
        )
        == 0
    )
    assert mod.read_frame(_FRAME_NAME, tmp_path) is None


def test_a_first_frame_that_fails_leaves_no_record(tmp_path: Path) -> None:
    """Nothing was framed, so nothing may read as a frame to boot again."""
    registry = _RecordingRegistry()

    code = _frame(tmp_path, registry=registry, http=_FakeHttp(lambda _url: None))

    assert code == 1
    assert registry.registered == []
    assert mod.read_frame(_FRAME_NAME, tmp_path) is None
    assert not mod._state_dir(tmp_path, _FRAME_NAME).exists()


def test_a_frame_records_what_it_was_booted_with_until_it_comes_down(
    tmp_path: Path,
) -> None:
    """What lets a caller that never saw the note re-frame the app exactly as it was."""
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    assert _frame(tmp_path, runner=runner, registry=registry) == 0

    assert mod.read_frame(_FRAME_NAME, tmp_path) == mod.FrameRecord(
        inner_service="inbox",
        frame_service_name=_FRAME_NAME,
        title="Inbox",
        display_name="Inbox (UI sketch)",
        kind=FrameKind.MOCK,
        note="Sample data; refresh does not work yet.",
        inner_path="/",
    )
    assert (
        mod.down(
            _FRAME_NAME,
            tmp_path,
            runner=runner,
            registry=registry,
            desktops=_RecordingDesktops(),
        )
        == 0
    )
    assert mod.read_frame(_FRAME_NAME, tmp_path) is None


def test_reading_a_frame_from_a_state_that_records_none_is_an_error(
    tmp_path: Path,
) -> None:
    assert _up_preview(tmp_path) == 0

    with pytest.raises(mod.InstanceError, match="records no frame"):
        mod.read_frame(_NAME, tmp_path)


def test_down_removes_the_frame_and_nothing_else(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    registry = _RecordingRegistry()
    spawner = _FakeSpawner()
    assert _frame(tmp_path, runner=runner, registry=registry, spawner=spawner) == 0

    code = mod.down(
        _FRAME_NAME,
        tmp_path,
        runner=runner,
        registry=registry,
        desktops=_RecordingDesktops(),
    )

    assert code == 0
    assert runner.killed_pgroup(spawner.detached_pids[0])
    assert registry.removed == [_FRAME_NAME]
    assert not mod._state_dir(tmp_path, _FRAME_NAME).exists()
