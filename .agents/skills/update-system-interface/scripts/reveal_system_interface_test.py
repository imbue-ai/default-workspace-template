"""Tests for ``reveal_system_interface.py``.

Run via: ``uv run pytest .agents/skills/update-system-interface/scripts/reveal_system_interface_test.py``

The orchestration tests inject a recording ``Runner`` (so no real
``git``/``npm``/``uv``/``mngr`` runs), a programmable ``HttpClient`` (so the
health probe is deterministic), a fake ``Spawner`` (so no throwaway server is
launched), and a no-op sleeper. We assert on the exact commands the reveal hands
to subprocess and on the failure-then-rollback control flow -- the part that must
never regress, because a broken backend takes down the user's whole UI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest

_SCRIPT = Path(__file__).parent / "reveal_system_interface.py"
_spec = importlib.util.spec_from_file_location("reveal_system_interface", _SCRIPT)
assert _spec is not None and _spec.loader is not None
reveal_mod = importlib.util.module_from_spec(_spec)
# Register before exec so the module's own dataclasses can resolve __module__.
sys.modules[_spec.name] = reveal_mod
_spec.loader.exec_module(reveal_mod)

_REPO = Path("/repo")
_ROLLBACK = "abc123def456"
_LIVE_BASE = "http://test-live"


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(reveal_mod.Runner):
    """Records every ``run`` call; returns canned results keyed by argv prefix.

    A response may be a single ``_Result`` or a list consumed in order (the last
    entry repeats) -- used to make a command fail once then succeed on retry.
    """

    calls: list[list[str]] = field(default_factory=list)
    _responses: dict[tuple[str, ...], object] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: object) -> None:
        self._responses[prefix] = result

    def run(self, argv: Sequence[str], **kwargs) -> _Result:
        argv_list = list(argv)
        self.calls.append(argv_list)
        for prefix, result in self._responses.items():
            if tuple(argv_list[: len(prefix)]) == prefix:
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                # A canned exception models a command that raises (e.g. a missing
                # binary -> FileNotFoundError) rather than exiting non-zero.
                if isinstance(result, BaseException):
                    raise result
                assert isinstance(result, _Result)
                return result
        return _Result()

    def argvs_starting(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def ran(self, *prefix: str) -> bool:
        return bool(self.argvs_starting(*prefix))


class _FakeHttp(reveal_mod.HttpClient):
    """Returns whatever ``responder(url)`` yields for GETs; records POSTs."""

    def __init__(self, responder: Callable[[str], int | None]) -> None:
        self._responder = responder
        self.get_urls: list[str] = []
        self.post_urls: list[str] = []

    def get_status(self, url: str, timeout: float) -> int | None:
        self.get_urls.append(url)
        return self._responder(url)

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> int | None:
        self.post_urls.append(url)
        return 200


@dataclass
class _FakeSpawned:
    terminated: bool = False

    def terminate(self) -> None:
        self.terminated = True


@dataclass
class _FakeSpawner(reveal_mod.Spawner):
    """Records the pre-flight throwaway boot ``reveal`` runs before a live restart."""

    spawns: list[list[str]] = field(default_factory=list)
    # The environment each pre-flight boot was given, so a test can assert how
    # the throwaway instance was configured (e.g. that it follows the live
    # observer rather than starting one of its own).
    envs: list[dict] = field(default_factory=list)
    last: _FakeSpawned | None = None

    def spawn(self, argv: Sequence[str], cwd: str, env: dict) -> _FakeSpawned:
        self.spawns.append(list(argv))
        self.envs.append(dict(env))
        self.last = _FakeSpawned()
        return self.last


@pytest.fixture(autouse=True)
def _no_ambient_live_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default every test to "no live layout" so preview seeding never reads the
    developer's real mngr state.

    MNGR_HOST_DIR is pointed at an empty directory rather than unset: the
    resolver falls back to ``~/.mngr`` when it is missing, which on a developer's
    box is a real host dir full of real agents. Seeding tests repopulate this
    host dir via ``_seed_live_layout``.
    """
    empty_host_dir = tmp_path / "empty-mngr-host"
    empty_host_dir.mkdir()
    monkeypatch.setenv("MNGR_HOST_DIR", str(empty_host_dir))
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)


