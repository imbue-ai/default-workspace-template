from pathlib import Path

from imbue.chat.chat_model_defaults import ChatModelDefaultsStore
from imbue.chat.models import ModelPick


def test_model_defaults_survive_a_restart_and_stay_separate_for_each_account(tmp_path: Path) -> None:
    path = tmp_path / "chat" / "model-defaults.json"
    store = ChatModelDefaultsStore(path=path)
    opus = ModelPick(model_id="opus[1m]", effort="max")
    store.remember("anthropic", opus)
    store.remember("openai", ModelPick(model_id="gpt-6-astra", effort="high"))

    restarted = ChatModelDefaultsStore(path=path)
    assert restarted.get("anthropic") == opus
    assert restarted.get("openai") == ModelPick(model_id="gpt-6-astra", effort="high")
    assert restarted.get("signed-out-account") is None


def test_unreadable_model_defaults_leave_new_chats_usable(tmp_path: Path) -> None:
    path = tmp_path / "model-defaults.json"
    path.write_text("not json")
    store = ChatModelDefaultsStore(path=path)
    assert store.get("account") is None
    store.remember("account", ModelPick(model_id="haiku", effort="low"))
    assert ChatModelDefaultsStore(path=path).get("account") == ModelPick(model_id="haiku", effort="low")
