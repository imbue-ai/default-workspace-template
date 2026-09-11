"""Tests for ``create_worker.py``.

Run via: ``uv run pytest .agents/skills/launch-task/scripts/create_worker_test.py``

The ``launch`` tests inject a recording ``Runner`` so no real ``mngr``
processes are spawned. We assert on (a) the exact argv lists launch hands to
subprocess (so the lifecycle contract with ``mngr`` cannot drift silently)
and (b) pre-flight validation. The ``await`` tests inject a fake clock and
sleeper so the poll loop runs without real time.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid

_SCRIPT = Path(__file__).parent / "create_worker.py"
_spec = importlib.util.spec_from_file_location("create_worker", _SCRIPT)
assert _spec is not None and _spec.loader is not None
create_worker_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_worker_mod)


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
class _RecordingRunner(create_worker_mod.Runner):
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


# A fixed archive stamp for tests that pin the ``consumed/`` filename, so the
# assertions do not depend on the wall clock. Deliberately far from any real run.
_PINNED_STAMP = "20260102T030405Z"


def _unique(prefix: str) -> str:
    """A globally unique agent name, so no two tests can collide on one."""
    return f"{prefix}-{uuid4().hex[:12]}"


def _agent_record(
    name: str,
    state: str,
    lead_agent: str | None = None,
    work_dir: str | None = None,
) -> dict[str, object]:
    """One ``mngr list --format jsonl`` agent record, in mngr's own shape.

    Mirrors ``AgentDetails``: ``resource_type``/``name``/``state``, the optional
    ``labels`` map ``launch`` writes ``lead_agent`` into, and the ``work_dir``
    the report fallback resolves a lead's checkout by.
    """
    record: dict[str, object] = {
        "resource_type": "agent",
        "name": name,
        "state": state,
    }
    if lead_agent is not None:
        record["labels"] = {"lead_agent": lead_agent}
    if work_dir is not None:
        record["work_dir"] = work_dir
    return record


def _listing(*records: Mapping[str, object]) -> _StubResult:
    """What ``mngr list --format jsonl`` prints for ``records``."""
    return _StubResult(stdout="".join(json.dumps(r) + "\n" for r in records))


def _make_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create runtime_dir / task_file / artifacts_dir under tmp_path.

    The task file has plain frontmatter (no ``source_artifacts_dir``); tests
    that exercise the artifacts sync overwrite it via ``_write_task``.
    """
    runtime = tmp_path / "data" / ".tasks" / "launch-task" / "demo"
    runtime.mkdir(parents=True)
    task = runtime / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    artifacts = tmp_path / "data" / ".tasks" / "fetch-process-show" / "demo"
    artifacts.mkdir(parents=True)
    (artifacts / "sample.json").write_text("{}")
    return runtime, task, artifacts


def _write_task(task: Path, source_artifacts_dir: str | None) -> None:
    """Overwrite ``task`` with frontmatter optionally declaring artifacts."""
    fm = "lead_agent: lead\n"
    if source_artifacts_dir is not None:
        fm += f"source_artifacts_dir: {source_artifacts_dir}\n"
    task.write_text(f"---\n{fm}---\n\nbody\n")


def test_happy_path_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    # Outside an mngr agent the file's own `lead_agent: lead` is the resolved
    # lead, and that is what the label must carry.
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    argvs = [c.argv for c in runner.calls]
    assert argvs == [
        ["git", "status", "--porcelain"],
        ["git", "rev-parse", "--show-toplevel"],
        [
            "mngr",
            "create",
            "demo-worker",
            "-t",
            "worker",
            "--label",
            "agent_created=true",
            "--label",
            "lead_agent=lead",
        ],
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=clobber",
        ],
        ["mngr", "message", "demo-worker", "--message-file", str(task)],
    ]


def test_source_artifacts_dir_synced_after_runtime(tmp_path: Path) -> None:
    """A frontmatter ``source_artifacts_dir`` is synced right after the runtime dir."""
    runtime, task, artifacts = _make_layout(tmp_path)
    _write_task(task, str(artifacts))
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    rsync_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "rsync"]]
    assert rsync_calls == [
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=clobber",
        ],
        [
            "mngr",
            "rsync",
            f"{artifacts}/",
            f"demo-worker:{artifacts}/",
            "--uncommitted-changes=clobber",
        ],
    ]


def test_emitted_mngr_argv_accepted_by_live_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ``mngr ...`` argv launch actually emits must be accepted by the
    live mngr CLI surface.

    Rather than re-asserting a hand-written expected argv (which mirrors the
    production assumption and so can never catch a divergence when system/vendor/mngr
    changes its CLI), we take exactly what ``launch`` hands the runner and
    confront it with ``imbue.mngr.main.cli``. It exercises the broadest argv set
    (create + two rsyncs + message) by declaring a ``source_artifacts_dir``.
    """
    runtime, task, artifacts = _make_layout(tmp_path)
    _write_task(task, str(artifacts))
    monkeypatch.setenv("MNGR_AGENT_NAME", "real-lead")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    mngr_calls = [c.argv for c in runner.calls if c.argv[:1] == ["mngr"]]
    # Vacuity guard: the full lifecycle is create + two rsyncs + message, so we
    # know the loop below actually validates four real invocations rather than
    # passing on an empty list. This counts steps; it deliberately does NOT pin
    # the subcommand names (that would re-introduce the hand-mirrored
    # expectation this test exists to replace) -- assert_mngr_argv_valid is what
    # confronts each argv with the live CLI.
    assert len(mngr_calls) == 4
    # Vacuity guard for the two argv details this launch newly depends on: the
    # lead label and the clobber sync mode have to be *in* what we validate,
    # or the loop below would prove nothing about either.
    flat = [word for argv in mngr_calls for word in argv]
    assert "lead_agent=real-lead" in flat
    assert "--uncommitted-changes=clobber" in flat
    for argv in mngr_calls:
        assert_mngr_argv_valid(argv)


def test_relative_runtime_dir_is_prefixed_for_local_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo-relative runtime dir is ``./``-prefixed as the local rsync source.

    This is the real launch contract (the skill passes repo-relative paths from
    the repo root). ``mngr rsync`` reads a bare ``data/foo/`` as an agent name
    and fails, so the source must be ``./``-prefixed -- while the agent
    destination stays repo-relative so mngr resolves it against the worker's
    workdir rather than the lead's. The absolute-path tests above don't exercise
    this because absolute paths are already recognized as local.
    """
    runtime, task, _ = _make_layout(tmp_path)
    monkeypatch.chdir(tmp_path)
    rel_runtime = runtime.relative_to(tmp_path)
    rel_task = task.relative_to(tmp_path)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=rel_runtime,
        task_file=rel_task,
        runner=runner,
    )

    assert rc == 0
    rsync_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "rsync"]]
    assert rsync_calls == [
        [
            "mngr",
            "rsync",
            f"./{rel_runtime}/",
            f"demo-worker:{rel_runtime}/",
            "--uncommitted-changes=clobber",
        ],
    ]


def test_source_artifacts_dir_missing_is_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A declared but nonexistent ``source_artifacts_dir`` aborts before launch."""
    runtime, task, _ = _make_layout(tmp_path)
    _write_task(task, str(tmp_path / "no-such-dir"))
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 2
    assert runner.calls == []
    assert "source_artifacts_dir" in capsys.readouterr().err


def test_source_artifacts_dir_non_string_raises(tmp_path: Path) -> None:
    """A non-string ``source_artifacts_dir`` value raises (full traceback) before
    any mngr call -- a malformed task file is an authoring bug, not a bad CLI arg."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text(
        "---\nlead_agent: lead\nsource_artifacts_dir: [a, b]\n---\n\nbody\n"
    )
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="source_artifacts_dir"):
        create_worker_mod.launch(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            runner=runner,
        )

    assert runner.calls == []


def test_launch_refuses_when_stale_report_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A leftover file at ``finish_report_path`` aborts before any mngr call.

    ``await`` returns as soon as the report file exists, so launching over a
    stale report would hand the caller the previous run's report instead of the
    new worker's -- launch must force the caller to deal with it first.
    """
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("---\ntype: status\nname: done\n---\n\nold run\n")
    task.write_text(
        f"---\nlead_agent: lead\nfinish_report_path: {report}\n---\n\nbody\n"
    )
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 2
    assert runner.calls == []
    err = capsys.readouterr().err
    assert "report path" in err
    assert str(report) in err


def test_launch_proceeds_when_report_path_is_clear(tmp_path: Path) -> None:
    """A declared ``finish_report_path`` with nothing at it launches normally."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text(
        f"---\nlead_agent: lead\nfinish_report_path: {runtime / 'reports' / 'report.md'}\n---\n\nbody\n"
    )
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert [c.argv[:2] for c in runner.calls] == [
        ["git", "status"],
        ["git", "rev-parse"],
        ["mngr", "create"],
        ["mngr", "rsync"],
        ["mngr", "message"],
    ]


