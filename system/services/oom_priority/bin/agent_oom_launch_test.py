"""Tests for the agent launch wrapper.

The wrapper sets its own memory-shedding band and records its pid, then execs the
harness named by its FIRST argument with the args mngr appended. We verify the band
classification directly, and the tag+exec+arg-forwarding end to end via a subprocess
with a fake harness binary on PATH (so the real ``execvp`` runs without launching
the real thing). Parametrized over every harness that uses the wrapper, since the
argv[1] form treats them all alike; a bare fake ``codex`` is not an npm install, so
it takes the same plain exec. An npm-installed codex is the exception: a fake npm
layout checks that the wrapper execs its native binary, so the pid earlyoom sheds
maps back to the agent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "agent_oom_launch.py"
_spec = importlib.util.spec_from_file_location("agent_oom_launch", _SCRIPT)
assert _spec is not None and _spec.loader is not None
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)

from oom_priority.ledger import has_pending_shed, read_records
from oom_priority.registry import lookup_agent

_SHED_HOOK = Path(__file__).parent / "earlyoom_record_shed.py"


def _write_agent_record(
    host_dir: Path, name: str, *, is_worker: bool, labels: dict | None = None
) -> None:
    """Seed the host agent record the identity checks read to classify ``name``."""
    agent_dir = host_dir / "agents" / "id"
    agent_dir.mkdir(parents=True)
    resolved = (
        labels
        if labels is not None
        else ({"agent_created": "true"} if is_worker else {"user_created": "true"})
    )
    (agent_dir / "data.json").write_text(json.dumps({"name": name, "labels": resolved}))


def test_tag_self_classifies_worker_into_the_worker_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker (``agent_created`` label) records the worker band; the registry
    entry maps this process's pid back to it as a worker."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    host = tmp_path / "host"
    _write_agent_record(host, "w1", is_worker=True)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MNGR_AGENT_NAME", "w1")

    wrapper._tag_self()

    entry = lookup_agent(os.getpid())
    assert entry is not None
    assert entry["agent_name"] == "w1"
    assert entry["is_worker"] is True


def test_tag_self_records_a_chat_as_non_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat (no ``agent_created`` label) is recorded as a non-worker so the
    kill hook can tell an agent shed from a subprocess shed."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    host = tmp_path / "host"
    _write_agent_record(host, "u1", is_worker=False)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MNGR_AGENT_NAME", "u1")

    wrapper._tag_self()

    entry = lookup_agent(os.getpid())
    assert entry is not None
    assert entry["is_worker"] is False


def test_band_for_pins_the_primary_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary (services) agent is pinned to the never-shed PRIMARY_AGENT band,
    ahead of the worker/user classification."""
    host = tmp_path / "host"
    _write_agent_record(
        host,
        "services",
        is_worker=False,
        labels={"is_primary": "true", "user_created": "true"},
    )
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    # is_primary wins even though the record also carries user_created.
    assert wrapper._band_for("services") == wrapper.bands.PRIMARY_AGENT


def test_band_for_starts_a_chat_expendable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat (user_created) launches at the most-expendable chat band, to be
    protected later by live UI engagement -- not at the protected floor."""
    host = tmp_path / "host"
    _write_agent_record(host, "c1", is_worker=False, labels={"user_created": "true"})
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    assert wrapper._band_for("c1") == wrapper.bands.CHAT_AGENT_BASE


def test_band_for_puts_workers_and_unidentifiable_agents_in_the_worker_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker, and an agent whose record cannot be read to classify it, both
    land at the least-protected agent tier -- we must not shield an agent we
    cannot identify."""
    host = tmp_path / "host"
    _write_agent_record(host, "w1", is_worker=True, labels={"agent_created": "true"})
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    assert wrapper._band_for("w1") == wrapper.bands.WORKER_AGENT
    # No record for "ghost" -> unclassifiable -> least protected.
    assert wrapper._band_for("ghost") == wrapper.bands.WORKER_AGENT