def _runner_with_diff(name_status: str, *, dirty: bool = False) -> _RecordingRunner:
    runner = _RecordingRunner()
    runner.respond(
        ("git", "status", "--porcelain"), _Result(stdout=" M foo\n" if dirty else "")
    )
    runner.respond(("git", "diff"), _Result(stdout=name_status))
    return runner


def _reveal(runner: _RecordingRunner, http: _FakeHttp, spawner: _FakeSpawner) -> int:
    return reveal_mod.reveal(
        _ROLLBACK,
        _REPO,
        runner=runner,
        http=http,
        spawner=spawner,
        sleeper=lambda _seconds: None,
        base_url=_LIVE_BASE,
    )


def _all_healthy(_url: str) -> int:
    return 200


def _is_live(url: str) -> bool:
    return url.startswith(_LIVE_BASE)


# --- classification ---------------------------------------------------------


def test_classify_distinguishes_all_four_kinds() -> None:
    changes = reveal_mod.classify_changes(
        [
            "system/apps/system_interface/frontend/src/views/Chat.ts",
            "system/apps/system_interface/frontend/package.json",
            "system/apps/system_interface/imbue/system_interface/server.py",
            "system/apps/system_interface/pyproject.toml",
        ]
    )
    assert (
        changes.frontend_src,
        changes.frontend_manifest,
        changes.backend_src,
        changes.backend_manifest,
    ) == (
        True,
        True,
        True,
        True,
    )


def test_classify_treats_root_uv_lock_as_backend_manifest() -> None:
    changes = reveal_mod.classify_changes(["uv.lock"])
    assert changes.backend_manifest and changes.backend and not changes.frontend


def test_classify_ignores_backend_test_files() -> None:
    changes = reveal_mod.classify_changes(
        [
            "system/apps/system_interface/imbue/system_interface/server_test.py",
            "system/apps/system_interface/imbue/system_interface/test_e2e.py",
        ]
    )
    assert not changes.any


def test_classify_ignores_unrelated_paths() -> None:
    changes = reveal_mod.classify_changes(
        ["README.md", "system/vendor/mngr/libs/mngr/x.py"]
    )
    assert not changes.any


# --- happy paths ------------------------------------------------------------


def test_frontend_only_builds_and_broadcasts_without_restart() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n"
    )
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _reveal(runner, http, spawner)

    assert code == 0
    assert runner.ran("npm", "run", "build")
    assert not runner.ran("mngr", "start")  # frontend change never restarts the backend
    assert not runner.ran(
        "uv", "tool", "install"
    )  # no manifest change -> no dep refresh
    assert not spawner.spawns  # no pre-flight for a frontend-only change
    assert http.post_urls  # reload broadcast sent


def test_backend_with_manifest_refreshes_preflights_restarts_and_probes() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\nM\tsystem/apps/system_interface/pyproject.toml\n"
    )
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _reveal(runner, http, spawner)

    assert code == 0
    assert runner.argvs_starting("uv", "tool", "install")[0] == [
        "uv",
        "tool",
        "install",
        "-e",
        "system/apps/system_interface",
        "--reinstall",
    ]
    assert spawner.spawns and spawner.spawns[0] == [
        reveal_mod.TOOL_NAME
    ]  # pre-flight booted
    assert spawner.last is not None and spawner.last.terminated  # and torn down
    assert runner.ran("mngr", "start", "--restart", "system-services")
    assert any(_is_live(u) for u in http.get_urls)  # live health probed


