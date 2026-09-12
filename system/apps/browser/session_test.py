import asyncio
import sys
from pathlib import Path

import pytest
from browser import session


def test_message_agent_goes_through_the_chat_messenger_by_id_as_a_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wake rides ``system/scripts/message_chat.py`` (the chat app, with ``mngr message`` as
    its own backoff), addressed by the agent's id and marked ``--system`` so the transcript
    renders it as a collapsed chip; the agent's name is never the address."""
    spawned: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class _Done:
        async def wait(self) -> int:
            return 0

    async def fake_exec(*argv: str, **kwargs: object) -> _Done:
        spawned.append((argv, kwargs))
        return _Done()

    monkeypatch.setattr(session.asyncio, "create_subprocess_exec", fake_exec)
    browser = session.LiveBrowser(browser_id="b1")

    asyncio.run(
        browser._message_agent(
            "agent-0123456789abcdef0123456789abcdef", "riley", "the browser is yours"
        )
    )

    [(argv, kwargs)] = spawned
    assert argv == (
        sys.executable,
        str(Path("system") / "scripts" / "message_chat.py"),
        "agent-0123456789abcdef0123456789abcdef",
        "--system",
        "--message",
        "the browser is yours",
    )
    assert (
        Path(str(kwargs["cwd"]))
        .joinpath("system", "scripts", "message_chat.py")
        .is_file()
    )
