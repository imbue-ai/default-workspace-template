"""Tests for ``reveal_system_interface.py``.

Run via: ``uv run pytest .agents/skills/update-system-interface/scripts/reveal_system_interface_test.py``

The orchestration tests inject a recording ``Runner`` (so no real
``git``/``npm``/``uv``/``mngr`` runs), a programmable ``HttpClient`` (so the
health and frontend probes are deterministic), a fake ``Spawner`` (so no
throwaway server is launched), and a no-op sleeper. We assert on the exact
commands the reveal hands to subprocess and on the failure-then-rollback control
flow -- the part that must never regress, because a broken backend takes down the
user's whole UI.

They run against a real temporary repo directory rather than a fictional path,
and the recording runner emulates the build tool's destructive behaviour
(emptying the bundle directory before writing). That is deliberate: the bundle
snapshot exists precisely because a build deletes before it produces, so a
harness that never deletes anything could not tell a working rollback from a
broken one.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Sequence

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
_ASSET_NAME = "index-abc123.js"


def _write_bundle(repo_root: Path) -> None:
    """Seed a built frontend bundle, as a successful ``npm run build`` leaves it."""
    static = repo_root / reveal_mod.STATIC_DIR
    (static / "assets").mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text(
        f'<!doctype html><html><head><script type="module" src="/assets/{_ASSET_NAME}">'
        "</script></head><body></body></html>"
    )
    (static / "assets" / _ASSET_NAME).write_text("console.log('app');")


def _bundle_exists(repo_root: Path) -> bool:
    return (repo_root / reveal_mod.FRONTEND_BUILD_INDEX).exists()


def _make_repo_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / reveal_mod.FRONTEND_DIR).mkdir(parents=True)
    return repo_root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo root that already serves a built bundle, as a live workspace does."""
    repo_root = _make_repo_root(tmp_path)
    _write_bundle(repo_root)
    return repo_root


@pytest.fixture
def unbuilt_repo(tmp_path: Path) -> Path:
    """A repo root that has never built a bundle, so there is none to snapshot.

    The counterpart to ``repo``, and the distinction the recovery tests turn on:
    with no bundle to save, recovery has to fall back to rebuilding.
    """
    return _make_repo_root(tmp_path)


# The prefix :func:`reveal_system_interface.snapshot_bundle` files its copies
# under. They go in the *system* temp dir on purpose -- a stray directory inside
# the repo would dirty the tree -- so ``tmp_path`` does not reap them.
_SNAPSHOT_GLOB = "system-interface-bundle-*"


def _unwrap_expendable(argv: list[str]) -> list[str]:
    """Strip the ``sh -c <oom tag> exec "$@"`` wrapper the hungry steps carry.

    The orchestration tests are about *which command ran*, not which band it
    ran in, so the recorders below present the command that was asked for.
    That the wrapper is applied to the right steps -- and only those -- is
    asserted on the raw argv in its own test.
    """
    if argv[:2] == ["sh", "-c"] and argv[3:4] == ["sh"]:
        return argv[4:]
    return argv