def test_launch_refuses_dirty_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dirty working tree aborts before any mngr call, with a commit-first
    message that names stashing as the wrong move.

    The worker branches from committed HEAD, so uncommitted changes never reach
    it; ``mngr create`` also refuses a dirty tree. Catching it here turns an
    opaque ``mngr create`` failure into an actionable one.
    """
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(("git", "status"), _StubResult(stdout=" M some_file.py\n"))

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 2
    # The clean check ran, and nothing was handed to mngr.
    assert ["git", "status", "--porcelain"] in [c.argv for c in runner.calls]
    assert not any(c.argv[:1] == ["mngr"] for c in runner.calls)
    err = capsys.readouterr().err
    assert "uncommitted changes" in err
    assert "Commit" in err
    assert "stash" in err


def test_launch_proceeds_when_not_a_git_repo(tmp_path: Path) -> None:
    """A non-zero ``git status`` (not a git repo / git unavailable) is treated as
    'nothing to gate on' and launch proceeds -- mngr surfaces its own error later
    if it needs a repo."""
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(
        ("git", "status"),
        _StubResult(returncode=128, stderr="fatal: not a git repository"),
    )

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert any(c.argv[:2] == ["mngr", "create"] for c in runner.calls)


def test_invalid_frontmatter_yaml_raises(tmp_path: Path) -> None:
    """A present frontmatter block with invalid YAML raises rather than being
    silently treated as 'no frontmatter' -- it would otherwise mask an
    authoring bug and launch the worker with the wrong inputs."""
    runtime, task, _ = _make_layout(tmp_path)
    # A ``---`` block whose body is not valid YAML (unclosed bracket).
    task.write_text("---\nsource_artifacts_dir: [a, b\n---\n\nbody\n")
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="invalid YAML"):
        create_worker_mod.launch(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            runner=runner,
        )

    assert runner.calls == []


def test_malformed_frontmatter_does_not_abort_launch(tmp_path: Path) -> None:
    """A task file with no/broken frontmatter launches normally with no artifacts
    sync -- frontmatter schema validation is the worker's job, not launch's."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("no frontmatter here, just a body\n")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    rsync_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "rsync"]]
    assert rsync_calls == [
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=clobber",
        ],
    ]


def test_lead_agent_stamped_from_env_over_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A literal, unexpanded ``$MNGR_AGENT_NAME`` is replaced with the launching
    agent's real name so the worker has a valid address to send its report to."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("---\nlead_agent: $MNGR_AGENT_NAME\n---\n\nbody\n")
    monkeypatch.setenv("MNGR_AGENT_NAME", "real-lead")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    body = task.read_text()
    assert "lead_agent: real-lead" in body
    assert "$MNGR_AGENT_NAME" not in body
    assert [
        "mngr",
        "create",
        "demo-worker",
        "-t",
        "worker",
        "--label",
        "agent_created=true",
        "--label",
        "lead_agent=real-lead",
    ] in [c.argv for c in runner.calls]


def test_lead_agent_env_overrides_resolved_file_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher's own identity is authoritative: even a plain, resolved
    file value is overwritten with MNGR_AGENT_NAME (the agent that polls for the
    report). The file value is never trusted when the env names the launcher."""
    runtime, task, _ = _make_layout(tmp_path)  # lead_agent: lead
    monkeypatch.setenv("MNGR_AGENT_NAME", "real-lead")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert "lead_agent: real-lead" in task.read_text()


def test_lead_agent_injected_when_field_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task file that omits lead_agent entirely still gets it filled in from
    the environment -- authors no longer need to set it."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("---\nfinish_report_path: r/report.md\n---\n\nbody\n")
    monkeypatch.setenv("MNGR_AGENT_NAME", "real-lead")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    body = task.read_text()
    assert "lead_agent: real-lead" in body
    assert "finish_report_path: r/report.md" in body  # sibling field preserved


def test_unresolved_lead_agent_without_env_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the launcher cannot name itself (no MNGR_AGENT_NAME) and the file value
    is unresolved, fail before provisioning rather than launch an unaddressable
    worker."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("---\nlead_agent: $MNGR_AGENT_NAME\n---\n\nbody\n")
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 2
    # Only the preflight cleanliness probe ran -- no worker was provisioned.
    assert [c.argv for c in runner.calls] == [["git", "status", "--porcelain"]]
    assert "lead_agent is unresolved" in capsys.readouterr().err


def test_resolved_lead_agent_used_as_fallback_without_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside an mngr agent (no MNGR_AGENT_NAME), a plain author-set value is
    accepted as a fallback so manual/test invocations still work."""
    runtime, task, _ = _make_layout(tmp_path)  # lead_agent: lead
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert "lead_agent: lead" in task.read_text()


# --- task_file stamping -----------------------------------------------------


def _toplevel_result(path: Path) -> _StubResult:
    """What ``git rev-parse --show-toplevel`` prints for a repo at ``path``."""
    return _StubResult(stdout=f"{path}\n")


def test_repo_relative_task_path_resolves_against_the_real_git_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against a real git repo and the real ``Runner``: the stamped path is
    relative to the repo root even when launch runs from a subdirectory.

    A lead may launch from anywhere in its checkout, and the worker resolves the
    stamped path against *its own* worktree root -- so anything cwd-relative
    would point at nothing on the other side.
    """
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    task = tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "task.md"
    task.parent.mkdir(parents=True)
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    monkeypatch.chdir(tmp_path / "data" / ".tasks")

    relative = create_worker_mod._repo_relative_task_path(
        Path("launch-task/demo/task.md"), create_worker_mod.Runner()
    )

    assert relative == "data/.tasks/launch-task/demo/task.md"


def test_repo_relative_task_path_falls_back_for_a_file_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task file that is not under the repo root has no repo-relative form, so
    the path is stamped as given rather than mangled into a wrong one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    outside = tmp_path / "elsewhere" / "task.md"
    outside.parent.mkdir()
    outside.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    monkeypatch.chdir(repo)

    relative = create_worker_mod._repo_relative_task_path(
        outside, create_worker_mod.Runner()
    )

    assert relative == outside.as_posix()


def test_launch_stamps_the_task_file_path_relative_to_the_repo_root(
    tmp_path: Path,
) -> None:
    """launch writes the task file's own repo-relative path into its
    frontmatter, so the worker reads where its task file is instead of
    searching for it."""
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(("git", "rev-parse"), _toplevel_result(tmp_path))

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert "task_file: data/.tasks/launch-task/demo/task.md" in task.read_text(), (
        task.read_text()
    )
    # The stamp lands before the worker is created, so the file the worker is
    # messaged already names itself.
    stamp_position = next(
        i for i, c in enumerate(runner.calls) if c.argv[:2] == ["git", "rev-parse"]
    )
    create_position = next(
        i for i, c in enumerate(runner.calls) if c.argv[:2] == ["mngr", "create"]
    )
    assert stamp_position < create_position


def test_launch_from_a_subdirectory_stamps_the_same_repo_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamped path does not depend on where the lead ran launch from: a
    relative --task-file resolved from a subdirectory yields the same
    repo-relative path as an absolute one from the root."""
    runtime, task, _ = _make_layout(tmp_path)
    monkeypatch.chdir(runtime.parent)
    runner = _RecordingRunner()
    runner.respond(("git", "rev-parse"), _toplevel_result(tmp_path))

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=Path("demo"),
        task_file=Path("demo/task.md"),
        runner=runner,
    )

    assert rc == 0
    assert "task_file: data/.tasks/launch-task/demo/task.md" in task.read_text()


def test_launch_leaves_a_task_file_without_frontmatter_unstamped(
    tmp_path: Path,
) -> None:
    """No frontmatter block means nowhere to stamp: the file is passed through
    byte for byte (schema validation is the worker's job, not launch's)."""
    runtime, task, _ = _make_layout(tmp_path)
    body = "no frontmatter here, just a body\n"
    task.write_text(body)
    runner = _RecordingRunner()
    runner.respond(("git", "rev-parse"), _toplevel_result(tmp_path))

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert task.read_text() == body


def test_launch_stamps_the_path_as_given_when_git_cannot_answer(
    tmp_path: Path,
) -> None:
    """Outside a git repo (or with git unavailable) there is no root to
    relativize against, so the path is stamped exactly as the caller gave it
    rather than launch guessing."""
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(
        ("git", "rev-parse"),
        _StubResult(returncode=128, stderr="fatal: not a git repository"),
    )

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert f"task_file: {task.as_posix()}" in task.read_text()


# --- the lead_agent label ---------------------------------------------------


