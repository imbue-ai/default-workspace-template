"""The shell answers what the app SDK asks it for: where it listens, its inventory, and its two browser modules.

The SDK is a package of its own, installed from the mngr commit ``pyproject.toml`` pins, and it
names the shell's address, routes and inventory fields itself, since an app must run outside a
workspace; these tests are what hold the two sides to one contract.
"""

import pytest
from app_manifest.registry import APP_CONTRACT_ROUTE, CONTEXT_MENU_ROUTE
from flask import Flask
from imbue.app_sdk import shell_client
from imbue.app_sdk.apps import resolve_app_url
from imbue.app_sdk.shell_modules import register_shell_module_routes
from workspace_layout import shell_url
from workspace_layout.answers import InventoryDocument
from workspace_layout.testing import LoopbackShell, fake_app


def test_the_sdk_and_the_workspace_find_the_shell_at_the_same_address() -> None:
    assert shell_client.SHELL_URL_ENVIRONMENT_VARIABLE == shell_url.ENV_SHELL_URL
    assert shell_client.DEFAULT_SHELL_URL == shell_url.DEFAULT_SHELL_URL


def test_an_app_finds_another_app_in_the_inventory_the_shell_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes = fake_app("notes")
    inventory = InventoryDocument(
        is_preview=False,
        workspace_name="workspace",
        desktops=(),
        apps=(fake_app("files"), notes),
        clients=(),
    )

    with LoopbackShell() as shell:
        shell.get_answers[shell_url.INVENTORY_ROUTE] = (
            200,
            inventory.model_dump(mode="json"),
        )
        monkeypatch.setenv(shell_url.ENV_SHELL_URL, shell.url)

        assert resolve_app_url("notes") == str(notes.url)


@pytest.mark.parametrize("route", [APP_CONTRACT_ROUTE, CONTEXT_MENU_ROUTE])
def test_an_app_serves_each_browser_module_the_shell_builds_at_the_route_pages_import_it_from(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    source = f"export const servedAt = '{route}';"
    app = Flask("notes", static_folder=None)
    register_shell_module_routes(app)

    with LoopbackShell() as shell:
        shell.get_answers[route] = (200, source)
        monkeypatch.setenv(shell_url.ENV_SHELL_URL, shell.url)

        response = app.test_client().get(route)

    assert response.status_code == 200
    assert response.get_data(as_text=True) == source
