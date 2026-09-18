"""Unit tests for the verifier's last step, which keeps the judges' derived inputs under
/logs/verifier. Like the other verifier scripts it ships as a self-contained stdlib file, so it is
loaded by file path rather than imported as a package module."""

from pathlib import Path

from imbue.minds_evals.template_loading import TEMPLATES_DIR
from imbue.minds_evals.template_loading import load_template_module

_KEEPER = load_template_module("tests/verifier/keep_derived_outputs.py", "minds_evals_derived_output_keeper")


def test_every_derived_input_that_was_written_is_copied_and_the_screenshots_are_listed(tmp_path: Path) -> None:
    agent_logs_dir = tmp_path / "agent"
    screenshots_dir = agent_logs_dir / "judge_screenshots"
    screenshots_dir.mkdir(parents=True)
    (agent_logs_dir / "judge_transcript.txt").write_text("[PROGRESS · step declared]\nDIAG alpha 7f3a\n")
    (agent_logs_dir / "progress_summary.json").write_text('{"steps": 2}')
    (agent_logs_dir / "harness_failures.json").write_text('{"main": {}}')
    (agent_logs_dir / "judge_flows_digest.txt").write_text("# UI flow evidence\n")
    (screenshots_dir / "01_add_step_003.png").write_bytes(b"\x89PNG")
    (screenshots_dir / "00_add_step_002.png").write_bytes(b"\x89PNG")
    derived_dir = tmp_path / "verifier" / "derived"

    failures = _KEEPER.keep_derived_outputs(agent_logs_dir, derived_dir)

    assert failures == []
    assert (derived_dir / "judge_transcript.txt").read_text() == "[PROGRESS · step declared]\nDIAG alpha 7f3a\n"
    assert (derived_dir / "progress_summary.json").read_text() == '{"steps": 2}'
    assert (derived_dir / "harness_failures.json").read_text() == '{"main": {}}'
    assert (derived_dir / "judge_flows_digest.txt").read_text() == "# UI flow evidence\n"
    assert (derived_dir / "judge_screenshots.txt").read_text() == "00_add_step_002.png\n01_add_step_003.png\n"


def test_an_input_a_failed_pre_step_never_wrote_is_skipped_rather_than_reported(tmp_path: Path) -> None:
    """The keeper runs on the way out of a grade that may have died before writing anything, and a
    listing that never existed is different from an empty one, so neither is invented."""
    agent_logs_dir = tmp_path / "agent"
    agent_logs_dir.mkdir()
    (agent_logs_dir / "progress_summary.json").write_text("{}")
    derived_dir = tmp_path / "verifier" / "derived"

    failures = _KEEPER.keep_derived_outputs(agent_logs_dir, derived_dir)

    assert failures == []
    assert sorted(path.name for path in derived_dir.iterdir()) == ["progress_summary.json"]


def test_a_derived_directory_that_cannot_be_made_is_reported_without_raising(tmp_path: Path) -> None:
    agent_logs_dir = tmp_path / "agent"
    agent_logs_dir.mkdir()
    (agent_logs_dir / "progress_summary.json").write_text("{}")
    blocking_file = tmp_path / "verifier"
    blocking_file.write_text("a file where the directory should go")

    failures = _KEEPER.keep_derived_outputs(agent_logs_dir, blocking_file / "derived")

    assert len(failures) == 1
    assert str(blocking_file / "derived") in failures[0]


def test_the_verifier_keeps_its_derived_inputs_on_every_exit_and_never_fails_the_grade_for_it() -> None:
    """The copy is armed before the first pre-step, so a grade that aborts under `set -e` still keeps
    what it had derived, and it is guarded so a copy that fails cannot change the exit status."""
    test_sh = (TEMPLATES_DIR / "tests" / "verifier" / "test.sh").read_text()

    assert "trap keep_derived_outputs EXIT" in test_sh
    assert "python3 /tests/keep_derived_outputs.py || true" in test_sh
    assert test_sh.index("trap keep_derived_outputs EXIT") < test_sh.index("python3 /tests/render_judge_transcript.py")
