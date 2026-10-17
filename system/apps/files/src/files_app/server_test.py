import base64
import json
from pathlib import Path

import httpx

from files_app.connections import shared_paths
from files_app.server import create_app
from files_app.server import safe_content_disposition
from files_app.testing import DEVICE
from files_app.testing import FakeComputer
from files_app.testing import json_of
from files_app.testing import mock_dufs
from files_app.testing import propfind_body
from files_app.webdav import parse_propfind

NARROW_NAME = "Screenshot at 4.16.34 PM.png"


def _dufs_page(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text='<script src="/__dufs_v0.46.0__/index.js?v=1"></script>')


def _app(computer: FakeComputer | None, tmp_path: Path, dufs: httpx.Client | None = None):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text('<script src="__ASSETS_PREFIX__index.js"></script>__INDEX_DATA__')
    return create_app(
        dufs_url="http://dufs.test",
        gateway=computer.gateway() if computer is not None else None,
        static_directory=static,
        synced_root=tmp_path / "synced",
        client=dufs or mock_dufs(_dufs_page),
    ).test_client()


def test_a_name_outside_latin_1_reaches_the_browser_as_ascii_with_the_exact_name_kept_beside_it() -> None:
    header = f"inline; filename=\"{NARROW_NAME}\"; filename*=UTF-8''Screenshot%20at%204.16.34%E2%80%AFPM.png"
    safe = safe_content_disposition(header)
    assert safe.isascii()
    assert 'filename="Screenshot at 4.16.34_PM.png"' in safe
    assert "filename*=UTF-8''Screenshot%20at%204.16.34%E2%80%AFPM.png" in safe


def test_dufs_answers_pass_through_with_their_path_and_body_and_a_safe_name_header(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def dufs(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"\x89PNG"),
            headers=[
                (b"Content-Disposition", f'inline; filename="{NARROW_NAME}"'.encode()),
                (b"Content-Type", b"image/png"),
            ],
        )

    client = _app(None, tmp_path, mock_dufs(dufs))
    response = client.get("/home/user/Screenshot%20at%204.16.34%E2%80%AFPM.png?t=1")
    assert response.status_code == 200
    assert response.data == b"\x89PNG"
    assert response.headers["Content-Disposition"].isascii()
    assert seen[0].url.raw_path == b"/home/user/Screenshot%20at%204.16.34%E2%80%AFPM.png?t=1"

    put = client.put("/home/user/new.txt", data=b"hello")
    assert put.status_code == 200
    assert seen[1].method == "PUT" and seen[1].read() == b"hello"


