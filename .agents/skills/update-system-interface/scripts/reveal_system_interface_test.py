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
import subprocess
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
    """Returns whatever ``responder(url)`` yields for the health-probe GETs."""

    def __init__(self, responder: Callable[[str], int | None]) -> None:
        self._responder = responder
        self.get_urls: list[str] = []

    def get_status(self, url: str, timeout: float) -> int | None:
        self.get_urls.append(url)
        return self._responder(url)


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


def _supervisor_status(pid: int) -> _Result:
    """What ``supervisorctl status`` prints for a RUNNING program on ``pid``."""
    return _Result(
        stdout=f"{reveal_mod.SUPERVISOR_PROGRAM}   RUNNING   pid {pid}, uptime 0:00:12\n"
    )


def _runner_with_diff(name_status: str, *, dirty: bool = False) -> _RecordingRunner:
    runner = _RecordingRunner()
    runner.respond(
        ("git", "status", "--porcelain"), _Result(stdout=" M foo\n" if dirty else "")
    )
    runner.respond(("git", "diff"), _Result(stdout=name_status))
    # A settled service by default: RUNNING on one pid that does not turn over.
    # Tests about an unsettled stack override this with a pid-changing response.
    runner.respond(("supervisorctl", "status"), _supervisor_status(4242))
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


def _refreshed_the_view(runner: _RecordingRunner) -> bool:
    """Whether the reveal delegated to the shared post-change refresh helper."""
    return runner.ran(
        sys.executable, str(_REPO / "system/scripts/refresh_workspace_view.py")
    )


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


def test_frontend_only_builds_and_refreshes_without_restart() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n"
    )
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _reveal(runner, http, spawner)

    assert code == 0
    assert runner.ran("npm", "run", "build")
    assert not runner.ran(
        "supervisorctl", "restart"
    )  # frontend change never restarts the backend
    assert not runner.ran(
        "uv", "tool", "install"
    )  # no manifest change -> no dep refresh
    assert not spawner.spawns  # no pre-flight for a frontend-only change
    assert _refreshed_the_view(runner)


def test_backend_only_change_still_refreshes_the_view() -> None:
    """A backend-only reveal must reload the open view too.

    The restart swaps the API underneath a page that keeps rendering from what
    it already fetched, and a restart quick enough not to look unreachable
    never gets a reload from anywhere else -- so skipping it here (as the
    frontend-gated broadcast used to) leaves the user on stale output.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    assert code == 0
    assert runner.ran("supervisorctl", "restart", reveal_mod.SUPERVISOR_PROGRAM)
    assert _refreshed_the_view(runner)


def test_refresh_runs_after_the_restart_not_before() -> None:
    """Refreshing before the backend is back would just reload the old code."""
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )

    _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    restart_index = next(
        i for i, c in enumerate(runner.calls) if c[:2] == ["supervisorctl", "restart"]
    )
    refresh_index = next(
        i
        for i, c in enumerate(runner.calls)
        if c[:1] == [sys.executable] and c[1].endswith("refresh_workspace_view.py")
    )
    assert restart_index < refresh_index


def test_unspawnable_refresh_helper_does_not_fail_a_successful_reveal() -> None:
    """The refresh runs last, after the reveal has already succeeded.

    It is the one step that cannot fail the reveal: the change has landed and
    the live UI is confirmed healthy, so a helper we cannot even spawn (no
    memory to fork right after the restart) must not turn that into a
    non-zero exit the lead reads as "the change did not land".
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    runner.respond((sys.executable,), OSError("Cannot allocate memory"))

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    assert code == 0
    assert _refreshed_the_view(runner)  # it was attempted, not skipped


