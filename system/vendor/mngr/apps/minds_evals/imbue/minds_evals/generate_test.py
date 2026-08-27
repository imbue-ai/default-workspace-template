import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DEFAULT_AVG_WORD_COUNT_BASELINE
from imbue.minds_evals.data_types import DEFAULT_DWT_REPO
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.errors import EvalConfigError
from imbue.minds_evals.errors import GitSourceError
from imbue.minds_evals.generate import derive_case_id
from imbue.minds_evals.generate import generate_dataset
from imbue.minds_evals.generate import load_eval_config
from imbue.minds_evals.generate import resolve_remote_tip
from imbue.minds_evals.testing import make_local_git_repo


def _write_config(tmp_path: Path, config: dict[str, object]) -> Path:
    config_path = tmp_path / "eval-config.json"
    config_path.write_text(json.dumps(config))
    return config_path


def _valid_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "mngr_branch": "main",
        "timeout_seconds": 1800,
        "personas": [
            {
                "id": "todo-app",
                "persona": "Non-technical founder.",
                "prompts": ["Build me a to-do app", "Sounds good.", DECIDE_SENTINEL],
            },
            {"prompts": ["hi what can you do", DECIDE_SENTINEL]},
        ],
    }
    config.update(overrides)
    return config


def test_load_eval_config_parses_cases_and_defaults(tmp_path: Path) -> None:
    config = load_eval_config(_write_config(tmp_path, _valid_config()))

    assert config.mngr_branch == "main"
    assert config.dwt_repo == DEFAULT_DWT_REPO
    assert config.timeout_seconds == 1800.0
    assert config.avg_word_count_baseline == DEFAULT_AVG_WORD_COUNT_BASELINE
    assert [case.case_id for case in config.cases] == ["todo-app", "case-2"]
    assert config.cases[1].persona == ""


def test_load_eval_config_rejects_missing_mngr_branch(tmp_path: Path) -> None:
    with pytest.raises(EvalConfigError, match="mngr_branch"):
        load_eval_config(_write_config(tmp_path, {"personas": [{"prompts": ["hi"]}]}))


def test_load_eval_config_rejects_decide_sentinel_as_first_prompt(tmp_path: Path) -> None:
    config = _valid_config(personas=[{"id": "bad", "prompts": [DECIDE_SENTINEL, "hi"]}])

    with pytest.raises(EvalConfigError, match="first prompt"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    config = _valid_config(personas=[{"id": "dup", "prompts": ["a"]}, {"id": "dup", "prompts": ["b"]}])

    with pytest.raises(EvalConfigError, match="duplicate case id"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_rejects_empty_prompt(tmp_path: Path) -> None:
    config = _valid_config(personas=[{"id": "empty", "prompts": ["hi", "  "]}])

    with pytest.raises(EvalConfigError, match="empty prompt"):
        load_eval_config(_write_config(tmp_path, config))


def test_derive_case_id_prefers_explicit_id_and_falls_back_to_position() -> None:
    assert derive_case_id({"id": "explicit"}, 0) == "explicit"
    assert derive_case_id({}, 2) == "case-3"


def test_resolve_remote_tip_returns_the_branch_tip(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=2)

    assert resolve_remote_tip(str(repo.repo_dir), "main") == repo.commit_shas[-1]


def test_resolve_remote_tip_raises_for_missing_branch(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)

    with pytest.raises(GitSourceError, match="not found"):
        resolve_remote_tip(str(repo.repo_dir), "no-such-branch-8471")


def _dir_content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_generate_dataset_writes_complete_byte_identical_tasks(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=2)
    config_path = _write_config(tmp_path, _valid_config(dwt_repo=str(dwt_repo.repo_dir)))
    output_dir = tmp_path / "dataset"

    task_dirs = generate_dataset(config_path=config_path, output_dir=output_dir, mngr_repo=str(repo.repo_dir))

    assert [task_dir.name for task_dir in task_dirs] == ["todo-app", "case-2"]
    expected_sha = repo.commit_shas[-1]
    expected_dwt_sha = dwt_repo.commit_shas[-1]

    for task_dir in task_dirs:
        task_config = tomllib.loads((task_dir / "task.toml").read_text())
        assert task_config["task"]["name"] == "minds-evals/{}".format(task_dir.name)
        assert task_config["metadata"]["mngr_sha"] == expected_sha
        # The workspace template is pinned to an exact SHA, recorded alongside
        # the branch it was resolved from.
        assert task_config["metadata"]["dwt_sha"] == expected_dwt_sha
        assert task_config["metadata"]["dwt_branch"] == "main"
        assert task_config["metadata"]["dwt_repo"] == str(dwt_repo.repo_dir)
        assert task_config["agent"]["timeout_sec"] == 1800.0 + 300.0
        assert task_config["verifier"]["environment_mode"] == "separate"
        assert task_config["verifier"]["env"]["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY}"
        assert set(task_config["artifacts"]) == {
            "/logs/agent/conversation.jsonl",
            "/logs/agent/full_transcript.jsonl",
            "/logs/agent/state.json",
        }

        # The instruction's fenced json block round-trips through the driver's parser.
        case_config = parse_case_config((task_dir / "instruction.md").read_text())
        assert case_config.case_id == task_dir.name
        assert case_config.mngr_sha == expected_sha
        assert case_config.dwt_sha == expected_dwt_sha

        # tests/ carries the verifier image inputs plus the case data.
        tests_case = json.loads((task_dir / "tests" / "case.json").read_text())
        assert tests_case == case_config.model_dump(mode="json")
        for expected_file in ("Dockerfile", "test.sh", "finalize.py", "gates/checks.py", "quality/judge.toml"):
            assert (task_dir / "tests" / expected_file).is_file()

        # environment/ carries the box image inputs plus the staged mngr clone
        # (no .git: Modal's build-context upload drops it, so nothing may rely on it).
        assert (task_dir / "environment" / "Dockerfile").is_file()
        assert (task_dir / "environment" / "entrypoint.sh").is_file()
        assert (task_dir / "environment" / "mngr" / "README.md").is_file()
        assert not (task_dir / "environment" / "mngr" / ".git").exists()
        assert (task_dir / "environment" / "mngr_sha").read_text().strip() == expected_sha

        # The oracle writes one user message per prompt into both the raw
        # transcript and the graded conversation file (hence twice).
        solve_text = (task_dir / "solution" / "solve.sh").read_text()
        assert solve_text.count('"user_message"') == 2 * len(case_config.prompts)
        assert solve_text.count('"assistant_message"') == 2 * len(case_config.prompts)
        assert "/logs/agent/conversation.jsonl" in solve_text

    # environment/ must be byte-identical across tasks or the Modal image cache diverges.
    digests = {_dir_content_digest(task_dir / "environment") for task_dir in task_dirs}
    assert len(digests) == 1


def test_generate_dataset_rejects_nonempty_output_dir(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config_path = _write_config(tmp_path, _valid_config(dwt_repo=str(dwt_repo.repo_dir)))
    output_dir = tmp_path / "dataset"
    output_dir.mkdir()
    (output_dir / "leftover.txt").write_text("stale")

    with pytest.raises(EvalConfigError, match="not empty"):
        generate_dataset(config_path=config_path, output_dir=output_dir, mngr_repo=str(repo.repo_dir))