def test_lead_agent_label_carries_the_resolved_lead_not_the_file_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label names the agent that will actually poll for the report: when
    the launcher can name itself, its identity beats whatever the task file
    said -- the same precedence the stamped `lead_agent` follows, resolved once
    and used for both."""
    runtime, task, _ = _make_layout(tmp_path)  # lead_agent: lead
    monkeypatch.setenv("MNGR_AGENT_NAME", "outer-worker")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="inner-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    create_argv = next(c.argv for c in runner.calls if c.argv[:2] == ["mngr", "create"])
    assert create_argv[-2:] == ["--label", "lead_agent=outer-worker"]
    assert "lead_agent=lead" not in create_argv
    # The label and the stamp agree, so a worker's own record and its task file
    # never name different leads.
    assert "lead_agent: outer-worker" in task.read_text()


def test_no_lead_label_when_the_task_file_has_no_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no frontmatter there is no lead to resolve and nothing was stamped,
    so the create carries no lead label rather than an invented one."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("no frontmatter here, just a body\n")
    monkeypatch.setenv("MNGR_AGENT_NAME", "real-lead")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    create_argv = next(c.argv for c in runner.calls if c.argv[:2] == ["mngr", "create"])
    assert not any(arg.startswith("lead_agent=") for arg in create_argv)
    assert "--label" in create_argv  # the agent_created label still rides along


def test_set_frontmatter_field_replaces_inserts_and_ignores_bodyless() -> None:
    replaced = create_worker_mod._set_frontmatter_field(
        "---\nlead_agent: old\nx: 1\n---\nbody\n", "lead_agent", "new"
    )
    assert "lead_agent: new" in replaced
    assert "lead_agent: old" not in replaced
    assert "x: 1" in replaced  # sibling fields preserved
    inserted = create_worker_mod._set_frontmatter_field(
        "---\nx: 1\n---\nbody\n", "lead_agent", "new"
    )
    assert "lead_agent: new" in inserted
    assert "x: 1" in inserted
    assert (
        create_worker_mod._set_frontmatter_field("just body", "lead_agent", "new")
        == "just body"
    )


def test_runtime_dir_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=tmp_path / "missing",
        task_file=task,
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
    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=runtime / "missing.md",
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
    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )
    assert rc == 2
    # Nothing past the create runs: no sync, no task message.
    assert [c.argv[:2] for c in runner.calls] == [
        ["git", "status"],
        ["git", "rev-parse"],
        ["mngr", "create"],
    ]


def _launch_argv(runtime: Path, task: Path) -> list[str]:
    return [
        "launch",
        "--name",
        "x",
        "--template",
        "worker",
        "--runtime-dir",
        str(runtime),
        "--task-file",
        str(task),
    ]


def test_main_create_carries_no_workspace_label(
    tmp_path: Path,
) -> None:
    # Workers belong to their workspace by sharing the host; they carry no
    # workspace label (the label was removed from the naming model).
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = create_worker_mod.main(_launch_argv(runtime, task), runner=runner)

    assert rc == 0
    create_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "create"]]
    assert create_calls, runner.calls
    assert not any(arg.startswith("workspace=") for arg in create_calls[0])


def _make_state_dir_with_converter(tmp_path: Path) -> Path:
    """Create a state_dir containing a stub common_transcript.sh."""
    state_dir = tmp_path / "state"
    (state_dir / "commands").mkdir(parents=True)
    script = state_dir / "commands" / "common_transcript.sh"
    script.write_text("#!/usr/bin/env bash\n:\n")
    return state_dir


def test_common_transcript_flushed_before_message_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When state_dir has the converter, launch flushes it right before the message."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    argvs = [c.argv for c in runner.calls]
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    assert argvs == [
        ["git", "status", "--porcelain"],
        ["git", "rev-parse", "--show-toplevel"],
        [
            "mngr",
            "create",
            "demo-worker",
            "-t",
            "worker",
            "--label",
            "agent_created=true",
            "--label",
            "lead_agent=lead",
        ],
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=clobber",
        ],
        [expected_script, "--single-pass"],
        ["mngr", "message", "demo-worker", "--message-file", str(task)],
    ]


def test_common_transcript_skipped_when_state_dir_is_none(tmp_path: Path) -> None:
    """No converter call when state_dir is None (tests / non-mngr envs)."""
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
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

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    assert not any(
        "common_transcript.sh" in arg for call in runner.calls for arg in call.argv
    )


def test_common_transcript_failure_does_not_abort_launch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-zero converter exit must NOT abort launch (worker is mid-launch)."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    runner.respond((expected_script, "--single-pass"), _StubResult(returncode=2))

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
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
    """main() reads MNGR_AGENT_STATE_DIR and threads it into launch."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    rc = create_worker_mod.main(_launch_argv(runtime, task), runner=runner)

    assert rc == 0
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    flush_calls = [
        c.argv for c in runner.calls if c.argv == [expected_script, "--single-pass"]
    ]
    assert len(flush_calls) == 1


# --- await subcommand -----------------------------------------------------


class _FakeClock:
    """Monotonic clock that advances by a fixed step on every read.

    Lets ``await_report`` reach its deadline deterministically without real
    sleeping: each ``clock()`` read inside the poll loop moves time forward.
    """

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._now
        self._now += self._step
        return now


def _no_sleep(_seconds: float) -> None:
    return None


def _write_report_on_sleep(report: Path, text: str):
    """A sleeper that plays the worker: the report appears during the first
    poll sleep (launch refuses a report that pre-exists the worker)."""

    def _sleeper(_seconds: float) -> None:
        report.write_text(text)

    return _sleeper


def _write_await_task(task_file: Path, report_path: Path) -> None:
    """Write a task file whose frontmatter points await at ``report_path``."""
    task_file.write_text(
        f"---\nlead_agent: lead\nfinish_report_path: {report_path}\n---\n\nbody\n"
    )


def test_await_returns_report_immediately_when_present(tmp_path: Path) -> None:
    """A report already on disk is printed at once, before any sleep."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    report.write_text("---\ntype: status\nname: done\n---\n\nall good\n")
    out = io.StringIO()

    def _boom(_seconds: float) -> None:
        raise AssertionError("await must not sleep when the report already exists")

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_boom,
        clock=lambda: 0.0,
        out=out,
    )

    assert rc == 0
    assert "name: done" in out.getvalue()
    assert "all good" in out.getvalue()


def test_await_polls_until_report_appears(tmp_path: Path) -> None:
    """await loops, sleeping, until the report shows up, then prints it."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    out = io.StringIO()

    sleeps: list[float] = []

    def _sleeper_that_creates_report(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            report.write_text("---\ntype: gate\nname: question\n---\n\nwhich one?\n")

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_sleeper_that_creates_report,
        clock=lambda: 0.0,
        out=out,
    )

    assert rc == 0
    assert sleeps == [5, 5, 5]
    assert "name: question" in out.getvalue()


def test_await_times_out_when_report_never_appears(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the deadline passes with no report, await returns the timeout code."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=30,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
    )

    assert rc == create_worker_mod._AWAIT_TIMEOUT_RC
    assert out.getvalue() == ""
    assert "timed out" in capsys.readouterr().err


def test_await_returns_shed_code_when_worker_shed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worker shed for memory pressure ends the poll early with the shed code
    and an actionable revive message -- not the silent full-length timeout."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=lambda: 0.0,
        out=out,
        worker_name="demo",
        pending_shed_check=lambda name: name == "demo",
    )

    assert rc == create_worker_mod._AWAIT_SHED_RC
    assert out.getvalue() == ""
    err = capsys.readouterr().err
    assert "demo" in err and "--restart" in err


def test_await_returns_idle_code_when_worker_idle_without_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worker observed idle for the consecutive-poll threshold with no report
    ends the poll early with the idle code and a message pointing at the
    worker's own worktree -- not the silent full-length timeout."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    out = io.StringIO()
    idle_polls: list[str] = []

    def _always_idle(name: str) -> bool:
        idle_polls.append(name)
        return True

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=lambda: 0.0,
        out=out,
        worker_name="demo",
        pending_shed_check=lambda _name: False,
        idle_check=_always_idle,
    )

    assert rc == create_worker_mod._AWAIT_IDLE_RC
    assert len(idle_polls) == create_worker_mod._IDLE_POLLS_BEFORE_GIVING_UP
    assert out.getvalue() == ""
    err = capsys.readouterr().err
    assert "ended its turn" in err and "worktree" in err


def test_await_transient_idle_does_not_end_the_poll(tmp_path: Path) -> None:
    """Idle observations must be consecutive: a worker seen active again resets
    the counter, and a report that then appears wins normally."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    out = io.StringIO()

    # Idle twice, then active (counter resets), then idle again while the
    # report lands via the sleeper -- await must return the report, not the
    # idle code.
    idle_answers = iter([True, True, False, True, True, True])
    sleeps: list[float] = []

    def _sleeper_that_creates_report(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 4:
            report.write_text("---\ntype: status\nname: done\n---\n\nmade it\n")

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_sleeper_that_creates_report,
        clock=lambda: 0.0,
        out=out,
        worker_name="demo",
        pending_shed_check=lambda _name: False,
        idle_check=lambda _name: next(idle_answers),
    )

    assert rc == 0
    assert "made it" in out.getvalue()


def test_await_report_wins_over_pending_shed(tmp_path: Path) -> None:
    """The report file is checked before the shed ledger, so a worker that
    reported and was then shed still yields its report (rc 0)."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    report.write_text("---\ntype: status\nname: done\n---\n\nfinished first\n")
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=lambda: 0.0,
        out=out,
        worker_name="demo",
        pending_shed_check=lambda _name: True,
    )

    assert rc == 0
    assert "finished first" in out.getvalue()


# --- await: consuming the report it printed ---------------------------------