@pytest.fixture(autouse=True)
def reap_snapshots_left_for_the_operator() -> Iterator[None]:
    """Remove any bundle copy a test's reveal deliberately left behind.

    The emergency path (exit 3) keeps its copy rather than discarding it, because
    it is the operator's way back; every exit-3 test therefore leaves one, and
    without this they pile up in the system temp dir run after run. Only the
    directories that appear *during* the test are removed, so a copy some other
    process owns is never touched.
    """
    temp_root = Path(tempfile.gettempdir())
    before = set(temp_root.glob(_SNAPSHOT_GLOB))
    yield
    for left_behind in set(temp_root.glob(_SNAPSHOT_GLOB)) - before:
        shutil.rmtree(left_behind, ignore_errors=True)


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

    When ``repo_root`` is set, ``npm run build`` also *behaves* like the real
    build tool: it empties the bundle directory first and only writes a new
    bundle if the canned result is a success. Without that, a test could not
    distinguish "the rollback restored the UI" from "the UI was never removed".
    """

    calls: list[list[str]] = field(default_factory=list)
    # The same calls before :func:`_unwrap_expendable`, for the band test.
    raw_calls: list[list[str]] = field(default_factory=list)
    envs: list[dict | None] = field(default_factory=list)
    # What each executable resolves to on PATH; absent means "not installed",
    # which is also the default so tests never reach the real machine's PATH.
    executables: dict[str, str] = field(default_factory=dict)
    repo_root: Path | None = None
    # Set False to model a build that exits 0 after emptying its output but
    # before writing anything -- e.g. one killed part-way through.
    is_build_output_written: bool = True
    _responses: dict[tuple[str, ...], object] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: object) -> None:
        self._responses[prefix] = result

    def which(self, executable: str) -> str | None:
        return self.executables.get(executable)

    def run(self, argv: Sequence[str], **kwargs) -> _Result:
        self.raw_calls.append(list(argv))
        argv_list = _unwrap_expendable(list(argv))
        self.calls.append(argv_list)
        self.envs.append(kwargs.get("env"))
        result = self._canned_result(argv_list)
        if argv_list[:3] == ["npm", "run", "build"] and self.repo_root is not None:
            self._emulate_build(result.returncode == 0)
        return result

    def _canned_result(self, argv_list: list[str]) -> _Result:
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

    def _emulate_build(self, is_successful: bool) -> None:
        assert self.repo_root is not None
        static = self.repo_root / reveal_mod.STATIC_DIR
        # vite's `emptyOutDir: true` -- the output is destroyed before any of the
        # new output is written, so a failure part-way through leaves nothing.
        shutil.rmtree(static, ignore_errors=True)
        if is_successful and self.is_build_output_written:
            _write_bundle(self.repo_root)

    def argvs_starting(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def ran(self, *prefix: str) -> bool:
        return bool(self.argvs_starting(*prefix))


class _FakeHttp(reveal_mod.HttpClient):
    """Returns whatever ``responder(url)`` yields for the health-probe GETs.

    ``page_responder`` drives the frontend probe's body/header reads and defaults
    to a healthy built app shell whose module script serves as JavaScript.
    """

    def __init__(
        self,
        responder: Callable[[str], int | None],
        page_responder: Callable[[str], reveal_mod.FetchedPage | None] | None = None,
    ) -> None:
        self._responder = responder
        self._page_responder = page_responder or _built_app_page
        self.get_urls: list[str] = []
        self.page_urls: list[str] = []

    def get_status(self, url: str, timeout: float) -> int | None:
        self.get_urls.append(url)
        return self._responder(url)

    def get_page(self, url: str, timeout: float) -> reveal_mod.FetchedPage | None:
        self.page_urls.append(url)
        return self._page_responder(url)


def _built_app_page(url: str) -> reveal_mod.FetchedPage:
    """The real app shell and its module script, both served correctly."""
    if url.endswith(".js"):
        return reveal_mod.FetchedPage(
            status=200,
            body="console.log('app');",
            headers={"content-type": "text/javascript"},
        )
    return reveal_mod.FetchedPage(
        status=200,
        body=f'<!doctype html><script type="module" src="/assets/{_ASSET_NAME}"></script>',
        headers={"content-type": "text/html", reveal_mod.FRONTEND_BUILT_HEADER: "true"},
    )


def _placeholder_page(url: str) -> reveal_mod.FetchedPage:
    """What the backend serves when the bundle is missing: a healthy-looking 200."""
    return reveal_mod.FetchedPage(
        status=200,
        body="<!doctype html><p>Frontend not built</p>",
        headers={
            "content-type": "text/html",
            reveal_mod.FRONTEND_BUILT_HEADER: "false",
        },
    )


@dataclass
class _FakeSpawned:
    output: str = ""
    exited: bool = False
    terminated: bool = False

    def terminate(self) -> None:
        self.terminated = True

    def has_exited(self) -> bool:
        return self.exited

    def read_output(self) -> str:
        return self.output


@dataclass
class _FakeSpawner(reveal_mod.Spawner):
    """Records the pre-flight throwaway boot ``reveal`` runs before a live restart.

    ``output`` is what the throwaway backend "wrote" while failing to boot.
    """

    output: str = ""
    exited: bool = False
    spawns: list[list[str]] = field(default_factory=list)
    raw_spawns: list[list[str]] = field(default_factory=list)
    output_paths: list[Path] = field(default_factory=list)
    last: _FakeSpawned | None = None

    def spawn(
        self, argv: Sequence[str], cwd: str, env: dict, output_path: Path
    ) -> _FakeSpawned:
        self.raw_spawns.append(list(argv))
        self.spawns.append(_unwrap_expendable(list(argv)))
        self.output_paths.append(output_path)
        self.last = _FakeSpawned(output=self.output, exited=self.exited)
        return self.last


def _runner_with_diff(
    name_status: str, *, dirty: bool = False, repo_root: Path | None = None
) -> _RecordingRunner:
    runner = _RecordingRunner(repo_root=repo_root)
    runner.respond(
        ("git", "status", "--porcelain"), _Result(stdout=" M foo\n" if dirty else "")
    )
    runner.respond(("git", "diff"), _Result(stdout=name_status))
    return runner


def _reveal(
    runner: _RecordingRunner,
    http: _FakeHttp,
    spawner: _FakeSpawner,
    repo_root: Path = _REPO,
) -> int:
    return reveal_mod.reveal(
        _ROLLBACK,
        repo_root,
        runner=runner,
        http=http,
        spawner=spawner,
        sleeper=lambda _seconds: None,
        base_url=_LIVE_BASE,
    )


def _all_healthy(_url: str) -> int:
    return 200


def _refreshed_the_view(runner: _RecordingRunner, repo_root: Path = _REPO) -> bool:
    """Whether the reveal delegated to the shared post-change refresh helper.

    The helper's path is repo-relative, so tests running against a real
    temporary repo have to pass the same root they revealed against.
    """
    return runner.ran(
        sys.executable, str(repo_root / "system/scripts/refresh_workspace_view.py")
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


def test_classify_treats_a_vendored_manifest_as_a_backend_manifest() -> None:
    # The change that actually ships in a release. Every "system/vendor/mngr:
    # refresh" commit in this repo's history leaves uv.lock untouched, so keying
    # the dependency refresh only off the lock would never fire on the merge that
    # moves the vendored mngr -- which is exactly the one that stales the editable
    # tool's dependency closure and breaks the mngr CLI.
    changes = reveal_mod.classify_changes(
        ["system/vendor/mngr/libs/mngr/pyproject.toml"]
    )
    assert changes.backend_manifest and changes.backend


def test_classify_treats_the_root_manifest_as_a_backend_manifest() -> None:
    # It holds the [tool.uv.sources] the tool installs resolve through.
    assert reveal_mod.classify_changes(["pyproject.toml"]).backend_manifest


def test_classify_treats_the_vendored_workspace_root_as_a_backend_manifest() -> None:
    # `uv tool install -e system/vendor/mngr/libs/mngr` walks up to this file to
    # find the workspace that package belongs to, so it -- not the repo root --
    # supplies the sources for imbue-common and overlay (which libs/mngr pins
    # exactly and which resolve nowhere else), the exclude-newer cooldown mngr
    # advances before each release, and the dependency overrides. Vendor syncs
    # move it without necessarily touching any libs/*/pyproject.toml.
    assert reveal_mod.classify_changes(
        ["system/vendor/mngr/pyproject.toml"]
    ).backend_manifest


def test_classify_ignores_vendored_source_and_nested_paths() -> None:
    # Source edits do not move the dependency closure, and a manifest deeper in
    # the tree is not one of the vendored packages we install from.
    changes = reveal_mod.classify_changes(
        [
            "system/vendor/mngr/libs/mngr/imbue/mngr/main.py",
            "system/vendor/mngr/libs/mngr/imbue/mngr/pyproject.toml",
        ]
    )
    assert not changes.any


def test_classify_ignores_backend_test_files() -> None:
    changes = reveal_mod.classify_changes(
        [
            "system/apps/system_interface/imbue/system_interface/server_test.py",
            "system/apps/system_interface/imbue/system_interface/test_e2e.py",
        ]
    )
    assert not changes.any


def test_classify_counts_frontend_files_outside_src() -> None:
    # These all change the emitted bundle. Matching only frontend/src/ made them
    # classify as no change, so the reveal said "nothing to reveal" and left the
    # merged tree serving a stale bundle.
    for path in (
        "system/apps/system_interface/frontend/index.html",
        "system/apps/system_interface/frontend/vite.config.ts",
        "system/apps/system_interface/frontend/tsconfig.json",
        "system/apps/system_interface/frontend/media/favicon.ico",
    ):
        changes = reveal_mod.classify_changes([path])
        assert changes.frontend, path
        assert changes.any, path


def test_classify_ignores_unrelated_paths() -> None:
    changes = reveal_mod.classify_changes(
        ["README.md", "system/vendor/mngr/libs/mngr/x.py"]
    )
    assert not changes.any


# --- happy paths ------------------------------------------------------------


def test_frontend_only_builds_and_refreshes_without_restart(repo: Path) -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _reveal(runner, http, spawner, repo)

    assert code == 0
    assert runner.ran("npm", "run", "build")
    assert not runner.ran("mngr", "start")  # frontend change never restarts the backend
    assert not runner.ran(
        "uv", "tool", "install"
    )  # no manifest change -> no dep refresh
    assert not spawner.spawns  # no pre-flight for a frontend-only change
    assert _refreshed_the_view(runner, repo)


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
    assert runner.ran("mngr", "start", "--restart", "system-services")
    assert _refreshed_the_view(runner)


def test_refresh_runs_after_the_restart_not_before() -> None:
    """Refreshing before the backend is back would just reload the old code."""
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )

    _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    restart_index = next(
        i for i, c in enumerate(runner.calls) if c[:2] == ["mngr", "start"]
    )
    refresh_index = next(
        i
        for i, c in enumerate(runner.calls)
        if c[:1] == [sys.executable] and c[1].endswith("refresh_workspace_view.py")
    )
    assert restart_index < refresh_index


def test_only_the_memory_hungry_steps_are_made_expendable(repo: Path) -> None:
    """The build may be shed; the thing that would roll it back may not.

    This process bands itself into the system interface's own band, and every
    child inherits that unless it is tagged back. Getting the split wrong in
    either direction is a real failure: an untagged ``npm run build`` is
    protected ahead of the user's chats, while a tagged ``git checkout`` puts
    the rollback itself in the first rank to be shed.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/package-lock.json\n"
        "M\tsystem/apps/system_interface/pyproject.toml\n",
        repo_root=repo,
    )
    spawner = _FakeSpawner()

    assert _reveal(runner, _FakeHttp(_all_healthy), spawner, repo) == 0

    expendable = [
        _unwrap_expendable(c) for c in runner.raw_calls if c != _unwrap_expendable(c)
    ]
    # The dependency refresh mirrors build_workspace.sh: both tool reinstalls and
    # the workspace ``uv sync`` are memory-hungry and go into the expendable band
    # alongside the npm commands. Only the pre-flight boot (below) and the process
    # itself stay out of it.
    assert [c[:3] for c in expendable] == [
        ["npm", "ci"],
        ["uv", "tool", "install"],
        ["uv", "tool", "install"],
        ["uv", "sync", "--all-packages"],
        ["npm", "run", "build"],
    ]
    # The pre-flight boot is a second copy of the backend, so it goes too.
    assert spawner.raw_spawns == [reveal_mod.as_expendable([reveal_mod.TOOL_NAME])]
    # Everything else -- git, mngr, the refresh helper -- keeps this process's
    # protection. Those are the steps that put the workspace back. The ``uv tool
    # dir`` lookup the reinstall makes is a cheap metadata query, not one of the
    # memory-hungry installs, so it is not banded either.
    protected = [c for c in runner.raw_calls if c == _unwrap_expendable(c)]
    assert all(
        c[0] in ("git", "mngr", sys.executable) or c[:3] == ["uv", "tool", "dir"]
        for c in protected
    ), protected


