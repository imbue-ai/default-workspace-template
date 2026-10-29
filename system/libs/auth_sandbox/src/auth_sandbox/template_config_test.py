import json
from datetime import datetime, timezone
from pathlib import Path

from imbue.mngr_latchkey.core import (
    HIDDEN_BUILTIN_SERVICES,
    merge_minds_latchkey_config,
)

from auth_sandbox.template_config import render_template_config


def test_render_template_config_adds_latchkeys_browser_entry_to_minds_config() -> None:
    text = render_template_config(
        Path("/opt/fortress/tilion-fortress/tilion"),
        datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc),
    )
    config = json.loads(text)
    assert config["browser"] == {
        "executablePath": "/opt/fortress/tilion-fortress/tilion",
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
    # The merge minds runs at every initialize keeps the entry verbatim, which is what the sign-in relies on.
    assert json.loads(merge_minds_latchkey_config(text))["browser"] == config["browser"]
