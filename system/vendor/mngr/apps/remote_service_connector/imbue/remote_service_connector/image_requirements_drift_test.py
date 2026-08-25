"""Drift tests for the connector's pinned Modal image inputs.

The image installs exactly the committed hash-locked image_requirements.txt,
which is an export of this app's ``[dependency-groups] image`` from the
workspace uv.lock. These tests fail whenever the committed export goes stale
(regenerate with ``just export-image-requirements``), a group entry loses its
``==`` pin, or the group and the allowed import roots drift apart.
"""

import tomllib
from pathlib import Path

from imbue.imbue_common.modal_image_requirements import IMAGE_DEPENDENCY_GROUP
from imbue.imbue_common.modal_image_requirements import image_requirements_path
from imbue.modal_app_kit.testing import export_image_requirements
from imbue.remote_service_connector.deploy_constants import THIRD_PARTY_IMPORT_ROOTS

_APP_DIR = Path(__file__).parents[2]
_REPO_ROOT = Path(__file__).parents[4]
_PACKAGE_NAME = "remote-service-connector"

# Import roots provided transitively by the image group rather than by a
# group entry of their own: pydantic is a hard dependency of fastapi, and
# botocore of boto3.
_TRANSITIVELY_PROVIDED_ROOTS = frozenset({"pydantic", "botocore"})

# Import roots whose distribution name differs beyond the usual dash ->
# underscore normalization.
_IMPORT_ROOT_BY_DISTRIBUTION = {"psycopg2-binary": "psycopg2", "pyjwt": "jwt", "pyyaml": "yaml"}


def _image_group_entries() -> list[str]:
    with (_APP_DIR / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)
    return pyproject["dependency-groups"][IMAGE_DEPENDENCY_GROUP]


def _distribution_name(requirement: str) -> str:
    return requirement.split("==")[0].split("[")[0].strip()


def test_committed_image_requirements_match_uv_lock() -> None:
    committed = image_requirements_path(_REPO_ROOT, _PACKAGE_NAME).read_text()
    assert committed == export_image_requirements(_REPO_ROOT, _PACKAGE_NAME), (
        "The committed image_requirements.txt no longer matches uv.lock; "
        "regenerate it with `just export-image-requirements`."
    )


def test_every_image_group_entry_is_exactly_pinned() -> None:
    unpinned = [entry for entry in _image_group_entries() if "==" not in entry]
    assert not unpinned, f"Image group entries must be ==-pinned: {unpinned}"


def test_image_group_and_allowed_import_roots_agree() -> None:
    derived_roots = {
        _IMPORT_ROOT_BY_DISTRIBUTION.get(name, name.replace("-", "_"))
        for name in (_distribution_name(entry) for entry in _image_group_entries())
    }
    assert derived_roots | _TRANSITIVELY_PROVIDED_ROOTS == THIRD_PARTY_IMPORT_ROOTS, (
        "The image dependency group and THIRD_PARTY_IMPORT_ROOTS have drifted apart; "
        "update deploy_constants.py or the pyproject image group together."
    )