def test_preflight_boot_follows_the_live_observer_and_uses_the_strict_health_path() -> (
    None
):
    """The pre-flight boots beside the still-running live service, so it must follow it.

    Trying to run its own observer would lose the single-writer lock, and the old
    ``/api/agents`` probe would have passed the pre-flight anyway -- so the gate
    proved nothing about whether the merged backend can serve a live agent view.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _reveal(runner, http, spawner)

    assert code == 0
    assert len(spawner.envs) == 1
    assert (
        spawner.envs[0][reveal_mod.PREVIEW_AGENT_EVENTS_MODE_ENV]
        == reveal_mod.FOLLOW_AGENT_EVENTS_MODE
    )
    preflight_urls = [u for u in http.get_urls if not _is_live(u)]
    assert preflight_urls
    assert all(u.endswith(reveal_mod.STRICT_HEALTH_PATH) for u in preflight_urls)
    # The live service keeps the looser probe: a rollback here is heavy and
    # lifecycle-stream trouble on the live UI is not something reverting fixes.
    live_urls = [u for u in http.get_urls if _is_live(u)]
    assert live_urls
    assert all(u.endswith(reveal_mod.HEALTH_PATH) for u in live_urls)


def test_backend_src_only_skips_dependency_refresh() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 0
    assert not runner.ran("uv", "tool", "install")
    assert runner.ran("mngr", "start")


def test_no_relevant_changes_does_nothing() -> None:
    runner = _runner_with_diff("M\tREADME.md\n")
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 0
    assert not runner.ran("npm", "run", "build")
    assert not runner.ran("mngr", "start")


# --- failure + autonomous rollback ------------------------------------------


def test_failed_preflight_never_restarts_live_service_and_rolls_back() -> None:
    # New backend file that cannot boot: pre-flight (non-live URL) never returns
    # 200; live URL is healthy (old code still running, and healthy after revert).
    runner = _runner_with_diff(
        "A\tsystem/apps/system_interface/imbue/system_interface/new_module.py\n"
    )
    http = _FakeHttp(lambda url: 200 if _is_live(url) else None)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2  # rolled back, UI healthy
    # The live service was never restarted -- pre-flight failed before the
    # restart, so the running service is still healthy on known-good code and
    # recovery must NOT restart it (doing so would needlessly blip a live UI).
    assert not runner.ran("mngr", "start")
    # Recovery still re-confirmed the untouched service via the health probe.
    assert any(_is_live(u) for u in http.get_urls)
    # An added file is removed on rollback (not checked out).
    assert runner.ran("git", "rm", "--force", "--ignore-unmatch")
    assert not runner.ran("git", "checkout", _ROLLBACK)


def test_failed_preflight_with_manifest_refreshes_deps_but_does_not_restart() -> None:
    # A backend manifest change whose merged code fails pre-flight. Recovery must
    # still reinstall deps back to known-good (to fix the on-disk venv) but must
    # NOT restart the live service, which was never touched.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/pyproject.toml\nM\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(lambda url: 200 if _is_live(url) else None)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2
    assert not runner.ran("mngr", "start")  # untouched live service is not restarted
    # uv tool install ran twice: once in the failed reveal, once in recovery to
    # restore the known-good dependency set on disk.
    assert len(runner.argvs_starting("uv", "tool", "install")) == 2


def test_failed_post_restart_health_triggers_rollback_then_recovers() -> None:
    # Pre-flight passes, but the live service stays unhealthy after the first
    # restart and only recovers after the rollback's restart. Key the health off
    # how many restarts have happened (wait_healthy retries many times, so a
    # short None sequence would otherwise pass on a later poll).
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )

    def responder(url: str) -> int | None:
        if not _is_live(url):
            return 200  # pre-flight always boots
        restarts = runner.calls.count(["mngr", "start", "--restart", "system-services"])
        return 200 if restarts >= 2 else None

    http = _FakeHttp(responder)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2
    assert runner.ran(
        "git", "checkout", _ROLLBACK
    )  # modified file restored from known-good
    assert (
        len(
            [
                c
                for c in runner.calls
                if c == ["mngr", "start", "--restart", "system-services"]
            ]
        )
        == 2
    )


def test_emergency_when_rollback_cannot_restore_health() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(
        lambda url: None if _is_live(url) else 200
    )  # live never healthy, even after revert

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 3


def test_frontend_build_failure_rolls_back() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n"
    )
    # First build (the reveal) fails; the recovery rebuild from known-good succeeds.
    runner.respond(
        ("npm", "run", "build"), [_Result(returncode=1, stderr="type error"), _Result()]
    )
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    # First build fails -> rollback -> recovery rebuild (default success) -> healthy serve probe.
    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK)


# --- preconditions ----------------------------------------------------------


def test_dirty_tree_refuses_before_touching_anything() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n", dirty=True
    )
    http = _FakeHttp(_all_healthy)

    with pytest.raises(reveal_mod.PreconditionError):
        _reveal(runner, http, _FakeSpawner())

    assert not runner.ran("mngr", "start")
    assert not runner.ran("npm", "run", "build")


def test_main_maps_precondition_to_exit_1(tmp_path: Path) -> None:
    # main() wires real deps; point it at an empty dir so the first git call
    # (status) fails as a CalledProcessError -> exit 1, proving the mapping
    # without needing a real repo.
    code = reveal_mod.main(
        ["reveal", "--rollback-to", _ROLLBACK, "--repo-root", str(tmp_path)]
    )
    assert code == 1


# --- tree restoration -------------------------------------------------------


def test_restore_tree_removes_adds_and_checks_out_the_rest() -> None:
    runner = _RecordingRunner()
    reveal_mod._restore_tree(
        [
            ("A", "system/apps/system_interface/imbue/system_interface/new_module.py"),
            ("M", "system/apps/system_interface/imbue/system_interface/server.py"),
            ("D", "system/apps/system_interface/frontend/src/old.ts"),
        ],
        _ROLLBACK,
        _REPO,
        runner,
    )
    assert runner.argvs_starting("git", "rm") == [
        [
            "git",
            "rm",
            "--force",
            "--ignore-unmatch",
            "system/apps/system_interface/imbue/system_interface/new_module.py",
        ]
    ]
    checkouts = [c[-1] for c in runner.argvs_starting("git", "checkout")]
    assert checkouts == [
        "system/apps/system_interface/imbue/system_interface/server.py",
        "system/apps/system_interface/frontend/src/old.ts",
    ]


# --- preview / unpreview adapters -------------------------------------------
#
# ``preview`` / ``unpreview`` are thin system-interface adapters over the shared
# ``serve_isolated_instance.py`` script. These tests assert the adapter validates
# its input and hands the shared script the system-interface specifics; the
# preview *mechanism* (booting, health, registration, teardown, state) is
# exercised in ``.agents/shared/scripts/serve_isolated_instance_test.py``.


_SLUG = "demo-change"
_SERVE_UP = (sys.executable, str(reveal_mod._SHARED_SERVE_SCRIPT), "up")
_SERVE_DOWN = (sys.executable, str(reveal_mod._SHARED_SERVE_SCRIPT), "down")
_SERVE_REFRESH = (sys.executable, str(reveal_mod._SHARED_SERVE_SCRIPT), "refresh")


def _make_work_dir(tmp_path: Path, *, built: bool = True) -> Path:
    """A stand-in for a worker's work_dir: a folder with an system/apps/system_interface.

    ``built`` seeds the frontend build output the backend serves, modelling a
    worker that produced a bundle; pass ``built=False`` for a worker that reported
    done without building it -- the case the preview must refuse.
    """
    work_dir = tmp_path / "worker"
    (work_dir / reveal_mod.APP_DIR).mkdir(parents=True)
    if built:
        static_index = work_dir / reveal_mod.FRONTEND_BUILD_INDEX
        static_index.parent.mkdir(parents=True, exist_ok=True)
        static_index.write_text("<!doctype html><html></html>")
    return work_dir


def _flag(argv: Sequence[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _flags(argv: Sequence[str], flag: str) -> list[str]:
    """Every value of a repeatable flag (``--env`` is passed more than once)."""
    return [argv[index + 1] for index, item in enumerate(argv) if item == flag]


def test_preview_delegates_to_the_shared_script_with_si_specifics(
    tmp_path: Path,
) -> None:
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()

    code = reveal_mod.preview(_SLUG, str(work_dir), tmp_path, runner=runner)

    assert code == 0
    up_calls = runner.argvs_starting(*_SERVE_UP)
    assert len(up_calls) == 1
    argv = up_calls[0]
    # Boots the worker's already-built app dir -- no re-clone / rebuild here.
    assert not runner.ran("uv", "sync")
    assert not runner.ran("npm", "run", "build")
    assert _flag(argv, "--cwd") == str(work_dir / reveal_mod.APP_DIR)
    # The launch command (after ``--``) is ``uv run system-interface``.
    assert argv[-3:] == ["uv", "run", reveal_mod.TOOL_NAME]
    # System-interface specifics: bind port/host env, point layout persistence at
    # a throwaway copy (SYSTEM_INTERFACE_LAYOUT_DIR) with MNGR_AGENT_ID dropped as a
    # backstop, register the inner app + wrapper.
    assert _flag(argv, "--port-env") == reveal_mod.PREVIEW_PORT_ENV
    assert _flag(argv, "--host-env") == reveal_mod.PREVIEW_HOST_ENV
    assert _flag(argv, "--unset-env") == reveal_mod.ENV_MNGR_AGENT_ID
    env_overrides = _flags(argv, "--env")
    assert any(
        v.startswith(f"{reveal_mod.PREVIEW_LAYOUT_DIR_ENV}=") for v in env_overrides
    )
    assert _flag(argv, "--service-name") == reveal_mod.PREVIEW_INNER_SERVICE_NAME
    assert _flag(argv, "--preview-service-name") == reveal_mod.PREVIEW_SERVICE_NAME
    assert _flag(argv, "--preview-title") == _SLUG


def test_preview_follows_the_live_observer_and_gates_on_the_strict_health_path(
    tmp_path: Path,
) -> None:
    """A preview must read the live agent event stream, not compete for it.

    ``mngr observe`` is single-writer per mngr host dir and the live system
    interface holds that lock, so a preview that started its own observer had it
    die seconds into boot and showed a frozen agent list plus "No conversation
    data" for every agent created afterwards -- while still passing a health
    probe that only listed agents. FOLLOW mode fixes the cause; the strict health
    path is what stops a preview from coming up if it is broken anyway.
    """
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()

    code = reveal_mod.preview(_SLUG, str(work_dir), tmp_path, runner=runner)

    assert code == 0
    argv = runner.argvs_starting(*_SERVE_UP)[0]
    assert (
        f"{reveal_mod.PREVIEW_AGENT_EVENTS_MODE_ENV}={reveal_mod.FOLLOW_AGENT_EVENTS_MODE}"
        in _flags(argv, "--env")
    )
    assert _flag(argv, "--health-path") == reveal_mod.STRICT_HEALTH_PATH
    assert reveal_mod.STRICT_HEALTH_PATH != reveal_mod.HEALTH_PATH


def test_preview_rejects_a_work_dir_without_the_app(tmp_path: Path) -> None:
    # A wrong --work-dir (or a destroyed worker) should fail fast, before the
    # shared script is ever invoked.
    runner = _RecordingRunner()
    bad_work_dir = tmp_path / "gone"  # no system/apps/system_interface under it

    code = reveal_mod.preview(_SLUG, str(bad_work_dir), tmp_path, runner=runner)

    assert code == 1
    assert not runner.argvs_starting(*_SERVE_UP)


def _write_agent_record(host_dir: Path, agent_id: str, *, is_primary: bool) -> Path:
    """Create one agent's state dir with the ``data.json`` mngr writes there."""
    state_dir = host_dir / "agents" / agent_id
    state_dir.mkdir(parents=True, exist_ok=True)
    labels = {"is_primary": "true"} if is_primary else {"user_created": "true"}
    (state_dir / "data.json").write_text(json.dumps({"labels": labels}))
    return state_dir