def _report_in(tmp_path: Path) -> Path:
    """An empty reports dir with the report path await polls for inside it."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    return report


def _await_once(report: Path, text: str, stamp: str = _PINNED_STAMP) -> str:
    """Run one await whose worker writes ``text`` during the first poll sleep,
    and return what await printed."""
    out = io.StringIO()
    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_write_report_on_sleep(report, text),
        clock=lambda: 0.0,
        out=out,
        archive_timestamp=lambda: stamp,
    )
    assert rc == 0
    return out.getvalue()


def test_await_archives_the_report_it_printed_under_its_kind(tmp_path: Path) -> None:
    """await consumes the report: it prints the contents verbatim and moves the
    file into consumed/ under a timestamped name carrying the report's own
    type and name, so a lead reading the archive sees what each report was."""
    report = _report_in(tmp_path)
    text = "---\ntype: gate\nname: question\n---\n\nwhich database?\n"

    printed = _await_once(report, text)

    assert printed == text
    assert not report.exists()
    consumed = report.parent / "consumed"
    archived = consumed / f"{_PINNED_STAMP}-gate-question.md"
    assert [p.name for p in consumed.iterdir()] == [archived.name]
    assert archived.read_text() == text


def test_await_archives_an_unparseable_report_without_losing_it(
    tmp_path: Path,
) -> None:
    """A report with no frontmatter still archives (nothing is discarded); the
    missing type/name read as `unparsed` rather than crashing the consume."""
    report = _report_in(tmp_path)

    printed = _await_once(report, "the worker just wrote prose\n")

    assert "just wrote prose" in printed
    consumed = report.parent / "consumed"
    archived = consumed / f"{_PINNED_STAMP}-unparsed-unparsed.md"
    assert archived.read_text() == "the worker just wrote prose\n"


def test_two_reports_in_the_same_second_are_both_kept(tmp_path: Path) -> None:
    """A gate cycle can archive two reports inside one timestamp tick; the
    second is disambiguated rather than overwriting the first."""
    report = _report_in(tmp_path)
    first_text = "---\ntype: gate\nname: question\n---\n\nfirst\n"
    second_text = "---\ntype: gate\nname: question\n---\n\nsecond\n"

    assert _await_once(report, first_text) == first_text
    assert _await_once(report, second_text) == second_text

    consumed = report.parent / "consumed"
    assert sorted(p.name for p in consumed.iterdir()) == [
        f"{_PINNED_STAMP}-gate-question.1.md",
        f"{_PINNED_STAMP}-gate-question.md",
    ]
    archived = {p.read_text() for p in consumed.iterdir()}
    assert archived == {first_text, second_text}


def test_a_relaunch_after_an_awaited_gate_is_not_blocked_by_that_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the archive: after a gate report is awaited, relaunching the
    worker on the same task file succeeds instead of tripping launch's
    stale-report guard (which would exit 2 without creating anything)."""
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    _write_await_task(task, report)

    _await_once(report, "---\ntype: gate\nname: question\n---\n\nwhich one?\n")

    runner = _RecordingRunner()
    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 0
    assert any(c.argv[:2] == ["mngr", "create"] for c in runner.calls)


# --- idle detection with sub-workers ----------------------------------------


@pytest.mark.parametrize(
    "child_state,expected_idle",
    [("RUNNING", False), ("WAITING", False), ("STOPPED", True), ("DONE", True)],
)
def test_a_worker_is_idle_only_once_its_own_children_are_finished(
    child_state: str, expected_idle: bool
) -> None:
    """A worker that has ended its turn is idle only if no agent labelled with
    it as lead is still live. RUNNING is obviously live; WAITING is too -- a
    sub-worker sitting on its own gate report is exactly what an intermediate
    lead is waiting to answer -- while STOPPED and DONE are finished."""
    worker = _unique("worker")
    child = _unique("child")
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "WAITING"),
            _agent_record(child, child_state, lead_agent=worker),
        ),
    )

    is_idle = create_worker_mod._worker_is_idle(
        worker, runner, pending_shed_check=lambda _name: False
    )

    assert is_idle is expected_idle


def test_a_shed_child_does_not_hold_its_parent_open() -> None:
    """A child shed by the OOM daemon keeps a live-looking state forever without
    doing any work, so it must not keep its parent counted as busy -- otherwise
    the lead waits out the full timeout on a dispatch that will never move."""
    worker = _unique("worker")
    child = _unique("child")
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "WAITING"),
            _agent_record(child, "RUNNING", lead_agent=worker),
        ),
    )

    shed_checks: list[str] = []

    def _child_was_shed(name: str) -> bool:
        shed_checks.append(name)
        return True

    assert (
        create_worker_mod._worker_is_idle(
            worker, runner, pending_shed_check=_child_was_shed
        )
        is True
    )
    # The shed question is asked about the *child*, not the worker being polled.
    assert shed_checks == [child]


def test_the_default_shed_check_reads_the_real_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an injected check, the child scan consults the real OOM ledger --
    so the shed tolerance is wired to the same records the kill hook writes, not
    only to what a test hands it."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path))
    worker = _unique("worker")
    child = _unique("child")
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "WAITING"),
            _agent_record(child, "RUNNING", lead_agent=worker),
        ),
    )

    # Nothing in the ledger yet: the live child keeps its parent busy.
    assert create_worker_mod._worker_is_idle(worker, runner) is False

    src = create_worker_mod._oom_priority_src()
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from oom_priority.ledger import append_shed_record

    append_shed_record(pid=8271, comm="claude", agent_name=child, is_worker=True)

    assert create_worker_mod._worker_is_idle(worker, runner) is True


def test_a_child_of_another_lead_does_not_keep_this_worker_busy() -> None:
    """Only agents labelled with *this* worker as their lead count: a sibling
    dispatch running under someone else is irrelevant to this poll."""
    worker = _unique("worker")
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "WAITING"),
            _agent_record(
                _unique("stranger"), "RUNNING", lead_agent=_unique("other-lead")
            ),
        ),
    )

    assert (
        create_worker_mod._worker_is_idle(
            worker, runner, pending_shed_check=lambda _name: False
        )
        is True
    )


def test_a_running_worker_is_never_idle_even_with_no_children() -> None:
    """The worker's own state is still the first question: mid-turn is not idle."""
    worker = _unique("worker")
    runner = _RecordingRunner()
    runner.respond(("mngr", "list"), _listing(_agent_record(worker, "RUNNING")))

    assert (
        create_worker_mod._worker_is_idle(
            worker, runner, pending_shed_check=lambda _name: False
        )
        is False
    )


def test_the_idle_check_costs_exactly_one_mngr_list() -> None:
    """Both questions -- the worker's own state and its children's -- are
    answered from a single listing, so poll cost does not grow with depth."""
    worker = _unique("worker")
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "WAITING"),
            _agent_record(_unique("child"), "STOPPED", lead_agent=worker),
        ),
    )

    create_worker_mod._worker_is_idle(
        worker, runner, pending_shed_check=lambda _name: False
    )

    assert [c.argv for c in runner.calls] == [
        ["mngr", "list", "--format", "jsonl", "--on-error", "continue"]
    ]


def test_an_unreadable_listing_answers_not_idle() -> None:
    """A failed ``mngr list`` must never end a healthy await: a non-zero exit is
    no evidence at all, even when partial output happens to look conclusive, so
    the worker stays counted as busy and the timeout remains the backstop."""
    worker = _unique("worker")
    runner = _RecordingRunner()
    # Partial output that *would* read as idle if the exit code were ignored.
    failed = _listing(_agent_record(worker, "STOPPED"))
    runner.respond(
        ("mngr", "list"),
        _StubResult(returncode=1, stdout=failed.stdout, stderr="mngr exploded"),
    )

    assert (
        create_worker_mod._worker_is_idle(
            worker, runner, pending_shed_check=lambda _name: False
        )
        is False
    )


def test_a_non_agent_row_with_the_same_name_is_not_mistaken_for_the_worker() -> None:
    """``mngr list`` emits more than agents (hosts, and whatever it grows next);
    only ``resource_type: agent`` rows are agent state, so a same-named row of
    another kind must not answer the poll's question."""
    worker = _unique("worker")
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _StubResult(
            stdout=json.dumps(
                {"resource_type": "host", "name": worker, "state": "STOPPED"}
            )
            + "\n"
            + json.dumps({"resource_type": "agent", "name": worker, "state": "RUNNING"})
            + "\n"
        ),
    )

    assert (
        create_worker_mod._worker_is_idle(
            worker, runner, pending_shed_check=lambda _name: False
        )
        is False
    )


def test_read_finish_report_path_returns_field(tmp_path: Path) -> None:
    """_read_finish_report_path pulls the path out of the task frontmatter."""
    task = tmp_path / "task.md"
    _write_await_task(
        task, Path("data/.tasks/harden/crystallize-demo/reports/report.md")
    )

    result = create_worker_mod._read_finish_report_path(task)

    assert result == Path("data/.tasks/harden/crystallize-demo/reports/report.md")


def test_read_finish_report_path_missing_raises(tmp_path: Path) -> None:
    """A task file without finish_report_path is a hard error for await."""
    task = tmp_path / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")

    with pytest.raises(ValueError, match="finish_report_path"):
        create_worker_mod._read_finish_report_path(task)


def _await_argv(task_file: Path, extra: Sequence[str] = ()) -> list[str]:
    return ["await", "--task-file", str(task_file), "--name", "demo", *extra]


def test_main_await_prints_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """await parses the task file, finds finish_report_path, and prints the report.

    The report exists up front, so main()'s real ``time.sleep`` is never
    reached and the loop returns immediately.
    """
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    report.write_text("hello from worker\n")
    task = tmp_path / "task.md"
    _write_await_task(task, report)

    rc = create_worker_mod.main(_await_argv(task))

    assert rc == 0
    assert capsys.readouterr().out == "hello from worker\n"


def test_main_await_missing_finish_report_path_raises(tmp_path: Path) -> None:
    """await raises (full traceback) when the required field is absent, rather
    than swallowing it into a terse exit-2 message."""
    task = tmp_path / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")

    with pytest.raises(ValueError, match="finish_report_path"):
        create_worker_mod.main(_await_argv(task))


