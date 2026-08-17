import json

import httpx
import pytest

import imbue.remote_service_connector.cloudflare as cloudflare_mod
from imbue.remote_service_connector.cloudflare import _is_bucket_not_empty_error
from imbue.remote_service_connector.cloudflare import _is_bucket_not_found_error
from imbue.remote_service_connector.cloudflare import cf_check
from imbue.remote_service_connector.cloudflare import cf_create_bucket
from imbue.remote_service_connector.cloudflare import cf_delete_bucket
from imbue.remote_service_connector.cloudflare import cf_list_buckets
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import R2BucketNotEmptyError
from imbue.remote_service_connector.errors import R2BucketNotFoundError
from imbue.remote_service_connector.errors import R2StorageResultTruncatedError


def test_cf_check_raises_on_error() -> None:
    response = httpx.Response(400, json={"success": False, "errors": [{"message": "bad"}]})
    with pytest.raises(CloudflareApiError) as exc_info:
        cf_check(response)
    assert exc_info.value.status_code == 400


def test_cf_check_returns_data_on_success() -> None:
    response = httpx.Response(200, json={"success": True, "result": {"id": "123"}})
    data = cf_check(response)
    assert data["result"]["id"] == "123"


def test_is_bucket_not_found_error_matches_status_and_code() -> None:
    by_status = CloudflareApiError(404, [{"message": "anything"}])
    by_code = CloudflareApiError(400, [{"code": 10007, "message": "The specified key does not exist."}])
    unrelated = CloudflareApiError(400, [{"code": 7003, "message": "no such account"}])
    assert _is_bucket_not_found_error(by_status) is True
    assert _is_bucket_not_found_error(by_code) is True
    assert _is_bucket_not_found_error(unrelated) is False


def test_is_bucket_not_empty_error_matches_message_and_code() -> None:
    by_message = CloudflareApiError(400, [{"message": "The bucket you tried to delete is not empty"}])
    by_code = CloudflareApiError(400, [{"code": 10040, "message": "unrelated wording"}])
    unrelated = CloudflareApiError(400, [{"code": 7003, "message": "no such bucket"}])
    assert _is_bucket_not_empty_error(by_message) is True
    assert _is_bucket_not_empty_error(by_code) is True
    assert _is_bucket_not_empty_error(unrelated) is False


def test_cf_create_bucket_posts_name_and_returns_result() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/acct-1/r2/buckets"
        assert json.loads(request.content) == {"name": "bucket-a"}
        return httpx.Response(200, json={"success": True, "result": {"name": "bucket-a"}})

    client = httpx.Client(transport=httpx.MockTransport(_handler), base_url="https://api.cloudflare.example")
    assert cf_create_bucket(client, "acct-1", "bucket-a") == {"name": "bucket-a"}


def test_cf_list_buckets_follows_pagination_cursors() -> None:
    # Two pages: the first returns a cursor, the second ends the walk. The
    # name_contains filter must ride along on every page request.
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name_contains"] == "minds-"
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"buckets": [{"name": "minds-one"}]},
                    "result_info": {"cursor": "page-2"},
                },
            )
        assert request.url.params["cursor"] == "page-2"
        return httpx.Response(200, json={"success": True, "result": {"buckets": [{"name": "minds-two"}]}})

    client = httpx.Client(transport=httpx.MockTransport(_handler), base_url="https://api.cloudflare.example")
    buckets = cf_list_buckets(client, "acct-1", name_contains="minds-")
    assert [bucket["name"] for bucket in buckets] == ["minds-one", "minds-two"]


def test_cf_delete_bucket_translates_not_found_and_not_empty_errors() -> None:
    def _make_client(status_code: int, error: dict[str, object]) -> httpx.Client:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"success": False, "errors": [error]})

        return httpx.Client(transport=httpx.MockTransport(_handler), base_url="https://api.cloudflare.example")

    with pytest.raises(R2BucketNotFoundError):
        cf_delete_bucket(_make_client(404, {"message": "no such bucket"}), "acct-1", "gone")
    with pytest.raises(R2BucketNotEmptyError):
        cf_delete_bucket(_make_client(400, {"code": 10040, "message": "bucket not empty"}), "acct-1", "full")
    # Anything else keeps the raw Cloudflare error so the caller sees the real cause.
    with pytest.raises(CloudflareApiError):
        cf_delete_bucket(_make_client(500, {"message": "internal error"}), "acct-1", "b")


def test_parse_r2_storage_graphql_response_maps_one_row_per_bucket() -> None:
    response = {
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "r2StorageAdaptiveGroups": [
                            {
                                "max": {"payloadSize": 100, "metadataSize": 5},
                                "dimensions": {"bucketName": "u1--a"},
                            },
                            {
                                "max": {"payloadSize": 7, "metadataSize": 0},
                                "dimensions": {"bucketName": "u2--b"},
                            },
                        ]
                    }
                ]
            }
        }
    }
    usage = cloudflare_mod.parse_r2_storage_graphql_response(response)
    assert usage == {"u1--a": 105, "u2--b": 7}


def test_parse_r2_storage_graphql_response_raises_when_row_budget_is_hit() -> None:
    """A response filling the query's row budget may be truncated and must fail the sweep loudly."""
    full_page = {
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "r2StorageAdaptiveGroups": [
                            {
                                "max": {"payloadSize": 1, "metadataSize": 0},
                                "dimensions": {"bucketName": "u1--a"},
                            }
                        ]
                        * cloudflare_mod._R2_STORAGE_GRAPHQL_ROW_LIMIT
                    }
                ]
            }
        }
    }
    with pytest.raises(R2StorageResultTruncatedError):
        cloudflare_mod.parse_r2_storage_graphql_response(full_page)
    # A small (untruncated) response parses normally.
    assert cloudflare_mod.parse_r2_storage_graphql_response({"data": {"viewer": {"accounts": []}}}) == {}
