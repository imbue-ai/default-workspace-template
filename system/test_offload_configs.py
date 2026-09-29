"""The offload configs at the repo root build their checkpoint image from
``system/Dockerfile``, and rebuild it only when a listed build input changes.
The list is written by hand, once per config, so this holds it to the
Dockerfile: every file the Dockerfile copies in is a build input (or is
exempted here for a stated reason), the three configs agree, and no entry
names a file that no longer exists (a rename would otherwise silently stop
invalidating the image).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_DOCKERFILE_PATH = _REPO_ROOT / "system" / "Dockerfile"
_OFFLOAD_CONFIG_PATHS = (
    _REPO_ROOT / "offload-modal.toml",
    _REPO_ROOT / "offload-modal-chat.toml",
    _REPO_ROOT / "offload-modal-system-interface.toml",
)

# Copied by the Dockerfile but deliberately not build inputs: the whole tree
# (every commit would rebuild), and the per-member dependency manifests, whose
# dependency changes also change uv.lock or package-lock.json (which are build
# inputs) while their other edits churn far more often than they matter.
_EXEMPT_COPY_SOURCES = frozenset({"."})
_EXEMPT_COPY_SOURCE_PATTERNS = (
    re.compile(r"^system/(libs|services|apps)/[\w-]+/pyproject\.toml$"),
    re.compile(r"^system/(libs|apps)/[\w-]+(/frontend)?/package\.json$"),
)


def _build_inputs(config_path: Path) -> list[str]:
    with config_path.open("rb") as config_file:
        return tomllib.load(config_file)["checkpoint"]["build_inputs"]


def _dockerfile_copy_sources() -> list[str]:
    sources: list[str] = []
    for line in _DOCKERFILE_PATH.read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        # Every argument but the last is a source.
        sources.extend(line.split()[1:-1])
    return sources


def _is_exempt(copy_source: str) -> bool:
    if copy_source in _EXEMPT_COPY_SOURCES:
        return True
    return any(pattern.match(copy_source) for pattern in _EXEMPT_COPY_SOURCE_PATTERNS)


def test_the_three_offload_configs_list_the_same_build_inputs() -> None:
    lists = [_build_inputs(path) for path in _OFFLOAD_CONFIG_PATHS]
    assert lists[0] != []
    assert lists[1] == lists[0]
    assert lists[2] == lists[0]


def test_every_file_the_dockerfile_copies_is_a_build_input() -> None:
    build_inputs = set(_build_inputs(_OFFLOAD_CONFIG_PATHS[0]))
    copy_sources = _dockerfile_copy_sources()
    assert copy_sources != []
    missing = [
        source
        for source in copy_sources
        if not _is_exempt(source) and source not in build_inputs
    ]
    assert missing == []


def test_every_build_input_exists() -> None:
    missing = [
        entry
        for entry in _build_inputs(_OFFLOAD_CONFIG_PATHS[0])
        if not (_REPO_ROOT / entry).is_file()
    ]
    assert missing == []
