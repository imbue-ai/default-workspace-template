import pytest
from app_manifest.primitives import ActionId
from flask.testing import FlaskClient

from app_instances.blueprint import (
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_SERVICE_UNAVAILABLE,
    build_instances_app,
    status_code_for_error,
)
from app_instances.data_types import InstanceRecord
from app_instances.errors import (
    AppInstancesError,
    InstanceConflictError,
    InstanceStoreError,
    InvalidInstanceValueError,
    InvalidParamsError,
    LocationNotTrackedError,
    MalformedRequestError,
    NotReadyError,
    NotRenameableError,
    UnknownActionError,
    UnknownInstanceError,
)
from app_instances.testing import RecordingNudger, StubInstanceSource


def _create(source: StubInstanceSource, path: str) -> InstanceRecord:
    return source.create_instance(ActionId("new"), {"path": path})


def test_the_instances_app_serves_only_the_contract_routes() -> None:
    app = build_instances_app(StubInstanceSource(), RecordingNudger())

    assert {rule.rule for rule in app.url_map.iter_rules()} == {
        "/_instances",
        "/_instances/<key>",
        "/_instances/<key>/rename",
        "/_instances/<key>/location",
    }


def test_list_answers_every_record_in_the_wire_shape_without_nudging(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    first = _create(stub_source, "/one/")
    second = _create(stub_source, "/two/")

    response = instances_client.get("/_instances")

    assert response.status_code == 200
    assert response.get_json() == {
        "instances": [first.model_dump(mode="json"), second.model_dump(mode="json")]
    }
    assert recording_nudger.nudge_count == 0


def test_list_is_empty_for_a_fresh_source(instances_client: FlaskClient) -> None:
    assert instances_client.get("/_instances").get_json() == {"instances": []}


def test_list_answers_503_while_the_source_is_not_ready(
    instances_client: FlaskClient, stub_source: StubInstanceSource
) -> None:
    stub_source.is_ready = False

    response = instances_client.get("/_instances")

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert response.get_json() == {"detail": "the stub is still initialising"}


def test_create_answers_201_with_the_record_and_nudges_once(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    response = instances_client.post(
        "/_instances", json={"action": "new", "params": {"path": "/docs/"}}
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["instance"]["key"] == "stub-1"
    assert body["instance"]["url"] == "/docs/"
    assert stub_source.calls == ["create:new:{'path': '/docs/'}"]
    assert recording_nudger.nudge_count == 1


def test_create_without_a_content_type_still_reads_the_json_body(
    instances_client: FlaskClient,
) -> None:
    response = instances_client.post("/_instances", data='{"action": "new"}')

    assert response.status_code == 201


@pytest.mark.parametrize(
    ("body", "expected_detail_fragment"),
    [
        ("not json", "must be a JSON object"),
        ("[1, 2]", "must be a JSON object"),
        ('{"params": {}}', "field 'action'"),
        ('{"action": "new", "extra": 1}', "field 'extra'"),
    ],
)
def test_create_answers_400_for_a_body_that_is_not_the_contract_shape(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
    body: str,
    expected_detail_fragment: str,
) -> None:
    response = instances_client.post("/_instances", data=body)

    assert response.status_code == HTTP_BAD_REQUEST
    assert expected_detail_fragment in response.get_json()["detail"]
    assert stub_source.calls == []
    assert recording_nudger.nudge_count == 0


def test_create_answers_400_for_an_unknown_action_without_nudging(
    instances_client: FlaskClient, recording_nudger: RecordingNudger
) -> None:
    response = instances_client.post("/_instances", json={"action": "other"})

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.get_json() == {"detail": "unknown action 'other'"}
    assert recording_nudger.nudge_count == 0


def test_create_answers_409_with_the_apps_refusal_verbatim(
    instances_client: FlaskClient, stub_source: StubInstanceSource
) -> None:
    stub_source.create_refusal = "no free browser slots"

    response = instances_client.post("/_instances", json={"action": "new"})

    assert response.status_code == HTTP_CONFLICT
    assert response.get_json() == {"detail": "no free browser slots"}


def test_delete_answers_204_and_nudges_even_for_an_unknown_key(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    _create(stub_source, "/one/")

    known = instances_client.delete("/_instances/stub-1")
    unknown = instances_client.delete("/_instances/stub-9")

    assert known.status_code == 204
    assert unknown.status_code == 204
    assert stub_source.records == []
    assert stub_source.calls[-2:] == ["delete:stub-1", "delete:stub-9"]
    assert recording_nudger.nudge_count == 2


def test_a_malformed_key_answers_400_before_the_source_is_consulted(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    responses = [
        instances_client.delete("/_instances/bad%20key"),
        instances_client.post("/_instances/-lead/rename", json={"title": "x"}),
        instances_client.post("/_instances/.lead/location", json={"path": "/"}),
    ]

    assert [response.status_code for response in responses] == [HTTP_BAD_REQUEST] * 3
    assert all(
        "invalid instance key" in response.get_json()["detail"]
        for response in responses
    )
    assert stub_source.calls == []
    assert recording_nudger.nudge_count == 0


def test_rename_answers_200_with_the_retitled_record_and_nudges(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    _create(stub_source, "/one/")

    response = instances_client.post(
        "/_instances/stub-1/rename", json={"title": "  Notes  "}
    )

    assert response.status_code == 200
    assert response.get_json()["instance"]["title"] == "Notes"
    assert stub_source.records[0].title == "Notes"
    assert recording_nudger.nudge_count == 1


def test_rename_maps_unknown_not_renameable_collision_and_bad_title(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    _create(stub_source, "/one/")
    _create(stub_source, "/two/")

    unknown = instances_client.post("/_instances/stub-9/rename", json={"title": "x"})
    collision = instances_client.post(
        "/_instances/stub-1/rename", json={"title": "Stub 2"}
    )
    blank = instances_client.post("/_instances/stub-1/rename", json={"title": "  "})
    stub_source.is_renameable = False
    refused = instances_client.post("/_instances/stub-1/rename", json={"title": "x"})

    assert unknown.status_code == HTTP_NOT_FOUND
    assert collision.status_code == HTTP_CONFLICT
    assert blank.status_code == HTTP_BAD_REQUEST
    assert refused.status_code == HTTP_BAD_REQUEST
    assert recording_nudger.nudge_count == 0


def test_location_answers_200_with_the_relocated_record_and_nudges(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    _create(stub_source, "/one/")

    response = instances_client.post(
        "/_instances/stub-1/location", json={"path": "/data/docs/"}
    )

    assert response.status_code == 200
    assert response.get_json()["instance"]["url"] == "/data/docs/"
    assert stub_source.records[0].url == "/data/docs/"
    assert recording_nudger.nudge_count == 1


def test_location_hands_an_absolute_url_to_the_source(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    _create(stub_source, "/one/")

    # The stub records paths under its own origin, so it refuses the URL (a 400 from the
    # source, not from the body parser): the call reached it with the URL intact.
    response = instances_client.post(
        "/_instances/stub-1/location", json={"path": "https://example.com/page"}
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert stub_source.calls[-1] == "location:stub-1:https://example.com/page"
    assert recording_nudger.nudge_count == 0


def test_location_maps_unknown_untracked_and_bad_path(
    instances_client: FlaskClient,
    stub_source: StubInstanceSource,
    recording_nudger: RecordingNudger,
) -> None:
    _create(stub_source, "/one/")

    unknown = instances_client.post("/_instances/stub-9/location", json={"path": "/"})
    placeholder = instances_client.post(
        "/_instances/stub-1/location", json={"path": "/{tab}"}
    )
    unrooted = instances_client.post(
        "/_instances/stub-1/location", json={"path": "docs"}
    )
    stub_source.is_location_tracked = False
    untracked = instances_client.post("/_instances/stub-1/location", json={"path": "/"})

    assert unknown.status_code == HTTP_NOT_FOUND
    assert placeholder.status_code == HTTP_BAD_REQUEST
    assert unrooted.status_code == HTTP_BAD_REQUEST
    assert untracked.status_code == HTTP_BAD_REQUEST
    assert recording_nudger.nudge_count == 0


class _BrokenSource(StubInstanceSource):
    """A source whose store is unreadable."""

    def list_instances(self) -> list[InstanceRecord]:
        raise InstanceStoreError("instances.json is not valid JSON")


def test_an_unmapped_library_error_answers_500_with_a_detail_body() -> None:
    client = build_instances_app(_BrokenSource(), RecordingNudger()).test_client()

    response = client.get("/_instances")

    assert response.status_code == HTTP_INTERNAL_ERROR
    assert response.get_json() == {"detail": "instances.json is not valid JSON"}


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (InvalidInstanceValueError("x"), HTTP_BAD_REQUEST),
        (UnknownActionError("x"), HTTP_BAD_REQUEST),
        (InvalidParamsError("x"), HTTP_BAD_REQUEST),
        (NotRenameableError("x"), HTTP_BAD_REQUEST),
        (LocationNotTrackedError("x"), HTTP_BAD_REQUEST),
        (MalformedRequestError("x"), HTTP_BAD_REQUEST),
        (UnknownInstanceError("x"), HTTP_NOT_FOUND),
        (InstanceConflictError("x"), HTTP_CONFLICT),
        (NotReadyError("x"), HTTP_SERVICE_UNAVAILABLE),
        (InstanceStoreError("x"), HTTP_INTERNAL_ERROR),
        (AppInstancesError("x"), HTTP_INTERNAL_ERROR),
    ],
)
def test_status_code_for_error_follows_the_contract(
    error: AppInstancesError, expected_status: int
) -> None:
    assert status_code_for_error(error) == expected_status