def test_tag_self_records_the_agent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stable agent id is recorded so the prioritizer can resolve the pid by id."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    host = tmp_path / "host"
    _write_agent_record(host, "u1", is_worker=False)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MNGR_AGENT_NAME", "u1")
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-xyz")

    wrapper._tag_self()

    entry = lookup_agent(os.getpid())
    assert entry is not None
    assert entry["agent_id"] == "agent-xyz"


def test_tag_self_noops_without_agent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``MNGR_AGENT_NAME`` -> nothing is recorded (the band check is skipped)."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)

    wrapper._tag_self()

    assert lookup_agent(os.getpid()) is None


def _fake_harness_dir(tmp_path: Path, args_out: Path, binary: str = "claude") -> Path:
    """A directory holding a fake ``binary`` that records the args it was exec'd
    with, so the wrapper's real ``execvp`` can be observed without the real harness."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / binary
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > ' + str(args_out) + "\n")
    fake.chmod(0o755)
    return bindir


@pytest.mark.parametrize("binary", ["claude", "codex", "pi", "opencode", "agy"])
def test_wrapper_execs_named_harness_forwarding_args_after_tagging(
    tmp_path: Path, binary: str
) -> None:
    """End to end: the wrapper records its pid, then execs the harness named by its
    first argument with exactly the args after it (the flags mngr splices after the
    command base). argv[1] is consumed, never forwarded."""
    args_out = tmp_path / "harness_args.txt"
    bindir = _fake_harness_dir(tmp_path, args_out, binary)
    runtime = tmp_path / "rt"
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OOM_PRIORITY_RUNTIME_DIR": str(runtime),
        "MNGR_AGENT_NAME": "u1",
    }
    env.pop("MNGR_HOST_DIR", None)  # no host records -> classified as a user agent

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), binary, "--settings", "foo", "--resume", "bar"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert args_out.read_text().splitlines() == ["--settings", "foo", "--resume", "bar"]
    # The wrapper recorded its own pid (which became the harness's) as agent u1.
    # OOM_PRIORITY_RUNTIME_DIR is the runtime dir itself (the override is used
    # verbatim), so the registry lives directly under it.
    pid_files = list((runtime / "agent_pids").glob("*.json"))
    assert len(pid_files) == 1
    assert json.loads(pid_files[0].read_text())["agent_name"] == "u1"


def test_wrapper_still_execs_harness_when_tagging_fails(tmp_path: Path) -> None:
    """A tagging failure must never block the agent: even when the registry path
    is unwritable, the wrapper still execs the harness with its args."""
    args_out = tmp_path / "harness_args.txt"
    bindir = _fake_harness_dir(tmp_path, args_out)
    # Point the runtime dir under a regular file so the registry mkdir raises.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OOM_PRIORITY_RUNTIME_DIR": str(blocker / "rt"),
        "MNGR_AGENT_NAME": "u1",
    }
    env.pop("MNGR_HOST_DIR", None)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "claude", "--session-id", "abc"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert args_out.read_text().splitlines() == ["--session-id", "abc"]


def test_wrapper_refuses_with_no_harness_argument(tmp_path: Path) -> None:
    """A misconfigured ``command`` (wrapper with no binary after it) must fail loudly
    rather than exec whatever mngr's first spliced flag happens to be."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        env={**os.environ, "OOM_PRIORITY_RUNTIME_DIR": str(tmp_path / "rt")},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "missing harness binary" in result.stderr


_NPM_CODEX_NATIVE = (
    "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
)