def test_the_expendable_wrapper_hands_the_command_its_argv_intact() -> None:
    """``exec "$@"`` must not re-split or glob what it is handed.

    The wrapper is a shell, so an argument containing a space or a metacharacter
    is exactly where this would go wrong -- and it would go wrong silently, as a
    build that ran against the wrong path.
    """
    wrapped = reveal_mod.as_expendable(["npm", "run", "build --out dir", "*"])

    assert wrapped[:2] == ["sh", "-c"]
    # argv is passed positionally, never interpolated into the script.
    assert "build --out dir" not in wrapped[2]
    assert wrapped[3:] == ["sh", "npm", "run", "build --out dir", "*"]
    assert wrapped[2].endswith('exec "$@"')


def test_only_the_reveal_bands_itself_out_of_the_expendable_range() -> None:
    """The protection is the rollback's, and only ``reveal`` has one.

    ``preview`` hands the shared serve script a *detached* system-interface
    server that outlives the invocation and inherits whatever band this process
    holds, so banding here would leave a throwaway second copy of the whole UI
    protected ahead of every chat and every agent for as long as it runs.
    """
    assert reveal_mod._is_shed_protected_command(["reveal", "--rollback-to", "abc"])
    assert not reveal_mod._is_shed_protected_command(
        ["preview", "--slug", "s", "--work-dir", "/w"]
    )
    assert not reveal_mod._is_shed_protected_command(["unpreview", "--slug", "s"])
    assert not reveal_mod._is_shed_protected_command([])
    # Any sequence, not just the list ``sys.argv`` slicing happens to produce.
    assert reveal_mod._is_shed_protected_command(("reveal", "--rollback-to", "abc"))


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
    # Every environment the served backend can be started from gets rebuilt, in
    # build_workspace.sh's order: the vendored mngr the backend shells out to,
    # the backend's own tool, then the workspace venv.
    assert runner.argvs_starting("uv", "tool", "install") == [
        ["uv", "tool", "install", "-e", "system/vendor/mngr/libs/mngr", "--reinstall"],
        ["uv", "tool", "install", "-e", "system/apps/system_interface", "--reinstall"],
    ]
    assert runner.ran("uv", "sync", "--all-packages", "--frozen")
    assert spawner.spawns and spawner.spawns[0] == [
        reveal_mod.TOOL_NAME
    ]  # pre-flight booted
    assert spawner.last is not None and spawner.last.terminated  # and torn down
    assert runner.ran("mngr", "start", "--restart", "system-services")
    assert any(_is_live(u) for u in http.get_urls)  # live health probed


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
    assert not runner.ran("mngr", "start")
    # Recovery still re-confirmed the untouched service via the health probe.
    assert any(_is_live(u) for u in http.get_urls)
    # An added file is removed on rollback (not checked out).
    assert runner.ran("git", "rm", "--force", "--ignore-unmatch")
    assert not runner.ran("git", "checkout", _ROLLBACK)


