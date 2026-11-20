#!/usr/bin/env python3
"""Prepare, lint, and browser-check a static site folder before (and after) it is published.

    uv run --no-sync check_site.py prepare --site <SITE> --title <text> --description <text> [--spa]
    uv run --no-sync check_site.py lint --site <SITE>
    uv run --no-sync check_site.py browse --site <SITE> --repo <repo> \\
        [--page / --page /about/ ...] [--screenshot-dir <dir>]
    uv run --no-sync check_site.py browse --live-url https://<owner>.github.io/<repo>/ \\
        [--page ...] [--screenshot-dir <dir>]

`prepare` adds what GitHub Pages needs: an empty `.nojekyll` (so the files are
served as they are, not run through Jekyll), a short `README.md` if the site has
none, and with `--spa` a `404.html` copy of `index.html` so a deep link into an
app with several views still loads it.

`lint` fails (exit 1) on anything that would break or leak once the site is
public, one line per finding with its file and line:
  - references the static host cannot serve: root-relative `src`/`href`/`action`
    and CSS `url(/...)`, and `fetch("/...")`-style calls to the app's server;
  - references back into the workspace: `localhost`, `127.0.0.1`, `/home/user`,
    latchkey;
  - email addresses (personal data unless the user confirmed them);
  - then both secret scanners, through the publish-template skill's
    `scan_secrets.sh`.
A `.publish-allow` file in the site (one `path:line` per line) records findings
the user explicitly confirmed may ship; it is never published (`lint` checks it
is listed in `.gitignore`).

`browse` loads each page in a real browser and fails on any failed request, any
HTTP error, any console error or uncaught exception, and any request to a local
address. With `--site` it serves the folder under `/<repo>/`, exactly as GitHub
Pages will (anything outside that prefix is a 404), so broken paths show up
before publishing; with `--live-url` it checks the published site. It saves one
full-page screenshot per page into `--screenshot-dir`.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, Error, Page, Request, Response

# This file is .agents/skills/publish-to-github-pages/python/<package>/check_site.py; the
# scanners are the publish-template skill's.
_SKILLS_DIR = Path(__file__).resolve().parents[3]
SCAN_SECRETS = _SKILLS_DIR / "publish-template" / "scripts" / "scan_secrets.sh"
FORTRESS_PATH = Path("/opt/fortress/tilion-fortress/tilion")
ALLOW_FILE = ".publish-allow"

_TEXT_SUFFIXES = {
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".txt",
    ".md",
    ".svg",
    ".xml",
    ".webmanifest",
    ".map",
}
_CHECKS = (
    (
        "root-relative reference (breaks under /<repo>/)",
        re.compile(r"""\b(?:src|href|action|poster)\s*=\s*["']/(?!/)"""),
    ),
    (
        "root-relative CSS url (breaks under /<repo>/)",
        re.compile(r"""url\(\s*['"]?/(?!/)"""),
    ),
    (
        "call to the app's server",
        re.compile(
            r"""\b(?:fetch|axios(?:\.\w+)?|\.open|EventSource|WebSocket)\s*\(\s*(?:['"`][A-Z]+['"`]\s*,\s*)?['"`]/(?!/)"""
        ),
    ),
    (
        # Catches calls through a page's own helper (`api("/api/sheet")`), which the fetch rule
        # above cannot see. Scripts and pages (inline scripts) only.
        "root-relative path in a script (likely a call to the app's server)",
        re.compile(r"""["'`]/[A-Za-z_][\w.-]*(?:/|["'`$])"""),
    ),
    (
        "reference to the workspace",
        re.compile(r"""localhost|127\.0\.0\.1|/home/user\b|latchkey""", re.IGNORECASE),
    ),
    (
        "email address",
        re.compile(
            r"""\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"""
        ),
    ),
)


def _text_files(site: Path) -> list[Path]:
    files = []
    for path in sorted(site.rglob("*")):
        if ".git" in path.relative_to(site).parts or not path.is_file():
            continue
        if path.suffix.lower() in _TEXT_SUFFIXES:
            files.append(path)
    return files


