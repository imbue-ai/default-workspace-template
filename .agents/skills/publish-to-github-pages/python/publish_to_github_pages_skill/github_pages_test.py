"""`github_pages.py` against a stand-in latchkey, pushing to a local bare repo in place of GitHub."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from publish_to_github_pages_skill import check_site
from publish_to_github_pages_skill.pages_testing import (
    FakeLatchkey,
    gateway_remote,
    git,
    run_tool,
)

_API = "https://api.github.com"
_REPO = "octo/demo"


@pytest.fixture
def latchkey(tmp_path: Path) -> FakeLatchkey:
    fake = FakeLatchkey(tmp_path / "latchkey")
    fake.answer("GET", f"{_API}/user", 200, {"login": "octo"})
    return fake


def _run(
    latchkey: FakeLatchkey, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = latchkey.env(env)
    full_env.pop("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", None)
    return run_tool("github_pages.py", *args, env=full_env)


def _site(root: Path) -> Path:
    site = root / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><title>Demo</title><h1>Demo</h1>\n"
    )
    check_site.prepare(site, "Demo", "A demo site.", is_spa=False)
    return site


# whoami and access -----------------------------------------------------------------------


def test_whoami_prints_the_connected_account(latchkey: FakeLatchkey) -> None:
    result = _run(latchkey, "whoami")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "octo"


def test_an_unconnected_account_says_to_request_access(latchkey: FakeLatchkey) -> None:
    latchkey.answer(
        "GET",
        f"{_API}/user",
        400,
        {"error": "Error: No credentials found for github.", "latchkeyError": True},
    )
    result = _run(latchkey, "whoami")
    assert result.returncode == 1
    assert "GitHub is not connected yet" in result.stderr


def test_a_refused_endpoint_names_the_grants_not_a_missing_approval(
    latchkey: FakeLatchkey,
) -> None:
    latchkey.answer(
        "GET",
        f"{_API}/user",
        403,
        {"error": "Error: Request not permitted by the user."},
    )
    result = _run(latchkey, "whoami")
    assert result.returncode == 1
    assert "outside the granted GitHub permissions" in result.stderr


# create-repo --------------------------------------------------------------------------------


def _create(
    latchkey: FakeLatchkey, owner: str = "octo"
) -> subprocess.CompletedProcess[str]:
    return _run(
        latchkey,
        "create-repo",
        "--name",
        "demo",
        "--description",
        'The "Demo" site',
        "--owner",
        owner,
    )


def test_create_repo_asks_for_a_public_repo_with_the_site_address(
    latchkey: FakeLatchkey,
) -> None:
    latchkey.answer(
        "POST", f"{_API}/user/repos", 201, {"full_name": _REPO, "private": False}
    )
    result = _create(latchkey)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _REPO
    ((_, _, body),) = [call for call in latchkey.calls() if call[0] == "POST"]
    assert body == {
        "name": "demo",
        "description": 'The "Demo" site',
        "homepage": "https://octo.github.io/demo/",
        "private": False,
        "has_issues": False,
        "has_wiki": False,
        "has_projects": False,
        "auto_init": False,
    }


def test_a_different_connected_account_stops_before_anything_is_created(
    latchkey: FakeLatchkey,
) -> None:
    result = _create(latchkey, owner="someone-else")
    assert result.returncode == 4
    assert "'octo', not 'someone-else'" in result.stderr
    assert [call for call in latchkey.calls() if call[0] == "POST"] == []


def test_a_taken_name_has_its_own_exit_code(latchkey: FakeLatchkey) -> None:
    latchkey.answer(
        "POST",
        f"{_API}/user/repos",
        422,
        {
            "message": "Repository creation failed.",
            "errors": [{"message": "name already exists on this account"}],
        },
    )
    result = _create(latchkey)
    assert result.returncode == 3
    assert "already exists" in result.stderr


def test_a_repo_that_comes_back_private_is_refused(latchkey: FakeLatchkey) -> None:
    latchkey.answer(
        "POST",
        f"{_API}/user/repos",
        201,
        {"full_name": _REPO, "private": True, "html_url": "u"},
    )
    result = _create(latchkey)
    assert result.returncode == 1
    assert "came back private" in result.stderr


def test_an_invalid_repo_name_is_refused_before_any_call(
    latchkey: FakeLatchkey,
) -> None:
    result = _run(
        latchkey,
        "create-repo",
        "--name",
        "bad name",
        "--description",
        "d",
        "--owner",
        "octo",
    )
    assert result.returncode == 1
    assert "not a valid repo name" in result.stderr
    assert latchkey.calls() == []


# push ------------------------------------------------------------------------------------


@pytest.mark.timeout(180)
def test_a_first_push_publishes_exactly_the_checked_files_as_the_users_noreply_address(
    tmp_path: Path, latchkey: FakeLatchkey
) -> None:
    site = _site(tmp_path)
    (site / check_site.ALLOW_FILE).write_text("")
    remote, env = gateway_remote(tmp_path, _REPO)
    result = _run(
        latchkey,
        "push",
        "--site",
        str(site),
        "--repo",
        _REPO,
        "--message",
        "Publish Demo",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    pushed = result.stdout.strip()
    assert git(remote, "rev-parse", "main") == pushed
    assert git(remote, "ls-tree", "-r", "--name-only", "main").splitlines() == [
        ".gitignore",
        ".nojekyll",
        "README.md",
        "index.html",
    ]
    assert (
        git(remote, "log", "-1", "--format=%an <%ae>|%s", "main")
        == "octo <octo@users.noreply.github.com>|Publish Demo"
    )


@pytest.mark.timeout(180)
def test_a_republish_adds_one_commit_on_top(
    tmp_path: Path, latchkey: FakeLatchkey
) -> None:
    site = _site(tmp_path)
    remote, env = gateway_remote(tmp_path, _REPO)
    first = _run(
        latchkey,
        "push",
        "--site",
        str(site),
        "--repo",
        _REPO,
        "--message",
        "v1",
        env=env,
    )
    assert first.returncode == 0, first.stderr
    (site / "index.html").write_text(
        "<!doctype html><title>Demo</title><h1>Demo v2</h1>\n"
    )
    second = _run(
        latchkey,
        "push",
        "--site",
        str(site),
        "--repo",
        _REPO,
        "--message",
        "v2",
        env=env,
    )
    assert second.returncode == 0, second.stderr
    assert git(remote, "log", "--format=%s", "main").splitlines() == ["v2", "v1"]


@pytest.mark.timeout(180)
def test_a_site_that_fails_the_lint_is_never_committed_or_pushed(
    tmp_path: Path, latchkey: FakeLatchkey
) -> None:
    site = _site(tmp_path)
    (site / "index.html").write_text('<link rel="stylesheet" href="/style.css">\n')
    remote, env = gateway_remote(tmp_path, _REPO)
    result = _run(
        latchkey,
        "push",
        "--site",
        str(site),
        "--repo",
        _REPO,
        "--message",
        "x",
        env=env,
    )
    assert result.returncode == 1
    assert "did not pass `check_site.py lint`" in result.stderr
    assert not (site / ".git").exists()
    assert git(remote, "for-each-ref") == ""


@pytest.mark.timeout(180)
def test_a_site_inside_another_repo_gets_a_history_of_its_own(
    tmp_path: Path, latchkey: FakeLatchkey
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git(workspace, "init", "-q", "-b", "main")
    git(
        workspace,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "workspace",
    )
    before = git(workspace, "rev-parse", "HEAD")
    site = _site(workspace / "data")
    _, env = gateway_remote(tmp_path, _REPO)
    result = _run(
        latchkey,
        "push",
        "--site",
        str(site),
        "--repo",
        _REPO,
        "--message",
        "x",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert git(workspace, "rev-parse", "HEAD") == before
    assert Path(git(site, "rev-parse", "--show-toplevel")).resolve() == site.resolve()


@pytest.mark.timeout(180)
def test_changes_made_on_github_are_never_overwritten(
    tmp_path: Path, latchkey: FakeLatchkey
) -> None:
    site = _site(tmp_path)
    remote, env = gateway_remote(tmp_path, _REPO)
    assert (
        _run(
            latchkey,
            "push",
            "--site",
            str(site),
            "--repo",
            _REPO,
            "--message",
            "v1",
            env=env,
        ).returncode
        == 0
    )
    elsewhere = tmp_path / "elsewhere"
    git(tmp_path, "clone", "-q", str(remote), str(elsewhere))
    (elsewhere / "edited-on-github.txt").write_text("x\n")
    git(elsewhere, "add", "-A")
    git(
        elsewhere,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "-m",
        "edited on GitHub",
    )
    git(elsewhere, "push", "-q", "origin", "main")
    (site / "index.html").write_text("<!doctype html><title>Demo</title><h1>v2</h1>\n")
    result = _run(
        latchkey,
        "push",
        "--site",
        str(site),
        "--repo",
        _REPO,
        "--message",
        "v2",
        env=env,
    )
    assert result.returncode == 1
    assert "never force-push" in result.stderr
    assert git(remote, "log", "-1", "--format=%s", "main") == "edited on GitHub"


# enable, wait, disable, delete ------------------------------------------------------------


@pytest.mark.parametrize("status", [201, 409])
def test_enable_turns_pages_on_from_main_or_accepts_it_already_on(
    latchkey: FakeLatchkey, status: int
) -> None:
    latchkey.answer("POST", f"{_API}/repos/{_REPO}/pages", status, {})
    latchkey.answer(
        "GET",
        f"{_API}/repos/{_REPO}/pages",
        200,
        {"html_url": "https://octo.github.io/demo/"},
    )
    result = _run(latchkey, "enable", "--repo", _REPO)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://octo.github.io/demo/"
    ((_, _, body),) = [call for call in latchkey.calls() if call[0] == "POST"]
    assert body == {"source": {"branch": "main", "path": "/"}}


def test_wait_reports_a_built_site(latchkey: FakeLatchkey) -> None:
    latchkey.answer(
        "GET",
        f"{_API}/repos/{_REPO}/pages/builds/latest",
        200,
        {"status": "built", "commit": "abc"},
    )
    result = _run(latchkey, "wait", "--repo", _REPO, "--interval-seconds", "0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "abc"


def test_wait_reports_githubs_reason_for_a_failed_build(latchkey: FakeLatchkey) -> None:
    latchkey.answer(
        "GET",
        f"{_API}/repos/{_REPO}/pages/builds/latest",
        200,
        {"status": "errored", "error": {"message": "Page build failed."}},
    )
    result = _run(latchkey, "wait", "--repo", _REPO, "--interval-seconds", "0")
    assert result.returncode == 1
    assert "Page build failed." in result.stderr


def test_wait_gives_up_after_its_timeout(latchkey: FakeLatchkey) -> None:
    latchkey.answer(
        "GET", f"{_API}/repos/{_REPO}/pages/builds/latest", 200, {"status": "building"}
    )
    result = _run(
        latchkey,
        "wait",
        "--repo",
        _REPO,
        "--timeout-seconds",
        "0",
        "--interval-seconds",
        "0",
    )
    assert result.returncode == 1
    assert "no finished build" in result.stderr


@pytest.mark.parametrize(("status", "exit_code"), [(204, 0), (404, 0), (403, 1)])
def test_disable_accepts_off_and_already_off(
    latchkey: FakeLatchkey, status: int, exit_code: int
) -> None:
    latchkey.answer("DELETE", f"{_API}/repos/{_REPO}/pages", status, None)
    assert _run(latchkey, "disable", "--repo", _REPO).returncode == exit_code


def test_delete_repo_needs_its_explicit_flag(latchkey: FakeLatchkey) -> None:
    refused = _run(latchkey, "delete-repo", "--repo", _REPO)
    assert refused.returncode == 2
    assert latchkey.calls() == []
    latchkey.answer("DELETE", f"{_API}/repos/{_REPO}", 204, None)
    assert (
        _run(
            latchkey, "delete-repo", "--repo", _REPO, "--yes-delete-forever"
        ).returncode
        == 0
    )