def _with_receipt(
    runner: _RecordingRunner, tool_dir: Path, tool: str, body: str
) -> None:
    """Point ``uv tool dir`` at ``tool_dir`` and give ``tool`` a receipt there."""
    runner.respond(("uv", "tool", "dir"), _Result(stdout=f"{tool_dir}\n"))
    (tool_dir / tool).mkdir(parents=True, exist_ok=True)
    (tool_dir / tool / "uv-receipt.toml").write_text(body)


def test_dependency_refresh_preserves_a_tools_registered_plugins(
    tmp_path: Path,
) -> None:
    # A bare --reinstall rebuilds a tool from its base package alone. For the mngr
    # tool the extras ARE its plugins, so dropping them leaves a CLI that cannot
    # parse its own plugin config -- swapping one broken workspace for another.
    runner = _runner_with_diff("M\tuv.lock\n")
    _with_receipt(
        runner,
        tmp_path / "tools",
        "imbue-mngr",
        """
        [tool]
        requirements = [
            { name = "imbue-mngr", editable = "/repo/system/vendor/mngr/libs/mngr" },
            { name = "imbue-mngr-claude", editable = "/repo/system/vendor/mngr/libs/mngr_claude" },
            { name = "imbue-mngr-wait", editable = "/repo/system/vendor/mngr/libs/mngr_wait" },
        ]
        """,
    )

    _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    assert runner.argvs_starting("uv", "tool", "install")[0] == [
        "uv",
        "tool",
        "install",
        "-e",
        "system/vendor/mngr/libs/mngr",
        "--with-editable",
        "/repo/system/vendor/mngr/libs/mngr_claude",
        "--with-editable",
        "/repo/system/vendor/mngr/libs/mngr_wait",
        "--reinstall",
    ]


def test_dependency_refresh_repins_the_base_to_the_in_tree_source(
    tmp_path: Path,
) -> None:
    # A receipt that has lost its editable marker (observed in the wild) must not
    # make us resolve the base from the index -- that would silently swap the
    # workspace's own vendored code for a published release.
    runner = _runner_with_diff("M\tuv.lock\n")
    _with_receipt(
        runner,
        tmp_path / "tools",
        "imbue-mngr",
        '[tool]\nrequirements = [{ name = "imbue-mngr" }]\n',
    )

    _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    install = runner.argvs_starting("uv", "tool", "install")[0]
    assert install == [
        "uv",
        "tool",
        "install",
        "-e",
        "system/vendor/mngr/libs/mngr",
        "--reinstall",
    ]


def test_tool_location_comes_from_the_console_scripts_shebang(tmp_path: Path) -> None:
    # uv's default tool directory follows $HOME, and the workspace runs under a
    # different $HOME than build_workspace.sh installed with -- so defaulting
    # rebuilds a shadow copy nothing on PATH runs, and the stale tool everyone
    # actually executes is reported as successfully refreshed. The console script
    # names its own environment, so we take the answer from there.
    bin_dir = tmp_path / "root" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    tools = tmp_path / "root" / ".local" / "share" / "uv" / "tools"
    script = bin_dir / "mngr"
    script.write_text(
        f"#!{tools}/imbue-mngr/bin/python3\n# -*- coding: utf-8 -*-\nimport sys\n"
    )

    (tools / "imbue-mngr").mkdir(parents=True)
    (tools / "imbue-mngr" / "uv-receipt.toml").write_text("[tool]\nrequirements = []\n")

    location = reveal_mod._tool_location(script, "imbue-mngr")

    assert location == (
        tmp_path / "root" / ".local" / "share" / "uv" / "tools",
        bin_dir,
    )


@pytest.mark.parametrize(
    "contents",
    [
        "import sys\n",  # no shebang at all
        "#!\n",  # shebang with no interpreter
        "#!/python3\n",  # too shallow to name a tool directory
    ],
)
def test_tool_location_declines_what_it_cannot_read(
    contents: str, tmp_path: Path
) -> None:
    # Anything we cannot interpret means we do not know which installation this
    # is, and the caller falls back to letting uv decide -- guessing a directory
    # would be worse than uv's own default.
    script = tmp_path / "mngr"
    script.write_text(contents)

    assert reveal_mod._tool_location(script, "imbue-mngr") is None


def test_tool_location_declines_the_workspace_venvs_console_script(
    tmp_path: Path,
) -> None:
    # Both names are also uv sync members, so PATH can resolve to the venv's own
    # entrypoint. Deriving a "tool directory" from that would build a tool
    # environment inside the served checkout -- dirtying the tree the next reveal
    # refuses to run on, and overwriting the venv's entrypoint. No receipt, no deal.
    venv_bin = tmp_path / "workspace" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    script = venv_bin / "system-interface"
    script.write_text(f"#!{venv_bin}/python3\nimport sys\n")

    assert reveal_mod._tool_location(script, "system-interface") is None


def test_tool_location_declines_a_script_it_cannot_open(tmp_path: Path) -> None:
    assert reveal_mod._tool_location(tmp_path / "does-not-exist", "imbue-mngr") is None


