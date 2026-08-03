from pathlib import Path

import modal

from imbue.imbue_common.modal_image_requirements import IMAGE_PINNED_PACKAGE_NAMES
from imbue.imbue_common.modal_image_requirements import IMAGE_REQUIREMENTS_FILENAME
from imbue.imbue_common.modal_image_requirements import image_pinned_app_dir
from imbue.imbue_common.modal_image_requirements import image_requirements_path
from imbue.modal_app_kit import image as modal_app_kit_image
from imbue.modal_app_kit.image import PINNED_BASE_IMAGE
from imbue.modal_app_kit.image import pinned_image

_REPO_ROOT = Path(__file__).parents[4]


def test_base_image_is_digest_pinned() -> None:
    assert "@sha256:" in PINNED_BASE_IMAGE


def test_container_safe_filename_copy_matches_the_canonical_one() -> None:
    """The entrypoint-facing copy in modal_app_kit must equal the imbue_common original."""
    assert modal_app_kit_image.IMAGE_REQUIREMENTS_FILENAME == IMAGE_REQUIREMENTS_FILENAME


def test_pinned_app_dirs_exist_in_the_repo() -> None:
    for package_name in IMAGE_PINNED_PACKAGE_NAMES:
        assert (_REPO_ROOT / image_pinned_app_dir(package_name) / "pyproject.toml").is_file()


def test_registered_apps_have_committed_exports() -> None:
    for package_name in IMAGE_PINNED_PACKAGE_NAMES:
        assert image_requirements_path(_REPO_ROOT, package_name).is_file()


def test_pinned_image_builds_an_image_definition(tmp_path: Path) -> None:
    requirements_file = tmp_path / "image_requirements.txt"
    requirements_file.write_text(
        "tenacity==9.1.4 --hash=sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
    )
    assert isinstance(pinned_image(requirements_file), modal.Image)
