"""Tests for ``mock_frame.py``.

The app's registry row is written by the real ``forward_port`` into a sandboxed
registry, since what the frame does to that row (hidden, kept running, everything
else untouched, and put back afterwards) is the behavior that matters. The frame
substrate and ``workspace-layout`` are stood in for by recorders, so no server is
booted and nothing reaches a shell.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from workspace_bare_scripts import forward_port

from build_app_skill import mock_frame, scaffold_flask_lib

_APP = "inbox-status"
_URL = "http://localhost:8081"
_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M2 2h12v12H2z"/></svg>'
_NOTE = "Showing 20 of your emails as a sample; refresh does not work yet."


@dataclass(frozen=True)
class _FrameCall:
    app: mock_frame.FramedApp
    note: str
    inner_path: str


@dataclass
class _RecordingFrames(mock_frame.Frames):
    up_code: int = 0
    down_code: int = 0
    ups: list[_FrameCall] = field(default_factory=list)
    downs: list[str] = field(default_factory=list)
    # The app's registry row as the frame saw it when it came up.
    row_at_up: dict[str, object] | None = None
    registry: Path | None = None

    def up(
        self, app: mock_frame.FramedApp, note: str, inner_path: str, repo_root: Path
    ) -> int:
        self.ups.append(_FrameCall(app, note, inner_path))
        if self.registry is not None:
            self.row_at_up = _row(self.registry)
        return self.up_code

    def down(self, app_name: str, repo_root: Path) -> int:
        self.downs.append(app_name)
        return self.down_code


@dataclass
class _RecordingLayout(mock_frame.Layout):
    repo_root: Path = Path(".")
    codes_by_verb: dict[str, int] = field(default_factory=dict)
    ops: list[list[str]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> int:
        self.ops.append(list(argv))
        return self.codes_by_verb.get(argv[0], 0)


def _registered_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scaffolded app registered from its manifest, as its program does on start; the registry's path."""
    registry = tmp_path / "data" / ".state" / "apps.toml"
    monkeypatch.setenv(forward_port.ENV_APPS_FILE, str(registry))
    lib_dir = scaffold_flask_lib._write_lib(
        tmp_path, _APP, "inbox status dashboard", "Inbox status", 8081, [], _ICON
    )
    forward_port.register_manifest(registry, lib_dir / "app.toml", _URL)
    return registry


def _row(registry: Path) -> dict[str, object]:
    rows = tomllib.loads(registry.read_text())["apps"]
    return next(row for row in rows if row["name"] == _APP)


def test_up_frames_the_running_app_hidden_and_kept_alive_and_opens_the_mock_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    frames = _RecordingFrames(registry=registry)
    layout = _RecordingLayout()

    code = mock_frame.up(
        _APP,
        _NOTE,
        tmp_path,
        inner_path="/inbox",
        surface="open",
        frames=frames,
        layout=layout,
    )

    assert code == 0
    assert frames.ups == [
        _FrameCall(
            mock_frame.FramedApp(
                name=_APP,
                manifest_path=tmp_path
                / "system"
                / "apps"
                / "inbox_status"
                / "app.toml",
                display_name="Inbox status",
                url=_URL,
            ),
            _NOTE,
            "/inbox",
        )
    ]
    # The bare app was already out of the launcher and exempt from the no-window stop by
    # the time the frame came up, and nothing else on its row moved.
    assert frames.row_at_up is not None
    assert frames.row_at_up["internal"] is True
    assert frames.row_at_up["stop_when_no_windows"] is False
    assert {
        key: value for key, value in frames.row_at_up.items() if key != "internal"
    } == {
        **plain_row,
        "stop_when_no_windows": False,
    }
    assert layout.ops == [["open", "inbox-status-mock", "--beside"]]


def test_up_with_refresh_reloads_the_mock_window_rather_than_opening_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registered_app(tmp_path, monkeypatch)
    layout = _RecordingLayout()

    code = mock_frame.up(
        _APP,
        _NOTE,
        tmp_path,
        surface="refresh",
        frames=_RecordingFrames(),
        layout=layout,
    )

    assert code == 0
    assert layout.ops == [["refresh", "--app", "inbox-status-mock"]]


def test_a_frame_that_does_not_come_up_leaves_the_app_an_ordinary_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    layout = _RecordingLayout()

    code = mock_frame.up(
        _APP,
        _NOTE,
        tmp_path,
        surface="open",
        frames=_RecordingFrames(up_code=1),
        layout=layout,
    )

    assert code == 1
    assert _row(registry) == plain_row
    assert layout.ops == []


def test_up_refuses_an_app_that_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(forward_port.ENV_APPS_FILE, str(tmp_path / "apps.toml"))
    scaffold_flask_lib._write_lib(
        tmp_path, _APP, "inbox status dashboard", "Inbox status", 8081, [], _ICON
    )
    frames = _RecordingFrames()

    with pytest.raises(mock_frame.MockFrameError, match="not registered"):
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())

    assert frames.ups == []


def test_up_refuses_a_blank_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    frames = _RecordingFrames()

    code = mock_frame.up(_APP, "  ", tmp_path, frames=frames, layout=_RecordingLayout())

    assert code == 1
    assert frames.ups == []
    assert _row(registry) == plain_row


def test_down_closes_the_mock_window_removes_the_frame_and_restores_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    frames = _RecordingFrames()
    layout = _RecordingLayout()
    assert mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=layout) == 0

    code = mock_frame.down(_APP, tmp_path, frames=frames, layout=layout)

    assert code == 0
    assert layout.ops == [["close", "inbox-status-mock"]]
    assert frames.downs == [_APP]
    assert _row(registry) == plain_row


def test_down_restores_the_app_even_when_no_mock_window_is_open_or_the_frame_will_not_die(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    frames = _RecordingFrames(down_code=1)
    assert (
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())
        == 0
    )

    code = mock_frame.down(
        _APP,
        tmp_path,
        frames=frames,
        layout=_RecordingLayout(codes_by_verb={"close": 1}),
    )

    assert code == 1
    assert _row(registry) == plain_row


def test_the_mock_windows_name_stays_within_the_registry_cap() -> None:
    long_name = "x" * forward_port.MAX_DISPLAY_NAME_LENGTH

    display_name = mock_frame.frame_display_name(long_name)

    assert len(display_name) == forward_port.MAX_DISPLAY_NAME_LENGTH
    assert forward_port.validate_display_name(display_name) is None
    assert display_name.endswith(mock_frame.DISPLAY_NAME_SUFFIX)