def test_dependency_refresh_targets_the_installation_actually_on_path(
    tmp_path: Path,
) -> None:
    # The whole point: uv would otherwise default to a tool directory under
    # $HOME, rebuild a copy there, and report success while the tool that is
    # really being run stays stale.
    bin_dir = tmp_path / "root" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    tools = tmp_path / "root" / ".local" / "share" / "uv" / "tools"
    (bin_dir / "mngr").write_text(f"#!{tools}/imbue-mngr/bin/python3\nimport sys\n")
    (tools / "imbue-mngr").mkdir(parents=True)
    (tools / "imbue-mngr" / "uv-receipt.toml").write_text("[tool]\nrequirements = []\n")
    runner = _runner_with_diff("M\tuv.lock\n")
    runner.executables["mngr"] = str(bin_dir / "mngr")

    _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    install_env = next(
        env
        for argv, env in zip(runner.calls, runner.envs)
        if argv[:4] == ["uv", "tool", "install", "-e"]
        and argv[4] == "system/vendor/mngr/libs/mngr"
    )
    assert install_env is not None
    assert install_env["UV_TOOL_DIR"] == str(tools)
    assert install_env["UV_TOOL_BIN_DIR"] == str(bin_dir)


def test_dependency_refresh_survives_a_tool_with_no_receipt() -> None:
    # No readable receipt means the tool is not installed (or predates receipts);
    # the refresh must still run as the plain install it would otherwise be.
    runner = _runner_with_diff("M\tuv.lock\n")
    runner.respond(("uv", "tool", "dir"), _Result(returncode=1, stdout=""))

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    assert code == 0
    assert len(runner.argvs_starting("uv", "tool", "install")) == 2


def test_dependency_refresh_reports_a_receipt_it_cannot_read(
    tmp_path: Path, capsys
) -> None:
    # A receipt that is present but garbled is not the fresh-install case: we had
    # a tool and lost the record of what it was installed with, so the reinstall
    # below rebuilds it without its plugins. Degrading silently would hand back
    # exactly the plugin-less CLI this refresh exists to prevent, and report
    # success while doing it.
    runner = _runner_with_diff("M\tuv.lock\n")
    _with_receipt(runner, tmp_path / "tools", "imbue-mngr", "[tool]\nrequirements = [")

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner())

    assert code == 0
    assert runner.argvs_starting("uv", "tool", "install")[0] == [
        "uv",
        "tool",
        "install",
        "-e",
        "system/vendor/mngr/libs/mngr",
        "--reinstall",
    ]
    reported = capsys.readouterr().err
    assert "imbue-mngr" in reported and "drops any plugins" in reported


def test_failed_preflight_reports_why_the_backend_did_not_boot(capsys) -> None:
    # The whole point of the pre-flight is that the merged code never reaches the
    # live service -- so its output is the only evidence of *why* it was rejected.
    # Without it a reveal exit 2 is indistinguishable from a slow boot, and whoever
    # picks it up diagnoses a cause they cannot see.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    spawner = _FakeSpawner(
        output=(
            "starting up\nDEBUG loading agents\nTraceback (most recent call last):\n"
            "ModuleNotFoundError: No module named 'frontmatter'\n"
        )
    )

    code = _reveal(
        runner, _FakeHttp(lambda url: 200 if _is_live(url) else None), spawner
    )

    assert code == 2
    reported = capsys.readouterr().err
    # stderr gets all of it -- whoever ran the reveal is looking right now.
    assert "ModuleNotFoundError: No module named 'frontmatter'" in reported
    assert "DEBUG loading agents" in reported
    # The rollback commit carries the cause, so the reason survives in git history
    # after the terminal that ran the reveal is gone -- but only the line that
    # names it. A commit body is read by everyone who ever looks at this file's
    # history, and a backend's startup log is a poor thing to write into git.
    commits = runner.argvs_starting("git", "commit")
    message = next(arg for arg in commits[0] if "auto-reverted" in arg)
    assert "ModuleNotFoundError: No module named 'frontmatter'" in message
    assert "DEBUG loading agents" not in message
    assert "starting up" not in message


def test_failed_preflight_that_produced_no_output_says_so(capsys) -> None:
    # A silent failure is itself a finding (the tool never got far enough to log),
    # and must not read as "the output was dropped again".
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )

    _reveal(
        runner, _FakeHttp(lambda url: 200 if _is_live(url) else None), _FakeSpawner()
    )

    assert "wrote nothing at all" in capsys.readouterr().err


def test_preflight_output_is_tailed_to_the_interesting_end() -> None:
    # A backend that logs its way to a crash would otherwise bury the traceback
    # under startup chatter, so we keep the end and say what was dropped.
    limit = reveal_mod._PREFLIGHT_OUTPUT_TAIL_LINES
    tailed = reveal_mod._tail(
        "\n".join([f"chatter {i}" for i in range(limit + 10)] + ["the actual error"]),
        limit,
    )

    assert tailed.splitlines()[-1] == "the actual error"
    assert len(tailed.splitlines()) == limit + 1  # the omission notice
    assert "11 earlier line(s) omitted" in tailed
    assert "chatter 0" not in tailed


def test_spawner_captures_both_streams_of_a_real_child(tmp_path: Path) -> None:
    # The capture has to survive a real Popen: stderr is redirected onto stdout's
    # file, and the parent closes its handle while the child keeps writing. Models
    # the case that matters -- a backend that dies on import, whose traceback is
    # the whole reason the pre-flight rejected the merge.
    output_path = tmp_path / "boot.log"
    spawned = reveal_mod.Spawner().spawn(
        [
            sys.executable,
            "-c",
            "import sys; print('on stdout'); print('on stderr', file=sys.stderr)",
        ],
        cwd=str(tmp_path),
        env=dict(os.environ),
        output_path=output_path,
    )
    for _ in range(500):
        if spawned.has_exited():
            break
        time.sleep(0.01)
    assert spawned.has_exited()
    spawned.terminate()

    captured = spawned.read_output()
    assert "on stdout" in captured
    assert "on stderr" in captured


def test_preflight_stops_polling_once_the_backend_has_died() -> None:
    # A backend that died on import will not become healthy, so the reveal must
    # not sit out the rest of the deadline before rolling back.
    probes: list[str] = []

    def _record(url: str) -> int | None:
        probes.append(url)
        return 200 if _is_live(url) else None

    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )

    code = _reveal(runner, _FakeHttp(_record), _FakeSpawner(exited=True))

    assert code == 2
    # One pre-flight probe, not _PREFLIGHT_ATTEMPTS of them.
    assert len([u for u in probes if not _is_live(u)]) == 1


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
    # Both tools are rebuilt twice: once in the failed reveal, once in recovery to
    # restore the known-good dependency set on disk. Recovery has to cover the mngr
    # tool too -- the rollback moves the vendored source back, which stales its
    # closure exactly as the merge did.
    assert len(runner.argvs_starting("uv", "tool", "install")) == 4
    assert len(runner.argvs_starting("uv", "sync", "--all-packages", "--frozen")) == 2


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


