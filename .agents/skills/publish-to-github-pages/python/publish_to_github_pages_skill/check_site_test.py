"""`check_site.py`: preparing a site, the leak and breakage lint, the local server, the browser check."""

from __future__ import annotations

import secrets
import string
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from publish_to_github_pages_skill import check_site
from publish_to_github_pages_skill.pages_testing import run_tool

_CLEAN_PAGE = """<!doctype html><html><head><title>Clean</title>
<link rel="stylesheet" href="style.css"></head>
<body><h1>Hello</h1><ul id="items"></ul><script src="app.js"></script></body></html>
"""
_CLEAN_SCRIPT = """fetch("data/items.json").then((r) => r.json()).then((items) => {
  document.getElementById("items").textContent = items.join(", ");
});
"""


def _clean_site(root: Path) -> Path:
    site = root / "site"
    site.mkdir()
    (site / "index.html").write_text(_CLEAN_PAGE)
    (site / "style.css").write_text("h1 { color: teal; }\n")
    (site / "app.js").write_text(_CLEAN_SCRIPT)
    (site / "data").mkdir()
    (site / "data" / "items.json").write_text('["a", "b"]\n')
    check_site.prepare(site, "Clean", "A clean site.", is_spa=False)
    return site


def _fake_anthropic_key() -> str:
    """A key in the real format, random each run, so no real secret is ever in the tree."""
    alphabet = string.ascii_letters + string.digits + "-_"
    return "sk-ant-api03-" + "".join(secrets.choice(alphabet) for _ in range(93)) + "AA"


# prepare ---------------------------------------------------------------------


def test_prepare_adds_what_github_pages_needs(tmp_path: Path) -> None:
    site = _clean_site(tmp_path)
    assert (site / ".nojekyll").read_text() == ""
    assert "Made in Imbue Studio." in (site / "README.md").read_text()
    assert check_site.ALLOW_FILE in (site / ".gitignore").read_text().splitlines()
    assert not (site / "404.html").exists()


