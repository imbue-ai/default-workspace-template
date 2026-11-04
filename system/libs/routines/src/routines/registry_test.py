from pathlib import Path

from routines.registry import CHAT_APP_FALLBACK_URL
from routines.registry import app_display_names
from routines.registry import chat_app_url
from routines.registry import registry_path_of

REGISTRY = """
[[apps]]
name = "chat"
url = "http://127.0.0.1:8010/"
display_name = "Chat"

[[apps]]
name = "news"
url = "http://127.0.0.1:8042"
display_name = "Daily News"

[[apps]]
name = "bare"
url = "http://127.0.0.1:8043"
"""


def test_the_registry_is_where_the_environment_says_else_under_the_root(tmp_path: Path) -> None:
    assert registry_path_of(tmp_path, {}) == tmp_path / "data" / ".state" / "apps.toml"
    assert registry_path_of(tmp_path, {"MINDS_APPS_FILE": "/elsewhere/apps.toml"}) == Path("/elsewhere/apps.toml")


def test_the_chat_app_is_found_by_its_row_without_a_trailing_slash(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(REGISTRY)
    assert chat_app_url(registry) == "http://127.0.0.1:8010"


def test_a_missing_or_broken_registry_means_the_fixed_port_and_no_names(tmp_path: Path) -> None:
    assert chat_app_url(tmp_path / "none.toml") == CHAT_APP_FALLBACK_URL
    assert app_display_names(tmp_path / "none.toml") == {}
    broken = tmp_path / "broken.toml"
    broken.write_text("[[apps\n")
    assert chat_app_url(broken) == CHAT_APP_FALLBACK_URL
    assert app_display_names(broken) == {}


def test_display_names_are_listed_by_app_name_leaving_out_apps_without_one(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(REGISTRY)
    assert app_display_names(registry) == {"chat": "Chat", "news": "Daily News"}