def _seed_live_layout(
    monkeypatch: pytest.MonkeyPatch,
    host_dir: Path,
    primary_agent_id: str = "services-agent",
    caller_agent_id: str = "chat-agent",
) -> Path:
    """Populate a fake live workspace_layout and point the env at that host dir.

    Models the real two-agent shape, which is what the resolution has to get
    right: the layout belongs to the workspace's *primary* (services) agent --
    the ``is_primary=true`` one the system interface itself runs under -- while
    MNGR_AGENT_ID names the ordinary chat agent that runs this script. The two
    ids are deliberately always distinct here, because deriving the layout path
    from MNGR_AGENT_ID landed on a directory that never exists and silently
    seeded nothing, so every preview opened with the default tabs.

    Writes the two layout files the preview should copy plus the two kinds of
    non-layout state it must *not* -- the client-activity event log (a nested
    sub-tree, at the path ``client_activity.get_events_path`` really uses) and
    the terminal banner.
    """
    primary_state_dir = _write_agent_record(host_dir, primary_agent_id, is_primary=True)
    _write_agent_record(host_dir, caller_agent_id, is_primary=False)
    layout_dir = primary_state_dir / "workspace_layout"
    (layout_dir / "layouts").mkdir(parents=True)
    (layout_dir / "layouts" / "desktop.json").write_text('{"panels":["chat"]}')
    (layout_dir / "layouts_meta.json").write_text('{"last_active_slug":"desktop"}')
    events_dir = layout_dir / "events" / "client_activity"
    events_dir.mkdir(parents=True)
    (events_dir / "events.jsonl").write_text('{"e":1}\n')
    (layout_dir / "terminal_banner.json").write_text('{"dismissed":true}')
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    monkeypatch.setenv("MNGR_AGENT_ID", caller_agent_id)
    return layout_dir


