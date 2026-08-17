"""Drift tests for the litellm proxy's pinned Modal image inputs.

The image installs exactly the committed hash-locked image_requirements.txt,
which is an export of this app's ``[dependency-groups] image`` from the
workspace uv.lock. These tests fail whenever the committed export goes stale
(regenerate with ``just export-image-requirements``) or a group entry loses
its ``==`` pin -- the litellm pin in particular is deliberate (budget
enforcement must not drift on a redeploy; see the group's comment).
"""

import importlib.metadata
import tomllib
from pathlib import Path

from imbue.imbue_common.modal_image_requirements import IMAGE_DEPENDENCY_GROUP
from imbue.imbue_common.modal_image_requirements import image_requirements_path
from imbue.modal_app_kit.testing import export_image_requirements

_APP_DIR = Path(__file__).parent
_REPO_ROOT = Path(__file__).parents[2]
_PACKAGE_NAME = "modal-litellm"


def _image_group_entries() -> list[str]:
    with (_APP_DIR / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)
    return pyproject["dependency-groups"][IMAGE_DEPENDENCY_GROUP]


def test_committed_image_requirements_match_uv_lock() -> None:
    committed = image_requirements_path(_REPO_ROOT, _PACKAGE_NAME).read_text()
    assert committed == export_image_requirements(_REPO_ROOT, _PACKAGE_NAME), (
        "The committed image_requirements.txt no longer matches uv.lock; "
        "regenerate it with `just export-image-requirements`."
    )


def test_every_image_group_entry_is_exactly_pinned() -> None:
    unpinned = [entry for entry in _image_group_entries() if "==" not in entry]
    assert not unpinned, f"Image group entries must be ==-pinned: {unpinned}"


def test_tests_run_the_same_litellm_version_the_image_ships() -> None:
    """The workspace venv (which these tests run in) and the image must agree on litellm."""
    installed_version = importlib.metadata.version("litellm")
    litellm_entries = [entry for entry in _image_group_entries() if entry.startswith("litellm")]
    assert len(litellm_entries) == 1
    pinned_version = litellm_entries[0].split("==")[1]
    assert installed_version == pinned_version, (
        f"Tests import litellm {installed_version} but the image ships {pinned_version}; "
        "relock so the workspace and the image group agree."
    )
