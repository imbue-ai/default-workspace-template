from pathlib import Path

import pytest
from browser import session


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml_text: str) -> None:
    """Point the navigation guard at a throwaway app registry via the documented override."""
    registry_path = tmp_path / "apps.toml"
    registry_path.write_text(toml_text)
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry_path))


_TODO_APP_REGISTRY = (
    '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\nlabel = "system_interface-aa"\n\n'
    '[[apps]]\nname = "todo"\nurl = "http://localhost:8080"\nlabel = "todo-bb"\n'
)


def test_navigation_to_a_registered_app_port_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of the exception: an app the workspace built and registered is served on a
    # loopback port, and is the origin the human's own app tab shows.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://localhost:8080/") is None
    # The same socket, reached by its IP or through an RFC 6761 loopback subdomain.
    assert session._unsafe_navigation_reason("http://127.0.0.1:8080/") is None
    assert session._unsafe_navigation_reason("http://todo-bb.host-abc.localhost:8080/") is None


def test_navigation_to_an_unregistered_loopback_port_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the case the guard exists for: an internal service nobody published -- a debugger, a
    # credential-bearing proxy -- must stay unreachable even though a registered app sits next to it.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://localhost:4000/") == "loopback host is not allowed"
    assert session._unsafe_navigation_reason("http://127.0.0.1:4000/") is not None
    assert session._unsafe_navigation_reason("http://evil.localhost:4000/") == "loopback host is not allowed"


def test_navigation_to_the_metadata_address_stays_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Link-local is not loopback, so no registration can ever whitelist it -- even one that
    # (nonsensically) named the metadata port.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY + '\n[[apps]]\nname = "odd"\nurl = "http://localhost:80"\n')

    assert session._unsafe_navigation_reason("http://169.254.169.254/latest/meta-data/") is not None
    assert session._unsafe_navigation_reason("http://169.254.169.254:80/") is not None


def test_local_files_stay_blocked_whatever_is_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The credential-read the guard was written to stop is a scheme check, untouched by the
    # loopback exception.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("file:///home/user/.mngr/env") is not None


def test_navigation_is_blocked_when_the_registry_is_absent_or_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No registry means nothing is published, so nothing is excused. A corrupt one is treated the
    # same way rather than failing open.
    monkeypatch.setenv("MINDS_APPS_FILE", str(tmp_path / "missing.toml"))
    assert session._unsafe_navigation_reason("http://localhost:8080/") == "loopback host is not allowed"

    _registry(tmp_path, monkeypatch, "this is not toml [[[")
    assert session._unsafe_navigation_reason("http://localhost:8080/") == "loopback host is not allowed"


def test_a_newly_registered_app_is_navigable_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry is read per check, not cached: apps register as the workspace builds them, and a
    # daemon that had to restart to notice would make the app tab unusable for the rest of the session.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)
    assert session._unsafe_navigation_reason("http://localhost:9001/") is not None

    (tmp_path / "apps.toml").write_text(
        _TODO_APP_REGISTRY + '\n[[apps]]\nname = "notes"\nurl = "http://localhost:9001"\nlabel = "notes-cc"\n'
    )
    assert session._unsafe_navigation_reason("http://localhost:9001/") is None


def test_a_non_loopback_registration_does_not_open_a_loopback_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the loopback origins the workspace serves count. A row pointing somewhere else must not
    # smuggle its port number into the loopback allowlist.
    _registry(tmp_path, monkeypatch, '[[apps]]\nname = "remote"\nurl = "https://example.com:8080"\n')

    assert session._unsafe_navigation_reason("http://localhost:8080/") == "loopback host is not allowed"


def test_wrap_system_message_wraps_in_sentinel() -> None:
    assert (
        session._wrap_system_message("Browser foo-1 was handed back to you.")
        == "<agentic-browser-fleet>Browser foo-1 was handed back to you.</agentic-browser-fleet>"
    )


def test_wrap_system_message_adds_no_newlines() -> None:
    # The wrapper must not introduce newlines: a wrapped message has to type into
    # the agent's pane identically to the same text sent unwrapped.
    text = "line one and line two on one line"
    wrapped = session._wrap_system_message(text)
    assert "\n" not in wrapped.replace(text, "")


def test_system_message_tag_matches_frontend_contract() -> None:
    # Cross-layer contract: the transcript UI recognises this exact tag
    # (BROWSER_FLEET_TAG in system/apps/system_interface/frontend/src/views/message-kinds.ts).
    # If this literal changes, the frontend constant must change with it, or fleet
    # nudges silently revert to bare user bubbles.
    assert session._SYSTEM_MESSAGE_TAG == "agentic-browser-fleet"
