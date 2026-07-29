from pathlib import Path

from browser import audio


class _Process:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


def test_capture_requires_visible_viewer_and_running_sink(monkeypatch) -> None:
    browser_audio = audio.BrowserAudio("quiet-fox", 0, Path("cert"))
    browser_audio._viewers = 1
    active = True
    spawned: list[tuple[list[str], _Process]] = []

    def popen(command, **_kwargs):
        process = _Process()
        spawned.append((command, process))
        return process

    monkeypatch.setattr(browser_audio, "_sink_active", lambda: active)
    monkeypatch.setattr(audio.subprocess, "Popen", popen)

    browser_audio._reconcile_capture()
    assert spawned[0][0][0] == "ffmpeg"

    active = False
    browser_audio._reconcile_capture()
    assert spawned[0][1].terminated
    assert browser_audio._ffmpeg is None
