"""Tests for ``preview_layout.py``.

Run via: ``uv run pytest .agents/skills/update-system-interface/scripts/preview_layout_test.py``

The wrapper's only logic is resolving the preview's inner port from its state
file and projecting it into a ``MINDS_WORKSPACE_SERVER_URL`` for ``layout.py``;
the subprocess hand-off itself is a thin shell. These tests cover the resolution
and the error paths (no preview, malformed state, missing/invalid port).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "preview_layout.py"
_spec = importlib.util.spec_from_file_location("preview_layout", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

_SLUG = "my-change"


def _write_state(repo_root: Path, payload: dict) -> Path:
    state_path = mod.preview_state_path(repo_root, _SLUG)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload))
    return state_path


def test_preview_base_url_uses_inner_port() -> None:
    """The base URL targets the inner instance's port (the layout endpoint)."""
    assert mod.preview_base_url({"inner_port": 51234, "wrapper_port": 8}) == "http://127.0.0.1:51234"


def test_preview_base_url_rejects_missing_port() -> None:
    with pytest.raises(mod.PreviewLayoutError):
        mod.preview_base_url({"wrapper_port": 8200})


def test_preview_base_url_rejects_non_int_port() -> None:
    with pytest.raises(mod.PreviewLayoutError):
        mod.preview_base_url({"inner_port": "51234"})


def test_preview_base_url_rejects_bool_port() -> None:
    """``True`` is an int subclass but never a valid port -- must be rejected."""
    with pytest.raises(mod.PreviewLayoutError):
        mod.preview_base_url({"inner_port": True})


def test_load_preview_state_round_trips(tmp_path: Path) -> None:
    _write_state(tmp_path, {"inner_port": 49999})
    state = mod.load_preview_state(mod.preview_state_path(tmp_path, _SLUG))
    assert state["inner_port"] == 49999


def test_load_preview_state_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(mod.PreviewLayoutError):
        mod.load_preview_state(mod.preview_state_path(tmp_path, _SLUG))


def test_load_preview_state_malformed_json_raises(tmp_path: Path) -> None:
    state_path = mod.preview_state_path(tmp_path, _SLUG)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json")
    with pytest.raises(mod.PreviewLayoutError):
        mod.load_preview_state(state_path)


def test_run_without_layout_args_is_an_error(tmp_path: Path) -> None:
    """A bare invocation with no layout subcommand fails before touching state."""
    assert mod.run(_SLUG, [], tmp_path) == 1


def test_run_with_no_preview_state_returns_error(tmp_path: Path) -> None:
    assert mod.run(_SLUG, ["inspect"], tmp_path) == 1


def test_run_invokes_layout_against_the_preview_port(tmp_path: Path) -> None:
    """``run`` points MINDS_WORKSPACE_SERVER_URL at the inner port and forwards args.

    A fake runner is injected so we observe the argv / env / cwd handed to
    ``layout.py`` and the propagated return code without launching a server.
    """
    _write_state(tmp_path, {"inner_port": 51000})

    captured: dict = {}

    def _fake_runner(argv: list[str], cwd: str, env: dict[str, str]) -> int:
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env"] = env
        return 3

    code = mod.run(_SLUG, ["focus", "chat:alice"], tmp_path, runner=_fake_runner)

    assert code == 3  # layout.py's exit code is passed straight through
    assert captured["env"][mod.ENV_WORKSPACE_URL] == "http://127.0.0.1:51000"
    assert captured["cwd"] == str(tmp_path)
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1].endswith(mod.LAYOUT_SCRIPT_RELPATH)
    assert captured["argv"][-2:] == ["focus", "chat:alice"]
