"""A fake capture backend for the video pipe tests: records what the pipe does to each capture."""

from collections.abc import Callable
from typing import Any

from browser.videopipe import PixelfluxCaptureBackend


class FakeCaptureSettings:
    """The attribute bag the pipe fills in; the fake capture keeps it for the tests to read."""

    def __init__(self) -> None:
        self.capture_width = 0
        self.capture_height = 0
        self.target_fps = 0.0
        self.output_mode = 0
        self.use_cpu = False
        self.video_crf = 0
        self.use_paint_over_quality = False
        self.video_paintover_crf = 0
        self.paint_over_trigger_frames = 0


class FakeCapture:
    """One capture the pipe started: its settings, region changes, and whether it was stopped."""

    def __init__(self) -> None:
        self.settings: FakeCaptureSettings | None = None
        self.frame_callback: Callable[..., None] | None = None
        self.cursor_callback: Callable[..., None] | None = None
        self.regions: list[tuple[int, int, int, int]] = []
        self.idr_requests = 0
        self.framerates: list[int] = []
        self.is_stopped = False

    def start_capture(self, callback: Callable[..., None], settings: FakeCaptureSettings) -> None:
        self.frame_callback = callback
        self.settings = settings

    def stop_capture(self) -> None:
        self.is_stopped = True

    def set_cursor_callback(self, callback: Callable[..., None]) -> None:
        self.cursor_callback = callback

    def update_capture_region(self, x: int, y: int, width: int, height: int) -> None:
        self.regions.append((x, y, width, height))

    def request_idr_frame(self) -> None:
        self.idr_requests += 1

    def update_framerate(self, fps: int) -> None:
        self.framerates.append(fps)


class FakeCaptureBackend(PixelfluxCaptureBackend):
    """A backend whose captures are fakes on a display of a fixed size, keeping every capture it minted."""

    def __init__(self, width: int = 1920, height: int = 1080) -> None:
        self.geometry = (width, height)
        self.captures: list[FakeCapture] = []

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def display_geometry(self, display: str) -> tuple[int, int]:
        return self.geometry

    def new_settings(self) -> Any:
        return FakeCaptureSettings()

    def new_capture(self) -> Any:
        capture = FakeCapture()
        self.captures.append(capture)
        return capture