def test_preview_seeds_a_copy_of_only_the_live_layout_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The preview must open with the user's real tab layout, so it boots pointed at
    # a copy of the live layout -- but only the layout files, never the
    # client-activity log or terminal banner that share the same directory.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    _seed_live_layout(monkeypatch, tmp_path / "host")
    runner = _RecordingRunner()

    code = reveal_mod.preview(_SLUG, str(work_dir), repo_root, runner=runner)

    assert code == 0
    seed_dir = reveal_mod._preview_layout_seed_dir(repo_root, _SLUG)
    # The layout files are copied through...
    assert (seed_dir / "layouts" / "desktop.json").read_text() == '{"panels":["chat"]}'
    assert (seed_dir / "layouts_meta.json").exists()
    # ...and the non-layout state is left behind, including the whole
    # client-activity sub-tree that shares the live layout dir.
    assert not (seed_dir / "events").exists()
    assert not (seed_dir / "terminal_banner.json").exists()
    # The override handed to the shared script points at exactly that copy.
    argv = runner.argvs_starting(*_SERVE_UP)[0]
    assert _flag(argv, "--env") == f"{reveal_mod.PREVIEW_LAYOUT_DIR_ENV}={seed_dir}"


def test_preview_seeds_the_primary_agents_layout_not_the_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live layout belongs to the is_primary services agent; the agent running
    # this script is a different one. Give the caller a workspace_layout of its own
    # with distinguishable content: the seed must take the primary's, since that is
    # the only one the live system interface reads and writes. Resolving by
    # MNGR_AGENT_ID instead is what made every preview open with default tabs.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    host_dir = tmp_path / "host"
    _seed_live_layout(monkeypatch, host_dir)
    caller_layouts = host_dir / "agents" / "chat-agent" / "workspace_layout" / "layouts"
    caller_layouts.mkdir(parents=True)
    (caller_layouts / "desktop.json").write_text('{"panels":["WRONG"]}')
    runner = _RecordingRunner()

    code = reveal_mod.preview(_SLUG, str(work_dir), repo_root, runner=runner)

    assert code == 0
    seed_dir = reveal_mod._preview_layout_seed_dir(repo_root, _SLUG)
    assert (seed_dir / "layouts" / "desktop.json").read_text() == '{"panels":["chat"]}'


