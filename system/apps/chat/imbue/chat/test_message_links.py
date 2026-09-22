from pathlib import Path
from urllib.parse import urlencode

import pytest
from flask import Flask
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import running_workspace
from imbue.chat.testing import serve_app
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.testing import find_free_port


@pytest.mark.timeout(120, func_only=False)
def test_nested_chat_opens_files_and_external_links_without_navigating_the_conversation(
    page: Page, tmp_path: Path
) -> None:
    files = Flask("files-link-test")
    files.add_url_rule("/docs/guide.md", view_func=lambda: "<textarea>Guide contents</textarea>")
    files.add_url_rule("/outside", endpoint="outside", view_func=lambda: "External destination")
    with serve_app(files) as served:
        markdown = (
            f'[Workspace guide](docs/guide.md:12) <a href="{served.http_url}/outside" target="_top">External guide</a>'
        )
        events = [
            {
                "type": "assistant",
                "uuid": "link-message",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": markdown}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ]
        with running_workspace(tmp_path, find_free_port(), find_free_port(), session_events=events) as workspace:
            registry = tmp_path / "registry" / "apps.toml"
            registry.write_text(registry.read_text() + registry_row_toml("files", served.http_url))
            query = urlencode({"open": f"chat:/?chat={FIXTURE_AGENT_ID}"})
            page.goto(f"{workspace.shell_url}/?{query}")
            root = page.frame_locator(f'iframe[src^="{workspace.chat_url}/"]')
            chat = root.frame_locator(f'iframe[data-chat-id="{FIXTURE_AGENT_ID}"]')
            link = chat.get_by_role("link", name="Workspace guide", exact=True)
            expect(link).to_be_visible(timeout=20000)
            link.press("Enter")
            file_frame = page.frame_locator(f'iframe[src="{served.http_url}/docs/guide.md?view"]')
            expect(file_frame.locator("textarea")).to_have_value("Guide contents", timeout=15000)
            expect(chat.locator(".markdown-content")).to_contain_text("Workspace guide")
            with page.context.expect_page() as popup:
                chat.get_by_role("link", name="External guide", exact=True).press("Enter")
            expect(popup.value.locator("body")).to_have_text("External destination")
            expect(root.locator(f'iframe[data-chat-id="{FIXTURE_AGENT_ID}"]')).to_have_attribute(
                "src", f"/{FIXTURE_AGENT_ID}"
            )
            popup.value.close()
