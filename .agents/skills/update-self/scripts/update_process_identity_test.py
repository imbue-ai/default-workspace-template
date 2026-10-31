"""Linux real-child contract: official recovery respects a running apply owner."""

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import update_apply_contract as contract

SCRIPTS = Path(__file__).resolve().parent
STARTUP = """import os, sys, time
from pathlib import Path
def pause(frame, event, arg):
    if event == "call" and frame.f_code.co_name == "main" and Path(frame.f_code.co_filename).name == "update_self.py":
        sys.settrace(None)
        Path(os.environ["READY"]).write_text(str(os.getpid()))
        while not Path(os.environ["RELEASE"]).exists():
            time.sleep(0.02)
        raise SystemExit(0)
    return pause
sys.settrace(pause)
"""


def _run(argv, root, env):
    result = subprocess.run(
        argv, cwd=root, env=env, text=True, capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def private_repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    env.pop("PYTHONPATH", None)
    _run(["git", "init", "-q"], root, env)
    _run(["git", "config", "core.hooksPath", "/dev/null"], root, env)
    (root / "canary").write_text("previous")
    _run(["git", "add", "."], root, env)
    _run(["git", "commit", "-qm", "Previous tree"], root, env)
    head = _run(["git", "rev-parse", "HEAD"], root, env).strip()
    return root, env, head


def _marker(root, head, pid, snapshots=()):
    marker = contract.ApplyMarker(
        dri_agent="private-fixture",
        rollback_to=head,
        merge_ref=head,
        target_ref="fixture",
        ff_only=True,
        worker_bundles=None,
        phase=contract.PHASE_SNAPSHOTTED,
        pid=pid,
        started_at=0,
        updated_at=0,
        snapshots=list(snapshots),
    )
    path = contract.marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker.to_json())


def _recover(root, env):
    return _run(
        [
            sys.executable,
            str(SCRIPTS / "update_self.py"),
            "recover",
            "--repo-root",
            str(root),
            "--if-stale",
            "--grace-seconds",
            "0",
            "--no-restart",
        ],
        root,
        env,
    )


def _hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.skipif(
    not Path("/proc/self/cmdline").exists(), reason="Linux argv identity contract"
)
@pytest.mark.parametrize("launcher", ["official", "wrapper", "bad-wrapper"])
def test_real_launcher_identity_and_separate_recovery(tmp_path, private_repo, launcher):
    root, env, head = private_repo
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text(STARTUP)
    ready, release = tmp_path / "ready", tmp_path / "release"
    child_env = {
        **env,
        "PYTHONPATH": os.pathsep.join([str(startup), str(SCRIPTS)]),
        "READY": str(ready),
        "RELEASE": str(release),
    }
    script = SCRIPTS / "update_self.py"
    if launcher != "official":
        script = tmp_path / (
            "update_self_wrapper.py" if launcher == "wrapper" else "apply_wrapper.py"
        )
        script.write_text("from update_self import main\nraise SystemExit(main())\n")
    child = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "apply",
            "--repo-root",
            str(root),
            "--merge-ref",
            head,
            "--target-ref",
            "fixture",
            "--ff-only",
        ],
        cwd=root,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert child.poll() is None, child.communicate()
            assert time.monotonic() < deadline, (
                "child never reached official entrypoint"
            )
            time.sleep(0.02)
        assert ready.read_text() == str(child.pid)
        assert contract.default_is_pid_a_live_apply(child.pid) == (
            launcher != "bad-wrapper"
        )
        if launcher != "bad-wrapper":
            _marker(root, head, child.pid)
            before = _hashes(root)
            _recover(root, env)  # Real separate official CLI; no startup trace.
            assert child.poll() is None
            assert _hashes(root) == before
        release.touch()
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize("owner", ["dead", "unrelated"])
def test_stale_or_reused_owner_allows_real_private_recovery(
    tmp_path, private_repo, owner
):
    root, env, head = private_repo
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        if owner == "dead":
            child.terminate()
            child.wait(timeout=10)
        assert not contract.default_is_pid_a_live_apply(child.pid)
        (root / "canary").write_text("updated")
        _run(["git", "add", "."], root, env)
        _run(["git", "commit", "-qm", "Interrupted tree"], root, env)
        source, saved = root / ".venv", tmp_path / "saved"
        source.mkdir()
        saved.mkdir()
        (source / "runtime").write_text("damaged")
        (saved / "runtime").write_text("known-good")
        _marker(
            root,
            head,
            child.pid,
            [contract.SnapshotRecord("venv", str(source), str(saved))],
        )
        _recover(root, env)
        assert (root / "canary").read_text() == "previous"
        assert (source / "runtime").read_text() == "known-good"
        assert not contract.marker_path(root).exists()
        if owner == "unrelated":
            assert child.poll() is None
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