def test_undecodable_refresh_output_does_not_fail_a_successful_reveal() -> None:
    """Capturing the helper's output must not become the thing that fails a reveal.

    ``capture_output=True, text=True`` decodes what the child wrote, and output
    the stdio encoding cannot decode raises ``UnicodeDecodeError`` -- a
    ``ValueError``, so neither ``OSError`` nor ``SubprocessError`` covers it.
    The helper guards the mirror image of this on its own side; letting it
    escape here would abort a reveal whose change has already landed.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    runner.respond(
        (sys.executable,),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    )

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    assert code == 0


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
    assert runner.ran("supervisorctl", "restart", reveal_mod.SUPERVISOR_PROGRAM)
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
    assert runner.ran("supervisorctl", "restart")


def test_no_relevant_changes_does_nothing() -> None:
    runner = _runner_with_diff("M\tREADME.md\n")
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 0
    assert not runner.ran("npm", "run", "build")
    assert not runner.ran("supervisorctl", "restart")
    # The unconditional refresh in _apply_reveal is only safe because this run
    # never reaches it: nothing changed, so there is nothing to reveal and no
    # reason to reload the view the user is already looking at.
    assert not _refreshed_the_view(runner)


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
    assert not runner.ran("supervisorctl", "restart")
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
    assert not runner.ran(
        "supervisorctl", "restart"
    )  # untouched live service is not restarted
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
        restarts = runner.calls.count(
            ["supervisorctl", "restart", reveal_mod.SUPERVISOR_PROGRAM]
        )
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
                if c == ["supervisorctl", "restart", reveal_mod.SUPERVISOR_PROGRAM]
            ]
        )
        == 2
    )
    # Recovery got the service healthy again, so the restored tree is what the
    # open view should be rendering -- a failed reveal must not leave the user
    # looking at the build that broke.
    assert _refreshed_the_view(runner)


def test_emergency_when_rollback_cannot_restore_health() -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(
        lambda url: None if _is_live(url) else 200
    )  # live never healthy, even after revert

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 3


def test_emergency_when_a_recovery_step_itself_fails() -> None:
    """Exit 3 is not only "never went green" -- a recovery *step* can fail outright.

    Here the tree is restored fine, but rebuilding from known-good fails, so the
    recovery never reaches its health probe at all. That has to read as the
    emergency it is rather than as a successful rollback.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n"
    )
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="type error"))
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 3
    assert runner.ran("git", "checkout", _ROLLBACK)  # the tree was still restored


def test_a_green_verdict_requires_the_service_to_stop_turning_over() -> None:
    """A single 200 can land in the gap between two restarts of a settling stack.

    Reveal printed "updated and confirmed healthy" on exactly that, and five
    seconds later the live UI did not answer and supervisord had a new pid. The
    verdict arms the automatic rollback, so it has to describe settled state.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    # A different pid on every status call for the reveal's entire budget, so no
    # run of consecutive probes ever shares one. The canned list's last entry
    # repeats once exhausted, which is the stack finally settling -- so the
    # rollback that follows can confirm the UI and this stays an ordinary exit 2.
    runner.respond(
        ("supervisorctl", "status"),
        [
            _supervisor_status(7000 + offset)
            for offset in range(reveal_mod._HEALTH_ATTEMPTS)
        ],
    )
    http = _FakeHttp(_all_healthy)  # every probe answers 200

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2, "a stack that is still restarting is not a healthy reveal"
    assert runner.ran("git", "checkout", _ROLLBACK)


def test_a_settled_verdict_tolerates_a_pid_that_settles_partway_through() -> None:
    """The confirmation restarts on a pid change rather than failing on one.

    The point is to wait a settling stack out, not to judge it: a restart landing
    mid-confirmation is exactly the normal case this budget exists for.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    runner.respond(
        ("supervisorctl", "status"),
        [_supervisor_status(7001), _supervisor_status(7002)],
    )
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 0


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

    assert not runner.ran("supervisorctl", "restart")
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


# --- the preview adapter ------------------------------------------------------
#
# ``preview`` is a thin system-interface adapter over the shared
# ``serve_isolated_instance.py`` script. These tests assert the adapter validates
# its input and hands the shared script the system-interface specifics; the
# preview *mechanism* (booting, health, registration, refresh, teardown, state) is
# exercised in ``.agents/shared/scripts/serve_isolated_instance_test.py``, which is
# also where refreshing and tearing a live preview down are covered -- the flow
# invokes those directly rather than through a wrapper here.


_SLUG = "demo-change"
_SERVE_UP = (sys.executable, str(reveal_mod._SHARED_SERVE_SCRIPT), "up")


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
    # System-interface specifics: bind port/host env, neuter layout persistence by
    # dropping MNGR_AGENT_ID, register the inner app + wrapper.
    assert _flag(argv, "--port-env") == reveal_mod.PREVIEW_PORT_ENV
    assert _flag(argv, "--host-env") == reveal_mod.PREVIEW_HOST_ENV
    assert _flag(argv, "--unset-env") == reveal_mod.ENV_MNGR_AGENT_ID
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


def test_the_shared_serve_script_path_resolves_to_a_runnable_script() -> None:
    # ``preview`` spawns the shared script by this path, and the refresh/teardown
    # halves of the flow are invoked directly by the agent rather than through
    # this module -- so nothing else here would notice the ``parents[3]`` arithmetic
    # drifting (a skill folder moving, the script being renamed). Run it for real.
    assert reveal_mod._SHARED_SERVE_SCRIPT.is_file()
    assert (
        subprocess.run(
            [sys.executable, str(reveal_mod._SHARED_SERVE_SCRIPT), "--help"],
            capture_output=True,
        ).returncode
        == 0
    )
    # The prose the agent copies from (this module's docstring, the skill) names
    # the same script repo-relatively; keep the two spellings in step.
    assert str(reveal_mod._SHARED_SERVE_SCRIPT).endswith(
        reveal_mod._SHARED_SERVE_SCRIPT_HINT
    )


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
