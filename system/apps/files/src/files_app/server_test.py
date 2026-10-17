import base64
import json
from pathlib import Path

import httpx

from app_schema.testing import assert_api_schema_describes_app
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
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


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
        agent_id="agent-main",
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
    assert shared_paths(rules) == {"/Users/kim/notes": "read", "/Users/kim/photo.png": "write"}


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
    assert listed["devices"] == [{"id": DEVICE, "name": "Kims-Mac.local"}]
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

    assert client.open(f"/__remote/{DEVICE}/Users/kim/Atkinson/Mono.otf", method="MOVE").status_code == 501


def test_a_shared_file_is_streamed_from_the_computer_and_written_back_with_write_access(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-write-/Users/kim/photo.png"], folders={})
    computer.files["/Users/kim/photo.png"] = b"\x89PNG"
    client = _app(computer, tmp_path)
    response = client.get(f"/__remote/{DEVICE}/Users/kim/photo.png")
    assert response.status_code == 200 and response.data == b"\x89PNG"

    assert client.put(f"/__remote/{DEVICE}/Users/kim/photo.png", data=b"new").status_code == 201
    assert computer.files["/Users/kim/photo.png"] == b"new"


def test_asking_for_a_share_reaches_imbue_studio_as_a_file_sharing_request_from_the_main_chat(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=[], folders={})
    response = _app(computer, tmp_path).post(
        "/__files/api/connections", json={"device": DEVICE, "path": "~/Projects", "access": "write", "sync": True}
    )
    assert response.status_code == 200
    (asked,) = [request for request in computer.requests if request.url.path == "/permission-requests"]
    body = json.loads(asked.read())
    assert body["agent_id"] == "agent-main"
    assert body["type"] == "file-sharing"
    assert body["payload"] == {"path": "~/Projects", "access": "WRITE", "sync": {"conflict": "NEWER"}}
    assert asked.headers["X-Latchkey-Device"] == DEVICE


def test_with_no_gateway_there_are_no_shares_and_asking_explains_why(tmp_path: Path) -> None:
    client = _app(None, tmp_path)
    assert json_of(client.get("/__files/api/connections")) == {"devices": [], "shares": []}
    assert client.post("/__files/api/connections", json={}).status_code == 503


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