def test_main_await_requires_name(tmp_path: Path) -> None:
    """await refuses to run without --name: the shed-ledger watch needs the
    worker name, and it is the same name the caller already passed to launch."""
    report = tmp_path / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("hi\n")
    task = tmp_path / "task.md"
    _write_await_task(task, report)

    with pytest.raises(SystemExit):
        create_worker_mod.main(["await", "--task-file", str(task)])


def test_worker_has_pending_shed_reflects_real_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_worker_has_pending_shed imports oom_priority for real (no swallowed
    ImportError) and reflects a shed recorded in the ledger -- True only for the
    worker whose own agent was shed."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path))
    # No ledger yet: nothing is pending.
    assert create_worker_mod._worker_has_pending_shed("demo-worker") is False

    # Record a shed of this worker's own agent via oom_priority's own writer
    # (the same module the kill hook uses -- no schema duplicated here).
    src = create_worker_mod._oom_priority_src()
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from oom_priority.ledger import append_shed_record

    append_shed_record(
        pid=4321, comm="claude", agent_name="demo-worker", is_worker=True
    )

    assert create_worker_mod._worker_has_pending_shed("demo-worker") is True
    assert create_worker_mod._worker_has_pending_shed("other-worker") is False


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30m", 1800.0),
        ("90s", 90.0),
        ("1h", 3600.0),
        ("45", 45.0),
        ("2.5m", 150.0),
    ],
)
def test_parse_duration_accepts_suffixes(text: str, expected: float) -> None:
    assert create_worker_mod._parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "-5m", "0s", "m"])
def test_parse_duration_rejects_invalid(bad: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        create_worker_mod._parse_duration(bad)


# --- launch-sync / destroy / report parsing -------------------------------------


def _write_launch_sync_task(task_file: Path, report_path: Path) -> None:
    """Write a task file whose frontmatter points the wait at ``report_path``."""
    task_file.write_text(
        f"---\nlead_agent: lead\nfinish_report_path: {report_path}\n---\n\nbody\n"
    )


def _destroy_argvs(runner: _RecordingRunner) -> list[list[str]]:
    return [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "destroy"]]


def test_parse_report_extracts_type_name_and_body() -> None:
    result = create_worker_mod.parse_report(
        "---\ntype: status\nname: done\n---\n\nall finished\n"
    )
    assert result.report_type == "status"
    assert result.name == "done"
    assert result.body == "all finished"
    assert result.raw == "---\ntype: status\nname: done\n---\n\nall finished\n"


def test_parse_report_tolerates_missing_frontmatter() -> None:
    # An agent-authored report without frontmatter must not crash collection: the
    # whole text is preserved so the caller can still surface the worker's output.
    result = create_worker_mod.parse_report("just prose, no fences\n")
    assert result.report_type is None
    assert result.name is None
    assert "just prose" in result.body
    assert "just prose" in result.raw


def test_parse_report_tolerates_malformed_yaml() -> None:
    text = "---\ntype: : : bad yaml\n---\nbody\n"
    result = create_worker_mod.parse_report(text)
    assert result.report_type is None
    assert result.name is None
    assert result.raw == text


def test_destroy_invokes_mngr_destroy_force() -> None:
    runner = _RecordingRunner()
    create_worker_mod.destroy("demo-worker", runner)
    assert _destroy_argvs(runner) == [["mngr", "destroy", "demo-worker", "--force"]]


def test_destroy_argv_accepted_by_live_cli() -> None:
    # Guard against drift in the mngr CLI: the destroy argv we emit must stay valid.
    runner = _RecordingRunner()
    create_worker_mod.destroy("demo-worker", runner)
    for call in runner.calls:
        assert_mngr_argv_valid(call.argv)


def test_launch_sync_collects_report_and_destroys(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    _write_launch_sync_task(task, report)
    result_json = tmp_path / "result.json"
    runner = _RecordingRunner()
    out = io.StringIO()

    rc = create_worker_mod.launch_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        runner=runner,
        # The report lands only after launch (launch refuses a pre-existing
        # one), so the "worker" writes it during the first await sleep.
        sleeper=_write_report_on_sleep(
            report, "---\ntype: status\nname: done\n---\n\nshipped it\n"
        ),
        clock=lambda: 0.0,
        out=out,
        result_path=result_json,
    )

    assert rc == 0
    # The worker is destroyed once its report is collected.
    assert _destroy_argvs(runner) == [["mngr", "destroy", "demo-worker", "--force"]]
    expected = {
        "timed_out": False,
        "type": "status",
        "name": "done",
        "body": "shipped it",
        "branch": "mngr/demo-worker",
        "raw_report": "---\ntype: status\nname: done\n---\n\nshipped it\n",
    }
    assert json.loads(result_json.read_text()) == expected
    # Stdout carries the same JSON object for shell/human callers.
    assert json.loads(out.getvalue()) == expected


def test_launch_sync_consumes_report_so_a_repeated_call_is_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: launch_sync calls launch(), whose stale-report guard refuses to
    # launch when anything sits at finish_report_path. destroy() removes the
    # worker's agent/worktree but NOT the report (it lives in the caller's runtime
    # dir), so without cleanup the report launch_sync just collected would trap the
    # next call. A non-interactive caller (a service) that calls launch_sync
    # repeatedly with the same task file -- hence the same report path -- must not
    # be blocked by its own previous report. So launch_sync moves the collected
    # report aside into consumed/ (archived, not deleted) once it is collected.
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    _write_launch_sync_task(task, report)
    consumed = report.parent / "consumed"

    def _run_once(runner: create_worker_mod.Runner) -> int:
        return create_worker_mod.launch_sync(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            timeout_seconds=1800,
            poll_interval_seconds=5,
            runner=runner,
            sleeper=_write_report_on_sleep(
                report, "---\ntype: status\nname: done\n---\n\nround\n"
            ),
            clock=lambda: 0.0,
            out=io.StringIO(),
            archive_timestamp=lambda: _PINNED_STAMP,
        )

    first = _RecordingRunner()
    assert _run_once(first) == 0
    # The collected report is cleared from the report path but preserved in
    # consumed/, not deleted -- under the timestamped, kind-naming archive name
    # every collected report now gets.
    assert not report.exists()
    assert (consumed / f"{_PINNED_STAMP}-status-done.md").read_text() == (
        "---\ntype: status\nname: done\n---\n\nround\n"
    )

    # A second identical call re-creates the worker and returns 0 instead of
    # aborting on the guard (exit 2, which would emit no `mngr create`).
    second = _RecordingRunner()
    assert _run_once(second) == 0
    create_calls = [c.argv for c in second.calls if c.argv[:2] == ["mngr", "create"]]
    assert create_calls == [
        [
            "mngr",
            "create",
            "demo-worker",
            "-t",
            "worker",
            "--label",
            "agent_created=true",
            "--label",
            "lead_agent=lead",
        ]
    ]
    # The second run's report is archived under a disambiguated name -- the first
    # archive is not overwritten, so both are retained.
    assert not report.exists()
    assert sorted(p.name for p in consumed.iterdir()) == [
        f"{_PINNED_STAMP}-status-done.1.md",
        f"{_PINNED_STAMP}-status-done.md",
    ]


def test_launch_sync_keep_agent_skips_destroy(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    _write_launch_sync_task(task, report)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        destroy_on_finish=False,
        runner=runner,
        sleeper=_write_report_on_sleep(
            report, "---\ntype: status\nname: done\n---\n\nok\n"
        ),
        clock=lambda: 0.0,
        out=io.StringIO(),
    )

    assert rc == 0
    assert _destroy_argvs(runner) == []


def test_launch_sync_timeout_keeps_worker_alive(tmp_path: Path) -> None:
    # No report ever appears: launch_sync returns the timeout code, marks the
    # result timed_out, and must NOT destroy the worker (the report may still come).
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)  # dir exists; the report file never appears
    _write_launch_sync_task(task, report)
    result_json = tmp_path / "result.json"
    runner = _RecordingRunner()

    rc = create_worker_mod.launch_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        timeout_seconds=30,
        poll_interval_seconds=5,
        runner=runner,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=io.StringIO(),
        result_path=result_json,
    )

    assert rc == create_worker_mod._AWAIT_TIMEOUT_RC
    assert _destroy_argvs(runner) == []
    # The idle poll goes through the injected runner, not a real `mngr list`.
    assert any(c.argv[:2] == ["mngr", "list"] for c in runner.calls)
    payload = json.loads(result_json.read_text())
    assert payload["timed_out"] is True
    assert payload["branch"] == "mngr/demo-worker"
    # The timeout arm carries the same key set as the success arm, so a consumer
    # can read any field (e.g. raw_report) without a KeyError on the timeout path.
    assert set(payload) == {
        "timed_out",
        "type",
        "name",
        "body",
        "branch",
        "raw_report",
    }


def test_launch_sync_surfaces_launch_failure(tmp_path: Path) -> None:
    # A failed preflight (missing runtime dir) is returned verbatim; launch_sync
    # never waits, never destroys, and never reaches mngr.
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    _write_launch_sync_task(task, report)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=tmp_path / "missing",
        task_file=task,
        timeout_seconds=30,
        poll_interval_seconds=5,
        runner=runner,
        sleeper=_no_sleep,
        clock=lambda: 0.0,
        out=io.StringIO(),
    )

    assert rc == 2
    assert runner.calls == []


