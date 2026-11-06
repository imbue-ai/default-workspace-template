"""The whole flow on the sample app: copy, rewrite, prepare, lint, browser-check, publish."""

from __future__ import annotations

from pathlib import Path

import pytest

from publish_to_github_pages_skill.pages_testing import (
    FakeLatchkey,
    gateway_remote,
    git,
    run_tool,
    running_app,
)

_REPO = "octo/sample"


@pytest.mark.browser
@pytest.mark.timeout(300)
def test_the_sample_app_is_published_exactly_as_checked(tmp_path: Path) -> None:
    site = tmp_path / "site"
    with running_app() as base_url:
        copied = run_tool(
            "snapshot_app.py",
            "--base-url",
            base_url,
            "--site",
            str(site),
            "--page",
            "/",
            "--page",
            "/about",
            "--data",
            "/api/items=data/items.json",
        )
    assert copied.returncode == 0, copied.stderr

    # Before the agent's rewrite the page still calls the app's server, and the lint says so.
    unrewritten = run_tool("check_site.py", "lint", "--site", str(site))
    assert unrewritten.returncode == 1
    assert "static/main.js:2: call to the app's server" in unrewritten.stderr

    main_js = site / "static" / "main.js"
    main_js.write_text(
        main_js.read_text().replace('fetch("/api/items")', 'fetch("data/items.json")')
    )
    prepared = run_tool(
        "check_site.py",
        "prepare",
        "--site",
        str(site),
        "--title",
        "Sample",
        "--description",
        "d",
    )
    assert prepared.returncode == 0, prepared.stderr
    assert run_tool("check_site.py", "lint", "--site", str(site)).returncode == 0

    browsed = run_tool(
        "check_site.py",
        "browse",
        "--site",
        str(site),
        "--repo",
        "sample",
        "--page",
        "/",
        "--page",
        "about/",
    )
    assert browsed.returncode == 0, browsed.stdout + browsed.stderr

    latchkey = FakeLatchkey(tmp_path / "latchkey")
    remote, gateway_env = gateway_remote(tmp_path, _REPO)
    env = latchkey.env(gateway_env)
    env.pop("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", None)
    pushed = run_tool(
        "github_pages.py",
        "push",
        "--site",
        str(site),
        "--repo",
        _REPO,
        "--message",
        "Publish",
        env=env,
    )
    assert pushed.returncode == 0, pushed.stderr

    published = git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    on_disk = sorted(
        p.relative_to(site).as_posix()
        for p in site.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(site).parts
    )
    assert published == on_disk
    assert git(remote, "show", "main:static/main.js") == main_js.read_text().strip()
