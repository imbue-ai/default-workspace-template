from pathlib import Path

import pytest

from imbue.chat.harnesses.codex.web_lookup import CodexWebLookup
from imbue.chat.harnesses.codex.web_lookup import codex_web_lookup_argv
from imbue.chat.harnesses.web_lookup import WebLookupError
from imbue.chat.testing import put_stand_in_cli_on_path

# Reads the prompt from stdin, and writes its answer to the file after --output-last-message, as codex exec does.
_ANSWERING_BODY = """cat > /dev/null
while [ $# -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then echo '{"candidates": []}' > "$2"; fi
  shift
done"""


def test_the_argv_turns_web_search_on_and_keeps_nothing_that_can_write() -> None:
    argv = codex_web_lookup_argv("gpt-6-sol", Path("/tmp/answer.txt"))

    assert argv[:4] == ["codex", "exec", "--model", "gpt-6-sol"]
    assert argv[argv.index("-c") + 1] == "tools.web_search.enabled=true"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check"):
        assert flag in argv
    assert argv[-3:] == ["--output-last-message", "/tmp/answer.txt", "-"]


def test_the_argv_names_no_model_for_the_accounts_default() -> None:
    assert "--model" not in codex_web_lookup_argv(None, Path("/tmp/answer.txt"))


def test_search_answers_on_the_model_it_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = put_stand_in_cli_on_path(tmp_path, "codex", _ANSWERING_BODY, monkeypatch)
    account_dir = tmp_path / "account"
    account_dir.mkdir()

    answer = CodexWebLookup().search(account_dir, "Find the person.", "Ada Lovelace, London", "gpt-6-sol")

    assert answer == '{"candidates": []}'
    (call,) = log_path.read_text().splitlines()
    assert "--model gpt-6-sol" in call
    assert "tools.web_search.enabled=true" in call


def test_search_raises_with_the_clis_complaint_when_it_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put_stand_in_cli_on_path(tmp_path, "codex", "cat > /dev/null; echo 'Not logged in' >&2; exit 1", monkeypatch)
    account_dir = tmp_path / "account"
    account_dir.mkdir()

    with pytest.raises(WebLookupError, match="exited 1: Not logged in"):
        CodexWebLookup().search(account_dir, "Find the person.", "Ada Lovelace, London", None)


def test_search_raises_when_codex_gives_no_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put_stand_in_cli_on_path(tmp_path, "codex", "cat > /dev/null", monkeypatch)
    account_dir = tmp_path / "account"
    account_dir.mkdir()

    with pytest.raises(WebLookupError, match="gave no answer"):
        CodexWebLookup().search(account_dir, "Find the person.", "Ada Lovelace, London", None)
