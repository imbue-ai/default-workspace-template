#!/usr/bin/env python3
"""Copy a running app's pages, their assets, and chosen data into a static site folder.

    uv run --no-sync snapshot_app.py --app <name> --site <SITE> \\
        [--page / --page /about ...] [--data /api/items=data/items.json ...] \\
        [--base-url http://localhost:<port>] [--fresh]

Each `--page` (default `/`) is fetched from the running app and saved where a
static host serves that path: `/` as `index.html`, `/about` as
`about/index.html`. Every same-origin stylesheet, script, image, font, icon and
module those pages (and their stylesheets and scripts) reference is fetched too
and saved at its own path. Root-relative references (`/static/app.css`) and
absolute ones to the app's own origin are rewritten relative to the file that
holds them, because a GitHub Pages site lives under `/<repo>/`, not at `/`.

Each `--data <api path>=<file>` fetches one API response and saves it, pretty
printed if it is JSON, at `<file>` inside the site -- the frozen snapshot the
page then reads in place of the API. Pointing the page's code at that file is
the agent's job; `check_site.py lint` lists every reference it still makes to
the server.

`--fresh` empties the site folder first, keeping its `.git` (the publish
history). The app's base URL is read from the workspace's app registry
(`data/.state/apps.toml`) unless `--base-url` is given. Prints one line per
file written; exits 1 if any page or data fetch fails (assets that fail are
listed but do not stop the copy, since `check_site.py` will catch any the page
really needs).
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import shutil
import sys
import time
import tomllib
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REGISTRY_PATH = Path("data/.state/apps.toml")

# Attributes whose value is one URL, by tag. `srcset` is handled separately.
_URL_ATTRIBUTES = {
    "link": ("href",),
    "script": ("src",),
    "img": ("src",),
    "source": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "input": ("src",),
    "a": ("href",),
    "form": ("action",),
    "iframe": ("src",),
    "embed": ("src",),
    "object": ("data",),
}
# Tags whose referenced file is an asset to copy (links and forms are only rewritten).
_ASSET_TAGS = {
    "link",
    "script",
    "img",
    "source",
    "video",
    "audio",
    "track",
    "input",
    "iframe",
    "embed",
    "object",
}
_CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)|@import\s+(['"])([^'"]+)\3""")
# The script every workspace app page carries to talk to the Studio shell around its window
# (`connectToShell` from the shell-built `/_static/app_contract.js`). It has no shell to talk to
# on a public site, and its imports would 404 there, so the copy leaves it out.
_SHELL_SCRIPT = re.compile(
    r"""<script\b[^>]*>(?:(?!</script>).)*?/_static/app_contract\.js(?:(?!</script>).)*?</script>[ \t]*\n?""",
    re.DOTALL,
)
_JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)(['"])([^'"]+\.m?js)\1""")


class SnapshotError(Exception):
    """Raised when the app cannot be read."""


def app_base_url(app: str, registry_path: Path = REGISTRY_PATH) -> str:
    try:
        registry = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SnapshotError(
            f"cannot read the app registry at {registry_path}: {e}"
        ) from e
    for entry in registry.get("apps", []):
        if entry.get("name") == app:
            return str(entry["url"]).rstrip("/")
    raise SnapshotError(f"no app named {app!r} in {registry_path}; pass --base-url")


# An app the shell stopped (no window open) answers 503 or refuses connections for a few
# seconds while it starts again; a fetch waits that out before reporting the app as down.
_STARTUP_WAIT_SECONDS = 30


def fetch(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "publish-to-github-pages"}
    )
    deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
    while True:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as e:
            if e.code != 503 or time.monotonic() > deadline:
                raise SnapshotError(f"{url} answered {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            if time.monotonic() > deadline:
                raise SnapshotError(
                    f"{url} did not answer ({e}); if the app is stopped, start its program "
                    "(`supervisorctl start <program>`) and retry"
                ) from e
        time.sleep(1)


def page_file(path: str) -> str:
    """Where a static host serves `path` from: `/` -> index.html, `/a/b` -> a/b/index.html."""
    clean = urlsplit(path).path.strip("/")
    if not clean:
        return "index.html"
    if posixpath.splitext(clean)[1] in (".html", ".htm"):
        return clean
    return f"{clean}/index.html"


def asset_file(path: str) -> str:
    clean = urlsplit(path).path.lstrip("/")
    return clean or "index.html"


def relative_to(from_file: str, target_path: str) -> str:
    """A reference from `from_file` (site-relative) to the site path `target_path`."""
    from_dir = posixpath.dirname(from_file) or "."
    rel = posixpath.relpath(target_path or ".", from_dir)
    return rel


class SiteBuilder:
    def __init__(self, base_url: str, site: Path, pages: list[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.origin = urlsplit(self.base_url)
        self.site = site
        self.page_paths = {urlsplit(p).path.rstrip("/") or "/" for p in pages}
        self.queue: list[tuple[str, str]] = []
        self.done: set[str] = set()
        self.written: list[str] = []
        self.failed_assets: list[str] = []

    def own_path(self, ref: str, base: str) -> str | None:
        """The app-local path `ref` points at, or None when it leaves the app."""
        if not ref or ref.startswith(
            ("#", "data:", "mailto:", "tel:", "javascript:", "blob:")
        ):
            return None
        if "{{" in ref or "${" in ref:
            return None
        absolute = urlsplit(urljoin(base, ref))
        if (absolute.scheme, absolute.netloc) != (
            self.origin.scheme,
            self.origin.netloc,
        ):
            return None
        return absolute.path or "/"

    def site_target(self, path: str) -> str:
        """The site file a link to `path` should reach: a page's folder, or an asset."""
        normalized = path.rstrip("/") or "/"
        if normalized in self.page_paths:
            file = page_file(path)
            return posixpath.dirname(file) + "/" if file != "index.html" else "./"
        return asset_file(path)

    def rewrite(self, ref: str, base: str, holder: str, kind: str | None) -> str:
        path = self.own_path(ref, base)
        if path is None:
            return ref
        if kind is not None and (path.rstrip("/") or "/") not in self.page_paths:
            self.enqueue(path, kind)
        target = self.site_target(path)
        suffix = ""
        parts = urlsplit(ref)
        if parts.query:
            suffix += "?" + parts.query
        if parts.fragment:
            suffix += "#" + parts.fragment
        rel = (
            relative_to(holder, target.rstrip("/"))
            if target != "./"
            else relative_to(holder, "")
        )
        if target.endswith("/") and not rel.endswith("/"):
            rel += "/"
        return rel + suffix

    def enqueue(self, path: str, kind: str) -> None:
        if path not in self.done:
            self.done.add(path)
            self.queue.append((path, kind))

    def write(self, rel: str, content: bytes) -> None:
        target = self.site / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.written.append(rel)
        print(f"wrote {rel}")

    def rewrite_css(self, text: str, base: str, holder: str) -> str:
        def replace(match: re.Match[str]) -> str:
            if match.group(2) is not None:
                new = self.rewrite(match.group(2), base, holder, "asset")
                return f"url({match.group(1)}{new}{match.group(1)})"
            new = self.rewrite(match.group(4), base, holder, "css")
            return f"@import {match.group(3)}{new}{match.group(3)}"

        return _CSS_URL.sub(replace, text)

    def rewrite_js(self, text: str, base: str, holder: str) -> str:
        def replace(match: re.Match[str]) -> str:
            ref = match.group(2)
            if not ref.startswith(("/", "./", "../", "http")):
                return match.group(0)
            new = self.rewrite(ref, base, holder, "js")
            if not new.startswith("."):
                new = "./" + new
            return match.group(0).replace(ref, new)

        return _JS_IMPORT.sub(replace, text)

    def build_page(self, path: str) -> None:
        url = self.base_url + path
        body, _ = fetch(url)
        holder = page_file(path)
        html = _SHELL_SCRIPT.sub("", body.decode("utf-8", errors="replace"))
        self.write(holder, _HtmlRewriter(self, url, holder).run(html).encode("utf-8"))

    def build_asset(self, path: str, kind: str) -> None:
        url = self.base_url + path
        body, content_type = fetch(url)
        holder = asset_file(path)
        if kind == "css" or content_type == "text/css" or holder.endswith(".css"):
            body = self.rewrite_css(
                body.decode("utf-8", errors="replace"), url, holder
            ).encode("utf-8")
        elif kind == "js" or holder.endswith((".js", ".mjs")):
            body = self.rewrite_js(
                body.decode("utf-8", errors="replace"), url, holder
            ).encode("utf-8")
        self.write(holder, body)

    def run(self, pages: list[str]) -> None:
        for page in pages:
            self.build_page(page)
        while self.queue:
            path, kind = self.queue.pop(0)
            try:
                self.build_asset(path, kind)
            except SnapshotError as e:
                self.failed_assets.append(f"{path}: {e}")


class _HtmlRewriter(HTMLParser):
    """Re-emits a page with its own-origin references rewritten, queueing its assets."""

    def __init__(self, builder: SiteBuilder, url: str, holder: str) -> None:
        super().__init__(convert_charrefs=False)
        self.builder = builder
        self.url = url
        self.base = url
        self.holder = holder
        self.out: list[str] = []
        self.in_style = False

    def run(self, html: str) -> str:
        self.feed(html)
        self.close()
        return "".join(self.out)

    def _kind(self, tag: str, attrs: dict[str, str | None]) -> str | None:
        if tag not in _ASSET_TAGS:
            return None
        if tag == "link":
            rel = (attrs.get("rel") or "").lower()
            if "stylesheet" in rel:
                return "css"
            if "modulepreload" in rel:
                return "js"
            if any(
                word in rel
                for word in ("icon", "manifest", "preload", "apple-touch-icon")
            ):
                return "asset"
            return None
        if tag == "script":
            return "js"
        return "asset"

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        values = dict(attrs)
        kind = self._kind(tag, values)
        parts = []
        for name, value in attrs:
            if value is not None and name in _URL_ATTRIBUTES.get(tag, ()):
                value = self.builder.rewrite(value, self.base, self.holder, kind)
            elif value is not None and name == "srcset":
                value = ", ".join(
                    " ".join(
                        [
                            self.builder.rewrite(
                                item.split()[0], self.base, self.holder, "asset"
                            ),
                            *item.split()[1:],
                        ]
                    )
                    for item in value.split(",")
                    if item.strip()
                )
            elif value is not None and name == "style":
                value = self.builder.rewrite_css(value, self.base, self.holder)
            if value is None:
                parts.append(f" {name}")
            else:
                parts.append(f' {name}="{value.replace(chr(34), "&quot;")}"')
        return "".join(parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "base":
            href = dict(attrs).get("href")
            if href:
                self.base = urljoin(self.url, href)
            return  # a <base> would point the static copy back at the app
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        self.in_style = tag == "style"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "base":
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        self.out.append(f"</{tag}>")
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
            data = self.builder.rewrite_css(data, self.base, self.holder)
        self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.out.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self.out.append(f"<![{data}]>")


def empty_site(site: Path) -> None:
    """Remove everything in the site folder except its `.git` (the publish history)."""
    if not site.exists():
        return
    for child in site.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def save_data(base_url: str, site: Path, spec: str) -> str:
    api_path, sep, file = spec.partition("=")
    if (
        not sep
        or not api_path.startswith("/")
        or not file
        or file.startswith("/")
        or ".." in Path(file).parts
    ):
        raise SnapshotError(
            f"--data {spec!r} must be <api path>=<file inside the site>, e.g. /api/items=data/items.json"
        )
    body, content_type = fetch(base_url + api_path)
    if content_type == "application/json" or file.endswith(".json"):
        try:
            body = (
                json.dumps(json.loads(body), indent=2, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        except json.JSONDecodeError as e:
            raise SnapshotError(f"{api_path} did not return JSON: {e}") from e
    target = site / file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    print(f"wrote {file} (data from {api_path})")
    return file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy a running app into a static site folder."
    )
    parser.add_argument(
        "--app", help="the app's registered name (reads its URL from the registry)"
    )
    parser.add_argument(
        "--base-url", help="the app's local URL, instead of the registry's"
    )
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument(
        "--page",
        action="append",
        default=[],
        help="a page path to copy (repeatable; default /)",
    )
    parser.add_argument(
        "--data",
        action="append",
        default=[],
        help="<api path>=<site file> (repeatable)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="empty the site folder first, keeping its history",
    )
    args = parser.parse_args(argv)

    if not args.app and not args.base_url:
        parser.error("pass --app or --base-url")
    pages = args.page or ["/"]
    try:
        base_url = (
            args.base_url.rstrip("/") if args.base_url else app_base_url(args.app)
        )
        if args.fresh:
            empty_site(args.site)
        args.site.mkdir(parents=True, exist_ok=True)
        builder = SiteBuilder(base_url, args.site, pages)
        builder.run(pages)
        for spec in args.data:
            save_data(base_url, args.site, spec)
    except SnapshotError as e:
        print(f"snapshot_app: {e}", file=sys.stderr)
        return 1
    for failure in builder.failed_assets:
        print(f"snapshot_app: could not copy {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
