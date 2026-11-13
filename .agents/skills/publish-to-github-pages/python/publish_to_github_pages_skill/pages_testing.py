"""Stand-ins for the publish-to-github-pages tests: a running app, latchkey, and GitHub's git remote."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
GATEWAY = "http://gateway.invalid"

# A small app that exercises everything the copy has to rewrite: two pages that link to
# each other, a stylesheet that imports another and names an image, a module script that
# imports a sibling, an icon, a srcset, links that leave the app, the Studio shell's
# connection script, and a data route.
APP_ROUTES: dict[str, tuple[str, str]] = {
    "/": (
        "text/html",
        """<!doctype html><html><head><title>Sample</title>
<link rel="stylesheet" href="/static/style.css"><link rel="icon" href="/static/icon.svg">
</head><body><nav><a href="/">Home</a> <a href="/about">About</a>
<a href="https://example.com/elsewhere">Elsewhere</a> <a href="#top">Top</a></nav>
<img src="/static/icon.svg" srcset="/static/icon.svg 1x, /static/icon.svg 2x" alt="">
<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="">
<ul id="items"></ul>
<script type="module" src="/static/main.js"></script>
<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  connectToShell({});
</script>
</body></html>""",
    ),
    "/about": (
        "text/html",
        """<!doctype html><html><head><title>About</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><a href="/">Back home</a></body></html>""",
    ),
    "/static/style.css": (
        "text/css",
        '@import "/static/extra.css";\nbody { background: url(/static/icon.svg) no-repeat; }\n',
    ),
    "/static/extra.css": ("text/css", "h1 { color: teal; }\n"),
    "/static/icon.svg": (
        "image/svg+xml",
        '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>\n',
    ),
    "/static/main.js": (
        "text/javascript",
        'import { render } from "/static/util.js";\n'
        'fetch("/api/items").then((r) => r.json()).then(render);\n',
    ),
    "/static/util.js": (
        "text/javascript",
        "export const render = (items) => {\n"
        '  document.getElementById("items").innerHTML = items.map((i) => `<li>${i.name}</li>`).join("");\n'
        "};\n",
    ),
    "/api/items": ("application/json", '[{"name": "Coffee"}, {"name": "Bagel"}]'),
}


class _AppHandler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[str, str]] = {}
    unavailable_for = 0  # answer 503 to this many requests first, as a waking app does

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        server_class = type(self)
        if server_class.unavailable_for > 0:
            server_class.unavailable_for -= 1
            self.send_error(503)
            return
        route = server_class.routes.get(self.path.split("?")[0])
        if route is None:
            self.send_error(404)
            return
        content_type, body = route
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def running_app(
    routes: dict[str, tuple[str, str]] | None = None, *, unavailable_for: int = 0
) -> Generator[str]:
    """Serve `routes` (default: the sample app) on a free local port; yields its base URL."""
    handler = type(
        "Handler",
        (_AppHandler,),
        {"routes": routes or APP_ROUTES, "unavailable_for": unavailable_for},
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def run_tool(
    name: str, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one of the skill's tools by its path, as the skill does."""
    return subprocess.run(
        [sys.executable, str(PACKAGE_DIR / name), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
        timeout=120,
    )


# Stands in for `latchkey curl`: answers from a JSON state file keyed by "METHOD URL",
# appends the status line `-w "\n%{http_code}"` asks for, and logs every argv.
_FAKE_LATCHKEY = """#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LATCHKEY_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
state = json.loads(open(os.environ["FAKE_LATCHKEY_STATE"]).read())
url = next(arg for arg in args if arg.startswith("http"))
method = args[args.index("-X") + 1] if "-X" in args else "GET"
answer = state.get(method + " " + url)
if answer is None:
    answer = state.get("default", {{"status": 404, "body": {{"message": "Not Found"}}}})
body = answer["body"]
sys.stdout.write("" if body is None else json.dumps(body))
if "-w" in args:
    sys.stdout.write("\\n" + str(answer["status"]))
"""


class FakeLatchkey:
    """A `latchkey` on PATH that answers GitHub from canned replies and records each call."""

    def __init__(self, root: Path) -> None:
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir(parents=True)
        self.state_path = root / "latchkey-state.json"
        self.log_path = root / "latchkey-log.jsonl"
        self.log_path.touch()
        script = self.bin_dir / "latchkey"
        script.write_text(_FAKE_LATCHKEY.format(python=sys.executable))
        script.chmod(0o755)
        self.answers: dict[str, dict[str, object]] = {}
        self._save()

    def answer(self, method: str, url: str, status: int, body: object) -> None:
        self.answers[f"{method} {url}"] = {"status": status, "body": body}
        self._save()

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.answers))

    def calls(self) -> list[tuple[str, str, object]]:
        """Each call as (method, url, the JSON body it sent or None)."""
        calls = []
        for line in self.log_path.read_text().splitlines():
            args = json.loads(line)
            url = next(arg for arg in args if arg.startswith("http"))
            method = args[args.index("-X") + 1] if "-X" in args else "GET"
            body = json.loads(args[args.index("-d") + 1]) if "-d" in args else None
            calls.append((method, url, body))
        return calls

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_LATCHKEY_STATE"] = str(self.state_path)
        env["FAKE_LATCHKEY_LOG"] = str(self.log_path)
        env.update(extra or {})
        return env


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def gateway_remote(root: Path, repo: str) -> tuple[Path, dict[str, str]]:
    """A bare repo standing in for GitHub's `<repo>`, and the env that routes the gateway URL to it.

    git's own `insteadOf`, set through the environment, maps the latchkey gateway's URL for
    `<repo>` onto the bare repo, so the tool's real push command runs unchanged.
    """
    remote = root / "remote.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(remote))
    env = {
        "LATCHKEY_GATEWAY": GATEWAY,
        "LATCHKEY_GATEWAY_PASSWORD": "pw",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{remote}.insteadOf",
        "GIT_CONFIG_VALUE_0": f"{GATEWAY}/gateway/https://github.com/{repo}.git",
    }
    return remote, env
