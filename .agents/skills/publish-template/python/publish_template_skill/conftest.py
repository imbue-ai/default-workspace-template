from pathlib import Path

import pytest

from publish_template_skill.latchkey_testing import FakeLatchkey, install_fake_latchkey


@pytest.fixture
def fake_latchkey(tmp_path: Path) -> FakeLatchkey:
    return install_fake_latchkey(tmp_path)