def _fake_npm_codex_install(
    prefix: Path, native_report: Path, native_in_package: str = _NPM_CODEX_NATIVE
) -> Path:
    """A global npm install of codex laid out like the real one, returning its
    package root. ``<prefix>/bin/codex`` links to the package's ``bin/codex.js``,
    which runs the native binary at ``native_in_package`` as a child process, as
    the real entry point does. The native binary writes its own pid, the codex
    install environment it was given, and its args to ``native_report``."""
    package_root = prefix / "lib" / "node_modules" / "@openai" / "codex"
    native = package_root / native_in_package
    native.parent.mkdir(parents=True)
    native.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$$" "$CODEX_MANAGED_BY_NPM" "$CODEX_MANAGED_PACKAGE_ROOT" "$@"'
        f" > {native_report}\n"
    )
    native.chmod(0o755)
    entry_point = package_root / "bin" / "codex.js"
    entry_point.parent.mkdir(parents=True)
    # The trailing exit keeps the shell from exec'ing its last command in place.
    entry_point.write_text(f'#!/bin/sh\n"{native}" "$@"\nexit $?\n')
    entry_point.chmod(0o755)
    (prefix / "bin").mkdir()
    (prefix / "bin" / "codex").symlink_to(
        Path("..") / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    )
    return package_root


def _npm_codex_launch_env(prefix: Path, runtime: Path, host: Path) -> dict[str, str]:
    """The wrapper's environment for agent ``w1``, with the fake npm install from
    ``prefix`` first on PATH and no inherited codex-install or earlyoom variables."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODEX_MANAGED_", "EARLYOOM_"))
    }
    env.update(
        PATH=f"{prefix / 'bin'}{os.pathsep}{os.environ['PATH']}",
        OOM_PRIORITY_RUNTIME_DIR=str(runtime),
        MNGR_HOST_DIR=str(host),
        MNGR_AGENT_NAME="w1",
    )
    return env


def _launch_codex(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "codex", "app-server", "--listen", "stdio://"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_a_shed_npm_installed_codex_is_attributed_to_its_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pid earlyoom kills when it sheds a codex agent is the pid the wrapper
    registered, so the kill hook records the shed against that agent and the
    worker report watcher's ledger check sees it."""
    runtime = tmp_path / "rt"
    host = tmp_path / "host"
    _write_agent_record(host, "w1", is_worker=True)
    native_report = tmp_path / "native_report.txt"
    package_root = _fake_npm_codex_install(tmp_path / "npm", native_report)
    env = _npm_codex_launch_env(tmp_path / "npm", runtime, host)

    launch = _launch_codex(env)
    assert launch.returncode == 0, launch.stderr
    harness_pid, managed_by_npm, managed_package_root, *harness_args = (
        native_report.read_text().splitlines()
    )

    # Shed the running harness as earlyoom does: its kill hook gets the victim's pid.
    subprocess.run(
        [sys.executable, str(_SHED_HOOK)],
        env={
            **env,
            "EARLYOOM_PID": harness_pid,
            "EARLYOOM_UID": "0",
            "EARLYOOM_NAME": "codex",
        },
        check=True,
        timeout=30,
    )

    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(runtime))
    (record,) = read_records()
    assert (record["agent_name"], record["is_worker"]) == ("w1", True)
    assert has_pending_shed("w1")
    assert harness_args == ["app-server", "--listen", "stdio://"]
    assert (managed_by_npm, managed_package_root) == ("1", str(package_root.resolve()))


def test_npm_codex_whose_native_binary_moved_still_launches_through_the_entry_point(
    tmp_path: Path,
) -> None:
    """A codex version that moves the native binary out of the layout the wrapper
    knows must not stop the agent: the wrapper falls back to the npm entry point,
    which launches codex as its child (under a pid the wrapper did not register)."""
    runtime = tmp_path / "rt"
    native_report = tmp_path / "native_report.txt"
    _fake_npm_codex_install(
        tmp_path / "npm",
        native_report,
        native_in_package="node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex/codex",
    )

    launch = _launch_codex(
        _npm_codex_launch_env(tmp_path / "npm", runtime, tmp_path / "host")
    )

    assert launch.returncode == 0, launch.stderr
    harness_pid, _, _, *harness_args = native_report.read_text().splitlines()
    assert harness_args == ["app-server", "--listen", "stdio://"]
    registered_pids = [
        int(entry.stem) for entry in (runtime / "agent_pids").glob("*.json")
    ]
    assert len(registered_pids) == 1
    assert int(harness_pid) not in registered_pids
