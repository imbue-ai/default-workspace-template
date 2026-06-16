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
                    return result.pop(0) if len(result) > 1 else result[0]
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
    spawns: list[list[str]] = field(default_factory=list)
    last: _FakeSpawned | None = None

    def spawn(self, argv: Sequence[str], cwd: str, env: dict) -> _FakeSpawned:
        self.spawns.append(list(argv))
        self.last = _FakeSpawned()
        return self.last


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
            "apps/system_interface/frontend/src/views/Chat.ts",
            "apps/system_interface/frontend/package.json",
            "apps/system_interface/imbue/system_interface/server.py",
            "apps/system_interface/pyproject.toml",
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
            "apps/system_interface/imbue/system_interface/server_test.py",
            "apps/system_interface/imbue/system_interface/test_e2e.py",
        ]
    )
    assert not changes.any


def test_classify_ignores_unrelated_paths() -> None:
    changes = reveal_mod.classify_changes(["README.md", "vendor/mngr/libs/mngr/x.py"])
    assert not changes.any


# --- happy paths ------------------------------------------------------------


def test_frontend_only_builds_and_broadcasts_without_restart() -> None:
    runner = _runner_with_diff("M\tapps/system_interface/frontend/src/views/Chat.ts\n")
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
        "M\tapps/system_interface/imbue/system_interface/server.py\nM\tapps/system_interface/pyproject.toml\n"
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
        "apps/system_interface",
        "--reinstall",
    ]
    assert spawner.spawns and spawner.spawns[0] == [
        reveal_mod.TOOL_NAME
    ]  # pre-flight booted
    assert spawner.last is not None and spawner.last.terminated  # and torn down
    assert runner.ran("mngr", "start", "--restart", "system-services")
    assert any(_is_live(u) for u in http.get_urls)  # live health probed


def test_backend_src_only_skips_dependency_refresh() -> None:
    runner = _runner_with_diff(
        "M\tapps/system_interface/imbue/system_interface/server.py\n"
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
        "A\tapps/system_interface/imbue/system_interface/new_module.py\n"
    )
    http = _FakeHttp(lambda url: 200 if _is_live(url) else None)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2  # rolled back, UI healthy
    # The live service must NOT have been restarted during the failed attempt...
    # exactly one restart, in recovery, after the rollback commit.
    restart_index = runner.calls.index(
        ["mngr", "start", "--restart", "system-services"]
    )
    commit_calls = [i for i, c in enumerate(runner.calls) if c[:2] == ["git", "commit"]]
    assert commit_calls and restart_index > commit_calls[0]
    # An added file is removed on rollback (not checked out).
    assert runner.ran("git", "rm", "--force", "--ignore-unmatch")
    assert not runner.ran("git", "checkout", _ROLLBACK)


def test_failed_post_restart_health_triggers_rollback_then_recovers() -> None:
    # Pre-flight passes, but the live service stays unhealthy after the first
    # restart and only recovers after the rollback's restart. Key the health off
    # how many restarts have happened (wait_healthy retries many times, so a
    # short None sequence would otherwise pass on a later poll).
    runner = _runner_with_diff(
        "M\tapps/system_interface/imbue/system_interface/server.py\n"
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
        "M\tapps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(
        lambda url: None if _is_live(url) else 200
    )  # live never healthy, even after revert

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 3


def test_frontend_build_failure_rolls_back() -> None:
    runner = _runner_with_diff("M\tapps/system_interface/frontend/src/views/Chat.ts\n")
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
        "M\tapps/system_interface/imbue/system_interface/server.py\n", dirty=True
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
    code = reveal_mod.main(["--rollback-to", _ROLLBACK, "--repo-root", str(tmp_path)])
    assert code == 1


# --- tree restoration -------------------------------------------------------


def test_restore_tree_removes_adds_and_checks_out_the_rest() -> None:
    runner = _RecordingRunner()
    reveal_mod._restore_tree(
        [
            ("A", "apps/system_interface/imbue/system_interface/new_module.py"),
            ("M", "apps/system_interface/imbue/system_interface/server.py"),
            ("D", "apps/system_interface/frontend/src/old.ts"),
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
            "apps/system_interface/imbue/system_interface/new_module.py",
        ]
    ]
    checkouts = [c[-1] for c in runner.argvs_starting("git", "checkout")]
    assert checkouts == [
        "apps/system_interface/imbue/system_interface/server.py",
        "apps/system_interface/frontend/src/old.ts",
    ]