def test_launch_sync_missing_finish_report_path_raises_before_launch(
    tmp_path: Path,
) -> None:
    # A task file lacking finish_report_path must fail BEFORE any worker is
    # created, so a malformed task file can't orphan a half-launched worker.
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="finish_report_path"):
        create_worker_mod.launch_sync(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            timeout_seconds=30,
            poll_interval_seconds=5,
            runner=runner,
            sleeper=_no_sleep,
            clock=lambda: 0.0,
            out=io.StringIO(),
        )

    assert runner.calls == []


def test_main_launch_sync_emits_result_json(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    _write_launch_sync_task(task, report)
    result_json = tmp_path / "result.json"

    # main() wires the real sleeper, so the "worker" writes its report as a
    # side effect of receiving the task message -- await then finds it on its
    # first existence check. (It cannot pre-exist: launch refuses that.)
    class _WorkerRespondsRunner(_RecordingRunner):
        def run(self, argv: Sequence[str], **kwargs):
            result = super().run(argv, **kwargs)
            if list(argv)[:2] == ["mngr", "message"]:
                report.write_text("---\ntype: status\nname: done\n---\n\ndone\n")
            return result

    runner = _WorkerRespondsRunner()

    rc = create_worker_mod.main(
        [
            "launch-sync",
            "--name",
            "demo-worker",
            "--template",
            "worker",
            "--runtime-dir",
            str(runtime),
            "--task-file",
            str(task),
            "--result-json",
            str(result_json),
        ],
        runner=runner,
    )

    assert rc == 0
    payload = json.loads(result_json.read_text())
    assert payload["name"] == "done"
    assert payload["branch"] == "mngr/demo-worker"
    assert _destroy_argvs(runner) == [["mngr", "destroy", "demo-worker", "--force"]]


def test_main_destroy_invokes_mngr(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    rc = create_worker_mod.main(["destroy", "--name", "demo-worker"], runner=runner)
    assert rc == 0
    assert _destroy_argvs(runner) == [["mngr", "destroy", "demo-worker", "--force"]]


# --- launch: a refused mngr create ------------------------------------------


def test_a_refused_mngr_create_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # mngr's own refusal (a duplicate name, a dirty tree) has to come back as
    # an exit code and a message, not a traceback.
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "create"),
        subprocess.CalledProcessError(returncode=1, cmd=["mngr", "create"]),
    )

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=runner,
    )

    assert rc == 2
    argvs = [c.argv for c in runner.calls]
    assert not any(argv[:2] == ["mngr", "rsync"] for argv in argvs)
    assert not any(argv[:2] == ["mngr", "message"] for argv in argvs)
    assert "`mngr create demo-worker` failed" in capsys.readouterr().err


# --- report subcommand: the worker's side of the contract -------------------


def _write_worker_task(task: Path, report_path: str, lead_agent: str | None) -> None:
    """Write the task file a worker holds: where its report goes, and (usually)
    who to send it to."""
    lead_line = "" if lead_agent is None else f"lead_agent: {lead_agent}\n"
    task.write_text(
        f"---\n{lead_line}finish_report_path: {report_path}\n"
        f"task_file: {task.name}\n---\n\ndo the thing\n"
    )


def _worker_tree(tmp_path: Path) -> tuple[Path, str, Path]:
    """A worker's checkout: its task file, the repo-relative report path its
    frontmatter names, and a body file to report with."""
    runtime = tmp_path / "data" / ".tasks" / "launch-task" / "demo"
    runtime.mkdir(parents=True)
    task = runtime / "task.md"
    body = tmp_path / "body.md"
    body.write_text("Committed on branch `mngr/demo`. Ready to merge.\n")
    return task, "data/.tasks/launch-task/demo/reports/report.md", body


def _report_argv(task: Path, body: Path, extra: Sequence[str] = ()) -> list[str]:
    return [
        "report",
        "--task-file",
        str(task),
        "--type",
        "status",
        "--name",
        "done",
        "--body-file",
        str(body),
        *extra,
    ]


def test_report_writes_the_report_and_pushes_its_directory_to_the_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The happy path: the report lands at the task file's own
    finish_report_path inside the worker's tree, in the frontmatter shape the
    lead parses, and its *parent directory* is pushed to the lead -- which is
    what makes the file arrive at the lead's finish_report_path."""
    monkeypatch.chdir(tmp_path)
    task, report_rel, body = _worker_tree(tmp_path)
    lead = _unique("lead")
    _write_worker_task(task, report_rel, lead)
    runner = _RecordingRunner()

    rc = create_worker_mod.main(_report_argv(task, body), runner=runner)

    assert rc == 0
    assert Path(report_rel).read_text() == (
        "---\ntype: status\nname: done\n---\n\n"
        "Committed on branch `mngr/demo`. Ready to merge.\n"
    )
    report_dir = "data/.tasks/launch-task/demo/reports/"
    assert [c.argv for c in runner.calls] == [
        [
            "mngr",
            "rsync",
            f"./{report_dir}",
            f"{lead}:{report_dir}",
            "--uncommitted-changes=clobber",
        ]
    ]
    # The destination actually used is printed, so a worker's transcript records
    # where its report went.
    assert capsys.readouterr().out == f"{lead}:{report_dir}\n"


def test_the_written_report_is_what_the_lead_parses_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two halves of the contract meet: what ``report`` writes is exactly
    what ``parse_report`` (the lead's collection path) reads back."""
    monkeypatch.chdir(tmp_path)
    task, report_rel, body = _worker_tree(tmp_path)
    body.write_text("I could not build it because: the venv is broken.\n")
    _write_worker_task(task, report_rel, _unique("lead"))

    rc = create_worker_mod.report_to_lead(
        task_file=task,
        report_type="gate",
        name="question",
        body_file=body,
        runner=_RecordingRunner(),
        worker_name=_unique("worker"),
    )

    assert rc == 0
    parsed = create_worker_mod.parse_report(Path(report_rel).read_text())
    assert parsed.report_type == "gate"
    assert parsed.name == "question"
    assert parsed.body == "I could not build it because: the venv is broken."


def test_report_push_argv_is_accepted_by_the_live_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push the worker emits must stay valid against the real mngr CLI, the
    same guard the launch-side argvs carry."""
    monkeypatch.chdir(tmp_path)
    task, report_rel, body = _worker_tree(tmp_path)
    _write_worker_task(task, report_rel, _unique("lead"))
    runner = _RecordingRunner()

    assert create_worker_mod.main(_report_argv(task, body), runner=runner) == 0

    mngr_calls = [c.argv for c in runner.calls if c.argv[:1] == ["mngr"]]
    assert len(mngr_calls) == 1  # vacuity guard: there is an argv to validate
    for argv in mngr_calls:
        assert_mngr_argv_valid(argv)


def test_report_falls_back_to_the_leads_work_dir_when_the_push_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the push fails, the worker resolves its own ``lead_agent`` label out
    of ``mngr list``, then that lead's ``work_dir``, and writes the report
    straight into the lead's checkout at the same relative path -- which is the
    exact file the lead's await polls for."""
    monkeypatch.chdir(tmp_path)
    task, report_rel, body = _worker_tree(tmp_path)
    worker = _unique("worker")
    lead = _unique("lead")
    _write_worker_task(task, report_rel, lead)
    lead_work_dir = tmp_path / "leads" / lead
    monkeypatch.setenv("MNGR_AGENT_NAME", worker)
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "rsync"),
        subprocess.CalledProcessError(returncode=1, cmd=["mngr", "rsync"]),
    )
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "RUNNING", lead_agent=lead),
            _agent_record(lead, "WAITING", work_dir=str(lead_work_dir)),
        ),
    )

    rc = create_worker_mod.main(_report_argv(task, body), runner=runner)

    assert rc == 0
    delivered = lead_work_dir / report_rel
    assert delivered.read_text() == Path(report_rel).read_text()
    captured = capsys.readouterr()
    assert captured.out == f"{delivered}\n"
    assert "pushing the report" in captured.err


def test_report_skips_the_push_entirely_when_the_task_file_names_no_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no ``lead_agent`` there is no address to rsync to, so no push is
    even attempted -- the listing fallback runs directly, with a note saying
    why."""
    monkeypatch.chdir(tmp_path)
    task, report_rel, body = _worker_tree(tmp_path)
    worker = _unique("worker")
    lead = _unique("lead")
    _write_worker_task(task, report_rel, None)
    lead_work_dir = tmp_path / "leads" / lead
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "RUNNING", lead_agent=lead),
            _agent_record(lead, "WAITING", work_dir=str(lead_work_dir)),
        ),
    )

    rc = create_worker_mod.main(
        _report_argv(task, body, ["--worker-name", worker]), runner=runner
    )

    assert rc == 0
    assert not any(c.argv[:2] == ["mngr", "rsync"] for c in runner.calls)
    assert (lead_work_dir / report_rel).is_file()
    assert "no `lead_agent`" in capsys.readouterr().err


def _fallback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listing: _StubResult,
    worker_name: str | None,
) -> tuple[int, Path]:
    """Run ``report`` with no lead in the task file (so delivery goes straight
    to the listing fallback) against ``listing``, and return the exit code and
    the local report path."""
    monkeypatch.chdir(tmp_path)
    task, report_rel, body = _worker_tree(tmp_path)
    _write_worker_task(task, report_rel, None)
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    runner = _RecordingRunner()
    runner.respond(("mngr", "list"), listing)
    extra = () if worker_name is None else ("--worker-name", worker_name)
    rc = create_worker_mod.main(_report_argv(task, body, extra), runner=runner)
    return rc, Path(report_rel)


def test_report_fails_loudly_when_the_worker_cannot_name_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No MNGR_AGENT_NAME and no --worker-name: there is nothing to look the
    lead up by, so the run fails instead of leaving the report where only the
    worker can see it."""
    rc, report = _fallback_failure(tmp_path, monkeypatch, _listing(), None)

    assert rc == 2
    err = capsys.readouterr().err
    assert "MNGR_AGENT_NAME is unset" in err
    # The local copy is still named, so the report is recoverable by hand.
    assert str(report) in err
    assert report.is_file()


