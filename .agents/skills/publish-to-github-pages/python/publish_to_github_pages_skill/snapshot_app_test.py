"""`snapshot_app.py` against a running stand-in app."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from publish_to_github_pages_skill import snapshot_app
from publish_to_github_pages_skill.pages_testing import run_tool, running_app


def _snapshot(
    base_url: str, site: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return run_tool(
        "snapshot_app.py", "--base-url", base_url, "--site", str(site), *args
    )


@pytest.fixture
def site(tmp_path: Path) -> Path:
    with running_app() as base_url:
        result = _snapshot(
            base_url,
            tmp_path / "site",
            "--page",
            "/",
            "--page",
            "/about",
            "--data",
            "/api/items=data/items.json",
        )
    assert result.returncode == 0, result.stderr
    return tmp_path / "site"


def test_every_page_and_asset_is_copied_where_a_static_host_serves_it(
    site: Path,
) -> None:
    files = sorted(
        p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()
    )
    assert files == [
        "about/index.html",
        "data/items.json",
        "index.html",
        "static/extra.css",
        "static/icon.svg",
        "static/main.js",
        "static/style.css",
        "static/util.js",
    ]


def test_references_are_relative_so_the_site_works_under_its_repo_path(
    site: Path,
) -> None:
    index = (site / "index.html").read_text()
    assert 'href="static/style.css"' in index
    assert 'src="static/main.js"' in index
    assert 'srcset="static/icon.svg 1x, static/icon.svg 2x"' in index
    assert '<a href="./">Home</a>' in index
    assert '<a href="about/">About</a>' in index
    about = (site / "about" / "index.html").read_text()
    assert 'href="../static/style.css"' in about
    assert '<a href="../">Back home</a>' in about


def test_stylesheets_and_module_imports_are_rewritten_too(site: Path) -> None:
    style = (site / "static" / "style.css").read_text()
    assert '@import "extra.css";' in style
    assert "url(icon.svg)" in style
    assert 'from "./util.js"' in (site / "static" / "main.js").read_text()


def test_links_that_leave_the_app_are_left_alone(site: Path) -> None:
    index = (site / "index.html").read_text()
    assert 'href="https://example.com/elsewhere"' in index
    assert 'href="#top"' in index
    assert 'src="data:image/gif;base64,' in index


def test_the_studio_shell_connection_is_left_out(site: Path) -> None:
    index = (site / "index.html").read_text()
    assert "app_contract.js" not in index
    assert "connectToShell" not in index


def test_a_data_snapshot_is_saved_pretty_printed(site: Path) -> None:
    text = (site / "data" / "items.json").read_text()
    assert json.loads(text) == [{"name": "Coffee"}, {"name": "Bagel"}]
    assert text.startswith("[\n  {")


@pytest.mark.parametrize(
    "spec", ["/api/items=../outside.json", "/api/items=/etc/x.json", "api/items=a.json"]
)
def test_a_data_path_outside_the_site_is_refused(tmp_path: Path, spec: str) -> None:
    with running_app() as base_url:
        result = _snapshot(base_url, tmp_path / "site", "--data", spec)
    assert result.returncode == 1
    assert "must be <api path>=<file inside the site>" in result.stderr
    assert not (tmp_path / "outside.json").exists()


def test_a_fresh_copy_empties_the_site_but_keeps_its_publish_history(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    (site / ".git").mkdir(parents=True)
    (site / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (site / "stale.html").write_text("old")
    with running_app() as base_url:
        result = _snapshot(base_url, site, "--fresh")
    assert result.returncode == 0, result.stderr
    assert not (site / "stale.html").exists()
    assert (site / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert (site / "index.html").is_file()


def test_an_app_still_waking_up_is_waited_for(tmp_path: Path) -> None:
    with running_app(unavailable_for=2) as base_url:
        result = _snapshot(base_url, tmp_path / "site")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "site" / "index.html").is_file()


def test_an_app_that_never_answers_fails_with_how_to_start_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshot_app, "_STARTUP_WAIT_SECONDS", 0)
    with pytest.raises(snapshot_app.SnapshotError, match="supervisorctl start"):
        snapshot_app.fetch("http://127.0.0.1:9/")


def test_the_app_url_is_read_from_the_registry(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "other"\nurl = "http://localhost:1"\n\n'
        '[[apps]]\nname = "planner"\nurl = "http://localhost:8082/"\n'
    )
    assert snapshot_app.app_base_url("planner", registry) == "http://localhost:8082"
    with pytest.raises(snapshot_app.SnapshotError, match="pass --base-url"):
        snapshot_app.app_base_url("missing", registry)