def test_frontend_build_failure_rolls_back_and_restores_the_bundle(repo: Path) -> None:
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="type error"))
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner(), repo)

    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK)
    # The failed build emptied the bundle directory; the snapshot put it back.
    assert _bundle_exists(repo)


def test_recovery_restores_the_bundle_without_rebuilding(repo: Path) -> None:
    # The incident this guards against: the build environment itself is broken,
    # so re-running the build during recovery -- the old behaviour -- fails the
    # same way and leaves the workspace with no UI at all. Restoring the
    # snapshot needs no npm, so the rollback still lands a working frontend.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    runner.respond(
        ("npm", "run", "build"),
        _Result(returncode=1, stderr="npm ERR! ENOTFOUND registry"),
    )
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner(), repo)

    assert code == 2
    assert _bundle_exists(repo)
    # One build attempt only: recovery restored rather than retrying the command
    # that had just failed.
    assert len(runner.argvs_starting("npm", "run", "build")) == 1


def test_recovery_skips_npm_ci_which_would_destroy_node_modules(repo: Path) -> None:
    # npm ci deletes node_modules before installing, so re-running it while
    # recovering can only make a broken frontend environment worse -- and the
    # restored bundle is compiled output that needs neither.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/package.json\n", repo_root=repo
    )
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="type error"))
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner(), repo)

    assert code == 2
    assert (
        len(runner.argvs_starting("npm", "ci")) == 1
    )  # the reveal's, not a second one
    assert _bundle_exists(repo)


def test_build_that_writes_no_bundle_is_a_failure_not_a_success(repo: Path) -> None:
    # A build tool killed after emptying its output directory can still exit 0.
    # Trusting the exit code alone would report success on an empty bundle and
    # hand the user a blank page.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    runner.is_build_output_written = False
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner(), repo)

    # Rolled back rather than revealed; the snapshot restored the old bundle.
    assert code == 2
    assert _bundle_exists(repo)


def test_recovery_rebuilds_when_there_was_no_bundle_to_snapshot(
    unbuilt_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A workspace that never built a bundle has nothing to restore, so recovery
    # falls back to building the known-good tree -- the behaviour from before
    # the snapshot existed, and the only path left for that case.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n",
        repo_root=unbuilt_repo,
    )
    # The reveal's build fails; the recovery build from known-good succeeds.
    runner.respond(
        ("npm", "run", "build"), [_Result(returncode=1, stderr="type error"), _Result()]
    )

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner(), unbuilt_repo)

    assert code == 2
    assert _bundle_exists(unbuilt_repo)
    assert len(runner.argvs_starting("npm", "run", "build")) == 2
    # The skipped snapshot says so, like a failed copy does: the emergency
    # path's docs tell the reader to check the stderr for why there is no
    # snapshot path, so both reasons must actually leave a line there.
    assert "no built bundle to copy aside" in capsys.readouterr().err


def test_a_recovery_rebuild_reinstalls_the_rolled_back_dependencies(
    unbuilt_repo: Path,
) -> None:
    # The one path that both compiles from source and rolls a manifest back.
    # node_modules holds what the failed reveal's `npm ci` installed, while the
    # restored tree holds the old lockfile, so building without refreshing them
    # compiles the known-good sources against the wrong dependency tree. The
    # trade that justifies skipping `npm ci` elsewhere does not apply here:
    # there is no snapshotted bundle for it to endanger.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/package-lock.json\n",
        repo_root=unbuilt_repo,
    )
    # The reveal's build fails; the recovery build from known-good succeeds.
    runner.respond(
        ("npm", "run", "build"), [_Result(returncode=1, stderr="type error"), _Result()]
    )

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner(), unbuilt_repo)

    assert code == 2
    assert _bundle_exists(unbuilt_repo)
    # Everything after the rollback checkout is the recovery's, so this shows
    # the refresh is the recovery's own and that it precedes its build.
    rolled_back_at = next(
        i for i, c in enumerate(runner.calls) if c[:3] == ["git", "checkout", _ROLLBACK]
    )
    recovery_npm = [c[:3] for c in runner.calls[rolled_back_at:] if c[0] == "npm"]
    assert recovery_npm == [["npm", "ci"], ["npm", "run", "build"]]


def test_a_bundle_that_cannot_be_copied_aside_still_reveals(repo: Path) -> None:
    # The snapshot is a precaution. Refusing to reveal because the copy failed
    # would make a full or read-only disk fatal to a change that is otherwise
    # fine, so it degrades to the behaviour from before the snapshot existed.
    # A dangling symlink in the bundle is what makes copytree fail here.
    (repo / reveal_mod.STATIC_DIR / "dangling").symlink_to("no-such-file")
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )

    code = _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner(), repo)

    assert code == 0
    assert runner.ran("npm", "run", "build")


def test_a_bundle_that_cannot_be_copied_aside_leaves_no_temporary_copy(
    repo: Path, tmp_path: Path
) -> None:
    # The half-written copy has to go with the failure. The documented reasons
    # to get here are a full or read-only disk, so leaving a partial bundle
    # behind on every attempt would make the next one likelier to fail the same
    # way -- and nothing else cleans it up, since the reveal only discards a
    # snapshot it was actually given.
    (repo / reveal_mod.STATIC_DIR / "dangling").symlink_to("no-such-file")
    temporary_root = tmp_path / "tmp"
    temporary_root.mkdir()
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(temporary_root)
    try:
        assert reveal_mod.snapshot_bundle(repo) is None
    finally:
        tempfile.tempdir = previous_tempdir

    assert list(temporary_root.iterdir()) == []


