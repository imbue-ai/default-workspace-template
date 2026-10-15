import pytest

from studio_discord import config, discord
from studio_discord.web import HttpError

GENERAL, SHOW, HELP = (c.id for c in config.CHANNELS)
EARLY_ACCESS = "1515031776420954112"


@pytest.mark.parametrize(
    "path",
    [
        "users/@me",
        f"channels/{GENERAL}",
        f"channels/{GENERAL}/messages?limit=3",
        f"channels/{SHOW}/messages?limit=3",
        f"channels/{HELP}/threads/search?archived=true&limit=3",
    ],
)
def test_the_studio_channels_and_the_signed_in_user_are_readable(path: str) -> None:
    assert discord._allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        f"channels/{EARLY_ACCESS}",  # in the Studio category, but archived: never shown
        f"channels/{EARLY_ACCESS}/messages",
        "users/@me/guilds",  # would list the user's other servers
        "users/@me/channels",  # the user's direct messages
        f"guilds/{config.GUILD_ID}/channels",
        f"channels/{GENERAL}/messages/123/reactions",
        f"channels/{GENERAL}x/messages",
        "channels/../users/@me/channels",
    ],
)
def test_anything_else_is_refused_before_any_request(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("a request was made")

    monkeypatch.setattr(discord, "request_json", fail)
    assert not discord._allowed(path)
    with pytest.raises(discord.OutOfScopeError):
        discord._get(path)


def test_reads_go_through_the_gateway_with_its_password(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(url: str, **kwargs: object) -> list[object]:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return []

    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.invalid:1989/")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw")
    monkeypatch.delenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", raising=False)
    monkeypatch.setattr(discord, "request_json", fake)
    discord.latest_messages(GENERAL, 3)
    assert (
        seen["url"]
        == f"http://gateway.invalid:1989/gateway/https://discord.com/api/v10/channels/{GENERAL}/messages?limit=3"
    )
    assert seen["headers"] == {"X-Latchkey-Gateway-Password": "pw"}


def test_only_discords_missing_access_answer_means_not_a_member() -> None:
    assert discord.is_not_a_member(HttpError(403, '{"message": "Missing Access", "code": 50001}', "u"))
    # Cloudflare's edge block is also a 403, but says nothing about membership.
    assert not discord.is_not_a_member(HttpError(403, '{"title": "Error 1010"}', "u"))
    assert not discord.is_not_a_member(HttpError(404, '{"code": 10003}', "u"))


def test_plain_text_makes_markup_readable() -> None:
    message = {
        "content": f"hi <@42> see <#{GENERAL}> and <#999> <@&7> <:wave:123> <a:spin:456>",
        "mentions": [{"id": "42", "global_name": "Ana", "username": "ana1"}],
    }
    assert discord.plain_text(message) == "hi @Ana see #general and #channel @role :wave: :spin:"


def test_plain_text_of_an_attachment_only_message_is_empty() -> None:
    assert discord.plain_text({"content": "", "attachments": [{}]}) == ""
