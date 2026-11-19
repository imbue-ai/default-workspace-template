from pathlib import Path

from app_schema.testing import assert_api_schema_describes_app

from memory.runner import app


def test_the_app_describes_its_api_the_way_its_manifest_says() -> None:
    assert_api_schema_describes_app(app.test_client(), Path(__file__).resolve().parents[2] / "app.toml")
