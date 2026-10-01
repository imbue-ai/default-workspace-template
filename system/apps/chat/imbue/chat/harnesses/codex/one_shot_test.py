from pathlib import Path
from typing import Any

import pytest

from imbue.chat.harnesses.codex.model import codex_model_options_path
from imbue.chat.harnesses.codex.model import write_codex_model_options
from imbue.chat.harnesses.codex.one_shot import CodexOneShotChoice
from imbue.chat.harnesses.codex.one_shot import CodexOneShotCompletion
from imbue.chat.harnesses.codex.one_shot import choose_codex_one_shot_model
from imbue.chat.harnesses.codex.one_shot import codex_one_shot_argv
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.testing import put_stand_in_cli_on_path
from imbue.mngr_codex.app_server_client import CodexModel


def _model(slug: str, efforts: tuple[str, ...] = ("low", "medium", "high"), is_default: bool = False) -> CodexModel:
    raw: dict[str, Any] = {
        "id": slug,
        "model": slug,
        "displayName": slug,
        "isDefault": is_default,
        "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in efforts],
    }
    return CodexModel.model_validate(raw)


def test_the_newest_luna_model_is_chosen_at_its_lowest_effort() -> None:
    models = (
        _model("gpt-6-sol", is_default=True),
        _model("gpt-5.6-luna", efforts=("minimal", "low")),
        _model("gpt-6-luna", efforts=("high", "low", "medium")),
        _model("gpt-6-astra"),
    )

    assert choose_codex_one_shot_model(models) == CodexOneShotChoice(model="gpt-6-luna", effort="low")


def test_an_account_with_no_luna_model_keeps_its_default_model_at_that_models_lowest_effort() -> None:
    models = (_model("gpt-6-astra"), _model("gpt-6-sol", efforts=("medium", "high"), is_default=True))

    assert choose_codex_one_shot_model(models) == CodexOneShotChoice(model=None, effort="medium")


def test_an_account_whose_models_are_unknown_runs_entirely_on_its_defaults() -> None:
    assert choose_codex_one_shot_model(()) == CodexOneShotChoice()


def test_codex_one_shot_argv_reads_the_prompt_from_stdin_and_keeps_nothing() -> None:
    argv = codex_one_shot_argv(CodexOneShotChoice(model="gpt-6-luna", effort="low"), Path("/tmp/answer.txt"))

    assert argv[:6] == ["codex", "exec", "--model", "gpt-6-luna", "-c", 'model_reasoning_effort="low"']
    assert {"--ephemeral", "--ignore-user-config", "--skip-git-repo-check"} <= set(argv)
    assert argv[-3:] == ["--output-last-message", "/tmp/answer.txt", "-"]


def test_codex_one_shot_argv_names_no_model_or_effort_for_the_default() -> None:
    argv = codex_one_shot_argv(CodexOneShotChoice(), Path("/tmp/answer.txt"))

    assert "--model" not in argv
    assert "-c" not in argv


# Writes its answer to the file after --output-last-message, as codex exec does.
_ANSWERING_BODY = """cat > /dev/null
while [ $# -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then echo "Rome trip: plan five days" > "$2"; fi
  shift
done"""


def _account_offering(tmp_path: Path, *models: CodexModel) -> Path:
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    write_codex_model_options(codex_model_options_path(account_dir), models)
    return account_dir


def test_complete_answers_on_the_luna_model_the_account_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_dir = _account_offering(tmp_path, _model("gpt-6-sol", is_default=True), _model("gpt-6-luna"))
    log_path = put_stand_in_cli_on_path(tmp_path, "codex", _ANSWERING_BODY, monkeypatch)

    answer = CodexOneShotCompletion().complete(account_dir, "Name this chat.", "Plan Rome")

    assert answer == "Rome trip: plan five days"
    assert '--model gpt-6-luna -c model_reasoning_effort="low"' in log_path.read_text()


def test_complete_falls_back_to_the_default_model_when_the_chosen_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_dir = _account_offering(tmp_path, _model("gpt-6-luna"))
    body = 'case " $* " in *" --model "*) echo "model is not supported" >&2; exit 1;; esac\n' + _ANSWERING_BODY
    log_path = put_stand_in_cli_on_path(tmp_path, "codex", body, monkeypatch)

    answer = CodexOneShotCompletion().complete(account_dir, "Name this chat.", "Plan Rome")

    assert answer == "Rome trip: plan five days"
    calls = log_path.read_text().splitlines()
    assert len(calls) == 2
    assert "--model gpt-6-luna" in calls[0]
    assert "--model" not in calls[1]


def test_complete_raises_when_codex_writes_no_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    account_dir = _account_offering(tmp_path, _model("gpt-6-sol", is_default=True))
    put_stand_in_cli_on_path(tmp_path, "codex", "cat > /dev/null", monkeypatch)

    with pytest.raises(OneShotCompletionError, match="no answer"):
        CodexOneShotCompletion().complete(account_dir, "Name this chat.", "Plan Rome")
