"""The sandbox's supervisord config and boot script, as files: what they name has to exist in the tree."""

import configparser
import os
import re
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from auth_sandbox.image_build import (
    BOOT_SCRIPT_SOURCE,
    CLAIM_WATCHER_COMMAND,
    SIGN_IN_BROWSER_SOURCE,
    SUPERVISORD_CONF_SOURCE,
)
from auth_sandbox.primitives import (
    AUTHORIZED_KEYS_PATH,
    CHROME_ORIGIN_ENV_VAR,
    CONNECT_INFO_PATH,
    FORTRESS_LAUNCHER_PATH,
    HOST_ID_ENV_VAR,
    INSTALL_DIR,
    RUNTIME_DIR,
    SCREEN_SERVICE_URL,
    SCREEN_TOKEN_SHA256_FILENAME,
    SIGN_IN_BROWSER_PATH,
    VENV_DIRNAME,
    WORKSPACE_ROOT,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_SECTION_PATTERN = re.compile(
    r"^\[(program|eventlistener|group):([^\]]+)\]", re.MULTILINE
)
_SANDBOX_PROGRAMS = frozenset(
    {
        "screen",
        "screen-xorg",
        "screen-pulse",
        "screen-session",
        "screen-neko",
        "screen-watch",
        "owner-exec",
    }
)


def _included_dropins() -> list[Path]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_REPO_ROOT / SUPERVISORD_CONF_SOURCE)
    assert parser.get("supervisord", "nodaemon") == "true"
    included: list[Path] = []
    for pattern in parser.get("include", "files").split():
        path = Path(pattern)
        assert (
            path.is_absolute()
            and path.parent == WORKSPACE_ROOT / "system" / "supervisord.conf.d"
        ), pattern
        included.append(_REPO_ROOT / "system" / "supervisord.conf.d" / path.name)
    return included


def test_sandbox_supervisord_runs_exactly_the_screen_and_owner_exec() -> None:
    dropins = _included_dropins()
    declared: set[str] = set()
    for dropin in dropins:
        assert dropin.is_file(), dropin
        for kind, name in _SECTION_PATTERN.findall(dropin.read_text()):
            if kind != "group":
                declared.add(name)
    assert declared == _SANDBOX_PROGRAMS
    # The Screen's stack group names only members the sandbox's config also declares.
    for dropin in dropins:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(dropin)
        for section in parser.sections():
            if section.startswith("group:"):
                assert set(parser[section]["programs"].split(",")) <= declared, section


def test_sandbox_supervisord_names_no_program_outside_the_workspaces_dropins() -> None:
    text = (_REPO_ROOT / SUPERVISORD_CONF_SOURCE).read_text()
    assert not _SECTION_PATTERN.search(text)


