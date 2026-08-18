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
import os
import sys
import time
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
    envs: list[dict | None] = field(default_factory=list)
    # What each executable resolves to on PATH; absent means "not installed",
    # which is also the default so tests never reach the real machine's PATH.
    executables: dict[str, str] = field(default_factory=dict)
    _responses: dict[tuple[str, ...], object] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: object) -> None:
        self._responses[prefix] = result

    def which(self, executable: str) -> str | None:
        return self.executables.get(executable)

    def run(self, argv: Sequence[str], **kwargs) -> _Result:
        argv_list = list(argv)
        self.calls.append(argv_list)
        self.envs.append(kwargs.get("env"))
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
    output_paths: list[Path] = field(default_factory=list)
    last: _FakeSpawned | None = None

    def spawn(
        self, argv: Sequence[str], cwd: str, env: dict, output_path: Path
    ) -> _FakeSpawned:
        self.spawns.append(list(argv))
        self.output_paths.append(output_path)
        self.last = _FakeSpawned(output=self.output, exited=self.exited)
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


def test_classify_treats_a_vendored_manifest_as_a_backend_manifest() -> None:
    # The change that actually ships in a release. Every "system/vendor/mngr:
    # refresh" commit in this repo's history leaves uv.lock untouched, so keying
    # the dependency refresh only off the lock would never fire on the merge that
    # moves the vendored mngr -- which is exactly the one that stales the editable
    # tool's dependency closure and breaks the mngr CLI.
    changes = reveal_mod.classify_changes(["system/vendor/mngr/libs/mngr/pyproject.toml"])
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
    assert not runner.ran("mngr", "start")  # frontend change never restarts the backend
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


def _with_receipt(runner: _RecordingRunner, tool_dir: Path, tool: str, body: str) -> None:
    """Point ``uv tool dir`` at ``tool_dir`` and give ``tool`` a receipt there."""
    runner.respond(("uv", "tool", "dir"), _Result(stdout=f"{tool_dir}\n"))
    (tool_dir / tool).mkdir(parents=True, exist_ok=True)
    (tool_dir / tool / "uv-receipt.toml").write_text(body)


def test_dependency_refresh_preserves_a_tools_registered_plugins(tmp_path: Path) -> None:
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


def test_dependency_refresh_repins_the_base_to_the_in_tree_source(tmp_path: Path) -> None:
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
def test_tool_location_declines_what_it_cannot_read(contents: str, tmp_path: Path) -> None:
    # Anything we cannot interpret means we do not know which installation this
    # is, and the caller falls back to letting uv decide -- guessing a directory
    # would be worse than uv's own default.
    script = tmp_path / "mngr"
    script.write_text(contents)

    assert reveal_mod._tool_location(script, "imbue-mngr") is None


def test_tool_location_declines_the_workspace_venvs_console_script(tmp_path: Path) -> None:
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


def test_failed_preflight_reports_why_the_backend_did_not_boot(capsys) -> None:
    # The whole point of the pre-flight is that the merged code never reaches the
    # live service -- so its output is the only evidence of *why* it was rejected.
    # Without it a reveal exit 2 is indistinguishable from a slow boot, and whoever
    # picks it up diagnoses a cause they cannot see.
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )
    spawner = _FakeSpawner(
        output="Traceback (most recent call last):\nModuleNotFoundError: No module named 'frontmatter'\n"
    )

    code = _reveal(runner, _FakeHttp(lambda url: 200 if _is_live(url) else None), spawner)

    assert code == 2
    reported = capsys.readouterr().err
    assert "ModuleNotFoundError: No module named 'frontmatter'" in reported
    # The rollback commit carries it too, so the reason survives in git history
    # after the terminal that ran the reveal is gone.
    commits = runner.argvs_starting("git", "commit")
    assert commits and any("frontmatter" in arg for arg in commits[0])


def test_failed_preflight_that_produced_no_output_says_so(capsys) -> None:
    # A silent failure is itself a finding (the tool never got far enough to log),
    # and must not read as "the output was dropped again".
    runner = _runner_with_diff(
        "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
    )

    _reveal(runner, _FakeHttp(lambda url: 200 if _is_live(url) else None), _FakeSpawner())

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
