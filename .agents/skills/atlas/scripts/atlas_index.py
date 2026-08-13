#!/usr/bin/env python3
"""The Atlas book index -- projects grouping feature one-pagers.

Atlas is two levels: a **project** groups the **feature** one-pagers built under
it. Different projects are different sections (the "tabs"); each large feature of
a project is its own page underneath. A topic declaration names its project via
`project = "<project-slug>"`; a topic with no project is its own standalone
project.

This builds `atlas/index.md`: one section per project, each listing its feature
pages with their live §0 status line, newest-touched first. It is the browsable
table of contents / the source the tab UI would render. No model.

Usage:
    atlas_index.py [--repo-root R] [--project P]   # P: only that project
"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402
import atlas_status  # noqa: E402


def topic_project(decl: dict, slug: str) -> str:
    """The project a topic belongs to; defaults to the slug (standalone)."""
    return str(decl.get("project") or slug)


def project_title(repo_root: Path, project: str) -> str:
    """Human title for a project: atlas/projects/<project>.toml, else titleized."""
    meta = repo_root / "atlas" / "projects" / f"{project}.toml"
    if meta.is_file():
        try:
            with meta.open("rb") as fh:
                title = tomllib.load(fh).get("title")
            if title:
                return str(title)
        except tomllib.TOMLDecodeError:
            pass
    return project.replace("-", " ").title()


def gather(repo_root: Path) -> dict[str, list[dict]]:
    """Map project -> list of feature topics ({slug,title,status,mtime})."""
    topics_dir = repo_root / "atlas" / "topics"
    projects: dict[str, list[dict]] = {}
    if not topics_dir.is_dir():
        return projects
    for path in sorted(topics_dir.glob("*.toml")):
        if path.stem.startswith("_"):
            continue
        try:
            decl = atlas_common.load_declaration(repo_root, path.stem, missing_ok=False)
        except (tomllib.TOMLDecodeError, OSError):
            continue
        slug = decl.get("slug", path.stem)
        page = atlas_common.page_path(repo_root, slug)
        projects.setdefault(topic_project(decl, slug), []).append(
            {
                "slug": slug,
                "title": decl.get("title", slug),
                "status": decl.get("status", "unknown"),
                "mtime": page.stat().st_mtime if page.is_file() else 0.0,
            }
        )
    return projects


def build_index(repo_root: Path, only: str | None, now: float) -> str:
    projects = gather(repo_root)
    if only:
        projects = {k: v for k, v in projects.items() if k == only}
    lines = [
        "# Atlas — project book",
        "",
        "_One section per project; each large feature is its own one-pager. "
        "Generated — do not edit by hand._",
        "",
    ]
    for project in sorted(projects):
        features = sorted(projects[project], key=lambda f: f["mtime"], reverse=True)
        lines.append(f"## {project_title(repo_root, project)}")
        lines.append("")
        for feat in features:
            try:
                status = atlas_status.build_status_line(repo_root, feat["slug"], now)
            except Exception:
                # One bad topic (missing declaration, transient git failure, a
                # malformed state file) must not abort the whole book -- degrade
                # that row to its declared status.
                status = feat["status"]
            lines.append(f"- **[{feat['title']}]({feat['slug']}.md)** — {status}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the Atlas project book index.")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--project", default=None, help="only this project")
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument(
        "--stdout", action="store_true", help="print instead of writing"
    )
    args = parser.parse_args(argv)
    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    now = args.now if args.now is not None else time.time()
    index = build_index(repo_root, args.project, now)
    if args.stdout:
        print(index)
    else:
        out = repo_root / "atlas" / "index.md"
        out.write_text(index + "\n", encoding="utf-8")
        print(f"atlas_index: wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