def test_prepare_keeps_an_existing_readme_and_copies_the_page_for_deep_links(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(_CLEAN_PAGE)
    (site / "README.md").write_text("mine\n")
    check_site.prepare(site, "T", "D", is_spa=True)
    assert (site / "README.md").read_text() == "mine\n"
    assert (site / "404.html").read_text() == _CLEAN_PAGE


def test_prepare_refuses_a_folder_with_no_page(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no index.html"):
        check_site.prepare(tmp_path, "T", "D", is_spa=False)


# lint rules --------------------------------------------------------------------


def test_a_clean_site_has_no_findings(tmp_path: Path) -> None:
    assert check_site.lint_findings(_clean_site(tmp_path)) == []


@pytest.mark.parametrize(
    ("file", "line", "label"),
    [
        ("index.html", '<link href="/style.css">', "root-relative reference"),
        ("style.css", "body { background: url(/bg.png); }", "root-relative CSS url"),
        ("app.js", 'fetch("/api/items")', "call to the app's server"),
        (
            "app.js",
            'const sheet = await api("/api/sheet");',
            "root-relative path in a script",
        ),
        (
            "index.html",
            '<script>load("/api/sheet")</script>',
            "root-relative path in a script",
        ),
        (
            "app.js",
            'const base = "http://localhost:8080";',
            "reference to the workspace",
        ),
        (
            "app.js",
            'const home = "/home/user/workspace";',
            "reference to the workspace",
        ),
        ("app.js", 'const owner = "jane.doe@example.org";', "email address"),
    ],
)
def test_each_rule_flags_its_case_with_file_and_line(
    tmp_path: Path, file: str, line: str, label: str
) -> None:
    site = _clean_site(tmp_path)
    with (site / file).open("a") as handle:
        handle.write(line + "\n")
    number = len((site / file).read_text().splitlines())
    findings = check_site.lint_findings(site)
    assert any(f.startswith(f"{file}:{number}: {label}") for f in findings), findings


@pytest.mark.parametrize(
    "line",
    [
        'fetch("data/items.json")',
        'const url = "https://example.com/path/x";',
        'const parts = path.split("/");',
        'const protocolRelative = "//cdn.example.com/lib.js";',
    ],
)
def test_paths_that_work_on_a_static_host_are_not_flagged(
    tmp_path: Path, line: str
) -> None:
    site = _clean_site(tmp_path)
    with (site / "app.js").open("a") as handle:
        handle.write(line + "\n")
    assert check_site.lint_findings(site) == []


def test_the_allow_list_excuses_only_the_lines_it_names(tmp_path: Path) -> None:
    site = _clean_site(tmp_path)
    (site / "app.js").write_text(
        'const a = "a@example.org";\nconst b = "b@example.org";\n'
    )
    (site / check_site.ALLOW_FILE).write_text(
        "# the user wants this one public\napp.js:1\n"
    )
    findings = check_site.lint_findings(site)
    assert [f.split(": ")[0] for f in findings] == ["app.js:2"]


def test_an_allow_list_that_would_be_published_is_a_finding(tmp_path: Path) -> None:
    site = _clean_site(tmp_path)
    (site / ".gitignore").write_text("")
    (site / check_site.ALLOW_FILE).write_text("")
    assert any("would be published" in f for f in check_site.lint_findings(site))


def test_a_site_without_nojekyll_is_a_finding(tmp_path: Path) -> None:
    site = _clean_site(tmp_path)
    (site / ".nojekyll").unlink()
    assert any(f.startswith(".nojekyll") for f in check_site.lint_findings(site))


# the secret scanners -------------------------------------------------------------


@pytest.mark.timeout(120)
def test_lint_passes_a_clean_site_through_both_scanners(tmp_path: Path) -> None:
    result = run_tool("check_site.py", "lint", "--site", str(_clean_site(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert "lint clean" in result.stdout


@pytest.mark.timeout(120)
def test_an_ai_key_in_the_copy_fails_the_lint_without_printing_it(
    tmp_path: Path,
) -> None:
    site = _clean_site(tmp_path)
    key = _fake_anthropic_key()
    (site / "config.js").write_text(f'const KEY = "{key}";\n')
    result = run_tool("check_site.py", "lint", "--site", str(site))
    assert result.returncode == 1
    assert "SECRET SCAN FINDING" in result.stderr
    assert "config.js" in result.stderr
    assert key not in result.stdout + result.stderr


# the local GitHub Pages stand-in ---------------------------------------------------


def _status(url: str) -> int:
    request = urllib.request.Request(url)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def test_the_site_is_served_only_under_its_repo_path(tmp_path: Path) -> None:
    site = _clean_site(tmp_path)
    server, base = check_site._serve(site, "demo")
    try:
        root = base.removesuffix("demo/")
        assert _status(base) == 200
        assert _status(base + "style.css") == 200
        assert _status(root + "style.css") == 404
        assert _status(root + "demo") == 301
        assert _status(base + "missing.html") == 404
    finally:
        server.shutdown()


def test_a_miss_serves_the_sites_404_page_when_it_has_one(tmp_path: Path) -> None:
    site = _clean_site(tmp_path)
    (site / "404.html").write_text("<p>fallback</p>")
    server, base = check_site._serve(site, "demo")
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "deep/link", timeout=10)
        assert caught.value.code == 404
        assert caught.value.read() == b"<p>fallback</p>"
    finally:
        server.shutdown()


# the browser check ------------------------------------------------------------------


def _browse(site: Path) -> tuple[int, str]:
    result = run_tool(
        "check_site.py",
        "browse",
        "--site",
        str(site),
        "--repo",
        "demo",
        "--screenshot-dir",
        str(site.parent / "shots"),
    )
    return result.returncode, result.stdout + result.stderr


@pytest.mark.browser
@pytest.mark.timeout(180)
def test_a_working_copy_passes_the_browser_check_and_is_screenshotted(
    tmp_path: Path,
) -> None:
    status, output = _browse(_clean_site(tmp_path))
    assert status == 0, output
    assert "browse clean" in output
    assert (tmp_path / "shots" / "00-index.png").stat().st_size > 0


@pytest.mark.browser
@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ('fetch("/api/items");', "HTTP 404"),
        ('fetch("missing.json");', "HTTP 404"),
        ("undefinedFunction();", "uncaught error"),
        ('console.error("boom");', "console error: boom"),
        ('fetch("http://localhost:9/").catch(() => {});', "request to a local address"),
    ],
)
def test_each_kind_of_breakage_fails_the_browser_check(
    tmp_path: Path, script: str, expected: str
) -> None:
    site = _clean_site(tmp_path)
    (site / "app.js").write_text(script + "\n")
    status, output = _browse(site)
    assert status == 1
    assert expected in output, output
