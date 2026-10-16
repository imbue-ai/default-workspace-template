from datetime import UTC, datetime

from studio_discord import config, invite, views

GENERAL, _, HELP = config.CHANNELS


def _message(message_id: str, when: str, text: str = "hi") -> dict[str, object]:
    return {
        "id": message_id,
        "timestamp": when,
        "content": text,
        "author": {"id": "7", "username": "ana1", "global_name": "Ana", "avatar": "abc"},
        "attachments": [{}, {}],
    }


def test_text_channel_shows_the_newest_few_oldest_first_like_discord() -> None:
    kept = {"items": {str(i): _message(str(i), f"2026-10-0{i}T10:00:00+00:00") for i in range(1, 6)}}
    items = views.channel_items(GENERAL, kept)
    assert [i["id"] for i in items] == ["3", "4", "5"]
    first = items[-1]
    assert first["author"] == "Ana"
    assert first["attachments"] == 2
    assert first["avatar"] == "https://cdn.discordapp.com/avatars/7/abc.png?size=64"
    assert first["url"] == f"https://discord.com/channels/{config.GUILD_ID}/{GENERAL.id}/5"


def test_forum_shows_posts_with_their_opening_message() -> None:
    thread = {
        "id": "900",
        "name": "Window goes blank",
        "message_count": 4,
        "thread_metadata": {"create_timestamp": "2026-10-10T10:00:00+00:00"},
    }
    opener = _message("900", "2026-10-10T10:00:00+00:00", "after sleep")
    kept = {
        "items": {
            "thread:900": {"id": "thread:900", "kind": "thread", "thread": thread},
            "first:900": {"id": "first:900", "kind": "first_message", "channel_id": "900", "message": opener},
        }
    }
    [post] = views.channel_items(HELP, kept)
    assert (post["title"], post["text"], post["author"], post["replies"]) == (
        "Window goes blank",
        "after sleep",
        "Ana",
        4,
    )
    assert post["url"] == f"https://discord.com/channels/{config.GUILD_ID}/900"


def test_a_forum_post_without_its_opener_still_shows() -> None:
    thread = {"id": "901", "name": "", "message_count": 0, "thread_metadata": {}}
    [post] = views.channel_items(
        HELP, {"items": {"thread:901": {"id": "thread:901", "kind": "thread", "thread": thread}}}
    )
    assert post["title"] == "Untitled post" and post["author"] is None


def test_live_topic_wins_over_the_built_in_one() -> None:
    assert views.channel_view(GENERAL, {"topic": "New words"}, {"items": {}})["topic"] == "New words"
    assert views.channel_view(GENERAL, None, {"items": {}})["topic"] == GENERAL.topic


def test_widget_view_puts_online_people_first_and_keeps_eight() -> None:
    members = [{"username": f"u{i}", "avatar_url": "x", "status": "idle" if i % 2 else "online"} for i in range(12)]
    view = views.widget_view({"name": "Imbue", "presence_count": 90, "members": members})
    assert view is not None and view["online"] == 90 and len(view["faces"]) == 8
    assert all(face["status"] == "online" for face in view["faces"][:6])
    assert views.widget_view(None) is None


def test_invite_prefers_the_live_catalog_then_the_widget_then_the_last_known() -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    live = {"invite_url": "https://discord.gg/live", "invite_expires_at": "2026-11-06T20:13:22+00:00"}
    expired = {"invite_url": "https://discord.gg/old", "invite_expires_at": "2026-10-01T00:00:00+00:00"}
    widget = {"instant_invite": "https://discord.com/invite/widget"}
    assert invite.choose(live, widget, now) == "https://discord.gg/live"
    assert invite.choose(expired, widget, now) == "https://discord.com/invite/widget"
    assert invite.choose(None, None, now) == config.LAST_KNOWN_INVITE


def test_forum_lists_the_newest_few_posts_newest_first_like_discord() -> None:
    def thread(i: int) -> dict[str, object]:
        return {
            "id": f"t{i}",
            "kind": "thread",
            "thread": {
                "id": f"t{i}",
                "name": f"post {i}",
                "message_count": 0,
                "thread_metadata": {"create_timestamp": f"2026-10-0{i}T10:00:00+00:00"},
            },
        }

    kept = {"items": {f"thread:t{i}": thread(i) for i in range(1, 6)}}
    assert [i["id"] for i in views.channel_items(HELP, kept)] == ["t5", "t4", "t3"]


def test_a_post_with_a_fresh_reply_rises_like_in_discord() -> None:
    def thread(i: int, last: str | None) -> dict[str, object]:
        t: dict[str, object] = {
            "id": f"t{i}",
            "name": f"post {i}",
            "message_count": 1,
            "thread_metadata": {"create_timestamp": f"2026-10-0{i}T10:00:00+00:00"},
        }
        if last:
            t["last_message_id"] = last
        return {"id": f"t{i}", "kind": "thread", "thread": t}

    # 1560000000000000000 is a Discord id made on 2026-10-14, after both posts were opened.
    kept = {"items": {"a": thread(1, "1560000000000000000"), "b": thread(2, None)}}
    items = views.channel_items(HELP, kept)
    assert [i["id"] for i in items] == ["t1", "t2"]
    assert items[0]["time"].startswith("2026-10-1")


def test_snowflake_time() -> None:
    assert views.snowflake_time("175928847299117063") == "2016-04-30T11:18:25+00:00"
