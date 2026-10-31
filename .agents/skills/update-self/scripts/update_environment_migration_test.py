"""Real offline installs: renamed RECORD owners and already-hollow environments."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import update_environment
from update_runtime import Runner

WHEELS = Path(__file__).resolve().parents[1] / "tests/fixtures/overlay-rename"


def _run(argv, root, env):
    return subprocess.run(
        argv, cwd=root, env=env, capture_output=True, text=True, timeout=90
    )


def _checked(argv, root, env):
    result = _run(argv, root, env)
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("initial", ["coowned", "hollow", "old-only", "clean"])
def test_root_refresh_restores_real_renamed_modules(tmp_path, monkeypatch, initial):
    uv = os.environ.get("UPDATE_SELF_TEST_UV") or shutil.which("uv")
    assert uv, "uv is required for the real migration fixture"
    root = tmp_path / "project"
    root.mkdir()
    venv = root / ".venv"
    python = str(venv / "bin/python")
    env = {
        **os.environ,
        "UV_CACHE_DIR": str(tmp_path / "cache"),
        "UV_PROJECT_ENVIRONMENT": str(venv),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_OFFLINE": "1",
        "UV_LINK_MODE": "copy",
    }
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    old = next(WHEELS.glob("overlay-*.whl"))
    new = next(WHEELS.glob("imbue_overlay-*.whl"))
    (root / "pyproject.toml").write_text(
        '[project]\nname="migration-fixture"\nversion="0.0.0"\n'
        'requires-python=">=3.12"\ndependencies=['
        + json.dumps("imbue-overlay @ " + new.as_uri())
        + "]\n[tool.uv]\npackage=false\n"
    )
    _checked([uv, "venv", "--python", sys._base_executable, str(venv)], root, env)
    if initial != "clean":
        _checked([uv, "pip", "install", "--python", python, str(old)], root, env)
    if initial in {"coowned", "hollow"}:
        _checked(
            [uv, "run", "python", "-c", "from imbue.overlay.markers import StaticDict"],
            root,
            env,
        )
        owners = _checked(
            [
                python,
                "-c",
                "from importlib.metadata import version;print(version('overlay'),version('imbue-overlay'))",
            ],
            root,
            env,
        )
        assert owners.stdout.strip() == "0.1.0 0.1.0"
    _checked([uv, "lock"], root, env)
    if initial == "hollow":
        _checked([uv, "sync", "--all-packages", "--frozen"], root, env)
        # Metadata audits succeed twice, while the real runtime import fails.
        _checked([uv, "sync", "--all-packages", "--frozen"], root, env)
        broken = _run(
            [python, "-c", "from imbue.overlay.markers import StaticDict"], root, env
        )
        assert broken.returncode != 0
        assert (
            "No module named 'imbue.overlay'" in broken.stderr
            or "No module named 'imbue'" in broken.stderr
        )
    lock_before = (root / "uv.lock").read_bytes()
    prefix_before = _checked(
        [python, "-c", "import sys;print(sys.prefix)"], root, env
    ).stdout

    class PrivateRunner(Runner):
        def run(self, argv, **kwargs):
            assert list(argv[:2]) == ["uv", "sync"]
            return super().run([uv, *argv[1:]], **{**kwargs, "env": env})

    # Only the independent mngr-tool/asset refreshes are replaced; the production
    # root refresh itself runs the real uv command, from the final project prefix.
    monkeypatch.setattr(update_environment, "_mngr_base_arguments", lambda root: [])
    monkeypatch.setattr(update_environment, "_reinstall_tool", lambda *a, **k: None)
    monkeypatch.setattr(update_environment, "_fetch_mngr_assets", lambda *a, **k: None)
    update_environment.refresh_backend_dependencies(
        root,
        PrivateRunner(),
        lambda argv: argv,
        {update_environment.MNGR_TOOL_NAME: None},
        timeout=90,
    )
    result = _checked(
        [
            python,
            "-c",
            "from imbue.overlay.markers import StaticDict;import sys;print(sys.prefix)",
        ],
        root,
        env,
    )
    assert result.stdout == prefix_before
    assert (root / "uv.lock").read_bytes() == lock_before
    absent = _run(
        [python, "-c", "from importlib.metadata import version;version('overlay')"],
        root,
        env,
    )
    assert absent.returncode != 0
    _checked(
        [
            python,
            "-c",
            "from importlib.metadata import version;assert version('imbue-overlay')=='0.1.0'",
        ],
        root,
        env,
    )