def _service_panel_layout(service_name: str) -> str:
    """A persisted layout whose single panel is an iframe on ``service_name``."""
    return json.dumps(
        {
            "panelParams": {
                "iframe-1": {"panelType": "iframe", "serviceName": service_name},
            }
        }
    )


@pytest.mark.parametrize(
    "service_name",
    [reveal_mod.PREVIEW_SERVICE_NAME, reveal_mod.PREVIEW_INNER_SERVICE_NAME, "browser"],
)
def test_preview_seeds_every_layout_verbatim_including_its_own_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_name: str
) -> None:
    # A layout that opens the preview tab is the normal case, not the exception:
    # that tab stays open for the whole editing pass, so any re-``preview`` copies
    # a layout containing it. Dropping such a layout took the user's real tabs with
    # it -- the nesting is refused by the previewed instance instead (the
    # self-referential-services env below), so the copy stays faithful.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    layout_dir = _seed_live_layout(monkeypatch, tmp_path / "host")
    content = _service_panel_layout(service_name)
    (layout_dir / "layouts" / "with-service.json").write_text(content)
    (layout_dir / "layout.json").write_text(content)
    runner = _RecordingRunner()

    code = reveal_mod.preview(_SLUG, str(work_dir), repo_root, runner=runner)

    assert code == 0
    seed_dir = reveal_mod._preview_layout_seed_dir(repo_root, _SLUG)
    assert (seed_dir / "layouts" / "with-service.json").read_text() == content
    assert (seed_dir / "layout.json").read_text() == content
    assert (seed_dir / "layouts" / "desktop.json").exists()
    assert (seed_dir / "layouts_meta.json").exists()


def test_preview_declares_its_own_services_self_referential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is what stops the faithfully-copied preview tab from rendering the
    # preview inside itself, so it must reach the booted instance -- naming BOTH
    # the wrapper service and the inner app service.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    _seed_live_layout(monkeypatch, tmp_path / "host")
    runner = _RecordingRunner()

    assert reveal_mod.preview(_SLUG, str(work_dir), repo_root, runner=runner) == 0

    argv = runner.argvs_starting(*_SERVE_UP)[0]
    expected = (
        f"{reveal_mod.PREVIEW_SELF_REFERENTIAL_SERVICES_ENV}="
        f"{reveal_mod.PREVIEW_SERVICE_NAME},{reveal_mod.PREVIEW_INNER_SERVICE_NAME}"
    )
    assert expected in argv


