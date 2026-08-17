"""Tests for ``serve_isolated_instance.py``.

Run via: ``uv run pytest .agents/shared/scripts/serve_isolated_instance_test.py``

Like the ``reveal_system_interface.py`` tests, these inject a recording
``Runner`` (so no real ``uv``/``forward_port`` runs), a programmable
``HttpClient`` (so the health probe is deterministic), a fake ``Spawner`` (so no
throwaway server is launched), and a no-op sleeper. We assert on the exact env
overrides / commands the ``up`` motion produces and on the teardown-on-failure
control flow, which must never regress: a leaked server holds a port and a leaked
service registration routes the live UI at a dead port.
"""

from __future__ import annotations

import importlib.util
import json
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest

_SCRIPT = Path(__file__).parent / "serve_isolated_instance.py"
_spec = importlib.util.spec_from_file_location("serve_isolated_instance", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wrapper_mod = _load_module("preview_wrapper_server")

_NAME = "demo"
_PORT_ENV = "MYSVC_PORT"
_LAUNCH = ["uv", "run", "my-service"]


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(mod.Runner):
    """Records every ``run`` call; returns canned results keyed by argv prefix."""

    calls: list[list[str]] = field(default_factory=list)
    _responses: dict[tuple[str, ...], object] = field(default_factory=dict)
    killed_pgroups: list[int] = field(default_factory=list)
    kill_signals: list[tuple[int, int]] = field(default_factory=list)
    # Pids reported as still-alive by ``process_group_alive`` (default: none, so a
    # just-killed process reads as gone immediately -- the common refresh path).
    alive_pids: set[int] = field(default_factory=set)

    def respond(self, prefix: tuple[str, ...], result: object) -> None:
        self._responses[prefix] = result

    def run(self, argv: Sequence[str], **kwargs) -> _Result:
        argv_list = list(argv)
        self.calls.append(argv_list)
        for prefix, result in self._responses.items():
            if tuple(argv_list[: len(prefix)]) == prefix:
                assert isinstance(result, _Result)
                return result
        return _Result()

    def argvs_starting(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def ran(self, *prefix: str) -> bool:
        return bool(self.argvs_starting(*prefix))

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        self.killed_pgroups.append(pid)
        self.kill_signals.append((pid, sig))

    def killed_pgroup(self, pid: int) -> bool:
        return pid in self.killed_pgroups

    def signals_sent_to(self, pid: int) -> list[int]:
        return [sig for killed_pid, sig in self.kill_signals if killed_pid == pid]

    def process_group_alive(self, pid: int) -> bool:
        return pid in self.alive_pids


@dataclass
class _SigtermProofRunner(_RecordingRunner):
    """A process group that ignores SIGTERM and dies only once SIGKILLed."""

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        super().kill_process_group(pid, sig)
        if sig == signal.SIGKILL:
            self.alive_pids.discard(pid)


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


def _all_healthy(_url: str) -> int:
    return 200


def _up(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
    cwd: Path | None = None,
    **kwargs,
) -> int:
    return mod.up(
        _NAME,
        _LAUNCH,
        str(cwd if cwd is not None else tmp_path),
        tmp_path,
        port_env=_PORT_ENV,
        runner=runner or _RecordingRunner(),
        http=http or _FakeHttp(_all_healthy),
        spawner=spawner or _FakeSpawner(),
        sleeper=lambda _seconds: None,
        **kwargs,
    )


def _state_path(tmp_path: Path) -> Path:
    return mod._state_path(tmp_path, _NAME)


def test_the_self_hint_still_names_where_this_script_actually_lives() -> None:
    # ``up``'s boot-success hint prints ``python3 <_SELF_HINT> refresh|down
    # --name ...`` as commands the agent copies, and the system-interface flow
    # learns its instance name from that hint -- so the spelling has to stay
    # runnable. A move or rename would leave the hardcoded constant naming a path
    # that no longer exists, which nothing else here would notice.
    assert str(_SCRIPT).endswith(mod._SELF_HINT)


# --- bare instance (own testing) --------------------------------------------


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
    runner = _RecordingRunner()

    code = _up(tmp_path, runner=runner)

    assert code == 0
    assert not runner.ran(*mod.FORWARD_PORT_CMD, "--name")


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
    runner = _RecordingRunner()
    spawner = _FakeSpawner(detached_raises=FileNotFoundError("uv not found"))

    code = _up(tmp_path, runner=runner, spawner=spawner)

    assert code == 1
    assert not runner.ran(*mod.FORWARD_PORT_CMD, "--name")
    assert not _state_path(tmp_path).exists()


def test_up_tears_down_when_the_instance_never_gets_healthy(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    http = _FakeHttp(lambda _url: None)  # never returns 200

    code = _up(tmp_path, runner=runner, http=http, spawner=spawner)

    assert code == 1
    assert spawner.detached_spawns  # it was booted
    assert runner.killed_pgroup(spawner.detached_pid)  # then killed
    assert not runner.ran(*mod.FORWARD_PORT_CMD, "--name")  # never registered
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

    code = _up(tmp_path, runner=runner)

    assert code == 0
    assert runner.killed_pgroup(999)  # old server killed
    assert runner.ran(*mod.FORWARD_PORT_CMD, "--remove", "--name")  # old svc removed
    assert json.loads(_state_path(tmp_path).read_text())["pids"][0] == 4242


# --- registered service (surfaced, no wrapper) ------------------------------


def test_up_with_service_name_registers_the_instance(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = _up(tmp_path, runner=runner, service_name="demo-app")

    assert code == 0
    registered = runner.argvs_starting(*mod.FORWARD_PORT_CMD, "--name")
    flat = [token for argv in registered for token in argv]
    assert "demo-app" in flat
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["services"] == ["demo-app"]


# --- preview (surfaced, wrapped) --------------------------------------------


def _up_preview(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
) -> int:
    return _up(
        tmp_path,
        runner=runner,
        http=http,
        spawner=spawner,
        service_name="demo-app",
        preview_service_name="demo-preview",
        preview_title="my change",
    )


def test_up_preview_boots_wrapper_registers_both_and_reports_tab(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _RecordingRunner()
    spawner = _FakeSpawner()

    code = _up_preview(tmp_path, runner=runner, spawner=spawner)

    assert code == 0
    # Two detached servers: the instance, then the wrapper chrome page.
    assert spawner.detached_spawns[0] == _LAUNCH
    wrapper_argv = spawner.detached_spawns[1]
    assert mod.WRAPPER_SCRIPT in wrapper_argv[1]
    assert "--inner-service" in wrapper_argv
    assert "demo-app" in wrapper_argv
    assert "my change" in wrapper_argv
    # Registered both the inner app and the user-facing wrapper.
    registered = runner.argvs_starting(*mod.FORWARD_PORT_CMD, "--name")
    flat = [token for argv in registered for token in argv]
    assert "demo-app" in flat
    assert "demo-preview" in flat
    # The tab to open (the wrapper's service name) is printed to stdout; its
    # browser origin depends on the workspace host, which is not knowable here.
    assert capsys.readouterr().out.strip() == "demo-preview"
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["pids"] == spawner.detached_pids
    assert state["services"] == ["demo-app", "demo-preview"]
    assert isinstance(state["wrapper_port"], int)


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


def test_up_preview_tears_down_both_when_the_wrapper_never_gets_healthy(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    # Inner health passes; the wrapper root probe never does. The inner is probed
    # on its own port with the caller's health path (``/`` here); the wrapper is
    # probed at ``/``. Distinguish by which port each call targets.
    http = _FakeHttp(lambda url: 200 if _first_port(url, spawner) else None)

    code = _up_preview(tmp_path, runner=runner, http=http, spawner=spawner)

    assert code == 1
    assert len(spawner.detached_pids) == 2  # both booted
    for pid in spawner.detached_pids:
        assert runner.killed_pgroup(pid)  # both killed
    assert runner.ran(*mod.FORWARD_PORT_CMD, "--remove", "--name")  # inner deregistered
    assert not _state_path(tmp_path).exists()


def _first_port(url: str, spawner: _FakeSpawner) -> bool:
    """True iff ``url`` targets the inner instance's port (the first spawn)."""
    inner_port = spawner.detached_envs[0][_PORT_ENV] if spawner.detached_envs else None
    return inner_port is not None and f":{inner_port}" in url


# --- teardown ---------------------------------------------------------------


def test_down_tears_down_servers_and_services(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps({"pids": [4242, 4243], "services": ["demo-app", "demo-preview"]})
    )
    runner = _RecordingRunner()

    code = mod.down(_NAME, tmp_path, runner=runner)

    assert code == 0
    assert runner.killed_pgroup(4242)
    assert runner.killed_pgroup(4243)
    removed = [
        argv[-1]
        for argv in runner.argvs_starting(*mod.FORWARD_PORT_CMD, "--remove", "--name")
    ]
    assert "demo-app" in removed
    assert "demo-preview" in removed
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

    code = mod.down(_NAME, tmp_path, runner=runner, sleeper=lambda _seconds: None)

    assert code == 0
    assert runner.signals_sent_to(4242) == [signal.SIGTERM, signal.SIGKILL]
    assert not state_dir.exists()


def test_down_keeps_the_state_and_fails_when_a_server_survives_sigkill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing could find the survivor again if its pid and port were deleted."""
    state_dir = _write_state(tmp_path, [4242], ["demo-app"])
    runner = _RecordingRunner(alive_pids={4242})

    code = mod.down(_NAME, tmp_path, runner=runner, sleeper=lambda _seconds: None)

    assert code == 1
    assert runner.signals_sent_to(4242) == [signal.SIGTERM, signal.SIGKILL]
    assert state_dir.exists()
    assert "4242" in capsys.readouterr().err


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


# --- refresh (in-place inner reboot) ----------------------------------------


def _refresh(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
) -> int:
    return mod.refresh(
        _NAME,
        tmp_path,
        runner=runner or _RecordingRunner(),
        http=http or _FakeHttp(_all_healthy),
        spawner=spawner or _FakeSpawner(),
        sleeper=lambda _seconds: None,
    )


def test_refresh_reboots_inner_on_same_port_leaving_wrapper_and_services(
    tmp_path: Path,
) -> None:
    # Stand up a preview (inner pid 4242, wrapper pid 4243, two services), then
    # refresh it with a distinct spawner so the new inner pid (5555) is
    # distinguishable from the old.
    up_spawner = _FakeSpawner()
    assert _up_preview(tmp_path, spawner=up_spawner) == 0
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
    # Services were never deregistered (the tab keeps routing to the same port).
    assert not runner.ran(*mod.FORWARD_PORT_CMD, "--remove", "--name")
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
    assert runner.killed_pgroup(4242)  # we did try to stop it
    assert not spawner.detached_spawns  # but never respawned


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

    code = mod.down(_NAME, tmp_path, runner=runner)

    assert code == 0
    assert not runner.killed_pgroups


def test_down_reports_unreadable_state(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text("not json{")
    runner = _RecordingRunner()

    code = mod.down(_NAME, tmp_path, runner=runner)

    assert code == 1


# --- CLI + parsing ----------------------------------------------------------


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


def test_main_routes_down(tmp_path: Path) -> None:
    code = mod.main(["down", "--name", _NAME, "--repo-root", str(tmp_path)])
    assert code == 0  # no state -> idempotent no-op


# --- wrapper page (moved here with the wrapper server) ----------------------


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


def test_wrapper_page_escapes_the_title() -> None:
    html = wrapper_mod.build_wrapper_html(inner_service="svc", title='<b>x</b> & "y"')
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
