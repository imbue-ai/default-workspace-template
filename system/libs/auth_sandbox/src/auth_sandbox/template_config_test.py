import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from imbue.mngr_latchkey.core import (
    HIDDEN_BUILTIN_SERVICES,
    LatchkeyError,
    merge_minds_latchkey_config,
)

from auth_sandbox.template_config import merge_minds_config, render_template_config


def test_render_template_config_adds_latchkeys_browser_entry_to_minds_config() -> None:
    text = render_template_config(
        Path("/opt/auth-sandbox/sign-in-browser.sh"),
        datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc),
    )
    config = json.loads(text)
    assert config["browser"] == {
        "executablePath": "/opt/auth-sandbox/sign-in-browser.sh",
        "source": "system",
        "discoveredAt": "2026-10-29T12:00:00Z",
    }
    assert all(
        service in config["settings"]["hideBuiltinServices"]
        for service in HIDDEN_BUILTIN_SERVICES
    )
    assert (
        config["registeredServices"]
        == json.loads(merge_minds_latchkey_config(None))["registeredServices"]
    )
    # The merge the sign-in commands run keeps the entry verbatim, which is what the sign-in relies on.
    assert json.loads(merge_minds_latchkey_config(text))["browser"] == config["browser"]


def test_merge_minds_config_keeps_the_browser_entry_and_refuses_a_config_that_is_not_an_object(
    tmp_path: Path,
) -> None:
    template = render_template_config(
        Path("/opt/auth-sandbox/sign-in-browser.sh"),
        datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc),
    )
    config = json.loads(template)
    del config["settings"]
    (tmp_path / "config.json").write_text(json.dumps(config))

    merge_minds_config(tmp_path)

    assert json.loads((tmp_path / "config.json").read_text()) == json.loads(template)

    (tmp_path / "config.json").write_text("[]")
    with pytest.raises(LatchkeyError, match=str(tmp_path / "config.json")):
        merge_minds_config(tmp_path)
