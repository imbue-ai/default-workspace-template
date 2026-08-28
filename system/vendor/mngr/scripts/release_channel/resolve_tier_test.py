"""What `resolve_tier.sh` reads out of a tier's client.toml, and what happens when it cannot.

The script greps the feed URL out of the file with a regex, and the workflow gates
both its dry-run and its publish step on the answer being non-empty. So a form
that is legal TOML but invisible to that regex -- an indented key, a quoted key, a
single-quoted value -- skips publishing entirely and reports the job green.

The script itself is not run here: it shells out to `node` and a nested `uv run`,
and node is absent from the sandboxes this suite runs in. What is checked instead
is the one fragile line, against the tier that actually configures a feed.
"""

import re
import tomllib
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).parents[2]
_PRODUCTION_CLIENT_TOML: Final[Path] = _REPO_ROOT / "apps/minds/imbue/minds/config/envs/production/client.toml"
_FEED_KEY: Final[str] = "update_feed_base_url"


def read_key_as_the_shell_does(text: str, key: str) -> str:
    """`grep -m1 -E "^<key>[[:space:]]*=" | cut -s -d'"' -f2`, from resolve_tier.sh."""
    for line in text.splitlines():
        if re.match(rf"^{re.escape(key)}[ \t]*=", line):
            fields = line.split('"')
            return fields[1] if len(fields) > 1 else ""
    return ""


def test_the_shell_reads_the_same_feed_the_config_declares() -> None:
    """Any TOML-legal form the grep cannot see resolves to empty and publishes nothing."""
    text = _PRODUCTION_CLIENT_TOML.read_text()
    assert read_key_as_the_shell_does(text, _FEED_KEY) == tomllib.loads(text)[_FEED_KEY]


def test_production_declares_a_feed_at_all() -> None:
    """An empty answer skips the dry-run AND the publish, and leaves the job green.

    Production sets a feed now, so empty means misconfiguration rather than the
    honest "this tier serves no manifest yet" the workflow reports it as.
    """
    assert read_key_as_the_shell_does(_PRODUCTION_CLIENT_TOML.read_text(), _FEED_KEY) != ""


def test_the_forms_that_would_silently_publish_nothing() -> None:
    """Pinning what the regex cannot read, so the model above is not vacuous."""
    assert read_key_as_the_shell_does(f'  {_FEED_KEY} = "https://x.test"\n', _FEED_KEY) == ""
    assert read_key_as_the_shell_does(f'"{_FEED_KEY}" = "https://x.test"\n', _FEED_KEY) == ""
    assert read_key_as_the_shell_does(f"{_FEED_KEY} = 'https://x.test'\n", _FEED_KEY) == ""
    assert read_key_as_the_shell_does(f'{_FEED_KEY} = "https://x.test"\n', _FEED_KEY) == "https://x.test"