def test_report_fails_loudly_when_the_listing_has_no_record_for_this_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = _unique("worker")

    rc, _report = _fallback_failure(tmp_path, monkeypatch, _listing(), worker)

    assert rc == 2
    err = capsys.readouterr().err
    assert "no agent record named" in err
    assert worker in err


def test_report_fails_loudly_when_this_worker_carries_no_lead_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = _unique("worker")

    rc, _report = _fallback_failure(
        tmp_path, monkeypatch, _listing(_agent_record(worker, "RUNNING")), worker
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "no `lead_agent` label" in err
    assert worker in err


def test_report_fails_loudly_when_the_labelled_lead_has_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = _unique("worker")
    lead = _unique("lead")

    rc, _report = _fallback_failure(
        tmp_path,
        monkeypatch,
        _listing(_agent_record(worker, "RUNNING", lead_agent=lead)),
        worker,
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "no agent record named" in err
    assert lead in err


def test_report_fails_loudly_when_the_lead_record_has_no_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = _unique("worker")
    lead = _unique("lead")

    rc, _report = _fallback_failure(
        tmp_path,
        monkeypatch,
        _listing(
            _agent_record(worker, "RUNNING", lead_agent=lead),
            _agent_record(lead, "WAITING"),
        ),
        worker,
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "no `work_dir`" in err
    assert lead in err


def test_report_missing_body_file_is_fatal_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --body-file that isn't there is a caller mistake: fail with a clean
    message rather than delivering an empty report."""
    monkeypatch.chdir(tmp_path)
    task, report_rel, _ = _worker_tree(tmp_path)
    _write_worker_task(task, report_rel, _unique("lead"))
    runner = _RecordingRunner()

    rc = create_worker_mod.main(
        _report_argv(task, tmp_path / "no-such-body.md"), runner=runner
    )

    assert rc == 2
    assert runner.calls == []
    assert not Path(report_rel).exists()
    assert "body-file" in capsys.readouterr().err


def test_report_missing_finish_report_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task file with no finish_report_path is an authoring bug on the lead's
    side; it raises with a full traceback rather than a terse exit code."""
    monkeypatch.chdir(tmp_path)
    task, _, body = _worker_tree(tmp_path)
    task.write_text(f"---\nlead_agent: {_unique('lead')}\n---\n\ndo the thing\n")

    with pytest.raises(ValueError, match="finish_report_path"):
        create_worker_mod.main(_report_argv(task, body), runner=_RecordingRunner())


def test_report_fails_loudly_when_the_report_path_cannot_be_relativized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absolute ``finish_report_path`` joins onto the lead's work dir as
    itself, so the fallback would rewrite the worker's own copy and claim
    success. That is the one outcome delivery must never produce, so it is a
    loud failure instead."""
    monkeypatch.chdir(tmp_path)
    task, _, body = _worker_tree(tmp_path)
    absolute_report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "report.md"
    )
    worker = _unique("worker")
    lead = _unique("lead")
    _write_worker_task(task, str(absolute_report), None)
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "list"),
        _listing(
            _agent_record(worker, "RUNNING", lead_agent=lead),
            _agent_record(lead, "WAITING", work_dir=str(tmp_path / "leads" / lead)),
        ),
    )

    rc = create_worker_mod.main(
        _report_argv(task, body, ["--worker-name", worker]), runner=runner
    )

    assert rc == 2
    assert "absolute" in capsys.readouterr().err
    # The worker's own copy is intact and named in the message, so the report is
    # still recoverable by hand.
    assert "type: status" in absolute_report.read_text()


# --- await: milestone reports --------------------------------------------
#
# A worker drops non-blocking milestone reports under ``milestones/`` beside
# ``report.md``. ``await`` returns one like a report, and archives it under its
# *own* name: the worker keeps its copy and re-delivers it with every later
# push, and the basename in ``consumed/`` is what makes a re-delivered copy
# inert while the same name at a new commit stays a new event.


def _write_milestone(report_path: Path, filename: str, body: str) -> Path:
    """Drop a milestone file into ``milestones/`` beside the report path.

    The layout is spelled out rather than taken from the module under test so
    the on-disk contract is asserted independently.
    """
    milestones_dir = report_path.parent / "milestones"
    milestones_dir.mkdir(parents=True, exist_ok=True)
    path = milestones_dir / filename
    path.write_text(f"---\ntype: milestone\nname: usable\n---\n\n{body}\n")
    return path


def _mark_consumed(report_path: Path, filename: str) -> Path:
    """Record a milestone basename as already handled by the lead."""
    consumed_dir = report_path.parent / "consumed"
    consumed_dir.mkdir(parents=True, exist_ok=True)
    path = consumed_dir / filename
    path.write_text("handled\n")
    return path


def test_await_returns_a_milestone_when_no_report_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconsumed milestone ends the poll like a report: contents out, path on
    stderr, and the file archived under its own name."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    milestone = _write_milestone(report, "abc1234-skill-runs.md", "invoke it now")
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=30,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
    )

    assert rc == 0
    assert "type: milestone" in out.getvalue()
    assert "invoke it now" in out.getvalue()
    err = capsys.readouterr().err
    assert str(milestone) in err
    archived = report.parent / "consumed" / milestone.name
    assert str(archived) in err
    # Archived, not deleted, and under the basename that keys the dedupe -- the
    # sha stays in the name so a deferred merge can still find its commit.
    assert not milestone.exists()
    assert "invoke it now" in archived.read_text()
    assert not report.exists()


def test_await_archived_milestone_makes_its_redelivery_inert(tmp_path: Path) -> None:
    """The worker re-pushes every milestone it declared; once one has been
    returned and archived, the same file arriving again does not end a poll."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    _write_milestone(report, "abc1234-skill-runs.md", "usable")
    assert (
        create_worker_mod.await_report(
            report_path=report,
            timeout_seconds=30,
            poll_interval_seconds=5,
            sleeper=_no_sleep,
            clock=_FakeClock(step=20),
            out=io.StringIO(),
        )
        == 0
    )

    # The worker's next push lands the same milestone file again.
    _write_milestone(report, "abc1234-skill-runs.md", "usable")
    out = io.StringIO()
    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=30,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
    )

    assert rc == create_worker_mod._AWAIT_TIMEOUT_RC
    assert out.getvalue() == ""
    # The archive is untouched by the re-delivery.
    assert sorted(p.name for p in (report.parent / "consumed").iterdir()) == [
        "abc1234-skill-runs.md"
    ]


def test_await_prefers_the_report_over_an_unconsumed_milestone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """report.md wins when both are present; the milestone waits for the next poll."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    report.write_text("---\ntype: status\nname: done\n---\n\nterminal\n")
    milestone = _write_milestone(report, "abc1234-skill-runs.md", "in flight")
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=30,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
    )

    assert rc == 0
    assert "terminal" in out.getvalue()
    assert "in flight" not in out.getvalue()
    # The milestone was neither emitted nor announced, and survives on disk for
    # the next poll.
    assert str(milestone) not in capsys.readouterr().err
    assert milestone.is_file()


def test_await_skips_a_milestone_whose_basename_is_already_consumed(
    tmp_path: Path,
) -> None:
    """A re-delivered milestone whose basename is in consumed/ is inert."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    _write_milestone(report, "abc1234-skill-runs.md", "already handled")
    _mark_consumed(report, "abc1234-skill-runs.md")
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=30,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
    )

    assert rc == create_worker_mod._AWAIT_TIMEOUT_RC
    assert out.getvalue() == ""


def test_await_returns_the_oldest_unconsumed_milestone_first(tmp_path: Path) -> None:
    """Oldest mtime first; the filenames are chosen so name order would disagree.
    Archiving the returned one is what advances the next poll to the newer."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    older = _write_milestone(report, "zzz9999-scenarios-pass.md", "declared first")
    newer = _write_milestone(report, "aaa1111-skill-runs.md", "declared second")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_600, 1_700_000_600))

    def _await_once() -> str:
        out = io.StringIO()
        rc = create_worker_mod.await_report(
            report_path=report,
            timeout_seconds=30,
            poll_interval_seconds=5,
            sleeper=_no_sleep,
            clock=_FakeClock(step=20),
            out=out,
        )
        assert rc == 0
        return out.getvalue()

    first = _await_once()
    assert "declared first" in first
    assert "declared second" not in first
    assert "declared second" in _await_once()


def test_await_ignores_milestones_when_the_watch_is_disabled(tmp_path: Path) -> None:
    """watch_milestones=False (the launch_sync contract) lets the wait run to timeout."""
    report = (
        tmp_path / "data" / ".tasks" / "launch-task" / "demo" / "reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    milestone = _write_milestone(report, "abc1234-skill-runs.md", "in flight")
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=30,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
        watch_milestones=False,
    )

    assert rc == create_worker_mod._AWAIT_TIMEOUT_RC
    assert out.getvalue() == ""
    assert milestone.is_file()


def test_launch_refuses_on_an_unconsumed_milestone_until_it_is_moved_aside(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A leftover milestone blocks a relaunch like a leftover report; consuming it clears the guard."""
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    milestone = _write_milestone(report, "abc1234-skill-runs.md", "from the last run")
    task.write_text(
        f"---\nlead_agent: lead\nfinish_report_path: {report}\n---\n\nbody\n"
    )
    blocked_runner = _RecordingRunner()

    blocked_rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=blocked_runner,
    )

    assert blocked_rc == 2
    assert blocked_runner.calls == []
    err = capsys.readouterr().err
    assert "milestone" in err
    assert str(milestone) in err

    # Consume it the way the message tells the caller to, and the same launch
    # goes through.
    consumed_dir = report.parent / "consumed"
    consumed_dir.mkdir(parents=True, exist_ok=True)
    milestone.replace(consumed_dir / milestone.name)
    cleared_runner = _RecordingRunner()

    cleared_rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        runner=cleared_runner,
    )

    assert cleared_rc == 0
    assert any(c.argv[:2] == ["mngr", "create"] for c in cleared_runner.calls)