def _allowed(site: Path) -> set[str]:
    allow = site / ALLOW_FILE
    if not allow.is_file():
        return set()
    return {
        line.strip()
        for line in allow.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def prepare(site: Path, title: str, description: str, is_spa: bool) -> None:
    if not (site / "index.html").is_file():
        raise SystemExit(
            f"check_site: {site} has no index.html; copy the app into it first"
        )
    (site / ".nojekyll").touch()
    print("wrote .nojekyll")
    gitignore = site / ".gitignore"
    lines = (
        gitignore.read_text(encoding="utf-8").splitlines()
        if gitignore.is_file()
        else []
    )
    if ALLOW_FILE not in lines:
        gitignore.write_text("\n".join([*lines, ALLOW_FILE]) + "\n", encoding="utf-8")
        print("wrote .gitignore")
    readme = site / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# {title}\n\n{description}\n\nMade in Imbue Studio.\n", encoding="utf-8"
        )
        print("wrote README.md")
    if is_spa:
        shutil.copyfile(site / "index.html", site / "404.html")
        print("wrote 404.html")


def lint_findings(site: Path) -> list[str]:
    allowed = _allowed(site)
    findings = []
    for path in _text_files(site):
        rel = path.relative_to(site).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if f"{rel}:{number}" in allowed:
                continue
            for label, pattern in _CHECKS:
                if label.startswith(
                    "root-relative path in a script"
                ) and path.suffix not in (".js", ".mjs", ".html", ".htm"):
                    continue
                if pattern.search(line):
                    findings.append(f"{rel}:{number}: {label}: {line.strip()[:160]}")
    if (site / ALLOW_FILE).is_file():
        gitignore = site / ".gitignore"
        if (
            not gitignore.is_file()
            or ALLOW_FILE not in gitignore.read_text(encoding="utf-8").splitlines()
        ):
            findings.append(
                f"{ALLOW_FILE}: not listed in .gitignore, so it would be published; run `prepare`"
            )
    if not (site / ".nojekyll").is_file():
        findings.append(".nojekyll: missing; run `prepare`")
    return findings


def lint(site: Path) -> int:
    findings = lint_findings(site)
    for finding in findings:
        print(f"check_site: {finding}", file=sys.stderr)
    scan = subprocess.run(
        [
            "bash",
            str(SCAN_SECRETS),
            *[
                str(p)
                for p in sorted(site.iterdir())
                if p.name not in (".git", ALLOW_FILE)
            ],
        ],
        check=False,
    )
    if findings or scan.returncode != 0:
        print(
            f"check_site: lint FAILED ({len(findings)} finding(s); secret scan exit {scan.returncode})",
            file=sys.stderr,
        )
        return 1
    print("check_site: lint clean")
    return 0


