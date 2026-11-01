"""The X clipboard bridge over a fake ``xclip``: what is placed, what is read, and the route that places an image."""

import io
import json
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from screen.errors import ClipboardError
from screen.service import ScreenService
from screen.xclipboard import ClipboardContents
from screen.xclipboard import MAX_CLIPBOARD_BYTES
from screen.xclipboard import XClipboard
from screen.xclipboard import parse_targets
from testing import start_service_stack

_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _clipboard(fake_xclip: Path) -> XClipboard:
    return XClipboard(display=":4242", xclip_command=(str(fake_xclip),))


def test_setting_an_image_passes_the_target_and_the_bytes_to_xclip(fake_xclip: Path) -> None:
    _clipboard(fake_xclip).set_contents(_PNG_HEADER, "image/png")
    record = fake_xclip.parent / "xclip-record"
    assert (record / "set.target").read_text() == "image/png"
    assert (record / "set.bytes").read_bytes() == _PNG_HEADER


def test_setting_text_passes_no_target(fake_xclip: Path) -> None:
    _clipboard(fake_xclip).set_contents(b"hello", "text/plain")
    record = fake_xclip.parent / "xclip-record"
    assert (record / "set.target").read_text() == ""
    assert (record / "set.bytes").read_bytes() == b"hello"


def test_a_payload_over_the_cap_is_refused_before_xclip_runs(fake_xclip: Path) -> None:
    with pytest.raises(ClipboardError, match="cap"):
        _clipboard(fake_xclip).set_contents(b"x" * (MAX_CLIPBOARD_BYTES + 1), "image/png")
    assert not (fake_xclip.parent / "xclip-record" / "set.bytes").exists()


def test_a_failing_xclip_is_an_error(tmp_path: Path) -> None:
    script = tmp_path / "xclip-fails"
    script.write_text("#!/usr/bin/env bash\nexit 3\n")
    script.chmod(0o755)
    with pytest.raises(ClipboardError, match="exited 3"):
        XClipboard(display=":4242", xclip_command=(str(script),)).set_contents(b"x", "text/plain")


def test_contents_and_the_image_read_follow_the_targets(fake_xclip: Path) -> None:
    record = fake_xclip.parent / "xclip-record"
    clipboard = _clipboard(fake_xclip)
    assert clipboard.contents() == ClipboardContents(targets=())
    assert clipboard.read_image() is None
    (record / "out.TARGETS").write_text("TIMESTAMP\nTARGETS\nUTF8_STRING\ntext/html\n")
    assert clipboard.contents().has_image is False
    (record / "out.TARGETS").write_text("TIMESTAMP\nTARGETS\nimage/png\nimage/jpeg\n")
    (record / "out.image_png").write_bytes(_PNG_HEADER)
    assert clipboard.contents().image_target == "image/png"
    assert clipboard.read_image() == (_PNG_HEADER, "image/png")


def test_parse_targets_handles_an_unreadable_selection() -> None:
    assert parse_targets(None) == ClipboardContents(targets=())
    assert parse_targets(b"UTF8_STRING image/bmp").image_target == "image/bmp"


def _paste(client: FlaskClient, session_id: str, image: bytes, mime: str):
    return client.post(
        "/api/clipboard/image",
        data={"session_id": session_id, "image": (io.BytesIO(image), "clip", mime)},
        content_type="multipart/form-data",
    )


def _controlling_session(client: FlaskClient) -> str:
    session = client.post("/api/session").get_json()
    assert client.post("/api/control/take", json={"session_id": session["session_id"]}).status_code == 200
    return session["session_id"]


def test_the_clipboard_image_route_places_the_controllers_image_on_the_display(
    client: FlaskClient, service: ScreenService, fake_xclip: Path
) -> None:
    start_service_stack(service)
    answer = _paste(client, _controlling_session(client), _PNG_HEADER, "image/png")
    assert answer.status_code == 204, answer.get_json()
    record = fake_xclip.parent / "xclip-record"
    assert (record / "set.target").read_text() == "image/png"
    assert (record / "set.bytes").read_bytes() == _PNG_HEADER


def test_the_clipboard_image_route_validates_its_body(client: FlaskClient, service: ScreenService) -> None:
    assert _paste(client, "s1", _PNG_HEADER, "image/png").status_code == 409
    start_service_stack(service)
    session_id = _controlling_session(client)
    no_session = client.post(
        "/api/clipboard/image",
        data={"image": (io.BytesIO(_PNG_HEADER), "clip", "image/png")},
        content_type="multipart/form-data",
    )
    assert no_session.status_code == 400
    no_part = client.post("/api/clipboard/image", data={"session_id": session_id}, content_type="multipart/form-data")
    assert no_part.status_code == 400
    assert _paste(client, session_id, b"hello", "text/plain").status_code == 415
    assert _paste(client, session_id, b"", "image/png").status_code == 400
    too_large = _paste(client, session_id, b"x" * (MAX_CLIPBOARD_BYTES + 1), "image/png")
    assert too_large.status_code == 413
    assert too_large.get_json()["detail"] == f"the body is over the {MAX_CLIPBOARD_BYTES} byte cap"


def test_an_oversize_chunked_paste_is_refused_as_it_is_read(client: FlaskClient, service: ScreenService) -> None:
    """A chunked body names no Content-Length, so the cap holds on the stream itself: the parse stops at the cap
    instead of spooling the whole image to the tmpfs."""
    start_service_stack(service)
    session_id = _controlling_session(client)
    chunked = client.post(
        "/api/clipboard/image",
        data={"session_id": session_id, "image": (io.BytesIO(b"x" * (MAX_CLIPBOARD_BYTES + 1)), "clip", "image/png")},
        content_type="multipart/form-data",
        environ_overrides={"HTTP_TRANSFER_ENCODING": "chunked", "wsgi.input_terminated": True},
    )
    assert chunked.status_code == 413
    assert chunked.get_json()["detail"] == f"the body is over the {MAX_CLIPBOARD_BYTES} byte cap"


def test_an_image_on_the_remote_clipboard_is_announced_to_viewers(service: ScreenService) -> None:
    client_queue = service.broadcaster.register()
    service.on_clipboard_changed(ClipboardContents(targets=("UTF8_STRING",)))
    assert client_queue.empty()
    service.on_clipboard_changed(ClipboardContents(targets=("UTF8_STRING", "image/png")))
    announced = client_queue.get_nowait()
    assert announced is not None
    assert json.loads(announced) == {"type": "clipboard", "kind": "image"}
