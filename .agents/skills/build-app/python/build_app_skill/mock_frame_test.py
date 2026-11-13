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
from app_registration import forward_port

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
    # The frame's record, kept as the real substrate keeps it: written by a successful
    # boot or a failed re-frame, never by a failed first frame.
    current: mock_frame.RecordedFrame | None = None

    def up(
        self, app: mock_frame.FramedApp, note: str, inner_path: str, repo_root: Path
    ) -> int:
        self.ups.append(_FrameCall(app, note, inner_path))
        if self.registry is not None:
            self.row_at_up = _row(self.registry)
        if self.up_code == 0 or self.current is not None:
            self.current = mock_frame.RecordedFrame(note=note, inner_path=inner_path)
        return self.up_code

    def recorded(
        self, app_name: str, repo_root: Path
    ) -> mock_frame.RecordedFrame | None:
        return self.current

    def down(self, app_name: str, repo_root: Path) -> int:
        self.downs.append(app_name)
        self.current = None
        return self.down_code


@dataclass
class _RecordingLayout(mock_frame.Layout):
    repo_root: Path = Path(".")
    codes_by_verb: dict[str, int] = field(default_factory=dict)
    ops: list[list[str]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> int:
        self.ops.append(list(argv))
        return self.codes_by_verb.get(argv[0], 0)


def _scaffolded_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A scaffolded app that has not registered yet, and a sandboxed registry; the registry's path and the app's lib dir."""
    registry = tmp_path / "data" / ".state" / "apps.toml"
    monkeypatch.setenv(forward_port.ENV_APPS_FILE, str(registry))
    lib_dir = scaffold_flask_lib._write_lib(
        tmp_path, _APP, "inbox status dashboard", "Inbox status", 8081, [], _ICON
    )
    return registry, lib_dir


def _registered_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scaffolded app registered from its manifest, as its program does on start; the registry's path."""
    registry, lib_dir = _scaffolded_app(tmp_path, monkeypatch)
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
    assert layout.ops == [["open", "inbox-status-sketch", "--beside"]]


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
    assert layout.ops == [["refresh", "--app", "inbox-status-sketch"]]


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
    _scaffolded_app(tmp_path, monkeypatch)
    frames = _RecordingFrames()

    with pytest.raises(mock_frame.MockFrameError, match="not registered"):
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())

    assert frames.ups == []


@pytest.mark.parametrize(
    ("note", "inner_path"),
    [("  ", "/"), (_NOTE, "inbox")],
    ids=["blank-note", "bad-path"],
)
def test_a_refused_rerun_leaves_the_frame_from_the_earlier_run_in_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, note: str, inner_path: str
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    frames = _RecordingFrames()
    assert (
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())
        == 0
    )
    framed_row = _row(registry)

    code = mock_frame.up(
        _APP,
        note,
        tmp_path,
        inner_path=inner_path,
        frames=frames,
        layout=_RecordingLayout(),
    )

    assert code == 1
    assert len(frames.ups) == 1
    # Still out of the launcher and kept running: the earlier frame's window shows it.
    assert _row(registry) == framed_row


def test_refresh_after_a_restart_reframes_with_the_recorded_note_and_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that never wrote the note, or the agent after a ``verify_app --restart``
    (whose registration un-hid the app), reloads the mock with the app's name alone."""
    registry = _registered_app(tmp_path, monkeypatch)
    frames = _RecordingFrames(registry=registry)
    assert (
        mock_frame.up(
            _APP,
            _NOTE,
            tmp_path,
            inner_path="/inbox",
            surface="open",
            frames=frames,
            layout=_RecordingLayout(),
        )
        == 0
    )
    framed_row = _row(registry)
    forward_port.register_manifest(
        registry, tmp_path / "system" / "apps" / "inbox_status" / "app.toml", _URL
    )
    layout = _RecordingLayout()

    code = mock_frame.refresh(_APP, tmp_path, frames=frames, layout=layout)

    assert code == 0
    assert [(call.note, call.inner_path) for call in frames.ups] == [
        (_NOTE, "/inbox"),
        (_NOTE, "/inbox"),
    ]
    assert _row(registry) == framed_row
    assert layout.ops == [["refresh", "--app", "inbox-status-sketch"]]


