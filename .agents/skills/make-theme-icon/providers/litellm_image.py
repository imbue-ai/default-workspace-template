"""Draw icon candidates with any image model litellm reaches (OpenAI, Azure, Vertex, Bedrock, and others).

Run from the repo root, with the provider's key in the environment litellm reads for it
(``OPENAI_API_KEY``, ``VERTEXAI_PROJECT``, ...), loaded from ``data/.secrets/`` rather than typed out:

    python3 system/scripts/with_secrets.py data/.secrets/openai.env -- \\
        uv run python .agents/skills/make-theme-icon/providers/litellm_image.py \\
        --model gpt-image-1 --prompt "..." --variants 2 --out-dir /tmp/icon-candidates

General image models draw large and smooth; ask for a flat, plain background and few colors, and let
``workspace-themes icon fit`` take it down to the theme's size and palette.
"""

import argparse
import base64
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import httpx
import litellm

# Image models answer in tens of seconds; past this the provider is not coming back.
GENERATION_TIMEOUT_SECONDS: Final[float] = 180.0


def candidate_bytes(item: Any) -> bytes:
    """The image one response item carries, whether the provider returned it inline or as a link."""
    encoded = getattr(item, "b64_json", None)
    if encoded:
        return base64.b64decode(encoded)
    url = getattr(item, "url", None)
    if not url:
        raise ValueError("the model returned an image with neither data nor a link")
    response = httpx.get(url, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    return response.content


def write_candidates(items: Sequence[Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, item in enumerate(items):
        path = out_dir / f"candidate-{index}.png"
        path.write_bytes(candidate_bytes(item))
        paths.append(path)
    return paths


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model", required=True, help="A litellm image model, e.g. gpt-image-1"
    )
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--variants", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--out-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    response = litellm.image_generation(
        model=arguments.model,
        prompt=arguments.prompt,
        n=arguments.variants,
        size=arguments.size,
        timeout=GENERATION_TIMEOUT_SECONDS,
    )
    for path in write_candidates(response.data, arguments.out_dir):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
