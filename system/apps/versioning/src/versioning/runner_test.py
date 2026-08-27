import importlib
import json
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest

import versioning.runner


@pytest.fixture
def client(
    scratch_repo: Path,
    commit_app_file: Callable[[str, str, str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[object]:
    """The Flask app wired to a scratch repo with one committed app, via the env overrides."""
    commit_app_file("news", "runner.py", "print('hello')\n", "news: first build")
    monkeypatch.setenv("VERSIONING_REPO_ROOT", str(scratch_repo))
    monkeypatch.setenv("VERSIONING_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VERSIONING_APPS_TOML", str(tmp_path / "missing-apps.toml"))
    runner = importlib.reload(versioning.runner)
    yield runner.app.test_client()
    # Re-derive module globals from the real environment for any later import.
    monkeypatch.undo()
    importlib.reload(versioning.runner)


def _news_sha(client: object) -> str:
    payload = json.loads(client.get("/api/app/news/history").data)
    return payload["nodes"][0]["sha"]


def test_health(client) -> None:
    assert json.loads(client.get("/health").data) == {"status": "ok"}


def test_index_redirects_to_first_app_timeline(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert b"__APP_NAME__" not in response.data


def test_timeline_page_serves_html_and_404s_unknown_app(client) -> None:
    assert client.get("/app/news").status_code == 200
    assert client.get("/app/nope").status_code == 404


def test_assets_are_served(client) -> None:
    assert client.get("/assets/timeline.js").status_code == 200
    assert client.get("/assets/purify.min.js").status_code == 200


def test_list_apps_discovers_the_scratch_app(client) -> None:
    payload = json.loads(client.get("/api/apps").data)
    assert [a["name"] for a in payload["apps"]] == ["news"]


def test_history_payload_carries_labels_sizes_and_restorability(client) -> None:
    payload = json.loads(client.get("/api/app/news/history").data)
    assert payload["is_restorable"] is True
    node = payload["nodes"][0]
    assert node["is_current"] is True
    assert node["when_label"]
    assert node["short_when_label"]
    assert node["dot_diameter_px"] > 0
    # The scratch commit records no kind, so it falls back to the generic noun.
    assert node["phrase"] == "A tiny change"
    assert json.loads(client.get("/api/app/nope/history").data)["error"]


def test_diff_endpoint_returns_files_and_diff(client) -> None:
    sha = _news_sha(client)
    payload = json.loads(client.get(f"/api/app/news/diff/{sha}").data)
    assert payload["sha"] == sha
    assert [f["display_path"] for f in payload["files"]] == ["runner.py"]
    assert "hello" in payload["diff"]
    assert client.get("/api/app/news/diff/" + "0" * 40).status_code == 404


def test_restore_validates_body_and_previews(client) -> None:
    assert client.post("/api/app/news/restore", json={}).status_code == 400
    sha = _news_sha(client)
    preview = json.loads(client.post("/api/app/news/restore", json={"sha": sha, "mode": "preview"}).data)
    assert preview["target_sha"] == sha
    assert preview["changed_file_count"] == 0
    assert client.post("/api/app/news/restore", json={"sha": "0" * 40, "mode": "preview"}).status_code == 404


def test_summary_endpoint_404s_unknown_version(client) -> None:
    assert client.post("/api/app/news/summary/" + "0" * 40).status_code == 404


def test_assist_validates_body_and_unknown_job(client) -> None:
    assert client.post("/api/app/news/assist", json={}).status_code == 400
    assert client.post("/api/app/news/assist", json={"sha": "0" * 40, "message": "hi"}).status_code == 404
    assert client.get("/api/app/news/assist/nojob").status_code == 404
