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


def test_an_ipv4_mapped_loopback_literal_is_treated_as_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``::ffff:127.0.0.1`` reaches the same socket as ``127.0.0.1`` but reports neither is_loopback
    # nor is_link_local in its mapped form, so without normalisation it slips through as an
    # ordinary address -- the metadata IP included.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://[::ffff:127.0.0.1]:4000/") is not None
    assert session._unsafe_navigation_reason("http://[::ffff:169.254.169.254]/") is not None
    assert session._unsafe_navigation_reason("http://[::ffff:127.0.0.1]:8080/") is None


def test_a_backslash_in_the_authority_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # WHATWG URL parsing -- what Chromium actually does -- reads a backslash as a slash, so it ends
    # the authority where ``urlparse`` does not. ``urlparse`` sees the host as example.com; the
    # browser sees 127.0.0.1:4000 and the rest as a path. Since the two disagree about which host is
    # being contacted, no answer the guard could give about the host would be trustworthy.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert (
        session._unsafe_navigation_reason("http://127.0.0.1:4000\\@example.com/")
        == "URL authority contains a backslash"
    )
    # Rejected on the shape of the URL, so registering the port does not excuse it either.
    assert session._unsafe_navigation_reason("http://127.0.0.1:8080\\@example.com/") is not None
    # A backslash further along is an ordinary path/query/fragment character and must not block a
    # perfectly normal external URL.
    assert session._unsafe_navigation_reason("https://example.com/a\\b") is None
    assert session._unsafe_navigation_reason("https://example.com/?q=a\\b") is None


def test_the_unspecified_address_is_treated_as_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``0.0.0.0`` and ``::`` are not loopback addresses, but as a navigation *destination* they land
    # on this machine's loopback, so every service bound to a loopback port is reachable through
    # them. They have to be gated exactly like ``127.0.0.1``.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://0.0.0.0:4000/") == "internal address 0.0.0.0 is not allowed"
    assert session._unsafe_navigation_reason("http://[::]:4000/") is not None
    assert session._unsafe_navigation_reason("http://0.0.0.0:8080/") is None
    assert session._unsafe_navigation_reason("http://[::]:8080/") is None


def test_a_bare_integer_host_is_read_as_the_ipv4_address_it_denotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Browsers accept an IPv4 address written as one decimal number: ``http://2130706433/`` connects
    # to 127.0.0.1, and ``http://0/`` to 0.0.0.0. Read as a name, such a host would look like an
    # ordinary external site.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://2130706433:4000/") == "internal address 2130706433 is not allowed"
    assert session._unsafe_navigation_reason("http://0:4000/") is not None
    assert session._unsafe_navigation_reason("http://2852039166/") is not None  # 169.254.169.254
    assert session._unsafe_navigation_reason("http://2130706433:8080/") is None


def test_short_hex_and_octal_ipv4_spellings_are_read_as_the_address_they_denote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A browser accepts one to four numeric labels, each hex, octal or decimal, with the last label
    # filling every remaining byte. Every spelling below dials the same socket as ``127.0.0.1``, so
    # the guard has to reach the same address the connection will.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://127.1:4000/") == "internal address 127.1 is not allowed"
    assert session._unsafe_navigation_reason("http://0x7f.0.0.1:4000/") is not None
    assert session._unsafe_navigation_reason("http://0177.0.0.1:4000/") is not None
    assert session._unsafe_navigation_reason("http://0x7f000001:4000/") is not None
    assert session._unsafe_navigation_reason("http://0xa9fea9fe/") is not None  # 169.254.169.254

    assert session._unsafe_navigation_reason("http://127.1:8080/") is None
    assert session._unsafe_navigation_reason("http://0x7f.0.0.1:8080/") is None
    assert session._unsafe_navigation_reason("http://0177.0.0.1:8080/") is None
    assert session._unsafe_navigation_reason("http://0x7f000001:8080/") is None


def test_a_numeric_host_the_guard_cannot_resolve_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A top-level domain is never all-numeric (RFC 3696), so a host whose last label is numeric is
    # not a name any resolver could answer for -- it is an IPv4 spelling. When one cannot be read as
    # an address, the guard must fail closed instead of falling through to "a regular hostname":
    # guessing is exactly how the interesting spellings of 127.0.0.1 get through.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://1.2.3.4.5:4000/") == "host '1.2.3.4.5' is not a usable address"
    assert session._unsafe_navigation_reason("http://127.0.0.256:4000/") is not None
    assert session._unsafe_navigation_reason("http://0199.0.0.1:4000/") is not None  # 199 is not octal
    assert session._unsafe_navigation_reason("http://notanip.1:4000/") is not None
    # Beyond the IPv4 range there is no address to speak of, and a big enough integer must not be
    # reinterpreted as an IPv6 one -- which is what ``ipaddress.ip_address`` does with an int.
    assert session._unsafe_navigation_reason("http://4294967296:4000/") is not None


def test_an_ordinary_hostname_is_still_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The numeric-host handling must not cost the guard its main job of letting the fleet browse the
    # web. Digits are perfectly ordinary in a name, as long as the last label is not one.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("https://example.com/") is None
    assert session._unsafe_navigation_reason("https://v2.example.com/") is None
    assert session._unsafe_navigation_reason("https://1.example.com/") is None
    assert session._unsafe_navigation_reason("https://0x1.example.com/") is None
    # And a numeric host that does resolve, but to somewhere that is not this machine, is allowed
    # like any other public address.
    assert session._unsafe_navigation_reason("http://16843009:4000/") is None  # 1.1.1.1


def test_a_trailing_root_dot_does_not_disguise_a_loopback_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``localhost.`` is the fully-qualified spelling of ``localhost`` and resolves identically, so a
    # single trailing dot must not turn a loopback name into an unrecognised one.
    _registry(tmp_path, monkeypatch, _TODO_APP_REGISTRY)

    assert session._unsafe_navigation_reason("http://localhost.:4000/") == "loopback host is not allowed"
    assert session._unsafe_navigation_reason("http://evil.localhost.:4000/") == "loopback host is not allowed"
    assert session._unsafe_navigation_reason("http://127.0.0.1.:4000/") is not None
    assert session._unsafe_navigation_reason("http://localhost.:8080/") is None
    assert session._unsafe_navigation_reason("http://todo-bb.host-abc.localhost.:8080/") is None
    # An external name is just as fully-qualifiable, and stays allowed.
    assert session._unsafe_navigation_reason("https://example.com./") is None


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
