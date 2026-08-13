import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat
from flask.testing import FlaskClient

from owner_exec.server import OwnerExecConfig
from owner_exec.server import build_owner_exec_app
from owner_exec.signing import NonceCache
from owner_exec.signing import build_signing_string

_AUDIENCE = "host-abc.user.us1.imbueminds.com"


def _openssh_public(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")


class _Harness:
    def __init__(self, client: FlaskClient, key: Ed25519PrivateKey, repo_root: Path) -> None:
        self.client = client
        self.key = key
        self.repo_root = repo_root
        self._nonce_counter = 0

    def signed_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        self._nonce_counter += 1
        timestamp = "1000"
        nonce = f"nonce-{self._nonce_counter}"
        message = build_signing_string(method, path, body, _AUDIENCE, timestamp, nonce)
        return {
            "X-Exec-Signature": base64.b64encode(self.key.sign(message)).decode("ascii"),
            "X-Exec-Public-Key": _openssh_public(self.key),
            "X-Exec-Timestamp": timestamp,
            "X-Exec-Nonce": nonce,
            "Content-Type": "application/json",
        }

    def post(self, path: str, payload: dict[str, object]):  # type: ignore[no-untyped-def]
        body = json.dumps(payload).encode("utf-8")
        return self.client.post(path, data=body, headers=self.signed_headers("POST", path, body))


def _make_harness(tmp_path: Path, audience: str = _AUDIENCE, chrome_origin: str = "") -> _Harness:
    key = Ed25519PrivateKey.generate()
    authorized_keys_path = tmp_path / "authorized_keys"
    authorized_keys_path.write_text(_openssh_public(key) + "\n")
    (tmp_path / "data" / ".secrets").mkdir(parents=True, exist_ok=True)
    config = OwnerExecConfig(
        audience_resolver=lambda: audience,
        authorized_keys_path=authorized_keys_path,
        repo_root=tmp_path,
        nonce_cache=NonceCache(),
        now=lambda: 1000.0,
        chrome_origin_resolver=lambda: chrome_origin,
    )
    app = build_owner_exec_app(config)
    return _Harness(app.test_client(), key, tmp_path)


def test_run_streams_stdout_and_exit_code(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    resp = harness.post("/run", {"command": ["printf", "hello"]})

    assert resp.status_code == 200
    events = [json.loads(line) for line in resp.get_data(as_text=True).splitlines() if line]
    stdout_text = "".join(event["data"] for event in events if event["type"] == "stdout")
    exit_events = [event for event in events if event["type"] == "exit"]
    assert stdout_text == "hello"
    assert exit_events == [{"type": "exit", "code": 0}]


def test_run_reports_stderr_and_nonzero_exit(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    resp = harness.post("/run", {"command": ["sh", "-c", "echo oops 1>&2; exit 3"]})

    events = [json.loads(line) for line in resp.get_data(as_text=True).splitlines() if line]
    stderr_text = "".join(event["data"] for event in events if event["type"] == "stderr")
    exit_events = [event for event in events if event["type"] == "exit"]
    assert "oops" in stderr_text
    assert exit_events[0]["code"] == 3


def test_write_then_read_file_roundtrips(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    content = b"restic-env-contents\n"

    write_resp = harness.post(
        "/write-file",
        {"path": "data/.secrets/restic.env", "content_b64": base64.b64encode(content).decode("ascii"), "mode": "0600"},
    )
    assert write_resp.status_code == 200
    written_file = tmp_path / "data" / ".secrets" / "restic.env"
    assert written_file.read_bytes() == content
    assert (written_file.stat().st_mode & 0o777) == 0o600

    read_resp = harness.post("/read-file", {"path": "data/.secrets/restic.env"})
    assert read_resp.status_code == 200
    body = read_resp.get_json()
    assert body["exists"] is True
    assert base64.b64decode(body["content_b64"]) == content


def test_read_missing_file_reports_absent(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    resp = harness.post("/read-file", {"path": "data/nope.txt"})

    assert resp.status_code == 404
    assert resp.get_json() == {"exists": False}


def _put_grants(harness: _Harness, payload: dict[str, object]):  # type: ignore[no-untyped-def]
    body = json.dumps(payload).encode("utf-8")
    return harness.client.put("/grants", data=body, headers=harness.signed_headers("PUT", "/grants", body))


def test_grants_put_validates_toml_and_get_reads_it_back(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    grants = '[workspace]\nemails = ["owner@example.com"]\nemail_domains = []\n'

    put = _put_grants(harness, {"grants_toml": grants})
    assert put.status_code == 200
    assert (tmp_path / "data" / ".secrets" / "share_grants.toml").read_text() == grants

    get = harness.client.get("/grants", headers=harness.signed_headers("GET", "/grants", b""))
    assert get.status_code == 200
    assert get.get_json()["grants_toml"] == grants
    # The read reports the same revision the write returned, closing the
    # read-modify-write loop.
    assert get.get_json()["revision"] == put.get_json()["revision"]


def test_grants_put_rejects_malformed_toml(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    put = _put_grants(harness, {"grants_toml": "not toml [["})

    assert put.status_code == 400
    assert not (tmp_path / "data" / ".secrets" / "share_grants.toml").exists()


def test_grants_get_reports_the_absent_revision_before_any_write(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    get = harness.client.get("/grants", headers=harness.signed_headers("GET", "/grants", b""))

    assert get.status_code == 200
    assert get.get_json() == {"grants_toml": "", "revision": ""}


def test_grants_put_with_matching_base_revision_succeeds(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    seeded = _put_grants(harness, {"grants_toml": "[workspace]\nemails = []\n"})

    updated = _put_grants(
        harness,
        {"grants_toml": '[workspace]\nemails = ["a@example.com"]\n', "base_revision": seeded.get_json()["revision"]},
    )

    assert updated.status_code == 200
    assert updated.get_json()["revision"] != seeded.get_json()["revision"]
    grants_file = tmp_path / "data" / ".secrets" / "share_grants.toml"
    assert "a@example.com" in grants_file.read_text()


def test_grants_put_seeding_participates_in_cas_via_the_absent_revision(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    seeded = _put_grants(harness, {"grants_toml": "[workspace]\nemails = []\n", "base_revision": ""})

    assert seeded.status_code == 200


def test_grants_put_with_stale_base_revision_conflicts_and_reports_the_current_document(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    first = _put_grants(harness, {"grants_toml": "[workspace]\nemails = []\n"})
    concurrent = '[workspace]\nemails = ["other@example.com"]\n'
    assert _put_grants(harness, {"grants_toml": concurrent}).status_code == 200

    stale = _put_grants(
        harness,
        {"grants_toml": '[workspace]\nemails = ["mine@example.com"]\n', "base_revision": first.get_json()["revision"]},
    )

    assert stale.status_code == 409
    conflict = stale.get_json()
    assert conflict["grants_toml"] == concurrent
    assert conflict["revision"]
    # The losing write must not have landed.
    assert (tmp_path / "data" / ".secrets" / "share_grants.toml").read_text() == concurrent


def test_grants_put_against_the_absent_revision_conflicts_once_a_document_exists(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    assert _put_grants(harness, {"grants_toml": "[workspace]\nemails = []\n"}).status_code == 200

    stale_seed = _put_grants(harness, {"grants_toml": "[workspace]\nemails = []\n", "base_revision": ""})

    assert stale_seed.status_code == 409


def test_grants_put_rejects_a_non_string_base_revision(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    put = _put_grants(harness, {"grants_toml": "[workspace]\n", "base_revision": 7})

    assert put.status_code == 400


def test_unsigned_request_is_rejected(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    resp = harness.client.post(
        "/run", data=json.dumps({"command": ["true"]}), headers={"Content-Type": "application/json"}
    )

    assert resp.status_code == 401


def test_exec_unavailable_when_workspace_not_shared(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, audience="")

    resp = harness.post("/run", {"command": ["true"]})

    assert resp.status_code == 401
    assert "not shared" in resp.get_json()["error"]


def test_alive_is_unauthenticated(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    assert harness.client.get("/_alive").status_code == 204


def test_run_rejects_empty_command(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    resp = harness.post("/run", {"command": []})

    assert resp.status_code == 400


_CHROME = "https://minds.example.com"


def test_preflight_answers_cors_for_the_configured_chrome_origin(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, chrome_origin=_CHROME)

    resp = harness.client.options("/run", headers={"Origin": _CHROME})

    assert resp.status_code == 204
    assert resp.headers["Access-Control-Allow-Origin"] == _CHROME
    assert resp.headers["Access-Control-Allow-Credentials"] == "true"
    assert "X-Exec-Signature" in resp.headers["Access-Control-Allow-Headers"]


def test_cors_headers_are_absent_for_other_origins_and_when_unconfigured(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    configured = _make_harness(tmp_path / "a", chrome_origin=_CHROME)
    foreign = configured.client.options("/run", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in foreign.headers

    unconfigured = _make_harness(tmp_path / "b")
    no_chrome = unconfigured.client.options("/run", headers={"Origin": _CHROME})
    assert "Access-Control-Allow-Origin" not in no_chrome.headers


def test_signed_responses_carry_cors_for_the_chrome_origin(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, chrome_origin=_CHROME)
    body = json.dumps({"command": ["printf", "cors"]}).encode()
    headers = harness.signed_headers("POST", "/run", body)
    headers["Origin"] = _CHROME

    resp = harness.client.post("/run", data=body, headers={**headers, "Content-Type": "application/json"})

    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Origin"] == _CHROME
