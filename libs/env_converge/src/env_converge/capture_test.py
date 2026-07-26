from env_converge.capture import parse_dpkg_versions
from env_converge.capture import parse_manual_packages
from env_converge.capture import parse_npm_global_versions
from env_converge.capture import parse_uv_tool_versions


def test_parse_dpkg_versions() -> None:
    output = "bash\t5.2.15-2\ncurl\t7.88.1-10\n\nmalformed-line\n"
    assert parse_dpkg_versions(output) == {"bash": "5.2.15-2", "curl": "7.88.1-10"}


def test_parse_manual_packages_sorts_and_strips() -> None:
    assert parse_manual_packages("curl\nbash\n\n  git  \n") == ("bash", "curl", "git")


def test_parse_npm_global_versions() -> None:
    npm_json = '{"dependencies": {"latchkey": {"version": "3.1.0"}, "corrupt": {}, "npm": {"version": "10.8.2"}}}'
    assert parse_npm_global_versions(npm_json) == {"latchkey": "3.1.0", "npm": "10.8.2"}


def test_parse_npm_global_versions_empty() -> None:
    assert parse_npm_global_versions("{}") == {}


def test_parse_uv_tool_versions_skips_entry_point_lines() -> None:
    output = "modal v1.4.2\n- modal\nruff v0.6.0\n- ruff\n"
    assert parse_uv_tool_versions(output) == {"modal": "1.4.2", "ruff": "0.6.0"}