def test_refresh_with_no_frame_up_reloads_the_apps_own_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    frames = _RecordingFrames()
    layout = _RecordingLayout()

    code = mock_frame.refresh(_APP, tmp_path, frames=frames, layout=layout)

    assert code == 0
    assert frames.ups == []
    assert layout.ops == [["refresh", "--app", _APP]]
    assert _row(registry) == plain_row


def test_up_without_a_note_keeps_the_frames_note_and_a_first_frame_needs_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registered_app(tmp_path, monkeypatch)
    frames = _RecordingFrames()

    assert (
        mock_frame.up(_APP, None, tmp_path, frames=frames, layout=_RecordingLayout())
        == 1
    )
    assert frames.ups == []
    assert (
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())
        == 0
    )
    assert (
        mock_frame.up(
            _APP,
            None,
            tmp_path,
            inner_path="/archive",
            frames=frames,
            layout=_RecordingLayout(),
        )
        == 0
    )

    assert [(call.note, call.inner_path) for call in frames.ups] == [
        (_NOTE, "/"),
        (_NOTE, "/archive"),
    ]


def test_down_removes_the_frame_and_restores_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    frames = _RecordingFrames()
    assert (
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())
        == 0
    )

    code = mock_frame.down(_APP, tmp_path, frames=frames)

    assert code == 0
    assert frames.downs == [_APP]
    assert _row(registry) == plain_row


def test_down_restores_the_app_even_when_the_frame_will_not_come_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    plain_row = _row(registry)
    frames = _RecordingFrames(down_code=1)
    assert (
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())
        == 0
    )

    code = mock_frame.down(_APP, tmp_path, frames=frames)

    assert code == 1
    assert _row(registry) == plain_row


def test_down_keeps_the_app_hidden_while_a_refused_frame_is_still_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teardown refused under a window it could not close leaves the frame showing the app."""
    registry = _registered_app(tmp_path, monkeypatch)
    frames = _RecordingFrames(down_code=1)
    assert (
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())
        == 0
    )
    framed_row = _row(registry)
    forward_port.register(
        registry,
        mock_frame.frame_name(_APP),
        "http://127.0.0.1:9",
        is_icon_declined=True,
    )

    code = mock_frame.down(_APP, tmp_path, frames=frames)

    assert code == 1
    assert _row(registry) == framed_row
    assert framed_row["internal"] is True


def test_down_takes_the_frame_down_for_an_app_removed_while_still_a_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registered_app(tmp_path, monkeypatch)
    frames = _RecordingFrames()
    assert (
        mock_frame.up(_APP, _NOTE, tmp_path, frames=frames, layout=_RecordingLayout())
        == 0
    )
    forward_port.remove(registry, _APP)

    code = mock_frame.down(_APP, tmp_path, frames=frames)

    assert code == 0
    assert frames.downs == [_APP]
    rows = tomllib.loads(registry.read_text()).get("apps", [])
    assert [row["name"] for row in rows] == []
    assert "is not registered" in capsys.readouterr().err


def test_the_frames_names_stay_within_the_registry_caps() -> None:
    long_name = "x" * forward_port.MAX_DISPLAY_NAME_LENGTH
    # The longest app name there can be, cut where the suffix would follow a hyphen.
    long_app_name = "monthly-engineering-sync-reviews"
    assert len(long_app_name) == forward_port.MAX_SERVICE_NAME_LENGTH

    display_name = mock_frame.frame_display_name(long_name)
    service_name = mock_frame.frame_name(long_app_name)

    assert len(display_name) == forward_port.MAX_DISPLAY_NAME_LENGTH
    assert forward_port.validate_display_name(display_name) is None
    assert display_name.endswith(mock_frame.DISPLAY_NAME_SUFFIX)
    assert forward_port.validate_service_name(service_name) is None
    assert service_name == "monthly-engineering-sync-sketch"
    assert mock_frame.frame_name(_APP) == "inbox-status-sketch"