class _PagesHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the site only under /<repo>/, and its 404.html for a miss, as GitHub Pages does."""

    prefix = "/"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def send_head(self):  # type: ignore[no-untyped-def]
        path = urlsplit(self.path).path
        if path == self.prefix.rstrip("/"):
            self.send_response(301)
            self.send_header("Location", self.prefix)
            self.end_headers()
            return None
        if not path.startswith(self.prefix):
            self.send_error(404)
            return None
        self.path = "/" + self.path[len(self.prefix) :]
        if not os.path.exists(self.translate_path(self.path)):
            fallback = Path(self.directory) / "404.html"
            if fallback.is_file():
                body = fallback.read_bytes()
                self.send_response(404)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
        return super().send_head()


def _serve(site: Path, repo: str) -> tuple[http.server.ThreadingHTTPServer, str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = functools.partial(
        type("Handler", (_PagesHandler,), {"prefix": f"/{repo}/"}), directory=str(site)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}/{repo}/"


def _watch(page: Page, url: str, base_host: str, problems: list[str]) -> None:
    """Record everything on `page` that would break or leak once the site is public."""

    def on_request(request: Request) -> None:
        parts = urlsplit(request.url)
        if (parts.hostname or "") in ("localhost", "127.0.0.1", "0.0.0.0") and (
            parts.netloc != base_host
        ):
            problems.append(f"{url}: request to a local address: {request.url}")

    def on_request_failed(request: Request) -> None:
        problems.append(f"{url}: request failed: {request.url} ({request.failure})")

    def on_response(response: Response) -> None:
        # GitHub Pages project sites have no /favicon.ico; the browser's own probe 404s.
        if response.status >= 400 and not response.url.endswith("/favicon.ico"):
            problems.append(f"{url}: HTTP {response.status}: {response.url}")

    def on_console(message: ConsoleMessage) -> None:
        # "Failed to load resource" repeats a failed response, already caught above.
        if message.type == "error" and not message.text.startswith(
            "Failed to load resource"
        ):
            problems.append(f"{url}: console error: {message.text}")

    def on_page_error(error: Error) -> None:
        problems.append(f"{url}: uncaught error: {error}")

    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    page.on("console", on_console)
    page.on("pageerror", on_page_error)


def browse(
    base_url: str,
    pages: list[str],
    screenshot_dir: Path | None,
    browser_path: str | None,
) -> int:
    from playwright.sync_api import sync_playwright

    base_host = urlsplit(base_url).netloc
    problems: list[str] = []
    executable = browser_path or (
        str(FORTRESS_PATH) if os.access(FORTRESS_PATH, os.X_OK) else None
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable, args=["--no-sandbox"]
        )
        try:
            for index, page_path in enumerate(pages):
                url = urljoin(base_url, page_path.lstrip("/"))
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                _watch(page, url, base_host, problems)
                response = page.goto(url, wait_until="networkidle", timeout=60000)
                if response is None or response.status >= 400:
                    problems.append(
                        f"{url}: page answered {response.status if response else 'nothing'}"
                    )
                if screenshot_dir is not None:
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    name = (
                        re.sub(r"[^A-Za-z0-9]+", "-", page_path.strip("/")) or "index"
                    )
                    shot = screenshot_dir / f"{index:02d}-{name}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    print(f"screenshot {shot}")
                print(f"loaded {url} (title: {page.title()!r})")
                page.close()
        finally:
            browser.close()
    seen = set()
    for problem in problems:
        if problem not in seen:
            seen.add(problem)
            print(f"check_site: {problem}", file=sys.stderr)
    if problems:
        print(f"check_site: browse FAILED ({len(seen)} problem(s))", file=sys.stderr)
        return 1
    print("check_site: browse clean")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare, lint and browser-check a static site."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--site", required=True, type=Path)
    prep.add_argument("--title", required=True)
    prep.add_argument("--description", required=True)
    prep.add_argument(
        "--spa", action="store_true", help="copy index.html to 404.html for deep links"
    )
    lint_parser = commands.add_parser("lint")
    lint_parser.add_argument("--site", required=True, type=Path)
    browse_parser = commands.add_parser("browse")
    target = browse_parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--site", type=Path, help="serve this folder under /<repo>/ and check it"
    )
    target.add_argument("--live-url", help="check the published site at this URL")
    browse_parser.add_argument("--repo", help="the repo name (with --site)")
    browse_parser.add_argument(
        "--page",
        action="append",
        default=[],
        help="page path relative to the site root (default /)",
    )
    browse_parser.add_argument("--screenshot-dir", type=Path)
    browse_parser.add_argument(
        "--browser-path",
        help="Chromium executable (default: the workspace's, else Playwright's)",
    )
    args = parser.parse_args(argv)

    if args.command == "prepare":
        prepare(args.site, args.title, args.description, args.spa)
        return 0
    if args.command == "lint":
        return lint(args.site)
    pages = args.page or ["/"]
    if args.live_url:
        base = args.live_url if args.live_url.endswith("/") else args.live_url + "/"
        return browse(base, pages, args.screenshot_dir, args.browser_path)
    if not args.repo:
        parser.error(
            "--site needs --repo, so the site is served under /<repo>/ as it will be live"
        )
    server, base = _serve(args.site.resolve(), args.repo)
    try:
        return browse(base, pages, args.screenshot_dir, args.browser_path)
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