def test_a_bundle_that_cannot_be_restored_reports_failure_rather_than_crashing(
    repo: Path, tmp_path: Path
) -> None:
    # Recovery is the last line of defense and the exit code is all the caller
    # has: a filesystem error putting the snapshot back (the temp copy reaped
    # from under us, a full disk) has to read as "not recovered" -- exit 3 --
    # and not as a traceback, which would exit 1, the code meaning "nothing was
    # changed", after the rollback commit has already landed.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )

    recovered = reveal_mod._recover_running_state(
        reveal_mod.classify_changes(
            ["system/apps/system_interface/frontend/src/views/Chat.ts"]
        ),
        repo,
        _LIVE_BASE,
        runner,
        _FakeHttp(_all_healthy),
        lambda _seconds: None,
        live_service_restarted=False,
        saved_bundle=tmp_path / "reaped" / "static",
        is_frontend_expected=True,
    )

    assert recovered is False


def _breaks_after_the_build(
    runner: _RecordingRunner, broken: Callable[[str], reveal_mod.FetchedPage]
) -> Callable[[str], reveal_mod.FetchedPage]:
    """Serve a working frontend until the reveal builds, then the broken one.

    Models a reveal that *regresses* the UI, which is the only case a rollback
    is the right answer for -- a UI that was already broken beforehand is not
    something rolling this change back would fix.
    """

    def responder(url: str) -> reveal_mod.FetchedPage:
        has_built = any(c[:3] == ["npm", "run", "build"] for c in runner.calls)
        return broken(url) if has_built else _built_app_page(url)

    return responder


def test_a_reveal_that_leaves_the_placeholder_showing_is_rolled_back(
    repo: Path,
) -> None:
    # The state the incident left behind: /api/agents answers fine and the app
    # shell is an HTTP 200, but what it serves is the "not built" placeholder.
    # Nothing in the old health check could see this.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    http = _FakeHttp(
        _all_healthy, page_responder=_breaks_after_the_build(runner, _placeholder_page)
    )

    code = _reveal(runner, http, _FakeSpawner(), repo)

    # Recovery cannot make the fake serve a good frontend either, so it escalates
    # rather than reporting a healthy rollback it cannot vouch for.
    assert code == 3
    assert http.page_urls  # the frontend was actually probed


def test_an_emergency_hands_the_bundle_snapshot_to_the_operator(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exit 3 means recovery could not put a working frontend back, so the copy
    # is the last known-good bundle anywhere: the tree's was destroyed before the
    # build that failed, and rebuilding is what just failed. Discarding it with
    # every other outcome would take the operator's only npm-free way back --
    # which is the whole premise of taking a copy in the first place.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    http = _FakeHttp(
        _all_healthy, page_responder=_breaks_after_the_build(runner, _placeholder_page)
    )

    assert _reveal(runner, http, _FakeSpawner(), repo) == 3

    # Named on stderr, and actually a bundle -- a path to an empty directory
    # would be worse than saying nothing. (Reaped by
    # ``reap_snapshots_left_for_the_operator``, since nothing else does now.)
    kept = _kept_snapshot_path(capsys.readouterr().err)
    assert (kept / "index.html").exists()


