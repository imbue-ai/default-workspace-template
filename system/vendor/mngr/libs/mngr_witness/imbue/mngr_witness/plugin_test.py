import pluggy

from imbue.mngr_witness.plugin import register_cli_commands


def test_register_cli_commands_returns_the_witness_command() -> None:
    commands = register_cli_commands()

    assert commands is not None
    assert [command.name for command in commands] == ["witness"]


def test_plugin_registers_with_pluggy(plugin_manager: pluggy.PluginManager) -> None:
    command_names = [
        command.name
        for result in plugin_manager.hook.register_cli_commands()
        if result is not None
        for command in result
    ]

    assert "witness" in command_names
