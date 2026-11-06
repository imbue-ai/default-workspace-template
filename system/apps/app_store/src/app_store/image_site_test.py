"""Installing a reviewed app from the ready-made site in one pinned Docker Hub image layer: the recipe's rules, the
install steps, the download's digest check and unpacking, the shipped Excalidraw entry's fit, and a full install
through the engine."""

import hashlib
import io
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

from app_store.catalog import ImageSiteRecipe
from app_store.catalog import load_app_catalog
from app_store.fetch_image_layer import LayerFetchError
from app_store.fetch_image_layer import download_layer
from app_store.fetch_image_layer import fetch
from app_store.fetch_image_layer import main as fetch_main
from app_store.fetch_image_layer import unpack_folder
from app_store.records import InstallStatus
from app_store.room import RoomVerdict
from app_store.room import WorkspaceRoom
from app_store.room import check_app_room
from app_store.system import install_steps
from app_store.testing import build_test_engine

_LAYER = "sha256:" + "ab" * 32


def _recipe(**overrides: object) -> ImageSiteRecipe:
    fields: dict[str, object] = {
        "kind": "image_site",
        "image": "excalidraw/excalidraw",
        "published": "2026-05-06",
        "layer": _LAYER,
        "path": "usr/share/nginx/html",
        "node_tools": ["http-server@14.1.1"],
    }
    fields.update(overrides)
    return ImageSiteRecipe.model_validate(fields)


def _layer_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _registry(blob: bytes) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.docker.io":
            assert request.url.params["scope"] == "repository:excalidraw/excalidraw:pull"
            return httpx.Response(200, json={"token": "t"})
        assert request.headers["Authorization"] == "Bearer t"
        return httpx.Response(200, content=blob)

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_a_recipe_names_its_image_date_and_layer() -> None:
    recipe = _recipe()
    assert recipe.pinned_version == "2026-05-06"
    assert recipe.source_label == "Ready-made image"
    assert recipe.package_spec == "excalidraw/excalidraw 2026-05-06 (abababababab)"


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"image": "docker.io/excalidraw/excalidraw"}, "Docker Hub <owner>/<name>"),
        ({"image": "excalidraw"}, "Docker Hub <owner>/<name>"),
        ({"published": "latest"}, "must be a date"),
        ({"layer": "sha256:abc"}, "must be a digest"),
        ({"path": "../etc"}, "plain relative path"),
        ({"path": "/usr/share"}, "plain relative path"),
        ({"node_tools": ["http-server"]}, "pin one exact version"),
        ({"node_tools": []}, "at least 1"),
    ],
)
def test_a_recipe_breaking_a_rule_is_refused(overrides: dict[str, object], complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        _recipe(**overrides)


def test_the_install_installs_the_server_then_fetches_the_layer_into_site(tmp_path: Path) -> None:
    env = tmp_path / "env"
    assert [step.argv for step in install_steps(_recipe(), env)] == [
        ("npm", "install", "--prefix", str(env), "--no-audit", "--no-fund", "http-server@14.1.1"),
        (
            sys.executable,
            "-m",
            "app_store.fetch_image_layer",
            "excalidraw/excalidraw",
            _LAYER,
            "usr/share/nginx/html",
            str(env / "site"),
        ),
    ]


def test_a_download_matching_its_digest_is_kept(tmp_path: Path) -> None:
    blob = b"the layer"
    layer = "sha256:" + hashlib.sha256(blob).hexdigest()
    with _registry(blob) as client:
        download_layer(client, "excalidraw/excalidraw", layer, tmp_path / "layer")
    assert (tmp_path / "layer").read_bytes() == blob


def test_a_download_that_is_not_the_reviewed_layer_is_refused(tmp_path: Path) -> None:
    with _registry(b"something else") as client, pytest.raises(LayerFetchError, match="not the reviewed"):
        download_layer(client, "excalidraw/excalidraw", _LAYER, tmp_path / "layer")


def test_only_the_named_folder_is_unpacked_as_the_site(tmp_path: Path) -> None:
    archive = tmp_path / "layer.tar.gz"
    archive.write_bytes(
        _layer_archive(
            {
                "usr/share/nginx/html/index.html": b"<!doctype html>",
                "usr/share/nginx/html/assets/app.js": b"draw()",
                "etc/nginx/nginx.conf": b"server {}",
            }
        )
    )
    site = tmp_path / "env" / "site"
    site.parent.mkdir()
    unpack_folder(archive, "usr/share/nginx/html", site)
    assert (site / "index.html").read_bytes() == b"<!doctype html>"
    assert (site / "assets" / "app.js").read_bytes() == b"draw()"
    assert sorted(path.name for path in site.parent.iterdir()) == ["site"]


def test_a_layer_without_the_site_fails(tmp_path: Path) -> None:
    archive = tmp_path / "layer.tar.gz"
    archive.write_bytes(_layer_archive({"etc/nginx/nginx.conf": b"server {}"}))
    (tmp_path / "env").mkdir()
    with pytest.raises(LayerFetchError, match="no site at usr/share/nginx/html"):
        unpack_folder(archive, "usr/share/nginx/html", tmp_path / "env" / "site")


def test_the_shipped_excalidraw_is_ready_made_and_needs_no_build_memory() -> None:
    excalidraw = load_app_catalog().find("excalidraw")
    assert excalidraw is not None
    assert isinstance(excalidraw.recipe, ImageSiteRecipe)
    assert excalidraw.resources.build_memory_mb is None
    fits = check_app_room(excalidraw, WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=2_000))
    assert fits.verdict is RoomVerdict.FITS