def test_preview_reseeds_from_scratch_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-running preview for a slug reflects the live layout as it is *now*: a stale
    # file from a prior seed must not survive into the new copy.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    _seed_live_layout(monkeypatch, tmp_path / "host")
    seed_dir = reveal_mod._preview_layout_seed_dir(repo_root, _SLUG)
    seed_dir.mkdir(parents=True)
    (seed_dir / "stale.json").write_text("{}")

    code = reveal_mod.preview(
        _SLUG, str(work_dir), repo_root, runner=_RecordingRunner()
    )

    assert code == 0
    assert not (seed_dir / "stale.json").exists()
    assert (seed_dir / "layouts_meta.json").exists()


def test_preview_seeds_empty_and_says_so_when_there_is_no_primary_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No primary agent (dev/test) -> nothing to copy; the preview still boots, just
    # to the fresh-workspace state, and the override still points at the empty copy.
    # It must say so: an empty seed is indistinguishable on screen from a working
    # preview, which is how a resolution bug survived a round of real use.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    runner = _RecordingRunner()

    code = reveal_mod.preview(_SLUG, str(work_dir), repo_root, runner=runner)

    assert code == 0
    seed_dir = reveal_mod._preview_layout_seed_dir(repo_root, _SLUG)
    assert seed_dir.is_dir()
    assert list(seed_dir.iterdir()) == []
    assert "is_primary=true" in capsys.readouterr().err
    argv = runner.argvs_starting(*_SERVE_UP)[0]
    assert _flag(argv, "--env") == f"{reveal_mod.PREVIEW_LAYOUT_DIR_ENV}={seed_dir}"


def test_preview_reports_a_primary_agent_that_has_no_saved_layout_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A workspace whose user has never saved a layout is a benign empty seed, but
    # the operator still gets told which of the two reasons it was.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    host_dir = tmp_path / "host"
    _write_agent_record(host_dir, "services-agent", is_primary=True)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))

    code = reveal_mod.preview(
        _SLUG, str(work_dir), repo_root, runner=_RecordingRunner()
    )

    assert code == 0
    assert list(reveal_mod._preview_layout_seed_dir(repo_root, _SLUG).iterdir()) == []
    assert "no saved layout yet" in capsys.readouterr().err


def test_unpreview_removes_the_seeded_layout_copy(tmp_path: Path) -> None:
    # The shared script only tears down its own state dir, so unpreview must remove
    # the throwaway layout copy this script created.
    seed_dir = reveal_mod._preview_layout_seed_dir(tmp_path, _SLUG)
    seed_dir.mkdir(parents=True)
    (seed_dir / "layouts_meta.json").write_text("{}")

    code = reveal_mod.unpreview(_SLUG, tmp_path, runner=_RecordingRunner())

    assert code == 0
    assert not seed_dir.exists()


def test_preview_refuses_a_work_dir_without_a_frontend_build(tmp_path: Path) -> None:
    # A worker that reported done without building its frontend leaves no bundle;
    # the preview must fail loudly (non-zero, no boot) rather than serve the
    # backend's "Frontend not built" placeholder as if it were the real UI. It does
    # not build for the worker -- fixing the worker is the point.
    work_dir = _make_work_dir(tmp_path, built=False)
    runner = _RecordingRunner()

    code = reveal_mod.preview(_SLUG, str(work_dir), tmp_path, runner=runner)

    assert code == 1
    assert not runner.argvs_starting(*_SERVE_UP)
    assert not runner.ran("npm", "run", "build")  # the preview never builds


def _make_preview_state(repo_root: Path, slug: str) -> None:
    """File a live preview instance the way the shared script does."""
    state_dir = (
        repo_root / reveal_mod._INSTANCES_ROOT / reveal_mod._preview_instance_name(slug)
    )
    state_dir.mkdir(parents=True)
    (state_dir / reveal_mod._INSTANCE_STATE_FILENAME).write_text("{}")


def test_preview_refuses_to_hijack_another_slugs_live_preview(tmp_path: Path) -> None:
    # The registered service names are fixed, so a second slug's preview would
    # silently take over the tab of the one already up. It must refuse instead.
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()
    _make_preview_state(tmp_path, "earlier-change")

    code = reveal_mod.preview(_SLUG, str(work_dir), tmp_path, runner=runner)

    assert code == 1
    assert not runner.argvs_starting(*_SERVE_UP)


