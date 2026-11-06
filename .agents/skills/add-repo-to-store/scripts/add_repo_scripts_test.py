"""Unit tests for the add-repo-to-store helpers: reading a pasted link, the license fallback, and what the hand-over
refuses before it reaches the store. The look-up's GitHub calls and the store's answer are exercised for real when
the skill runs; the store's side of the hand-over is tested in system/apps/app_store/src/app_store/repo_adds_test.py.
"""

import base64
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parent
_COMMIT = "d1cc495a966cafaed3e50dad73cd88e790031704"
_REPOSITORY = "https://github.com/simonw/datasette"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", _SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


look_up_repo = _load("look_up_repo")
hand_over = _load("hand_over")


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://github.com/simonw/datasette", ("simonw", "datasette", "")),
        ("github.com/excalidraw/excalidraw.git", ("excalidraw", "excalidraw", "")),
        ("https://github.com/simonw/datasette/", ("simonw", "datasette", "")),
        (
            "https://github.com/TiddlyWiki/TiddlyWiki5/tree/v5.3.6",
            ("TiddlyWiki", "TiddlyWiki5", "v5.3.6"),
        ),
        ("https://github.com/a/b/tree/feature/x", ("a", "b", "feature/x")),
        (
            f"https://github.com/simonw/datasette/commit/{_COMMIT}",
            ("simonw", "datasette", _COMMIT),
        ),
        (
            "https://github.com/nicolargo/glances/releases/tag/v4.5.8",
            ("nicolargo", "glances", "v4.5.8"),
        ),
        ("https://github.com/simonw/datasette?tab=readme", ("simonw", "datasette", "")),
    ],
)
def test_a_pasted_link_names_its_repository_and_ref(
    link: str, expected: tuple[str, str, str]
) -> None:
    assert look_up_repo.parse_link(link) == expected


@pytest.mark.parametrize(
    "link", ["https://gitlab.com/a/b", "https://github.com/onlyowner", "datasette"]
)
def test_a_link_that_is_not_a_github_repository_is_refused(link: str) -> None:
    with pytest.raises(
        look_up_repo.LookUpError, match="not a link to a GitHub repository"
    ):
        look_up_repo.parse_link(link)


def test_the_license_comes_from_the_package_file_when_github_cannot_tell() -> None:
    assert look_up_repo.license_from_files(
        {"pyproject.toml": 'name = "x"\nlicense = "LGPL-3.0-only"\n'}
    ) == ("LGPL-3.0-only")
    assert (
        look_up_repo.license_from_files(
            {"pyproject.toml": 'license = {text = "MIT"}\n'}
        )
        == "MIT"
    )
    assert look_up_repo.license_from_files(
        {"package.json": json.dumps({"license": "BSD-3-Clause"})}
    ) == ("BSD-3-Clause")
    assert look_up_repo.license_from_files({"package.json": "not json"}) == "Unknown"


def _file(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": 1,
        "title": "Datasette Latest",
        "description": "Explore data.",
        "repository": _REPOSITORY,
        "commit": _COMMIT,
        "recipe": {"kind": "python_git", "repository": _REPOSITORY, "commit": _COMMIT},
        "launch": {
            "argv": [
                "{env_dir}/bin/datasette",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "{port}",
            ]
        },
        "resources": {"disk_mb": 50},
    }
    document.update(overrides)
    return document


def _trial(tmp_path: Path, outcome: str, tried: dict[str, Any]) -> Path:
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "install-file.json").write_text(json.dumps(tried))
    (trial / "result.json").write_text(
        json.dumps({"outcome": outcome, "what_happened": "it stopped"})
    )
    return trial


def _hand_over(
    tmp_path: Path,
    file: dict[str, Any],
    trial: Path,
    capsys: pytest.CaptureFixture[str],
) -> Any:
    path = tmp_path / "file.json"
    path.write_text(json.dumps(file))
    status = hand_over.main([str(path), "--trial", str(trial)])
    return status, json.loads(capsys.readouterr().out)


def test_a_file_whose_trial_failed_is_not_handed_over(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, answer = _hand_over(
        tmp_path, _file(), _trial(tmp_path, "START_FAILED", _file()), capsys
    )
    assert status == 1
    assert answer == {
        "is_handed_over": False,
        "what_happened": "That trial didn't work (START_FAILED): it stopped",
    }


def test_a_file_that_runs_something_else_than_its_trial_is_not_handed_over(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other_launch = _file(
        launch={
            "argv": [
                "{env_dir}/bin/datasette",
                "--host",
                "127.0.0.1",
                "--port",
                "{port}",
            ]
        }
    )
    status, answer = _hand_over(
        tmp_path, other_launch, _trial(tmp_path, "WORKS", _file()), capsys
    )
    assert status == 1
    assert "ran something different" in answer["what_happened"]


def test_new_words_and_figures_after_the_trial_are_fine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hand_over, "store_url", lambda: None)
    measured = _file(
        resources={"disk_mb": 30, "memory_idle_mb": 90},
        basis="Tried: 27 MB.",
        about="Explores data.",
    )
    status, answer = _hand_over(
        tmp_path, measured, _trial(tmp_path, "WORKS", _file()), capsys
    )
    assert status == 1
    assert (
        answer["what_happened"] == "The App Store isn't registered in this workspace."
    )


def test_a_file_that_breaks_a_rule_is_not_handed_over(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, answer = _hand_over(
        tmp_path, _file(commit="main"), _trial(tmp_path, "WORKS", _file()), capsys
    )
    assert status == 1
    assert "breaks a rule" in answer["what_happened"]


class _StoreAnswer:
    status_code = 201
    headers = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return {"pending": {"id": "p1"}}


def test_the_hand_over_sends_the_screenshot_and_what_it_replaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    def post(url: str, json: dict[str, Any], timeout: float) -> _StoreAnswer:
        sent.append({"url": url, **json})
        return _StoreAnswer()

    monkeypatch.setattr(hand_over, "store_url", lambda: "http://127.0.0.1:8060")
    monkeypatch.setattr(hand_over.httpx, "post", post)
    monkeypatch.setenv("MINDS_CHAT_ID", "chat-7")
    trial = _trial(tmp_path, "WORKS", _file())
    (trial / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")
    path = tmp_path / "file.json"
    path.write_text(json.dumps(_file()))

    assert (
        hand_over.main(
            [str(path), "--trial", str(trial), "--replaces", "datasettelatest"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "is_handed_over": True,
        "pending": {"id": "p1"},
    }
    assert sent[0]["url"] == "http://127.0.0.1:8060/api/repo-adds"
    assert (sent[0]["chat_id"], sent[0]["replaces"]) == ("chat-7", "datasettelatest")
    assert (
        base64.b64decode(sent[0]["screenshot_png_base64"]) == b"\x89PNG\r\n\x1a\nimage"
    )

    (trial / "screenshot.png").unlink()
    assert hand_over.main([str(path), "--trial", str(trial)]) == 0
    assert (sent[1]["replaces"], sent[1]["screenshot_png_base64"]) == ("", "")
