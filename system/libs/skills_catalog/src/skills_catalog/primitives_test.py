import pytest

from skills_catalog.errors import InvalidSkillNameError
from skills_catalog.primitives import SkillName


@pytest.mark.parametrize("raw_name", ["", "..", ".hidden", "a/b", "a\\b"])
def test_a_skill_name_cannot_name_a_path_outside_the_skills_folder(raw_name: str) -> None:
    with pytest.raises(InvalidSkillNameError):
        SkillName(raw_name)


def test_a_plain_directory_name_is_a_skill_name() -> None:
    assert SkillName("build-app") == "build-app"