def test_launch_sync_collects_the_terminal_report_despite_a_milestone(
    tmp_path: Path,
) -> None:
    """A mid-run milestone neither ends the wait nor blocks the next launch_sync.

    The milestone lands on the first poll sleep (a pre-existing one would be
    refused at launch) and the terminal report on the second.
    """
    runtime, task, _ = _make_layout(tmp_path)
    report = runtime / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    _write_launch_sync_task(task, report)
    result_json = tmp_path / "result.json"
    runner = _RecordingRunner()
    sleeps: list[float] = []
    milestone_path = report.parent / "milestones" / "abc1234-skill-runs.md"

    def _worker_declares_then_finishes(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            _write_milestone(report, milestone_path.name, "usable already")
        elif len(sleeps) == 2:
            report.write_text("---\ntype: status\nname: done\n---\n\nhardened\n")

    rc = create_worker_mod.launch_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        runner=runner,
        sleeper=_worker_declares_then_finishes,
        clock=lambda: 0.0,
        out=io.StringIO(),
        result_path=result_json,
        archive_timestamp=lambda: _PINNED_STAMP,
    )

    assert rc == 0
    payload = json.loads(result_json.read_text())
    assert payload["type"] == "status"
    assert payload["name"] == "done"
    assert payload["body"] == "hardened"
    # The milestone did not end the wait, and is archived (not deleted) under
    # its own name once the terminal report is collected -- beside the report's
    # own timestamped archive.
    assert not milestone_path.exists()
    consumed = report.parent / "consumed"
    assert "usable already" in (consumed / milestone_path.name).read_text()
    assert sorted(p.name for p in consumed.iterdir()) == [
        f"{_PINNED_STAMP}-status-done.md",
        milestone_path.name,
    ]

    # A second identical call is not refused by launch's milestone guard.
    second_rc = create_worker_mod.launch_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        runner=_RecordingRunner(),
        sleeper=_write_report_on_sleep(
            report, "---\ntype: status\nname: done\n---\n\nagain\n"
        ),
        clock=lambda: 0.0,
        out=io.StringIO(),
    )
    assert second_rc == 0


# --- the git bookkeeping a provisional milestone merge relies on ---------------
#
# create_worker.py only delivers a milestone; the lead merges the pinned commit.
# These two tests run real git to prove the spec's two claims about what follows:
# a provisional merge advances the merge-base so `done` brings only the
# remainder, and reverting one leaves its commits as ancestors, so the change
# stays out of `done` until the revert is itself reverted.

_CREATION_FILENAME = "skill.md"

# The worker's two commits touch lines 2 and 8: far enough apart that the merges
# exercise bookkeeping, not conflict resolution.
_CREATION_BASE_TEXT = "".join(f"line{i}\n" for i in range(1, 10))


def _git(repo: Path, *args: str) -> str:
    """Run one git command in ``repo`` and return its trimmed stdout.

    Identity is pinned and the user's git config disabled, so a developer's
    gpgsign or hooks cannot make the test flaky.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Test Lead",
            "GIT_AUTHOR_EMAIL": "lead@example.invalid",
            "GIT_COMMITTER_NAME": "Test Lead",
            "GIT_COMMITTER_EMAIL": "lead@example.invalid",
        },
    )
    return result.stdout.strip()


def _make_lead_repo_and_worker_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A lead repo on ``main`` holding a committed creation file, plus a linked
    worktree on ``mngr/demo`` standing in for the worker's own checkout."""
    lead = tmp_path / "lead"
    lead.mkdir()
    _git(lead, "init", "--initial-branch=main")
    (lead / _CREATION_FILENAME).write_text(_CREATION_BASE_TEXT)
    _git(lead, "add", _CREATION_FILENAME)
    _git(lead, "commit", "-m", "Add the creation")
    worker = tmp_path / "worker"
    _git(lead, "worktree", "add", "-b", "mngr/demo", str(worker))
    return lead, worker


def _commit_creation_line(
    worktree: Path, line_index: int, text: str, subject: str
) -> str:
    """Rewrite one line of the creation file in ``worktree`` and commit it."""
    path = worktree / _CREATION_FILENAME
    lines = path.read_text().splitlines()
    lines[line_index] = text
    path.write_text("\n".join(lines) + "\n")
    _git(worktree, "commit", "-am", subject)
    return _git(worktree, "rev-parse", "HEAD")


def test_provisional_merge_advances_the_merge_base_so_done_brings_the_rest(
    tmp_path: Path,
) -> None:
    """The merge-base advances to the milestone, so ``done`` brings only the rest.

    The milestone commit lands once, and the lead's freshness check covers
    exactly the window since the provisional merge.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    lead, worker = _make_lead_repo_and_worker_worktree(tmp_path)

    # The worker reaches its milestone and the lead merges that exact sha --
    # never the branch tip, which keeps moving.
    milestone_sha = _commit_creation_line(
        worker, 1, "line2-usable", "Worker commit A (milestone)"
    )
    _git(
        lead,
        "merge",
        "--no-ff",
        milestone_sha,
        "-m",
        "Provisional merge of demo at milestone skill-runs",
    )

    # Before the worker's next commit, the merge-base is the milestone commit
    # and the lead has made no foreground edit of its own: the creation is
    # fresh, so the pass would not be superseded.
    assert _git(lead, "merge-base", "HEAD", "mngr/demo") == milestone_sha
    assert (
        _git(
            lead,
            "diff",
            "--name-only",
            milestone_sha,
            "HEAD",
            "--",
            _CREATION_FILENAME,
        )
        == ""
    )

    # The worker keeps hardening, then reports done and the lead merges the
    # branch exactly as it does today.
    _commit_creation_line(worker, 7, "line8-hardened", "Worker commit B")
    _git(lead, "merge", "--no-ff", "mngr/demo", "-m", "Merge demo (done)")

    creation_text = (lead / _CREATION_FILENAME).read_text()
    assert "line8-hardened" in creation_text
    assert "line2-usable" in creation_text
    subjects = _git(lead, "log", "--format=%s", "main").splitlines()
    assert subjects.count("Worker commit A (milestone)") == 1
    merge_base = _git(lead, "merge-base", "HEAD", "mngr/demo")
    assert (
        _git(lead, "diff", "--name-only", merge_base, "HEAD", "--", _CREATION_FILENAME)
        == ""
    )


def test_reverting_a_provisional_merge_hides_it_from_done_until_reinstated(
    tmp_path: Path,
) -> None:
    """``git revert -m 1`` leaves the commits as ancestors, so ``done`` omits them until the revert is reverted."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    lead, worker = _make_lead_repo_and_worker_worktree(tmp_path)

    milestone_sha = _commit_creation_line(
        worker, 1, "line2-usable", "Worker commit A (milestone)"
    )
    _git(
        lead,
        "merge",
        "--no-ff",
        milestone_sha,
        "-m",
        "Provisional merge of demo at milestone skill-runs",
    )
    provisional_merge = _git(lead, "rev-parse", "HEAD")

    # The lead rejects the milestone and rolls it back against the first parent.
    _git(lead, "revert", "-m", "1", "--no-edit", provisional_merge)
    revert_commit = _git(lead, "rev-parse", "HEAD")
    assert "line2-usable" not in (lead / _CREATION_FILENAME).read_text()

    # The worker carries on and reports done. The milestone commit is still an
    # ancestor, so this merge does NOT bring its change back.
    _commit_creation_line(worker, 7, "line8-hardened", "Worker commit B")
    _git(lead, "merge", "--no-ff", "mngr/demo", "-m", "Merge demo (done)")
    after_done_merge = (lead / _CREATION_FILENAME).read_text()
    assert "line8-hardened" in after_done_merge
    assert "line2-usable" not in after_done_merge

    # Reverting the revert is what puts it back.
    _git(lead, "revert", "--no-edit", revert_commit)
    assert "line2-usable" in (lead / _CREATION_FILENAME).read_text()
