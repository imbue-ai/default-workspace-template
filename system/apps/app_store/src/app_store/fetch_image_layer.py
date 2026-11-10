"""Download one layer of a public Docker Hub image by its digest, check it is exactly that digest, and unpack one folder
of it. The install of an ``image_site`` recipe runs this as one of its steps (``python -m app_store.fetch_image_layer
<image> <layer> <path> <dest>``), so what it logs lands in the install log.

The layer is streamed to disk while it is hashed, so the download takes no more memory than its buffer, and only the
named folder is unpacked, through tarfile's ``data`` filter (no links out of the folder, no devices, no absolute
paths), and a link that leads out of that folder is refused."""

import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Final

import httpx
from loguru import logger

from app_store.errors import AppStoreError

TOKEN_URL: Final[str] = "https://auth.docker.io/token"
REGISTRY_ROOT: Final[str] = "https://registry-1.docker.io/v2"
DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 120.0
CHUNK_BYTES: Final[int] = 1024 * 1024


class LayerFetchError(AppStoreError):
    """The layer could not be fetched, did not match its digest, or did not hold the folder."""


def anonymous_pull_token(client: httpx.Client, image: str) -> str:
    """Docker Hub's token for pulling public ``image`` without an account; raises LayerFetchError without one."""
    response = client.get(TOKEN_URL, params={"service": "registry.docker.io", "scope": f"repository:{image}:pull"})
    response.raise_for_status()
    try:
        document = response.json()
    except ValueError as e:
        raise LayerFetchError("Docker Hub's token answer is not JSON") from e
    token = document.get("token") if isinstance(document, dict) else None
    if not isinstance(token, str) or token == "":
        raise LayerFetchError("Docker Hub gave no pull token")
    return token


def download_layer(client: httpx.Client, image: str, layer: str, destination: Path) -> None:
    """Stream the layer into ``destination``; raises LayerFetchError when its sha256 is not ``layer``."""
    token = anonymous_pull_token(client, image)
    digest = hashlib.sha256()
    with client.stream(
        "GET", f"{REGISTRY_ROOT}/{image}/blobs/{layer}", headers={"Authorization": f"Bearer {token}"}
    ) as response:
        response.raise_for_status()
        with destination.open("wb") as out:
            for chunk in response.iter_bytes(CHUNK_BYTES):
                digest.update(chunk)
                out.write(chunk)
    actual = f"sha256:{digest.hexdigest()}"
    if actual != layer:
        raise LayerFetchError(f"the download is {actual}, not the reviewed {layer}")


def unpack_folder(archive: Path, path: str, site: Path) -> None:
    """Unpack the ``path`` folder of the layer ``archive`` as ``site``; raises LayerFetchError when it has none."""
    folder = path.strip("/")
    prefix = folder + "/"
    with tempfile.TemporaryDirectory(dir=site.parent) as scratch:
        with tarfile.open(archive, "r:*") as tar:
            members = [member for member in tar.getmembers() if member.name.lstrip("./").startswith(prefix)]
            tar.extractall(scratch, members=members, filter="data")
        # The folder as the layer names it, always inside the scratch folder (never an absolute path of this machine).
        unpacked = Path(scratch) / folder
        if not (unpacked / "index.html").is_file():
            raise LayerFetchError(f"the layer has no site at {path}")
        _refuse_links_out_of(unpacked)
        shutil.rmtree(site, ignore_errors=True)
        shutil.move(unpacked, site)


def _refuse_links_out_of(folder: Path) -> None:
    """Raise LayerFetchError for a symbolic link that leads outside ``folder``. The ``data`` filter keeps links inside
    the folder the layer is unpacked into, but only the site's own folder is kept, so a link that climbs out of it
    would point outside the app once the site is moved into place."""
    for directory, dirnames, filenames in os.walk(folder):
        for name in (*dirnames, *filenames):
            entry = Path(directory) / name
            if not entry.is_symlink():
                continue
            target = Path(os.path.normpath(entry.parent / os.readlink(entry)))
            if not target.is_relative_to(folder):
                raise LayerFetchError(f"the site's {entry.relative_to(folder)} links outside it")


def fetch(image: str, layer: str, path: str, site: Path, client: httpx.Client | None = None) -> None:
    """Download ``layer`` of ``image`` and unpack its ``path`` folder as ``site`` (``client`` for tests)."""
    site.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=site.parent) as scratch:
        archive = Path(scratch) / "layer.tar.gz"
        with client or httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as session:
            download_layer(session, image, layer, archive)
        logger.info("downloaded {} {} ({} bytes), digest checked", image, layer, archive.stat().st_size)
        unpack_folder(archive, path, site)
    logger.info("unpacked {} to {}", path, site)


def main() -> None:
    if len(sys.argv) != 5:
        logger.error("usage: python -m app_store.fetch_image_layer <image> <layer> <path> <dest>")
        sys.exit(2)
    image, layer, path, dest = sys.argv[1:]
    try:
        fetch(image, layer, path, Path(dest))
    except (httpx.HTTPError, LayerFetchError, tarfile.TarError, OSError) as e:
        logger.error("could not fetch {} {}: {}", image, layer, e)
        sys.exit(1)


if __name__ == "__main__":
    main()
