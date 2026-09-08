"""Pinned image inputs shared by our Modal apps.

Every input to a service image build is pinned so a rebuild is a pure function
of the repo state, never of when it runs: the base image is digest-pinned, the
pip set is installed from a committed hash-locked export of the workspace
``uv.lock`` (each app's ``[dependency-groups] image``), and even the uv that
performs the install inside the build is version-pinned. Regenerate the
committed exports with ``just export-image-requirements``; per-app drift tests
and the ``minds-admin env deploy`` preflight fail when an export no longer matches
``uv.lock``. See libs/modal_app_kit/README.md for the full deployment model.

This module holds only the modal-SDK side. The pure export machinery
(the canonical ``uv export`` command, app registry, and paths) lives in
``imbue.imbue_common.modal_image_requirements`` -- kept in the public
``imbue_common`` even though its consumers (the private ``minds-admin env
deploy`` preflight and the private Modal apps' drift tests) are private, so
the mirror's ``imbue_common`` stays self-contained without this private
package; shipped modal_app_kit code cannot import it back (stdlib+modal
only), hence
the mirrored ``IMAGE_REQUIREMENTS_FILENAME`` constant below (equality is
asserted by ``image_test.py``).
"""

from pathlib import Path
from typing import Final

import modal

# Digest-pinned base for every service image (same base family as the
# default-workspace-template Dockerfile, and the same Python minor as the
# repo's .python-version, so tests run the interpreter the containers ship).
# The digest freezes the base: security patches to the tag stop arriving
# automatically, so bump the digest by hand (resolve the tag's current
# multi-arch index digest, e.g. `docker buildx imagetools inspect
# python:3.12-slim-trixie`) alongside normal dependency maintenance.
PINNED_BASE_IMAGE: Final[str] = (
    "python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)

# The uv that runs inside the image build. Left unset, Modal picks its own
# default uv, which can change under us -- a floating build input. The
# hash-locked requirements already fix WHAT gets installed; this fixes the
# installer too. Keep in sync with the repo's own uv when convenient.
IMAGE_BUILD_UV_VERSION: Final[str] = "0.11.7"

# Container-safe copy of imbue.imbue_common.modal_image_requirements'
# IMAGE_REQUIREMENTS_FILENAME (see the module docstring for why it exists
# twice); the entrypoints build their export path from this one.
IMAGE_REQUIREMENTS_FILENAME: Final[str] = "image_requirements.txt"


def pinned_image(image_requirements_file: Path) -> modal.Image:
    """The digest-pinned base with the app's hash-locked pip set installed.

    ``--require-hashes`` makes uv refuse anything not exactly matching the
    export, so the built image is byte-reproducible from the repo state.
    """
    return modal.Image.from_registry(PINNED_BASE_IMAGE).uv_pip_install(
        requirements=[str(image_requirements_file)],
        extra_options="--require-hashes",
        uv_version=IMAGE_BUILD_UV_VERSION,
    )


def locate_image_requirements(entrypoint_file: Path) -> Path:
    """The app's committed ``image_requirements.txt``, searched upward from the entrypoint.

    Entrypoints must use this instead of fixed-depth path arithmetic like
    ``Path(__file__).parents[2]``: inside the Modal container the entrypoint is
    re-imported from its automatic file mount at ``/root/app.py``, where a
    fixed ancestor index can be out of range and crash the container at import
    (observed as ``IndexError`` killing every fresh remote-service-connector
    boot). The image is already built by then, so the returned path is never
    read in-container -- it only has to be computable. When no ancestor holds
    the export (the in-container case), the entrypoint's own directory is
    returned as the never-read placeholder.
    """
    resolved_entrypoint = entrypoint_file.resolve()
    for ancestor in resolved_entrypoint.parents:
        candidate = ancestor / IMAGE_REQUIREMENTS_FILENAME
        if candidate.exists():
            return candidate
    return resolved_entrypoint.parent / IMAGE_REQUIREMENTS_FILENAME
