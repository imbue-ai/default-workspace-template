"""Draw icon candidates with Retro Diffusion (https://retrodiffusion.ai), a pixel-art image model.

Run from the repo root, with the user's key in the environment (never on the command line):

    python3 system/scripts/with_secrets.py data/.secrets/retrodiffusion.env -- \\
        uv run python .agents/skills/make-theme-icon/providers/retro_diffusion.py \\
        --prompt "..." --size 32 --palette "#000000,#ffffff" --variants 2 --out-dir /tmp/icon-candidates

Each image costs the user Retro Diffusion credits, so draw only the icons that are missing. The candidates still
go through ``workspace-themes icon fit``; the palette here only steers the model.
"""

import argparse
import base64
import io
import os
import re
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import httpx
from PIL import Image

API_BASE: Final[str] = "https://api.retrodiffusion.ai/v2"
API_KEY_VARIABLE: Final[str] = "RETRODIFFUSION_API_KEY"
DEFAULT_STYLE: Final[str] = "rd_pro__simple"
# The model draws between these sizes; a theme's icon outside them is drawn at the nearest and fitted after.
MIN_SIZE_PX: Final[int] = 16
MAX_SIZE_PX: Final[int] = 256
POLL_INTERVAL_SECONDS: Final[float] = 2.0
POLL_TIMEOUT_SECONDS: Final[float] = 180.0
_RUNNING_STATUSES: Final[frozenset[str]] = frozenset({"pending", "running", "queued"})
_HEX_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{6}")


class RetroDiffusionError(Exception):
    """The API refused a request, or a drawing did not finish."""


def parse_palette(text: str) -> list[str]:
    """The comma-separated ``#rrggbb`` colors of ``--palette``; argparse reports any other entry."""
    colors = [color.strip() for color in text.split(",") if color.strip()]
    for color in colors:
        if _HEX_COLOR_PATTERN.fullmatch(color) is None:
            raise argparse.ArgumentTypeError(
                f"palette color {color!r} must be written #rrggbb"
            )
    return colors


def palette_image_base64(colors: Sequence[str]) -> str:
    """A one-row PNG holding each color once, the form the API takes a palette in."""
    image = Image.new("RGB", (len(colors), 1))
    for index, color in enumerate(colors):
        hex_digits = color.removeprefix("#")
        image.putpixel(
            (index, 0),
            tuple(int(hex_digits[offset : offset + 2], 16) for offset in (0, 2, 4)),
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def build_payload(
    prompt: str, size: int, palette: Sequence[str], variants: int, style: str
) -> dict[str, Any]:
    drawn_size = min(max(size, MIN_SIZE_PX), MAX_SIZE_PX)
    payload: dict[str, Any] = {
        "prompt": prompt,
        "prompt_style": style,
        "width": drawn_size,
        "height": drawn_size,
        "num_images": variants,
        "remove_bg": True,
    }
    if palette:
        payload["input_palette"] = palette_image_base64(palette)
    return payload


def write_candidates(base64_images: Sequence[str], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, encoded in enumerate(base64_images):
        path = out_dir / f"candidate-{index}.png"
        path.write_bytes(base64.b64decode(encoded))
        paths.append(path)
    return paths


def _call(
    client: httpx.Client,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = client.request(method, f"{API_BASE}{path}", json=body, headers=headers)
    if response.status_code >= 400:
        raise RetroDiffusionError(
            f"{method} {path} failed with HTTP {response.status_code}: {response.text}"
        )
    return response.json()


def draw(
    client: httpx.Client, payload: dict[str, Any], poll_interval_seconds: float
) -> dict[str, Any]:
    # One key for the drawing, so the API can tell a resent request from a new (billed) one.
    task_id = _call(
        client,
        "POST",
        "/inferences",
        payload,
        {"Idempotency-Key": str(uuid.uuid4())},
    )["task_id"]
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        task = _call(client, "GET", f"/inferences/tasks/{task_id}", None, {})
        status = task.get("status")
        if status == "succeeded":
            return task["result"]
        if status not in _RUNNING_STATUSES:
            raise RetroDiffusionError(f"drawing {task_id} ended as {status}: {task}")
        time.sleep(poll_interval_seconds)
    raise RetroDiffusionError(
        f"drawing {task_id} did not finish within {POLL_TIMEOUT_SECONDS:.0f}s"
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prompt", required=True)
    parser.add_argument(
        "--size", type=int, required=True, help="The theme's icon size, in pixels"
    )
    parser.add_argument(
        "--palette",
        type=parse_palette,
        default=[],
        help="Comma-separated #rrggbb colors the drawing keeps to",
    )
    parser.add_argument("--variants", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--style", default=DEFAULT_STYLE)
    parser.add_argument("--out-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    api_key = os.environ.get(API_KEY_VARIABLE)
    if not api_key:
        print(
            f"{API_KEY_VARIABLE} is not set (no data/.secrets/retrodiffusion.env): draw the icon another way.",
            file=sys.stderr,
        )
        return 2
    payload = build_payload(
        arguments.prompt,
        arguments.size,
        arguments.palette,
        arguments.variants,
        arguments.style,
    )
    with httpx.Client(headers={"X-RD-Token": api_key}, timeout=60.0) as client:
        result = draw(client, payload, POLL_INTERVAL_SECONDS)
    for path in write_candidates(result["base64_images"], arguments.out_dir):
        print(path)
    print(
        f"cost ${result.get('balance_cost')}, balance ${result.get('remaining_balance')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
