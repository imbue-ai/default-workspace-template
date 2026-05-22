"""Tests for ``dispatch.py``.

Run via: ``uv run pytest .agents/skills/launch-task/scripts/dispatch_test.py``

The tests inject a recording ``Runner`` so no real ``mngr`` processes are
spawned. We assert on (a) the exact argv lists dispatch.py hands to
subprocess (so the lifecycle contract with ``mngr`` cannot drift silently)
and (b) pre-flight validation.
"""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest

_SCRIPT = Path(__file__).parent / "dispatch.py"
_spec = importlib.util.spec_from_file_location("dispatch", _SCRIPT)
assert _spec is not None and _spec.loader is not None
dispatch_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatch_mod)


@dataclass
class _RecordedCall:
    argv: list[str]
    kwargs: dict[str, Any]


@dataclass
class _StubResult:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(dispatch_mod.Runner):
    """Records every ``run`` call; returns canned results keyed by argv prefix."""

    calls: list[_RecordedCall] = field(default_factory=list)
    _responses: dict[tuple[str, ...], Any] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: Any) -> None:
        self._responses[prefix] = result

    def run(self, argv: Sequence[str], **kwargs):
        argv_list = list(argv)
        self.calls.append(_RecordedCall(argv=argv_list, kwargs=kwargs))
        key = tuple(argv_list[:2])
        canned = self._responses.get(key, _StubResult())
        if isinstance(canned, BaseException):
            raise canned
        return canned


def _make_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create runtime_dir / task_file / artifacts_dir under tmp_path.

    The task file has plain frontmatter (no ``source_artifacts_dir``); tests
    that exercise the artifacts push overwrite it via ``_write_task``.
    """
    runtime = tmp_path / "runtime" / "launch-task" / "demo"
    runtime.mkdir(parents=True)
    task = runtime / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    artifacts = tmp_path / "runtime" / "do-something-new" / "demo"
    artifacts.mkdir(parents=True)
    (artifacts / "sample.json").write_text("{}")
    return runtime, task, artifacts


def _write_task(task: Path, source_artifacts_dir: str | None) -> None:
    """Overwrite ``task`` with frontmatter optionally declaring artifacts."""
    fm = "lead_agent: lead\n"
    if source_artifacts_dir is not None:
        fm += f"source_artifacts_dir: {source_artifacts_dir}\n"
    task.write_text(f"---\n{fm}---\n\nbody\n")


def test_happy_path_no_artifacts(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    argvs = [c.argv for c in runner.calls]
    assert argvs == [
        ["mngr", "create", "demo-worker", "-t", "worker", "--label", "workspace=ws-1"],
        [
            "mngr",
            "push",
            f"demo-worker:{runtime}/",
            "--source",
            f"{runtime}/",
            "--uncommitted-changes=merge",
        ],
        ["mngr", "message", "demo-worker", "--message-file", str(task)],
    ]


def test_source_artifacts_dir_pushed_after_runtime(tmp_path: Path) -> None:
    """A frontmatter ``source_artifacts_dir`` is pushed right after the runtime dir."""
    runtime, task, artifacts = _make_layout(tmp_path)
    _write_task(task, str(artifacts))
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    push_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "push"]]
    assert push_calls == [
        [
            "mngr",
            "push",
            f"demo-worker:{runtime}/",
            "--source",
            f"{runtime}/",
            "--uncommitted-changes=merge",
        ],
        [
            "mngr",
            "push",
            f"demo-worker:{artifacts}/",
            "--source",
            f"{artifacts}/",
            "--uncommitted-changes=merge",
        ],
    ]


def test_source_artifacts_dir_missing_is_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A declared but nonexistent ``source_artifacts_dir`` aborts before launch."""
    runtime, task, _ = _make_layout(tmp_path)
    _write_task(task, str(tmp_path / "no-such-dir"))
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws",
        runner=runner,
    )

    assert rc == 2
    assert runner.calls == []
    assert "source_artifacts_dir" in capsys.readouterr().err


def test_source_artifacts_dir_non_string_is_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-string ``source_artifacts_dir`` value aborts before launch."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text(
        "---\nlead_agent: lead\nsource_artifacts_dir: [a, b]\n---\n\nbody\n"
    )
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws",
        runner=runner,
    )

    assert rc == 2
    assert runner.calls == []
    assert "source_artifacts_dir" in capsys.readouterr().err


