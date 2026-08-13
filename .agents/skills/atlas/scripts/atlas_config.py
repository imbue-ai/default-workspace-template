#!/usr/bin/env python3
"""Atlas feature toggles -- which halves of Atlas are active in this workspace.

Two independent features, both on by default, so a user can run the book, the
in-chat summary, or both:

  - `pages`   -- the one-pager book: §0 checkpoints, automatic page generation,
                 topic detection, the prompt router, and the viewer app's content.
  - `summary` -- the plain-English "what changed" recap posted in chat after a
                 large task (Atlas Summary).

Stored in `atlas/config.toml`; a missing file or key means the default (on).

Usage:
    atlas_config.py show
    atlas_config.py enable  {pages|summary}
    atlas_config.py disable {pages|summary}
    atlas_config.py enabled {pages|summary}   # exit 0 if on, 1 if off
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402

# Friendly feature name -> the config key that stores it.
FEATURES = {"pages": "pages_enabled", "summary": "summary_enabled"}
DEFAULTS = {"pages_enabled": True, "summary_enabled": True}


def config_path(repo_root: Path) -> Path:
    return repo_root / "atlas" / "config.toml"


def load(repo_root: Path) -> dict:
    """The merged config: defaults overlaid with atlas/config.toml, if present."""
    data = dict(DEFAULTS)
    p = config_path(repo_root)
    if p.is_file():
        try:
            with p.open("rb") as fh:
                data.update(tomllib.load(fh))
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return data


def is_enabled(repo_root: Path, feature: str) -> bool:
    key = FEATURES.get(feature, feature)
    return bool(load(repo_root).get(key, DEFAULTS.get(key, True)))


def set_enabled(repo_root: Path, feature: str, value: bool) -> None:
    cfg = load(repo_root)
    cfg[FEATURES[feature]] = bool(value)
    p = config_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    atlas_common.atomic_write(
        p,
        "# Atlas feature toggles (see the atlas skill). Both default to true.\n"
        f"pages_enabled = {str(bool(cfg['pages_enabled'])).lower()}    "
        "# the one-pager book + viewer + automatic page generation\n"
        f"summary_enabled = {str(bool(cfg['summary_enabled'])).lower()}  "
        "# the plain-English 'what changed' summary in chat after a big task\n",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Atlas feature toggles.")
    parser.add_argument("action", choices=["show", "enable", "disable", "enabled"])
    parser.add_argument("feature", nargs="?", choices=list(FEATURES))
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args(argv)
    repo_root = atlas_common.resolve_repo_root(args.repo_root)

    if args.action == "show":
        cfg = load(repo_root)
        for name, key in FEATURES.items():
            print(f"{name}: {'on' if cfg.get(key) else 'off'}")
        return 0

    if not args.feature:
        print("atlas_config: a feature (pages|summary) is required", file=sys.stderr)
        return 2

    if args.action == "enabled":
        return 0 if is_enabled(repo_root, args.feature) else 1

    set_enabled(repo_root, args.feature, args.action == "enable")
    print(f"atlas_config: {args.feature} {args.action}d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