def test_a_move_from_the_workspace_onto_a_computer_is_refused_before_dufs(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def dufs(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, stream=httpx.ByteStream(b""))

    client = _app(None, tmp_path, mock_dufs(dufs))
    destination = f"http://localhost/__remote/{DEVICE}/Users/kim/notes.txt"
    response = client.open("/home/user/notes.txt", method="MOVE", headers={"Destination": destination})
    assert response.status_code == 501
    assert seen == []

    moved = client.open("/home/user/notes.txt", method="MOVE", headers={"Destination": "http://localhost/tmp/n.txt"})
    assert moved.status_code == 201
    assert seen[0].method == "MOVE"


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


def test_connections_say_how_each_share_reaches_the_workspace_and_when_a_copy_is_behind(tmp_path: Path) -> None:
    computer = FakeComputer(
        permissions=[
            "minds-file-server-read-/Users/kim/Brand Fonts",
            "minds-file-server-read-/Users/kim/notes",
            "minds-file-server-write-/Users/kim/notes",
            "minds-file-server-read-/Users/kim/Atkinson",
            "minds-file-server-write-/Users/kim/photo.png",
        ],
        folders={
            "/Users/kim/Brand Fonts": [("Diatype", True, 0), (".DS_Store", False, 6)],
            "/Users/kim/notes": [("todo.md", False, 4)],
            "/Users/kim/Atkinson": [("Mono.otf", False, 3)],
        },
    )
    computer.files["/Users/kim/photo.png"] = b"\x89PNG"
    (tmp_path / "synced" / DEVICE / "Users/kim/Brand Fonts").mkdir(parents=True)
    notes_copy = tmp_path / "synced" / DEVICE / "Users/kim/notes"
    notes_copy.mkdir(parents=True)
    (notes_copy / "todo.md").write_text("- a")

    listed = json_of(_app(computer, tmp_path).get("/__files/api/connections"))
    assert listed["devices"] == [{"id": DEVICE, "name": "Kims-Mac.local", "has_device_paths": False}]
    shares = {share["path"]: share for share in listed["shares"]}
    assert shares["/Users/kim/Brand Fonts"]["sync_state"] == "behind"
    assert shares["/Users/kim/Brand Fonts"]["access"] == "read"
    assert shares["/Users/kim/notes"]["sync_state"] == "current"
    assert shares["/Users/kim/notes"]["access"] == "write"
    assert shares["/Users/kim/notes"]["synced_copy"] == f"{notes_copy}/"
    assert shares["/Users/kim/Atkinson"]["synced_copy"] is None
    assert shares["/Users/kim/Atkinson"]["kind"] == "folder"
    assert shares["/Users/kim/photo.png"]["kind"] == "file"
    # Every call through the gateway names the device, and none puts it in the file path.
    file_calls = [request for request in computer.requests if "/files/" in request.url.path]
    assert all(request.headers["X-Latchkey-Device"] == DEVICE for request in file_calls)
    assert all(DEVICE not in request.url.path for request in file_calls)


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
    assert [path["name"] for path in listing["paths"]] == ["01.jpg"]
    assert client.get(f"/__remote/{DEVICE}/Users/kim/wallpapers/jpeg/01.jpg").data == b"\xff\xd8"
    assert client.put(f"/__remote/{DEVICE}/Users/kim/wallpapers/jpeg/02.jpg", data=b"new").status_code == 201
    assert computer.files["/Users/kim/wallpapers/jpeg/02.jpg"] == b"new"
    # The device's grants are read once, for its first request, not again for each.
    assert [request.url.path for request in computer.requests].count("/permissions/self") == 1


def test_a_sleeping_computer_leaves_its_shares_listed_but_unreachable(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-read-/Users/kim/Atkinson"], folders={}, is_awake=False)
    (share,) = json_of(_app(computer, tmp_path).get("/__files/api/connections"))["shares"]
    assert share["is_reachable"] is False
    assert share["kind"] == "unknown"


def test_a_shared_folder_with_no_copy_is_listed_live_in_dufs_own_shape(tmp_path: Path) -> None:
    computer = FakeComputer(
        permissions=["minds-file-server-read-/Users/kim/Atkinson"],
        folders={"/Users/kim/Atkinson": [("Mono.otf", False, 3), ("Variable", True, 0)]},
    )
    client = _app(computer, tmp_path)
    listing = json_of(client.get(f"/__remote/{DEVICE}/Users/kim/Atkinson/?json"))
    assert [(path["name"], path["path_type"], path["size"]) for path in listing["paths"]] == [
        ("Mono.otf", "File", 3),
        ("Variable", "Dir", 0),
    ]

    page = client.get(f"/__remote/{DEVICE}/Users/kim/Atkinson/").get_data(as_text=True)
    assert page.startswith('<script src="/__dufs_v0.46.0__/index.js">')
    data = json.loads(base64.b64decode(page.split("</script>")[1]))
    assert data["kind"] == "Index"
    # The page carries the listing: the frontend shows it as the folder's and does not ask again.
    assert [path["name"] for path in data["paths"]] == ["Mono.otf", "Variable"]

    assert client.open(f"/__remote/{DEVICE}/Users/kim/Atkinson/Mono.otf", method="MOVE").status_code == 501
    assert client.open(f"/__remote/{DEVICE}/Users/kim/Atkinson/Mono.otf", method="COPY").status_code == 501


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
    assert response.get_data(as_text=True).startswith("Couldn't read your shares")


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
