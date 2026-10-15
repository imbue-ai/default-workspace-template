from typing import Any

import pytest

from studio_discord import studio


def _rules(monkeypatch: pytest.MonkeyPatch, rules: list[dict[str, list[str]]]) -> None:
    monkeypatch.setattr(studio, "request_json", lambda url, **kwargs: {"rules": rules})


@pytest.mark.parametrize(
    ("rules", "granted"),
    [
        ([{"discord-api:me@example.com": ["discord-read-messages", "discord-read-users"]}], True),
        ([{"discord-api": ["discord-read-all"]}], True),
        ([{"discord-api:me@example.com": ["discord-read-users"]}], False),
        ([{"slack-api": ["slack-read-all"]}], False),
        ([], False),
    ],
)
def test_access_counts_only_a_discord_grant_that_reads_messages(
    monkeypatch: pytest.MonkeyPatch, rules: list[dict[str, list[str]]], granted: bool
) -> None:
    _rules(monkeypatch, rules)
    assert studio.discord_access_granted() is granted


def test_the_access_request_is_read_only_and_says_what_it_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def fake(url: str, **kwargs: Any) -> dict[str, str]:
        sent.update(url=url, **kwargs)
        return {"request_id": "r1"}

    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.invalid")
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    monkeypatch.setattr(studio, "request_json", fake)
    assert studio.request_discord_access() == "r1"
    assert sent["url"] == "http://gateway.invalid/permission-requests" and sent["method"] == "POST"
    body = sent["body"]
    assert body["agent_id"] == "agent-1" and body["payload"]["scope"] == "discord-api"
    assert not any("write" in p for p in body["payload"]["permissions"])
    assert "whole Discord account" in body["rationale"] and "only ever reads" in body["rationale"]


def test_uninstall_icons_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    ours = {
        "target": {"kind": "launch", "app": "studio-discord", "launch": "open"},
        "mode": "focus",
        "cell": {"column": 1, "row": 1},
    }
    other = {
        "target": {"kind": "launch", "app": "chat", "launch": "open"},
        "mode": "focus",
        "cell": {"column": 0, "row": 0},
    }
    desktops = {"desktops": [{"id": "home", "shortcuts": [ours, other]}, {"id": "d2", "shortcuts": [other]}]}

    def fake(url: str, **kwargs: Any) -> Any:
        calls.append((url, kwargs.get("body")))
        return desktops if url.endswith("/api/desktops") else {}

    monkeypatch.setattr(studio, "SHELL_URL", "http://shell.invalid")
    monkeypatch.setattr(studio, "request_json", fake)
    removed = studio.remove_desktop_icons()
    assert removed == [{"desktop_id": "home", "shortcut": ours}]
    assert (
        "http://shell.invalid/api/desktops/home/shortcuts/remove",
        {"app": "studio-discord", "launch": "open"},
    ) in calls
    calls.clear()
    studio.restore_desktop_icons(removed + [{"desktop_id": "gone", "shortcut": ours}])
    posts = [c for c in calls if c[1] is not None]
    assert posts == [
        (
            "http://shell.invalid/api/desktops/home/shortcuts",
            {"target": ours["target"], "mode": "focus", "cell": ours["cell"]},
        )
    ]