def test_a_rollback_whose_own_git_fails_is_an_emergency_that_keeps_the_snapshot(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rollback's git steps run with ``check=True``.

    An escape there would surface as exit 1 -- the code that means nothing was
    changed -- over a part-restored tree whose bundle the failed build already
    destroyed, and would discard the copy on the way out. Not recovering is what
    exit 3 is for, and the copy is what makes it survivable.
    """
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="type error"))
    runner.respond(
        ("git", "checkout"),
        subprocess.CalledProcessError(1, ["git", "checkout"], stderr="index.lock"),
    )

    assert _reveal(runner, _FakeHttp(_all_healthy), _FakeSpawner(), repo) == 3

    kept = _kept_snapshot_path(capsys.readouterr().err)
    assert (kept / "index.html").exists()


def test_a_backend_only_emergency_is_not_pointed_at_the_bundle_snapshot(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same exit 3, but nothing here ever wrote the bundle directory: the build is
    # the only step that empties it, and the rollback restores tracked files
    # while it is untracked output. So the copy is byte-identical to what is
    # already being served, and offering it would send someone whose UI is down
    # for a backend reason off to copy a bundle over itself.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n",
        repo_root=repo,
    )
    http = _FakeHttp(
        _all_healthy,
        page_responder=_breaks_after_the_restart(runner, _placeholder_page),
    )

    assert _reveal(runner, http, _FakeSpawner(), repo) == 3

    assert "bundle was kept" not in capsys.readouterr().err


def _breaks_after_the_restart(
    runner: _RecordingRunner, broken: Callable[[str], reveal_mod.FetchedPage]
) -> Callable[[str], reveal_mod.FetchedPage]:
    """The backend-change counterpart to :func:`_breaks_after_the_build`.

    A backend-only reveal never builds, so the frontend it regresses can only
    turn broken at the restart.
    """

    def responder(url: str) -> reveal_mod.FetchedPage:
        has_restarted = any(
            c[:3] == ["mngr", "start", "--restart"] for c in runner.calls
        )
        return broken(url) if has_restarted else _built_app_page(url)

    return responder


def _kept_snapshot_path(stderr: str) -> Path:
    """Where the emergency path told the operator the bundle copy is."""
    match = re.search(r"bundle was kept at (\S+) --", stderr)
    assert match is not None, stderr
    return Path(match.group(1))


def test_a_reveal_whose_asset_comes_back_as_html_is_rolled_back(repo: Path) -> None:
    # The blank-screen mode: the shell is the real app, but its module script
    # comes back as the SPA fallback HTML, which the browser refuses to execute.
    def html_for_the_asset(url: str) -> reveal_mod.FetchedPage:
        if url.endswith(".js"):
            return reveal_mod.FetchedPage(
                status=200,
                body="<!doctype html>",
                headers={"content-type": "text/html"},
            )
        return _built_app_page(url)

    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    http = _FakeHttp(
        _all_healthy, page_responder=_breaks_after_the_build(runner, html_for_the_asset)
    )

    code = _reveal(runner, http, _FakeSpawner(), repo)

    assert code == 3


def test_a_frontend_that_was_already_broken_is_reported_not_rolled_back(
    unbuilt_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Rolling an unrelated backend change back would not fix a frontend that was
    # already broken when the reveal started -- it would just lose the change.
    # The reveal is answerable for regressions only.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n",
        repo_root=unbuilt_repo,
    )
    http = _FakeHttp(_all_healthy, page_responder=_placeholder_page)

    code = _reveal(runner, http, _FakeSpawner(), unbuilt_repo)

    assert code == 0
    # The caller reports the closing line to the user, so it must not sign off
    # on a UI we have just established they cannot see -- the whole point of
    # probing the frontend was that the backend's own health check cannot.
    closing_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "confirmed healthy" not in closing_line
    assert "not serving a working frontend" in closing_line


def test_a_rollback_does_not_claim_health_over_an_already_broken_frontend(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same rule as the exit-0 case above, on the path that reaches it without
    # ever probing the frontend a second time. The rollback is not held to the
    # frontend standard when the workspace arrived without one, so the health it
    # confirms is the backend's -- and this reveal fails before _apply_reveal
    # gets as far as its own probe, so the closing line is the only thing the
    # caller ever sees about the UI.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="type error"))
    http = _FakeHttp(_all_healthy, page_responder=_placeholder_page)

    code = _reveal(runner, http, _FakeSpawner(), repo)

    assert code == 2
    closing_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "confirmed healthy" not in closing_line
    assert "cannot confirm it" in closing_line


def test_a_blip_on_the_pre_probe_does_not_disarm_the_regression_check(
    repo: Path,
) -> None:
    # The pre-reveal probe decides whether the reveal is answerable for the
    # frontend at all, and it is wrong in only one direction: a single
    # unanswered request would conclude "already broken" and silently downgrade
    # a rollback into a warning for the rest of the run. So a non-answer is
    # retried, and this reveal -- which really does break the frontend -- must
    # still be rolled back rather than reported.
    unanswered_calls = []

    def unreachable_once_then_healthy(url: str) -> reveal_mod.FetchedPage | None:
        if not unanswered_calls:
            unanswered_calls.append(url)
            return None
        return _built_app_page(url)

    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )

    def responder(url: str) -> reveal_mod.FetchedPage | None:
        has_built = any(c[:3] == ["npm", "run", "build"] for c in runner.calls)
        return (
            _placeholder_page(url) if has_built else unreachable_once_then_healthy(url)
        )

    http = _FakeHttp(_all_healthy, page_responder=responder)

    code = _reveal(runner, http, _FakeSpawner(), repo)

    # Rolled back (and escalated, because the fake keeps serving the
    # placeholder afterwards) rather than exiting 0 with a warning.
    assert code == 3
    assert unanswered_calls  # the blip really did happen


def test_a_blip_after_the_build_does_not_roll_back_a_reveal_that_landed(
    repo: Path,
) -> None:
    # The mirror of the case above, and the more expensive way to be wrong. On a
    # frontend-only reveal nothing else probes the live service -- the health
    # poll runs only when the backend was restarted -- so this one request is
    # the entire verdict. Unretried, a blip reverts a change that landed fine
    # and reports the reveal as failed.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n", repo_root=repo
    )
    unanswered_calls = []

    def responder(url: str) -> reveal_mod.FetchedPage | None:
        has_built = any(c[:3] == ["npm", "run", "build"] for c in runner.calls)
        if has_built and not unanswered_calls:
            unanswered_calls.append(url)
            return None
        return _built_app_page(url)

    http = _FakeHttp(_all_healthy, page_responder=responder)

    code = _reveal(runner, http, _FakeSpawner(), repo)

    assert code == 0
    assert unanswered_calls  # the blip really did happen
    assert not runner.ran("git", "commit")  # nothing was reverted


def test_the_frontend_probe_retries_a_non_answer_but_not_a_verdict() -> None:
    # The two halves of the same rule. A non-answer says nothing about the
    # frontend, so it is worth asking again; a verdict -- here the placeholder,
    # arriving as a perfectly healthy 200 -- is the service telling us the
    # frontend is broken, and asking again only spends the budget to reach the
    # same answer.
    answers = [None, None, _built_app_page("/")]
    http = _FakeHttp(
        _all_healthy,
        page_responder=lambda url: answers.pop(0) if answers else _built_app_page(url),
    )

    assert (
        reveal_mod.describe_frontend_failure(http, _LIVE_BASE, lambda _seconds: None)
        is None
    )
    assert not answers  # it kept asking until it got an answer

    verdict_http = _FakeHttp(_all_healthy, page_responder=_placeholder_page)

    assert (
        reveal_mod.describe_frontend_failure(
            verdict_http, _LIVE_BASE, lambda _seconds: None
        )
        is not None
    )
    assert len(verdict_http.page_urls) == 1


def test_a_service_that_never_answers_spends_the_budget_and_still_reports_a_failure() -> (
    None
):
    # Exhausting the retries has to leave a usable answer behind: the callers
    # after the reveal turn it into the rollback message, and a UI that will not
    # answer is one the user cannot see either.
    silent_http = _FakeHttp(_all_healthy, page_responder=lambda _url: None)

    failure = reveal_mod.describe_frontend_failure(
        silent_http, _LIVE_BASE, lambda _seconds: None
    )

    assert failure is not None
    assert len(silent_http.page_urls) == reveal_mod._FRONTEND_PROBE_ATTEMPTS


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
    # dropping MNGR_AGENT_ID, probe /api/agents, register the inner app + wrapper.
    assert _flag(argv, "--port-env") == reveal_mod.PREVIEW_PORT_ENV
    assert _flag(argv, "--host-env") == reveal_mod.PREVIEW_HOST_ENV
    assert _flag(argv, "--unset-env") == reveal_mod.ENV_MNGR_AGENT_ID
    assert _flag(argv, "--health-path") == reveal_mod.HEALTH_PATH
    assert _flag(argv, "--service-name") == reveal_mod.PREVIEW_INNER_SERVICE_NAME
    assert _flag(argv, "--preview-service-name") == reveal_mod.PREVIEW_SERVICE_NAME
    assert _flag(argv, "--preview-title") == _SLUG


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