def test_preview_allows_rerunning_the_same_slug(tmp_path: Path) -> None:
    # A stale instance of the *same* slug is the normal retry path -- the shared
    # script clears it itself, so the guard must not block it.
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()
    _make_preview_state(tmp_path, _SLUG)

    code = reveal_mod.preview(_SLUG, str(work_dir), tmp_path, runner=runner)

    assert code == 0
    assert len(runner.argvs_starting(*_SERVE_UP)) == 1


def test_preview_propagates_a_shared_script_failure(tmp_path: Path) -> None:
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()
    runner.respond(_SERVE_UP, _Result(returncode=1))

    code = reveal_mod.preview(_SLUG, str(work_dir), tmp_path, runner=runner)

    assert code == 1


def test_preview_removes_the_seeded_layout_when_the_boot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The seed is written before the boot is attempted. If the boot fails there is
    # no live preview and so no ``unpreview`` coming to clean up after it -- the
    # failing path has to remove its own copy rather than leave one orphaned.
    repo_root = tmp_path / "repo"
    work_dir = _make_work_dir(repo_root)
    _seed_live_layout(monkeypatch, tmp_path / "host")
    runner = _RecordingRunner()
    runner.respond(_SERVE_UP, _Result(returncode=1))

    code = reveal_mod.preview(_SLUG, str(work_dir), repo_root, runner=runner)

    assert code == 1
    assert not reveal_mod._preview_layout_seed_dir(repo_root, _SLUG).exists()


def test_preview_refresh_delegates_to_the_shared_script(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = reveal_mod.preview_refresh(_SLUG, tmp_path, runner=runner)

    assert code == 0
    refresh_calls = runner.argvs_starting(*_SERVE_REFRESH)
    assert len(refresh_calls) == 1
    # Refreshes the same instance name the preview created for this slug; it never
    # rebuilds or re-registers anything here (the shared script bounces the port).
    assert _flag(refresh_calls[0], "--name") == reveal_mod._preview_instance_name(_SLUG)
    assert not runner.ran("npm", "run", "build")


def test_preview_refresh_propagates_a_shared_script_failure(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    runner.respond(_SERVE_REFRESH, _Result(returncode=1))

    code = reveal_mod.preview_refresh(_SLUG, tmp_path, runner=runner)

    assert code == 1


def test_main_routes_preview_refresh_through_the_shared_script(tmp_path: Path) -> None:
    # End-to-end wiring: main() -> preview_refresh() spawns a *real* subprocess of
    # the shared script. No instance exists, so the shared ``refresh`` reports
    # "nothing to refresh" (exit 1) -- which proves the routing reached it.
    code = reveal_mod.main(
        ["preview-refresh", "--slug", _SLUG, "--repo-root", str(tmp_path)]
    )
    assert code == 1


def test_unpreview_delegates_to_the_shared_script(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = reveal_mod.unpreview(_SLUG, tmp_path, runner=runner)

    assert code == 0
    down_calls = runner.argvs_starting(*_SERVE_DOWN)
    assert len(down_calls) == 1
    # Tears down the same instance name the preview created for this slug.
    assert _flag(down_calls[0], "--name") == reveal_mod._preview_instance_name(_SLUG)


def test_unpreview_propagates_a_shared_script_failure(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    runner.respond(_SERVE_DOWN, _Result(returncode=1))

    code = reveal_mod.unpreview(_SLUG, tmp_path, runner=runner)

    assert code == 1


def test_main_routes_unpreview_through_the_shared_script(tmp_path: Path) -> None:
    # End-to-end wiring: main() -> unpreview() spawns a *real* subprocess of the
    # shared script -- proving ``_SHARED_SERVE_SCRIPT`` resolves to an existing,
    # runnable stdlib script. No instance exists, so the shared ``down`` is an
    # idempotent no-op success.
    code = reveal_mod.main(["unpreview", "--slug", _SLUG, "--repo-root", str(tmp_path)])
    assert code == 0


def test_main_preview_rejects_a_bad_work_dir(tmp_path: Path) -> None:
    # main() -> preview() bails on a missing work_dir before any subprocess.
    code = reveal_mod.main(
        [
            "preview",
            "--slug",
            _SLUG,
            "--work-dir",
            str(tmp_path / "gone"),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert code == 1
