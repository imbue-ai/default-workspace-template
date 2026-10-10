import argparse
import base64
import io
from pathlib import Path

import httpx
import pytest
import retro_diffusion
from PIL import Image


def test_the_palette_reaches_the_api_as_a_one_row_png_of_its_colors() -> None:
    encoded = retro_diffusion.palette_image_base64(["#000000", "#ffffff", "#3a6ea5"])

    image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
    assert image.size == (3, 1)
    assert [image.getpixel((x, 0)) for x in range(3)] == [
        (0, 0, 0),
        (255, 255, 255),
        (58, 110, 165),
    ]


def test_a_palette_is_read_as_rrggbb_colors_and_anything_else_is_refused() -> None:
    assert retro_diffusion.parse_palette(" #000000, #3A6EA5 ,") == [
        "#000000",
        "#3A6EA5",
    ]
    with pytest.raises(
        argparse.ArgumentTypeError, match="'#fff' must be written #rrggbb"
    ):
        retro_diffusion.parse_palette("#000000,#fff")


def test_a_size_the_model_cannot_draw_is_drawn_at_the_nearest_it_can() -> None:
    assert (
        retro_diffusion.build_payload("a folder", 512, [], 1, "s")["width"]
        == retro_diffusion.MAX_SIZE_PX
    )
    assert (
        retro_diffusion.build_payload("a folder", 8, [], 1, "s")["height"]
        == retro_diffusion.MIN_SIZE_PX
    )
    assert retro_diffusion.build_payload("a folder", 32, [], 1, "s")["width"] == 32


def test_no_palette_leaves_the_model_its_own_colors() -> None:
    assert "input_palette" not in retro_diffusion.build_payload(
        "a folder", 32, [], 2, "s"
    )
    assert "input_palette" in retro_diffusion.build_payload(
        "a folder", 32, ["#000000"], 2, "s"
    )


def _api(responses: list[dict[str, object]]) -> httpx.Client:
    calls = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(calls))

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_a_drawing_is_polled_until_it_succeeds() -> None:
    client = _api(
        [
            {"task_id": "t1"},
            {"status": "running"},
            {"status": "succeeded", "result": {"base64_images": ["aGk="]}},
        ]
    )

    assert retro_diffusion.draw(client, {}, 0.0)["base64_images"] == ["aGk="]


def test_a_drawing_that_fails_says_so() -> None:
    client = _api([{"task_id": "t1"}, {"status": "failed"}])

    with pytest.raises(retro_diffusion.RetroDiffusionError, match="ended as failed"):
        retro_diffusion.draw(client, {}, 0.0)


def test_each_candidate_is_written_as_its_own_file(tmp_path: Path) -> None:
    paths = retro_diffusion.write_candidates(["aGk=", "aG8="], tmp_path / "out")

    assert [path.read_bytes() for path in paths] == [b"hi", b"ho"]


def test_without_a_key_it_stops_before_calling_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(retro_diffusion.API_KEY_VARIABLE, raising=False)

    assert (
        retro_diffusion.main(
            ["--prompt", "a folder", "--size", "32", "--out-dir", str(tmp_path)]
        )
        == 2
    )
    assert list(tmp_path.iterdir()) == []


def test_only_the_drawing_request_carries_an_idempotency_key() -> None:
    keys: list[str | None] = []
    responses = iter(
        [
            {"task_id": "t1"},
            {"status": "running"},
            {"status": "succeeded", "result": {"base64_images": []}},
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(200, json=next(responses))

    client = httpx.Client(transport=httpx.MockTransport(handle))
    retro_diffusion.draw(client, {}, 0.0)

    assert keys[0] is not None
    assert keys[1:] == [None, None]
