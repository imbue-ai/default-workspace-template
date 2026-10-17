from pathlib import Path

import pytest

from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.pi_coding.one_shot import PiOneShotCompletion
from imbue.chat.harnesses.pi_coding.one_shot import choose_pi_one_shot_models
from imbue.chat.harnesses.pi_coding.one_shot import pi_one_shot_argv
from imbue.chat.harnesses.pi_coding.one_shot import pi_one_shot_env
from imbue.chat.testing import make_pi_account_with_anthropic_key
from imbue.chat.testing import put_stand_in_cli_on_path


@pytest.mark.parametrize(
    ("offered", "providers", "expected"),
    [
        (
            (
                "anthropic/claude-haiku-4-5",
                "anthropic/claude-haiku-4-5-20251001",
                "anthropic/claude-haiku-5-5",
                "anthropic/claude-opus-5-5",
                "anthropic/claude-3-5-haiku-20241022",
            ),
            {"anthropic"},
            ("anthropic/claude-haiku-5-5", None),
        ),
        (
            ("anthropic/claude-haiku-4-5", "anthropic/claude-haiku-4-5-20251001", "anthropic/claude-opus-4-8"),
            {"anthropic"},
            ("anthropic/claude-haiku-4-5", None),
        ),
        (("openai/gpt-6-sol", "openai/gpt-5.6-luna", "openai/gpt-6-luna"), {"openai"}, ("openai/gpt-6-luna", None)),
        (
            ("google/gemini-2.5-flash", "google/gemini-3-flash-preview", "google/gemini-3.1-pro-preview"),
            {"google"},
            ("google/gemini-3-flash-preview", None),
        ),
        (
            (
                "openrouter/google/gemini-3-flash",
                "openrouter/openai/gpt-6-luna",
                "openrouter/anthropic/claude-haiku-4.5",
            ),
            {"openrouter"},
            ("openrouter/anthropic/claude-haiku-4.5", None),
        ),
        (("opencode-go/kimi-k2.6", "opencode-go/glm-5.1"), {"opencode-go"}, (None,)),
        # Models reachable through credentials that are not the account's (an ~/.aws profile) are never used, and
        # pi's own default could land on them, so it is not tried either.
        (
            ("amazon-bedrock/us.anthropic.claude-haiku-9-9", "anthropic/claude-haiku-5-5"),
            {"anthropic"},
            ("anthropic/claude-haiku-5-5",),
        ),
        (("amazon-bedrock/us.anthropic.claude-haiku-9-9", "groq/openai/gpt-oss-120b"), {"groq"}, ()),
        ((), {"anthropic"}, ()),
    ],
)
def test_the_newest_small_model_on_the_accounts_own_provider_is_tried_before_the_default(
    offered: tuple[str, ...], providers: set[str], expected: tuple[str | None, ...]
) -> None:
    assert choose_pi_one_shot_models(offered, frozenset(providers)) == expected


def test_pi_one_shot_argv_runs_one_print_turn_with_nothing_loaded_and_thinking_off() -> None:
    argv = pi_one_shot_argv("Name this chat.", "anthropic/claude-haiku-5-5")

    assert argv[:2] == ["pi", "-p"]
    assert {"--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-context-files"} <= set(argv)
    assert argv[argv.index("--thinking") + 1] == "off"
    assert argv[argv.index("--model") + 1] == "anthropic/claude-haiku-5-5"
    assert argv[-2:] == ["--system-prompt", "Name this chat."]


def test_pi_one_shot_argv_names_no_model_for_the_default() -> None:
    assert "--model" not in pi_one_shot_argv("Name this chat.", None)


def test_pi_one_shot_env_runs_on_the_chats_account_not_the_servers_provider_keys(tmp_path: Path) -> None:
    ambient = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "server-key-5813", "OPENROUTER_API_KEY": "server-key-5813"}

    env = pi_one_shot_env(ambient, tmp_path / "account")

    assert env == {"PATH": "/usr/bin", "PI_CODING_AGENT_DIR": str(tmp_path / "account")}


# Lists models as `pi --list-models` does; answers anything else after reading the prompt from stdin.
_LIST_MODELS_THEN_ANSWER = """case " $* " in *" --list-models "*)
  printf 'provider    model               context\\n'
  printf 'anthropic   claude-haiku-4-5    200K\\n'
  printf 'anthropic   claude-haiku-5-5    1M\\n'
  printf 'anthropic   claude-opus-5-5     1M\\n'
  exit 0;;
esac
cat > /dev/null
echo 'Rome trip: plan five days'"""


def _complete_on_an_anthropic_account(tmp_path: Path) -> str:
    return PiOneShotCompletion().complete(make_pi_account_with_anthropic_key(tmp_path), "Name this chat.", "Plan Rome")


def test_complete_answers_on_the_newest_haiku_the_account_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = put_stand_in_cli_on_path(tmp_path, "pi", _LIST_MODELS_THEN_ANSWER, monkeypatch)

    answer = _complete_on_an_anthropic_account(tmp_path)

    assert answer == "Rome trip: plan five days"
    calls = log_path.read_text().splitlines()
    assert calls[0] == "--list-models"
    assert "--model anthropic/claude-haiku-5-5" in calls[1]


def test_complete_falls_back_to_the_default_model_when_the_chosen_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = 'case " $* " in *" --model "*) echo "400: model not found" >&2; exit 1;; esac\n' + _LIST_MODELS_THEN_ANSWER
    log_path = put_stand_in_cli_on_path(tmp_path, "pi", body, monkeypatch)

    answer = _complete_on_an_anthropic_account(tmp_path)

    assert answer == "Rome trip: plan five days"
    model_calls = [call for call in log_path.read_text().splitlines() if call != "--list-models"]
    assert len(model_calls) == 2
    assert "--model anthropic/claude-haiku-5-5" in model_calls[0]
    assert "--model" not in model_calls[1]


def test_complete_raises_with_every_models_failure_when_none_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _LIST_MODELS_THEN_ANSWER.replace("cat > /dev/null", 'echo "401: invalid x-api-key" >&2; exit 1')
    put_stand_in_cli_on_path(tmp_path, "pi", body, monkeypatch)

    with pytest.raises(OneShotCompletionError, match="claude-haiku-5-5: .*401.*default model: .*401"):
        _complete_on_an_anthropic_account(tmp_path)


@pytest.mark.parametrize(
    ("list_models_body", "error_match"),
    [
        pytest.param('echo "No models available. Use /login to authenticate."', "no model", id="pi-offers-none"),
        pytest.param('echo "auth.json is unreadable" >&2; exit 1', "could not list", id="pi-cannot-list"),
    ],
)
def test_complete_asks_no_model_when_pi_offers_none_on_the_account_or_cannot_list_them(
    list_models_body: str, error_match: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = put_stand_in_cli_on_path(tmp_path, "pi", list_models_body, monkeypatch)

    with pytest.raises(OneShotCompletionError, match=error_match):
        _complete_on_an_anthropic_account(tmp_path)
    assert log_path.read_text().splitlines() == ["--list-models"]


def test_complete_raises_when_pi_prints_no_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _LIST_MODELS_THEN_ANSWER.replace("echo 'Rome trip: plan five days'", "")
    put_stand_in_cli_on_path(tmp_path, "pi", body, monkeypatch)

    with pytest.raises(OneShotCompletionError, match="no answer"):
        _complete_on_an_anthropic_account(tmp_path)
