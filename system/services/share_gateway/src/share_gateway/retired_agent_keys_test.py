from pathlib import Path

from share_gateway.retired_agent_keys import AGENT_KEY_LINE_MARKER
from share_gateway.retired_agent_keys import remove_agent_key_lines

_OWNER_LINE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqI4910CfGV/VLbLTy6XXLKZwm/HZQSG/N0iAG0D29c owner@laptop"
_DESKTOP_LINE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDesktopGrantKeyDesktopGrantKeyDesktopGrantKey0 imbue-workspace-ssh"


def _agent_line(agent_digit: str) -> str:
    return f"ssh-ed25519 AAAA{agent_digit * 8} {AGENT_KEY_LINE_MARKER} agent_id=va-{agent_digit * 32}"


def _keys_file(tmp_path: Path, text: str) -> Path:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    keys_path = ssh_dir / "authorized_keys"
    keys_path.write_text(text)
    keys_path.chmod(0o600)
    return keys_path


def test_every_agent_line_is_removed_and_every_other_line_is_kept_in_its_order(tmp_path: Path) -> None:
    keys_path = _keys_file(
        tmp_path, "\n".join([_OWNER_LINE, _agent_line("1"), _DESKTOP_LINE, _agent_line("2")]) + "\n"
    )

    assert remove_agent_key_lines(keys_path) is True

    assert keys_path.read_text() == f"{_OWNER_LINE}\n{_DESKTOP_LINE}\n"
    assert keys_path.stat().st_mode & 0o777 == 0o600
    # A later start finds nothing to remove and leaves the file as it is.
    assert remove_agent_key_lines(keys_path) is False
    assert keys_path.read_text() == f"{_OWNER_LINE}\n{_DESKTOP_LINE}\n"


def test_a_file_holding_only_agent_lines_is_left_empty(tmp_path: Path) -> None:
    keys_path = _keys_file(tmp_path, _agent_line("1") + "\n")

    assert remove_agent_key_lines(keys_path) is True

    assert keys_path.read_text() == ""


def test_a_file_with_no_agent_line_is_not_rewritten_or_locked(tmp_path: Path) -> None:
    # No trailing newline, which a rewrite would add.
    keys_path = _keys_file(tmp_path, _OWNER_LINE)

    assert remove_agent_key_lines(keys_path) is False

    assert keys_path.read_text() == _OWNER_LINE
    assert sorted(entry.name for entry in keys_path.parent.iterdir()) == ["authorized_keys"]


def test_a_workspace_with_no_authorized_keys_file_gets_none(tmp_path: Path) -> None:
    keys_path = tmp_path / ".ssh" / "authorized_keys"

    assert remove_agent_key_lines(keys_path) is False

    assert not keys_path.parent.exists()