def test_a_ready_made_app_installs_through_the_engine_like_any_other(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    engine.request_install("excalidraw")
    engine.run_next_job()
    record = engine.records.read().find("excalidraw")
    assert record is not None and record.status is InstallStatus.INSTALLED
    assert (engine.layout.env_dir("excalidraw") / "site" / "index.html").is_file()
    assert "app-store-launch excalidraw" in engine.layout.dropin_path("excalidraw").read_text()


def test_a_fetch_downloads_checks_and_unpacks_the_site_leaving_no_archive(tmp_path: Path) -> None:
    blob = _layer_archive({"./usr/share/nginx/html/index.html": b"<!doctype html>"})
    layer = "sha256:" + hashlib.sha256(blob).hexdigest()
    site = tmp_path / "env" / "site"
    fetch("excalidraw/excalidraw", layer, "usr/share/nginx/html", site, client=_registry(blob))
    assert (site / "index.html").read_bytes() == b"<!doctype html>"
    assert sorted(path.name for path in site.parent.iterdir()) == ["site"]


@pytest.mark.parametrize(
    "link",
    [("usr/share/nginx/html/escape", "/etc/passwd"), ("usr/share/nginx/html/up", "../../../../outside")],
    ids=["absolute", "climbs-out"],
)
def test_a_site_with_a_link_out_of_its_folder_is_refused(tmp_path: Path, link: tuple[str, str]) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        page = tarfile.TarInfo("usr/share/nginx/html/index.html")
        page.size = 3
        tar.addfile(page, io.BytesIO(b"<p>"))
        symlink = tarfile.TarInfo(link[0])
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = link[1]
        tar.addfile(symlink)
    archive = tmp_path / "layer.tar.gz"
    archive.write_bytes(buffer.getvalue())
    (tmp_path / "env").mkdir()
    with pytest.raises((tarfile.TarError, LayerFetchError)):
        unpack_folder(archive, "usr/share/nginx/html", tmp_path / "env" / "site")
    assert not (tmp_path / "env" / "site").exists()


def test_a_link_that_stays_inside_the_site_is_kept(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        page = tarfile.TarInfo("usr/share/nginx/html/index.html")
        page.size = 3
        tar.addfile(page, io.BytesIO(b"<p>"))
        symlink = tarfile.TarInfo("usr/share/nginx/html/home.html")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "index.html"
        tar.addfile(symlink)
    archive = tmp_path / "layer.tar.gz"
    archive.write_bytes(buffer.getvalue())
    (tmp_path / "env").mkdir()
    unpack_folder(archive, "usr/share/nginx/html", tmp_path / "env" / "site")
    assert (tmp_path / "env" / "site" / "home.html").read_bytes() == b"<p>"


def test_the_step_exits_2_on_wrong_arguments_and_1_when_it_cannot_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["fetch_image_layer", "excalidraw/excalidraw"])
    with pytest.raises(SystemExit) as wrong:
        fetch_main()
    assert wrong.value.code == 2
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_image_layer", "excalidraw/excalidraw", _LAYER, "usr/share/nginx/html", str(blocked / "site")],
    )
    with pytest.raises(SystemExit) as failed:
        fetch_main()
    assert failed.value.code == 1
