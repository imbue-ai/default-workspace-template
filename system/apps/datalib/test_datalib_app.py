"""Integration: the launcher as a real process, registering through the real forward_port.py and
becoming a fake datalib-http."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

import pytest
from datalib_launcher_testing import (
    ENV_FAKE_DATALIB_HTTP_DIR,
    FAKE_DATALIB_HTTP_EXIT_STATUS,
    LAUNCHER_PATH,
    install_fake_datalib_http,
    launcher,
    read_fake_datalib_http_argv,
    read_fake_datalib_http_environment,
    read_fake_datalib_http_oom_score_adj,
)

# system/apps/datalib/test_datalib_app.py -> the repository root.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_PORT: Final[int] = 18731
_PUBLISHED_TOKEN: Final[str] = "published-token.0123_abc~"
# forward_port.py's override for where the registry lives.
_ENV_APPS_FILE: Final[str] = "MINDS_APPS_FILE"
_RUN_TIMEOUT_SECONDS: Final[float] = 60.0
# A host without it (macOS) cannot band a process, and the launcher only warns there.
_HAS_WRITABLE_PROC_OOM: Final[bool] = os.access("/proc/self/oom_score_adj", os.W_OK)


def _run_launcher(
    tmp_path: Path,
    datalib_http_path: Path,
    record_dir: Path,
    data_root: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(LAUNCHER_PATH),
            "--manifest",
            str(_REPO_ROOT / launcher.MANIFEST_PATH),
            "--port",
            str(_PORT),
            "--data-root",
            str(data_root if data_root is not None else tmp_path / "data-root"),
            "--datalib-http",
            str(datalib_http_path),
            "--state-directory",
            str(tmp_path / "state"),
            "--forward-port",
            str(_REPO_ROOT / launcher.FORWARD_PORT_PATH),
        ],
        env={
            **os.environ,
            _ENV_APPS_FILE: str(tmp_path / "apps.toml"),
            ENV_FAKE_DATALIB_HTTP_DIR: str(record_dir),
        },
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )


def _registered_rows(tmp_path: Path) -> list[dict[str, object]]:
    registry_path = tmp_path / "apps.toml"
    if not registry_path.exists():
        return []
    return tomllib.loads(registry_path.read_text()).get("apps", [])


def test_the_launcher_registers_the_token_on_the_launch_path_and_becomes_datalib_http(
    tmp_path: Path,
) -> None:
    executable, record_dir = install_fake_datalib_http(tmp_path / "fake")
    token_path = launcher.token_file_path(tmp_path / "data-root")
    token_path.parent.mkdir(parents=True)
    token_path.write_text(_PUBLISHED_TOKEN)

    result = _run_launcher(tmp_path, executable, record_dir)

    # The launcher became datalib-http, so its exit status is the server's.
    assert result.returncode == FAKE_DATALIB_HTTP_EXIT_STATUS, result.stderr
    assert read_fake_datalib_http_argv(record_dir) == [
        "--no-open",
        str(tmp_path / "data-root"),
    ]
    assert read_fake_datalib_http_environment(record_dir) == {
        "DATALIB_BIND": f"127.0.0.1:{_PORT}",
        "DATALIB_TOKEN": _PUBLISHED_TOKEN,
    }
    (row,) = _registered_rows(tmp_path)
    assert row["name"] == "datalib"
    assert row["url"] == f"http://localhost:{_PORT}"
    assert row["stop_when_no_windows"] is False
    assert row["default_shortcut"] == {"launch": "ui", "mode": "focus"}
    # The window opens at the launch path with its presets as the query: /?token=<token>.
    assert row["launch_paths"] == [
        {
            "id": "ui",
            "label": "Datalib",
            "path": "/",
            "presets": {"token": _PUBLISHED_TOKEN},
        }
    ]


def test_the_launcher_mints_a_token_when_none_is_published(tmp_path: Path) -> None:
    executable, record_dir = install_fake_datalib_http(tmp_path / "fake")

    result = _run_launcher(tmp_path, executable, record_dir)

    assert result.returncode == FAKE_DATALIB_HTTP_EXIT_STATUS, result.stderr
    environment = read_fake_datalib_http_environment(record_dir)
    assert environment is not None
    minted = environment["DATALIB_TOKEN"]
    assert len(minted) == 64
    (row,) = _registered_rows(tmp_path)
    assert row["launch_paths"] == [
        {"id": "ui", "label": "Datalib", "path": "/", "presets": {"token": minted}}
    ]


def test_the_launcher_moves_a_store_at_the_libraries_folder_into_the_library_it_serves(
    tmp_path: Path,
) -> None:
    """A workspace that used datalib before the libraries folder keeps its store, its token, and its sign-in.

    Run from a temporary workspace root on the launcher's real, relative data root: only a store at the real
    libraries folder is moved.
    """
    executable, record_dir = install_fake_datalib_http(tmp_path / "fake")
    workspace = tmp_path / "workspace"
    libraries_dir = workspace / launcher.LIBRARIES_DIR
    token_path = launcher.token_file_path(libraries_dir)
    token_path.parent.mkdir(parents=True)
    token_path.write_text(_PUBLISHED_TOKEN)
    (libraries_dir / "config.toml").write_text('[[groups]]\ntype = "slack"\n')

    result = _run_launcher(
        tmp_path, executable, record_dir, data_root=launcher.DATA_ROOT, cwd=workspace
    )

    assert result.returncode == FAKE_DATALIB_HTTP_EXIT_STATUS, result.stderr
    assert read_fake_datalib_http_argv(record_dir) == [
        "--no-open",
        str(launcher.DATA_ROOT),
    ]
    environment = read_fake_datalib_http_environment(record_dir)
    assert environment is not None
    assert environment["DATALIB_TOKEN"] == _PUBLISHED_TOKEN
    data_root = workspace / launcher.DATA_ROOT
    assert launcher.token_file_path(data_root).read_text() == _PUBLISHED_TOKEN
    assert (data_root / "config.toml").exists()
    assert sorted(path.name for path in libraries_dir.iterdir()) == ["Default"]
    assert sorted(path.name for path in libraries_dir.parent.iterdir()) == ["datalib"]


def test_the_launcher_exits_without_registering_when_the_binary_is_missing(
    tmp_path: Path,
) -> None:
    _, record_dir = install_fake_datalib_http(tmp_path / "fake")

    result = _run_launcher(
        tmp_path, tmp_path / "not-installed" / "datalib-http", record_dir
    )

    assert result.returncode == launcher.EXIT_NOT_READY
    assert "is not installed yet" in result.stderr
    assert _registered_rows(tmp_path) == []
    assert read_fake_datalib_http_argv(record_dir) is None


@pytest.mark.skipif(
    not _HAS_WRITABLE_PROC_OOM,
    reason="no writable /proc/self/oom_score_adj on this host",
)
def test_datalib_http_runs_in_the_agent_subprocess_band(tmp_path: Path) -> None:
    """Guards the dark launch: datalib, and every sync step it starts, is shed before any agent or service."""
    executable, record_dir = install_fake_datalib_http(tmp_path / "fake")

    result = _run_launcher(tmp_path, executable, record_dir)

    assert result.returncode == FAKE_DATALIB_HTTP_EXIT_STATUS, result.stderr
    assert (
        read_fake_datalib_http_oom_score_adj(record_dir)
        == launcher.bands.AGENT_SUBPROCESS
        == 900
    )