def test_boot_script_parses_runs_strict_and_names_the_contract_paths() -> None:
    boot_script = _REPO_ROOT / BOOT_SCRIPT_SOURCE
    text = boot_script.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert (
        subprocess.run(
            ["bash", "-n", str(boot_script)],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )
    assert f'readonly RUNTIME_DIR="{RUNTIME_DIR}"' in text
    assert f'readonly INSTALL_DIR="{INSTALL_DIR}"' in text
    assert f'readonly SCREEN_URL="{SCREEN_SERVICE_URL}"' in text
    for required in (HOST_ID_ENV_VAR, CHROME_ORIGIN_ENV_VAR):
        assert f"${{{required}:?" in text
    # Nothing about a user comes at create, and the boot generates no key: the session brings its own.
    assert "AUTH_SANDBOX_AUTHORIZED_KEY" not in text
    assert "AUTH_SANDBOX_SCREEN_TOKEN_SHA256" not in text
    assert "openssl rand" not in text
    assert 'export MNGR_HOST_DIR="$RUNTIME_DIR/host"' in text
    assert 'supervisord -n -c "$INSTALL_DIR/supervisord.conf"' in text
    assert '-X POST "$SCREEN_URL/api/start"' in text


def test_boot_script_hands_the_screen_and_owner_exec_their_sandbox_settings() -> None:
    text = (_REPO_ROOT / BOOT_SCRIPT_SOURCE).read_text()
    exports = text[: text.index("supervisord -n")]
    assert 'export SCREEN_LISTEN_HOST="0.0.0.0"' in exports
    assert 'export OWNER_EXEC_LISTEN_HOST="0.0.0.0"' in exports
    assert (
        f'export SCREEN_OWNER_TOKEN_SHA256_FILE="$RUNTIME_DIR/{SCREEN_TOKEN_SHA256_FILENAME}"'
        in exports
    )
    assert f'export SCREEN_FRAME_ANCESTORS="${CHROME_ORIGIN_ENV_VAR}"' in exports
    assert f'readonly CONNECT_INFO_PATH="{CONNECT_INFO_PATH}"' in text
    assert 'export SCREEN_CONNECT_INFO_PATH="$CONNECT_INFO_PATH"' in exports
    # The Screen's sign-in picture: every browser's codec at high quality, a display exactly the sign-in window
    # (1288x805, plus a row for an even height), the control profile throughout, no panel, and neko as an ICE lite
    # agent.
    assert 'export SCREEN_VIDEO_CODEC="vp8"' in exports
    assert 'export SCREEN_VIDEO_QUALITY="high"' in exports
    assert 'export SCREEN_FIXED_SIZE="1288x806"' in exports
    assert 'export SCREEN_VIDEO_PROFILE="control"' in exports
    assert 'export SCREEN_DESKTOP_PANEL="off"' in exports
    assert 'export SCREEN_ICE_LITE="on"' in exports
    # The connector may write the connect-info file before the script runs: the script never touches it.
    assert text.count("$CONNECT_INFO_PATH") == 1
    # owner-exec starts refusing everyone; the claim watcher installs the user's key.
    assert f'export HOME="{AUTHORIZED_KEYS_PATH.parent.parent}"' in exports
    assert ': > "$HOME/.ssh/authorized_keys"' in exports
    # owner-exec only reads `export KEY=value` lines from share.env.
    assert (
        f"printf 'export SHARE_CHROME_ORIGIN=%s\\n' \"${CHROME_ORIGIN_ENV_VAR}\""
        ' > "$REPO_ROOT/data/.secrets/share.env"' in text
    )


def test_sign_in_browser_script_runs_the_fortress_launcher_with_its_window_at_the_corner() -> (
    None
):
    script = _REPO_ROOT / SIGN_IN_BROWSER_SOURCE
    assert os.access(script, os.X_OK)
    assert SIGN_IN_BROWSER_PATH.name == script.name
    text = script.read_text()
    assert text.startswith("#!/bin/bash\n")
    commands = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert commands == [
        "set -euo pipefail",
        f'exec {FORTRESS_LAUNCHER_PATH} --window-position=0,0 "$@"',
    ]


def test_boot_script_turns_off_xfwm4s_compositor_before_the_screen_starts_it() -> None:
    text = (_REPO_ROOT / BOOT_SCRIPT_SOURCE).read_text()
    opener = (
        """cat > "$HOME/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml" <<'EOF'\n"""
    )
    start = text.index(opener) + len(opener)
    assert start < text.index("supervisord -n")
    channel = ElementTree.fromstring(text[start : text.index("\nEOF\n", start)])
    assert channel.tag == "channel"
    assert channel.get("name") == "xfwm4"
    (general,) = channel.findall("property")
    assert general.get("name") == "general"
    assert [prop.attrib for prop in general] == [
        {"name": "use_compositing", "type": "bool", "value": "false"}
    ]


def test_boot_script_runs_the_claim_watcher_and_ends_the_sandbox_with_it() -> None:
    text = (_REPO_ROOT / BOOT_SCRIPT_SOURCE).read_text()
    assert (
        f'readonly CLAIM_WATCHER="$INSTALL_DIR/{VENV_DIRNAME}/bin/{CLAIM_WATCHER_COMMAND}"'
        in text
    )
    # Started ahead of supervisord, so a claim written right after create is not waiting on the stack.
    assert text.index('"$CLAIM_WATCHER" &') < text.index("supervisord -n")
    assert 'wait -n "$CLAIM_WATCHER_PID" "$SUPERVISORD_PID"' in text
    ending = text[text.index("wait -n") :]
    assert 'kill -TERM "$CLAIM_WATCHER_PID" "$SUPERVISORD_PID"' in ending
    assert ending.rstrip().endswith('exit "$EXIT_STATUS"')


def test_owner_exec_listens_where_the_boot_script_says_and_on_loopback_otherwise() -> (
    None
):
    text = (_REPO_ROOT / "system" / "scripts" / "run_owner_exec.sh").read_text()
    assert 'LISTEN_HOST="${OWNER_EXEC_LISTEN_HOST:-127.0.0.1}"' in text
    assert 'listen_host = "${LISTEN_HOST}"' in text


_VALID_BOOT_ENVIRONMENT = {
    HOST_ID_ENV_VAR: "host-" + "a" * 32,
    CHROME_ORIGIN_ENV_VAR: "https://studio.example.com",
}


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        (HOST_ID_ENV_VAR, None, "must carry the sandbox host id"),
        (CHROME_ORIGIN_ENV_VAR, None, "must carry the hosted chrome origin"),
        (CHROME_ORIGIN_ENV_VAR, "https://studio.example.com/", "must be a bare origin"),
        (
            CHROME_ORIGIN_ENV_VAR,
            "https://studio.example.com\nexport X=1",
            "must be a bare origin",
        ),
        (
            HOST_ID_ENV_VAR,
            'host-1"\nlisten = "0',
            "must be host- and 32 lower-case hex digits",
        ),
    ],
)
def test_boot_script_refuses_a_missing_or_malformed_setting_before_writing_anything(
    name: str, value: str | None, reason: str
) -> None:
    # Every case is invalid, so the script exits at its checks, ahead of the first write.
    environment = {
        key: item
        for key, item in os.environ.items()
        if not key.startswith("AUTH_SANDBOX_")
    }
    environment.update(_VALID_BOOT_ENVIRONMENT)
    if value is None:
        del environment[name]
    else:
        environment[name] = value
    completed = subprocess.run(
        ["bash", str(_REPO_ROOT / BOOT_SCRIPT_SOURCE)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode != 0
    assert reason in completed.stderr