def test_malformed_frontmatter_does_not_abort_dispatch(tmp_path: Path) -> None:
    """A task file with no/broken frontmatter dispatches normally with no artifacts
    push -- frontmatter schema validation is the worker's job, not dispatch's."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("no frontmatter here, just a body\n")
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    push_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "push"]]
    assert push_calls == [
        [
            "mngr",
            "push",
            f"demo-worker:{runtime}/",
            "--source",
            f"{runtime}/",
            "--uncommitted-changes=merge",
        ],
    ]


def test_runtime_dir_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=tmp_path / "missing",
        task_file=task,
        workspace="ws",
        runner=runner,
    )
    assert rc == 2
    assert runner.calls == []
    assert "runtime-dir" in capsys.readouterr().err


def test_task_file_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, _, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=runtime / "missing.md",
        workspace="ws",
        runner=runner,
    )
    assert rc == 2
    assert runner.calls == []
    assert "task-file" in capsys.readouterr().err


def test_mngr_failure_is_fatal(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "create"),
        subprocess.CalledProcessError(returncode=1, cmd=["mngr"]),
    )
    with pytest.raises(subprocess.CalledProcessError):
        dispatch_mod.dispatch(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            workspace="ws",
            runner=runner,
        )


def _main_argv(runtime: Path, task: Path) -> list[str]:
    return [
        "--name",
        "x",
        "--template",
        "worker",
        "--runtime-dir",
        str(runtime),
        "--task-file",
        str(task),
    ]


def test_main_uses_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "alpha")

    rc = dispatch_mod.main(_main_argv(runtime, task), runner=runner)

    assert rc == 0
    create_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "create"]]
    assert create_calls, runner.calls
    assert "workspace=alpha" in create_calls[0]


def test_main_workspace_defaults_when_env_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    monkeypatch.delenv("MINDS_WORKSPACE_NAME", raising=False)

    rc = dispatch_mod.main(_main_argv(runtime, task), runner=runner)

    assert rc == 0
    create_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "create"]]
    assert "workspace=default" in create_calls[0]


def _make_state_dir_with_converter(tmp_path: Path) -> Path:
    """Create a state_dir containing a stub common_transcript.sh."""
    state_dir = tmp_path / "state"
    (state_dir / "commands").mkdir(parents=True)
    script = state_dir / "commands" / "common_transcript.sh"
    script.write_text("#!/usr/bin/env bash\n:\n")
    return state_dir


def test_common_transcript_flushed_before_message_send(tmp_path: Path) -> None:
    """When state_dir has the converter, dispatch flushes it right before the message."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    argvs = [c.argv for c in runner.calls]
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    assert argvs == [
        ["mngr", "create", "demo-worker", "-t", "worker", "--label", "workspace=ws-1"],
        [
            "mngr",
            "push",
            f"demo-worker:{runtime}/",
            "--source",
            f"{runtime}/",
            "--uncommitted-changes=merge",
        ],
        [expected_script, "--single-pass"],
        ["mngr", "message", "demo-worker", "--message-file", str(task)],
    ]


def test_common_transcript_skipped_when_state_dir_is_none(tmp_path: Path) -> None:
    """No converter call when state_dir is None (tests / non-mngr envs)."""
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=None,
        runner=runner,
    )

    assert rc == 0
    assert not any(
        "common_transcript.sh" in arg for call in runner.calls for arg in call.argv
    )


def test_common_transcript_skipped_when_script_missing(tmp_path: Path) -> None:
    """No converter call when the script isn't installed (non-claude agents)."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = tmp_path / "state-without-converter"
    state_dir.mkdir()
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    assert not any(
        "common_transcript.sh" in arg for call in runner.calls for arg in call.argv
    )


def test_common_transcript_failure_does_not_abort_dispatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-zero converter exit must NOT abort dispatch (worker is mid-launch)."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    runner.respond((expected_script, "--single-pass"), _StubResult(returncode=2))

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    # The subsequent message send must still run.
    assert [c.argv for c in runner.calls][-1] == [
        "mngr",
        "message",
        "demo-worker",
        "--message-file",
        str(task),
    ]
    err = capsys.readouterr().err
    assert "common_transcript.sh" in err
    assert "exited 2" in err


def test_main_picks_up_state_dir_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() reads MNGR_AGENT_STATE_DIR and threads it into dispatch."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    rc = dispatch_mod.main(_main_argv(runtime, task), runner=runner)

    assert rc == 0
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    flush_calls = [
        c.argv for c in runner.calls if c.argv == [expected_script, "--single-pass"]
    ]
    assert len(flush_calls) == 1
