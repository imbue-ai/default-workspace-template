from env_converge.capture import (
    parse_cargo_install_list,
    parse_dpkg_versions,
    parse_manual_packages,
    parse_npm_global_versions,
    parse_rustup_toolchain_list,
    parse_uv_tool_versions,
)


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


def test_parse_cargo_install_list_keeps_registry_crates_only() -> None:
    # Registry crates parse; the indented binary lines and path/git installs
    # (source suffix before the colon -- not replayable from crates.io) do not.
    output = (
        "ripgrep v14.1.0:\n"
        "    rg\n"
        "cargo-binstall v1.6.4:\n"
        "    cargo-binstall\n"
        "local-tool v0.1.0 (/home/user/workspace/tools/local-tool):\n"
        "    local-tool\n"
    )
    assert parse_cargo_install_list(output) == {
        "ripgrep": "14.1.0",
        "cargo-binstall": "1.6.4",
    }


def test_parse_cargo_install_list_empty() -> None:
    assert parse_cargo_install_list("") == {}


def test_parse_rustup_toolchain_list_finds_default() -> None:
    output = (
        "stable-x86_64-unknown-linux-gnu (default)\nnightly-x86_64-unknown-linux-gnu\n"
    )
    toolchains, default = parse_rustup_toolchain_list(output)
    assert toolchains == (
        "stable-x86_64-unknown-linux-gnu",
        "nightly-x86_64-unknown-linux-gnu",
    )
    assert default == "stable-x86_64-unknown-linux-gnu"


def test_parse_rustup_toolchain_list_handles_active_default_marker() -> None:
    # rustup >= 1.28 emits "(active, default)" instead of "(default)".
    output = "stable-aarch64-unknown-linux-gnu (active, default)\n"
    toolchains, default = parse_rustup_toolchain_list(output)
    assert toolchains == ("stable-aarch64-unknown-linux-gnu",)
    assert default == "stable-aarch64-unknown-linux-gnu"


def test_parse_rustup_toolchain_list_no_default() -> None:
    toolchains, default = parse_rustup_toolchain_list(
        "nightly-x86_64-unknown-linux-gnu\n"
    )
    assert toolchains == ("nightly-x86_64-unknown-linux-gnu",)
    assert default is None
