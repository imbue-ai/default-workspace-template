import base64
import io
import json
import os
import zipfile
from pathlib import Path

import httpx
from app_schema.testing import assert_api_schema_describes_app
from werkzeug.test import TestResponse

from files_app.connections import shared_paths
from files_app.device_files import safe_content_disposition
from files_app.server import create_app
from files_app.testing import DEVICE
from files_app.testing import FakeComputer
from files_app.testing import json_of
from files_app.testing import propfind_body
from files_app.webdav import parse_propfind

NARROW_NAME = "Screenshot at 4.16.34\u202fPM.png"
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


def _app(computer: FakeComputer | None, tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text('<script src="/__files/assets/index-AbCd1234.js"></script>__INDEX_DATA__')
    (static / "index-AbCd1234.js").write_text("app()")
    (static / "404.html").write_text("<h1>Not found</h1>")
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return create_app(
        gateway=computer.gateway() if computer is not None else None,
        static_directory=static,
        agent_id="agent-main",
        root=root,
        synced_root=tmp_path / "synced",
    ).test_client()


def _page_data(response: TestResponse) -> dict:
    return json.loads(base64.b64decode(response.get_data(as_text=True).split("</script>")[1]))


def test_a_folder_stamp_moves_when_something_is_added_to_it_and_not_when_a_file_in_it_is_edited(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "fonts"
    folder.mkdir()
    (folder / "Inter.ttf").write_text("a")
    client = create_app(gateway=None, static_directory=tmp_path, agent_id="agent-main", root=tmp_path).test_client()

    def stamps() -> dict[str, str | None]:
        return json_of(client.get("/__files/api/stamps?path=/fonts/&path=/fonts/Inter.ttf&path=/missing/"))["stamps"]

    first = stamps()
    assert first["/fonts/"] is not None
    # The listing carries the stamp it was read at, for the page to check against.
    assert json_of(client.get("/fonts/?json"))["stamp"] == first["/fonts/"]
    assert first["/fonts/Inter.ttf"] is None
    assert first["/missing/"] is None

    (folder / "Inter.ttf").write_text("edited")
    assert stamps()["/fonts/"] == first["/fonts/"]

    (folder / "Atkinson.otf").write_text("b")
    assert stamps()["/fonts/"] != first["/fonts/"]


def test_a_folder_is_listed_with_its_entries_kinds_sizes_and_links(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    (root / "projects" / "app").mkdir(parents=True)
    (root / "projects" / "notes.txt").write_text("hello")
    (root / "projects" / "linked").symlink_to(root / "projects" / "app")
    (root / "projects" / "gone").symlink_to(root / "nowhere")

    listing = json_of(client.get("/projects/?json"))
    assert listing["kind"] == "folder"
    entries = {entry["name"]: entry for entry in listing["entries"]}
    assert (entries["app"]["is_folder"], entries["app"]["is_symlink"], entries["app"]["size"]) == (True, False, 0)
    assert (entries["notes.txt"]["is_folder"], entries["notes.txt"]["size"]) == (False, 5)
    assert (entries["linked"]["is_folder"], entries["linked"]["is_symlink"]) == (True, True)
    assert (entries["gone"]["is_folder"], entries["gone"]["is_symlink"]) == (False, True)
    assert entries["notes.txt"]["mtime"] == int((root / "projects" / "notes.txt").stat().st_mtime * 1000)


def test_a_folders_page_carries_its_listing_with_or_without_its_slash(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    (tmp_path / "root" / "projects").mkdir()
    (tmp_path / "root" / "projects" / "a.txt").write_text("a")
    for path in ("/projects/", "/projects", "/projects?view"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/html")
        assert _page_data(response) == json_of(client.get("/projects/?json"))


def test_a_files_page_says_whether_it_reads_as_text(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    (root / "notes.md").write_text("# hi\n" + "\u00e9" * 5000)
    (root / "photo.png").write_bytes(b"\x89PNG\x00\x00")
    (root / "big.txt").write_bytes(b"a" * (4 * 1024 * 1024 + 1))
    assert _page_data(client.get("/notes.md?view")) == {"kind": "file", "is_text": True}
    assert _page_data(client.get("/notes.md?edit")) == {"kind": "file", "is_text": True}
    assert _page_data(client.get("/photo.png?view")) == {"kind": "file", "is_text": False}
    assert _page_data(client.get("/big.txt?view")) == {"kind": "file", "is_text": False}


def test_a_file_is_served_as_its_bytes_with_ranges_and_a_safe_name_header(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    (tmp_path / "root" / NARROW_NAME).write_bytes(b"\x89PNG-and-the-rest")
    url = "/Screenshot%20at%204.16.34%E2%80%AFPM.png"
    response = client.get(url)
    assert response.status_code == 200
    assert response.data == b"\x89PNG-and-the-rest"
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["Content-Disposition"].isascii()
    assert response.headers["Cache-Control"] == "no-cache"

    ranged = client.get(url, headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.data == b"\x89PNG"

    assert client.head(url).status_code == 200
    assert client.head("/missing.png").status_code == 404


def test_a_file_asked_for_as_a_download_is_an_attachment_under_its_name_and_a_refusal_stays_a_refusal(
    tmp_path: Path,
) -> None:
    computer = FakeComputer(permissions=["minds-file-server-read-/Users/kim/photo.png"], folders={})
    computer.files["/Users/kim/photo.png"] = b"\x89PNG"
    client = _app(computer, tmp_path)
    (tmp_path / "root" / "report.zip").write_bytes(b"PK")

    local = client.get("/report.zip?download")
    assert local.status_code == 200 and local.data == b"PK"
    assert local.headers["Content-Disposition"].startswith('attachment; filename="report.zip"')

    # Downloading it again revalidates the browser's saved copy, which takes the 304's headers over: an inline one
    # would show a text file in the hidden frame instead of saving it.
    again = client.get("/report.zip?download", headers={"If-None-Match": local.headers["ETag"]})
    assert again.status_code == 304
    assert again.headers["Content-Disposition"].startswith('attachment; filename="report.zip"')

    shared = client.get(f"/__remote/{DEVICE}/Users/kim/photo.png?download")
    assert shared.status_code == 200 and shared.data == b"\x89PNG"
    assert shared.headers["Content-Disposition"].startswith('attachment; filename="photo.png"')

    # The frame a download loads in accepts text/html; a refusal still comes back in plain words, never as the file.
    missing_here = client.get("/missing.zip?download", headers={"Accept": "text/html,*/*"})
    assert missing_here.status_code == 404
    assert missing_here.content_type.startswith("text/plain")
    assert missing_here.get_data(as_text=True) == "There's nothing at /missing.zip."
    assert "Content-Disposition" not in missing_here.headers
    missing_there = client.get(f"/__remote/{DEVICE}/Users/kim/gone.png?download")
    assert missing_there.status_code == 404
    assert "Content-Disposition" not in missing_there.headers


def test_a_path_that_is_not_there_is_files_own_not_found_page_for_a_browser_and_plain_words_otherwise(
    tmp_path: Path,
) -> None:
    client = _app(None, tmp_path)
    page = client.get("/missing/thing.txt", headers={"Accept": "text/html,*/*"})
    assert page.status_code == 404
    assert page.get_data(as_text=True) == "<h1>Not found</h1>"

    fetched = client.get("/missing/?json", headers={"Accept": "text/html,*/*"})
    assert fetched.status_code == 404
    assert fetched.get_data(as_text=True) == "There's nothing at /missing/."


def test_a_put_writes_the_file_creating_its_folders_and_keeping_its_mode(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    assert client.put("/new/deeper/notes.txt", data=b"hello").status_code == 201
    assert (root / "new" / "deeper" / "notes.txt").read_bytes() == b"hello"

    script = root / "run.sh"
    script.write_text("echo old")
    script.chmod(0o755)
    assert client.put("/run.sh", data=b"echo new").status_code == 201
    assert script.read_text() == "echo new"
    assert script.stat().st_mode & 0o777 == 0o755
    # Nothing is left beside it.
    assert sorted(path.name for path in root.iterdir()) == ["new", "run.sh"]


def test_saving_a_linked_file_writes_the_file_it_names_and_keeps_the_link(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    (root / "real.txt").write_text("old")
    (root / "link.txt").symlink_to(root / "real.txt")
    assert client.put("/link.txt", data=b"new").status_code == 201
    assert (root / "link.txt").is_symlink()
    assert (root / "real.txt").read_text() == "new"


def test_an_upload_cut_short_leaves_the_old_file_and_nothing_beside_it(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    (root / "notes.txt").write_text("old")
    # The browser said ten bytes were coming, and two came before the connection ended.
    response = client.put("/notes.txt", input_stream=io.BytesIO(b"ne"), environ_overrides={"CONTENT_LENGTH": "10"})
    assert response.status_code >= 400
    assert (root / "notes.txt").read_text() == "old"
    assert [path.name for path in root.iterdir()] == ["notes.txt"]


def test_folders_are_made_moved_and_deleted(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    assert client.open("/drafts", method="MKCOL").status_code == 201
    assert (root / "drafts").is_dir()
    assert client.open("/drafts", method="MKCOL").status_code == 409

    (root / "drafts" / "a.txt").write_text("a")
    moved = client.open("/drafts", method="MOVE", headers={"Destination": "http://localhost/archive/drafts%202"})
    assert moved.status_code == 201
    assert (root / "archive" / "drafts 2" / "a.txt").read_text() == "a"
    assert not (root / "drafts").exists()

    assert client.delete("/archive/").status_code == 204
    assert list(root.iterdir()) == []
    assert client.delete("/archive/").status_code == 404


def test_a_move_never_replaces_what_is_there_nor_puts_a_folder_inside_itself(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    (root / "box").mkdir()
    taken = client.open("/a.txt", method="MOVE", headers={"Destination": "http://localhost/b.txt"})
    assert taken.status_code == 412
    assert (root / "b.txt").read_text() == "b"
    inside = client.open("/box", method="MOVE", headers={"Destination": "http://localhost/box/inner"})
    assert inside.status_code == 409
    assert (root / "box").is_dir()


def test_a_move_from_the_workspace_onto_a_computer_is_refused(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    (tmp_path / "root" / "notes.txt").write_text("a")
    destination = f"http://localhost/__remote/{DEVICE}/Users/kim/notes.txt"
    response = client.open("/notes.txt", method="MOVE", headers={"Destination": destination})
    assert response.status_code == 501
    assert (tmp_path / "root" / "notes.txt").exists()


def test_a_folder_downloads_as_a_zip_of_everything_under_it_without_following_linked_folders(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    root = tmp_path / "root"
    (root / "site" / "css").mkdir(parents=True)
    (root / "site" / "empty").mkdir()
    (root / "site" / "index.html").write_text("<p>hi</p>")
    (root / "site" / ".env").write_text("A=1")
    (root / "site" / "css" / "main.css").write_bytes(os.urandom(300_000))
    (root / "site" / "loop").symlink_to(root / "site")
    os.mkfifo(root / "site" / "pipe")

    response = client.get("/site/?zip")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"
    assert response.headers["Content-Disposition"].startswith('attachment; filename="site.zip"')
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert sorted(archive.namelist()) == [".env", "css/", "css/main.css", "empty/", "index.html"]
        assert archive.read("css/main.css") == (root / "site" / "css" / "main.css").read_bytes()
        assert archive.read("index.html") == b"<p>hi</p>"


def test_a_path_that_climbs_with_dot_segments_is_refused(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    (tmp_path / "secret.txt").write_text("outside the root")
    assert client.get("/..%2fsecret.txt").status_code == 400
    assert client.get("/a/%2e%2e/%2e%2e/secret.txt").status_code == 400


def test_the_build_is_served_with_hashed_files_cached_for_good(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    hashed = client.get("/__files/assets/index-AbCd1234.js")
    assert hashed.data == b"app()"
    assert "immutable" in hashed.headers["Cache-Control"] or "max-age=31536000" in hashed.headers["Cache-Control"]
    assert client.get("/__files/assets/404.html").headers["Cache-Control"] == "no-cache"


def test_a_name_outside_latin_1_reaches_the_browser_as_ascii_with_the_exact_name_kept_beside_it() -> None:
    header = f"inline; filename=\"{NARROW_NAME}\"; filename*=UTF-8''Screenshot%20at%204.16.34%E2%80%AFPM.png"
    safe = safe_content_disposition(header)
    assert safe.isascii()
    assert 'filename="Screenshot at 4.16.34_PM.png"' in safe
    assert "filename*=UTF-8''Screenshot%20at%204.16.34%E2%80%AFPM.png" in safe


def test_shares_are_read_from_the_permissions_with_write_winning_over_read() -> None:
    rules = {
        "rules": [
            {"other": ["latchkey-self-read-devices"]},
            {
                "latchkey-self": [
                    "minds-file-server-read-/Users/kim/notes",
                    "minds-file-server-read-/Users/kim/photo.png",
                    "minds-file-server-write-/Users/kim/photo.png",
                ]
            },
        ]
    }
    assert shared_paths(rules) == {(None, "/Users/kim/notes"): "read", (None, "/Users/kim/photo.png"): "write"}


def test_a_grant_naming_its_device_is_read_with_the_device_apart_from_the_path() -> None:
    rules = {
        "rules": [
            {
                "latchkey-self": [
                    "minds-file-server-read-host-mac:/Users/kim/notes",
                    "minds-file-server-write-host-mac:/Users/kim/notes",
                    "minds-file-server-read-/Users/kim/a:b",
                ]
            }
        ]
    }
    assert shared_paths(rules) == {("host-mac", "/Users/kim/notes"): "write", (None, "/Users/kim/a:b"): "read"}


def _sync(local_path: str, **fields: object) -> dict[str, object]:
    """A sync as Imbue Studio answers for it, up to date unless ``fields`` say otherwise."""
    return {
        "local_path": local_path,
        "workspace_path": f"~/synced_folders/{DEVICE}{local_path}",
        "activity": "ACTIVE",
        "state": "SYNCED",
        "message": None,
        "bytes_done": None,
        "bytes_total": None,
        "direction": "BOTH",
        "conflict": "NEWER",
        **fields,
    }


def _shown(sync: dict[str, object]) -> dict[str, object]:
    """The part of a sync Files passes on."""
    return {field: sync[field] for field in ("state", "message", "bytes_done", "bytes_total")}


def test_connections_say_how_each_share_reaches_the_workspace_and_how_its_sync_stands(tmp_path: Path) -> None:
    fonts = _sync("/Users/kim/Brand Fonts", state="SYNCING", bytes_done=10, bytes_total=40)
    notes = _sync("/Users/kim/notes")
    atkinson = _sync("/Users/kim/Atkinson")
    # A sync the user turned off is no copy here.
    old = _sync("/Users/kim/old", activity="INACTIVE", state="STOPPED")
    computer = FakeComputer(
        permissions=[
            "minds-file-server-read-/Users/kim/Brand Fonts",
            "minds-file-server-read-/Users/kim/notes",
            "minds-file-server-write-/Users/kim/notes",
            "minds-file-server-read-/Users/kim/Atkinson",
            "minds-file-server-write-/Users/kim/photo.png",
            "minds-file-server-read-/Users/kim/old",
        ],
        folders={
            "/Users/kim/Brand Fonts": [("Diatype", True, 0)],
            "/Users/kim/notes": [("todo.md", False, 4)],
            "/Users/kim/Atkinson": [("Mono.otf", False, 3)],
            "/Users/kim/old": [],
        },
        syncs=[fonts, notes, atkinson, old],
    )
    computer.files["/Users/kim/photo.png"] = b"\x89PNG"

    listed = json_of(_app(computer, tmp_path).get("/__files/api/connections"))
    assert listed["devices"] == [{"id": DEVICE, "name": "Kims-Mac.local", "has_device_paths": False}]
    shares = {share["path"]: share for share in listed["shares"]}
    assert shares["/Users/kim/Brand Fonts"]["sync"] == _shown(fonts)
    assert shares["/Users/kim/Brand Fonts"]["synced_copy"] == f"{tmp_path}/synced/{DEVICE}/Users/kim/Brand Fonts/"
    assert shares["/Users/kim/notes"]["sync"] == _shown(notes)
    assert shares["/Users/kim/notes"]["access"] == "write"
    assert shares["/Users/kim/old"]["sync"] is None and shares["/Users/kim/old"]["synced_copy"] is None
    assert shares["/Users/kim/Atkinson"]["synced_copy"] == f"{tmp_path}/synced/{DEVICE}/Users/kim/Atkinson/"
    assert shares["/Users/kim/Atkinson"]["kind"] == "folder"
    assert shares["/Users/kim/photo.png"]["kind"] == "file"
    (asked,) = [request for request in computer.requests if request.url.path.endswith("/folder-syncs")]
    assert asked.url.path == "/minds-api-proxy/api/v1/agents/agent-main/folder-syncs"
    assert asked.headers["X-Latchkey-Device"] == DEVICE
    # Every call through the gateway names the device, and none puts it in the file path.
    file_calls = [request for request in computer.requests if "/files/" in request.url.path]
    assert all(request.headers["X-Latchkey-Device"] == DEVICE for request in file_calls)
    assert all(DEVICE not in request.url.path for request in file_calls)


def test_a_copy_whose_computer_does_not_say_how_it_stands_is_listed_with_no_sync(tmp_path: Path) -> None:
    computer = FakeComputer(
        permissions=["minds-file-server-read-/Users/kim/notes"], folders={"/Users/kim/notes": []}, syncs=None
    )
    copy = tmp_path / "synced" / DEVICE / "Users/kim/notes"
    copy.mkdir(parents=True)
    (share,) = json_of(_app(computer, tmp_path).get("/__files/api/connections"))["shares"]
    assert share["synced_copy"] == f"{copy}/"
    assert share["sync"] is None


def test_a_sync_in_a_shape_files_does_not_read_is_listed_as_cant_check(tmp_path: Path) -> None:
    """A state this Files does not know is not guessed at: the copy reads as one whose computer did not say."""
    computer = FakeComputer(
        permissions=["minds-file-server-read-/Users/kim/notes"],
        folders={"/Users/kim/notes": []},
        syncs=[_sync("/Users/kim/notes", state="PAUSED")],
    )
    copy = tmp_path / "synced" / DEVICE / "Users/kim/notes"
    copy.mkdir(parents=True)
    (share,) = json_of(_app(computer, tmp_path).get("/__files/api/connections"))["shares"]
    assert share["synced_copy"] == f"{copy}/"
    assert share["sync"] is None


def test_a_computer_granting_per_device_lists_each_share_once_and_is_reached_under_its_id(tmp_path: Path) -> None:
    computer = FakeComputer(
        permissions=[
            # A grant from before per-device grants, and the twin the change gave it.
            "minds-file-server-read-/Users/kim/notes",
            f"minds-file-server-read-{DEVICE}:/Users/kim/notes",
            # A grant made since, with no twin.
            f"minds-file-server-read-{DEVICE}:/Users/kim/wallpapers/jpeg",
            f"minds-file-server-write-{DEVICE}:/Users/kim/wallpapers/jpeg",
        ],
        folders={"/Users/kim/notes": [("todo.md", False, 4)], "/Users/kim/wallpapers/jpeg": [("01.jpg", False, 9)]},
    )
    listed = json_of(_app(computer, tmp_path).get("/__files/api/connections"))
    assert listed["devices"] == [{"id": DEVICE, "name": "Kims-Mac.local", "has_device_paths": True}]
    assert [(share["path"], share["access"], share["kind"], share["is_reachable"]) for share in listed["shares"]] == [
        ("/Users/kim/notes", "read", "folder", True),
        ("/Users/kim/wallpapers/jpeg", "write", "folder", True),
    ]
    file_calls = [request for request in computer.requests if "/files/" in request.url.path]
    assert all(request.url.path.startswith(f"/minds-api-proxy/api/v1/files/{DEVICE}/") for request in file_calls)


def test_a_share_on_a_computer_granting_per_device_opens_live_before_any_listing(tmp_path: Path) -> None:
    computer = FakeComputer(
        permissions=[f"minds-file-server-write-{DEVICE}:/Users/kim/wallpapers/jpeg"],
        folders={"/Users/kim/wallpapers/jpeg": [("01.jpg", False, 9)]},
    )
    computer.files["/Users/kim/wallpapers/jpeg/01.jpg"] = b"\xff\xd8"
    client = _app(computer, tmp_path)
    listing = json_of(client.get(f"/__remote/{DEVICE}/Users/kim/wallpapers/jpeg/?json"))
    assert [entry["name"] for entry in listing["entries"]] == ["01.jpg"]
    assert client.get(f"/__remote/{DEVICE}/Users/kim/wallpapers/jpeg/01.jpg").data == b"\xff\xd8"
    assert client.put(f"/__remote/{DEVICE}/Users/kim/wallpapers/jpeg/02.jpg", data=b"new").status_code == 201
    assert computer.files["/Users/kim/wallpapers/jpeg/02.jpg"] == b"new"
    # The device's grants are read once, for its first request, not again for each.
    assert [request.url.path for request in computer.requests].count("/permissions/self") == 1


def test_a_sleeping_computer_is_listed_without_failing_the_listing(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-read-/Users/kim/Atkinson"], folders={}, is_awake=False)
    response = _app(computer, tmp_path).get("/__files/api/connections")
    assert response.status_code == 200
    assert json_of(response) == {
        "devices": [{"id": DEVICE, "name": "Kims-Mac.local", "has_device_paths": False}],
        "shares": [],
    }


def test_a_shared_folder_with_no_copy_is_listed_live_as_a_workspace_folder_is(tmp_path: Path) -> None:
    computer = FakeComputer(
        permissions=["minds-file-server-read-/Users/kim/Atkinson"],
        folders={"/Users/kim/Atkinson": [("Mono.otf", False, 3), ("Variable", True, 0)]},
    )
    client = _app(computer, tmp_path)
    listing = json_of(client.get(f"/__remote/{DEVICE}/Users/kim/Atkinson/?json"))
    assert [(entry["name"], entry["is_folder"], entry["size"]) for entry in listing["entries"]] == [
        ("Mono.otf", False, 3),
        ("Variable", True, 0),
    ]

    # The page carries the listing: the frontend shows it as the folder's and does not ask again.
    assert _page_data(client.get(f"/__remote/{DEVICE}/Users/kim/Atkinson/")) == listing

    destination = {"Destination": f"http://localhost/__remote/{DEVICE}/Users/kim/Atkinson/Sans.otf"}
    assert (
        client.open(f"/__remote/{DEVICE}/Users/kim/Atkinson/Mono.otf", method="MOVE", headers=destination).status_code
        == 501
    )
    assert client.get(f"/__remote/{DEVICE}/Users/kim/Atkinson/?zip").status_code == 501


def test_a_live_share_path_that_climbs_with_dot_segments_never_reaches_the_gateway(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-read-/Users/kim/Atkinson"], folders={})
    client = _app(computer, tmp_path)
    climbing = "..%2f" * 5
    assert client.get(f"/__remote/{DEVICE}/Users/{climbing}devices").status_code == 400
    assert client.get(f"/__remote/{DEVICE}/Users/kim/{climbing}permissions/self?json").status_code == 400
    assert computer.requests == []


def test_a_shared_file_is_streamed_from_the_computer_and_written_back_with_write_access(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-write-/Users/kim/photo.png"], folders={})
    computer.files["/Users/kim/photo.png"] = b"\x89PNG"
    client = _app(computer, tmp_path)
    response = client.get(f"/__remote/{DEVICE}/Users/kim/photo.png")
    assert response.status_code == 200 and response.data == b"\x89PNG"

    assert client.put(f"/__remote/{DEVICE}/Users/kim/photo.png", data=b"new").status_code == 201
    assert computer.files["/Users/kim/photo.png"] == b"new"


def test_a_change_the_computer_refuses_is_answered_as_read_only(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-read-/Users/kim/photo.png"], folders={}, is_writable=False)
    computer.files["/Users/kim/photo.png"] = b"\x89PNG"
    response = _app(computer, tmp_path).put(f"/__remote/{DEVICE}/Users/kim/photo.png", data=b"new")
    assert response.status_code == 403
    assert response.get_data(as_text=True) == "Your computer only shares this to read."
    assert computer.files["/Users/kim/photo.png"] == b"\x89PNG"


class _GarbledGateway(FakeComputer):
    """A gateway that answers the device list with a page that is not JSON."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/devices":
            return httpx.Response(200, text="<html>Bad gateway</html>")
        return super().handle(request)


def test_a_gateway_answer_that_is_not_json_says_the_shares_could_not_be_read(tmp_path: Path) -> None:
    response = _app(_GarbledGateway(permissions=[], folders={}), tmp_path).get("/__files/api/connections")
    assert response.status_code == 502
    assert json_of(response)["detail"].startswith("Couldn't read your shares")


def test_with_no_gateway_there_are_no_shares(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    assert json_of(client.get("/__files/api/connections")) == {"devices": [], "shares": []}


def test_a_real_listing_from_the_computer_reads_names_kinds_and_sizes() -> None:
    entries = parse_propfind(
        propfind_body("/Users/kim/Brand Fonts/", [("Caslon Ionic", True, 0), ("a.otf", False, 9)])
    )
    assert [(entry.name, entry.is_folder, entry.size) for entry in entries] == [
        ("Brand Fonts", True, 0),
        ("Caslon Ionic", True, 0),
        ("a.otf", False, 9),
    ]
    assert entries[2].content_type == "text/plain"


def test_the_app_describes_its_api_the_way_its_manifest_says(tmp_path: Path) -> None:
    assert_api_schema_describes_app(_app(None, tmp_path), _MANIFEST_PATH)
