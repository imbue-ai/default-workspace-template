import json

import httpx
import pytest

import imbue.remote_service_connector.cloudflare as cloudflare_mod
from imbue.remote_service_connector.cloudflare import HttpCloudflareOps
from imbue.remote_service_connector.cloudflare import cf_check
from imbue.remote_service_connector.cloudflare import cf_list_all_pages
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import R2StorageResultTruncatedError
from imbue.remote_service_connector.testing import _build_http_ops_with_handler


def test_cf_check_raises_on_error() -> None:
    response = httpx.Response(400, json={"success": False, "errors": [{"message": "bad"}]})
    with pytest.raises(CloudflareApiError) as exc_info:
        cf_check(response)
    assert exc_info.value.status_code == 400


def test_cf_check_returns_data_on_success() -> None:
    response = httpx.Response(200, json={"success": True, "result": {"id": "123"}})
    data = cf_check(response)
    assert data["result"]["id"] == "123"


def test_cf_list_all_pages_paginates() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        page = int(dict(request.url.params).get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"id": "1"}, {"id": "2"}],
                    "result_info": {"total_count": 3, "page": 1, "per_page": 2, "count": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [{"id": "3"}],
                "result_info": {"total_count": 3, "page": 2, "per_page": 2, "count": 1},
            },
        )

    client = httpx.Client(base_url="https://test.example.com", transport=httpx.MockTransport(handler))
    results = cf_list_all_pages(client, "/test", {})
    assert len(results) == 3
    assert call_count == 2


def _cf_result(result: object, *, total_count: int | None = None) -> dict[str, object]:
    body: dict[str, object] = {"success": True, "result": result}
    if total_count is not None and isinstance(result, list):
        body["result_info"] = {
            "total_count": total_count,
            "page": 1,
            "per_page": len(result) or 1,
            "count": len(result),
        }
    return body


def _build_http_ops_with_routes(
    routes: dict[tuple[str, str], httpx.Response],
) -> HttpCloudflareOps:
    """Construct an HttpCloudflareOps whose client is wired to a MockTransport.

    Each key in ``routes`` is ``(method, path_prefix)``; the first matching
    route returns its response. Requests that don't match any route produce a
    clear AssertionError instead of a silent 404 so new uncovered code paths
    fail loudly in test output.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        for (method, path), response in routes.items():
            if request.method == method and request.url.path.startswith(path):
                return response
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    return _build_http_ops_with_handler(handler)


def test_http_ops_tunnel_roundtrip() -> None:
    """create_tunnel, list_tunnels, get_tunnel_by_name/id, get_tunnel_token, delete_tunnel."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("POST", "/client/v4/accounts/acc/cfd_tunnel"): httpx.Response(
            200, json=_cf_result({"id": "t1", "name": "alice--a1"})
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/t1/token"): httpx.Response(
            200, json=_cf_result("tunnel-token-value")
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/t1"): httpx.Response(
            200, json=_cf_result({"id": "t1", "name": "alice--a1"})
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel"): httpx.Response(
            200, json=_cf_result([{"id": "t1", "name": "alice--a1"}], total_count=1)
        ),
        ("DELETE", "/client/v4/accounts/acc/cfd_tunnel/t1"): httpx.Response(200, json=_cf_result(None)),
    }
    ops = _build_http_ops_with_routes(routes)
    tunnel = ops.create_tunnel("alice--a1")
    assert tunnel["id"] == "t1"
    assert ops.get_tunnel_token("t1") == "tunnel-token-value"
    assert ops.get_tunnel_by_id("t1") == {"id": "t1", "name": "alice--a1"}
    by_name = ops.get_tunnel_by_name("alice--a1")
    assert by_name is not None and by_name["id"] == "t1"
    tunnels = ops.list_tunnels(include_prefix="alice")
    assert len(tunnels) == 1
    ops.delete_tunnel("t1")


def test_http_ops_get_tunnel_by_id_returns_none_on_404() -> None:
    """cf_get_tunnel_by_id returns None (not raising) when the tunnel is missing."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/missing"): httpx.Response(
            404, json={"success": False, "errors": [{"message": "not found"}]}
        ),
    }
    ops = _build_http_ops_with_routes(routes)
    assert ops.get_tunnel_by_id("missing") is None


def test_http_ops_tunnel_config_roundtrip() -> None:
    """get_tunnel_config and put_tunnel_config both route through cf_check."""
    put_calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/configurations" in request.url.path:
            return httpx.Response(200, json=_cf_result({"config": {"ingress": []}}))
        if request.method == "PUT" and "/configurations" in request.url.path:
            put_calls.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    ops = _build_http_ops_with_handler(handler)
    config = ops.get_tunnel_config("t1")
    assert "config" in config
    ops.put_tunnel_config("t1", {"config": {"ingress": [{"service": "http_status:404"}]}})
    assert len(put_calls) == 1


def test_http_ops_dns_record_roundtrip() -> None:
    """create_cname, list_dns_records (with filter), delete_dns_record."""
    created: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/dns_records"):
            created.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result({"id": "r1", "name": "x.example.com"}))
        if request.method == "GET" and request.url.path.endswith("/dns_records"):
            return httpx.Response(
                200,
                json=_cf_result([{"id": "r1", "name": "x.example.com"}], total_count=1),
            )
        if request.method == "DELETE" and "/dns_records/r1" in request.url.path:
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    ops = _build_http_ops_with_handler(handler)
    record = ops.create_cname("x.example.com", "target.example.com")
    assert record["id"] == "r1"
    assert created[0]["type"] == "CNAME"
    assert created[0]["proxied"] is True
    records = ops.list_dns_records(name="x.example.com")
    assert len(records) == 1
    ops.delete_dns_record("r1")


def test_http_ops_access_app_and_policies_roundtrip() -> None:
    """Full Access Application + policy lifecycle flows through the real wrappers."""
    policies: list[dict[str, object]] = []
    created_apps: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/access/apps"):
            created_apps.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result({"id": "app1", "domain": "x.example.com"}))
        if request.method == "GET" and path.endswith("/access/apps"):
            return httpx.Response(200, json=_cf_result([{"id": "app1", "domain": "x.example.com"}]))
        if request.method == "DELETE" and "/access/apps/app1/policies/p1" in path:
            return httpx.Response(200, json=_cf_result(None))
        if request.method == "DELETE" and path.endswith("/access/apps/app1"):
            return httpx.Response(200, json=_cf_result(None))
        if request.method == "GET" and "/access/apps/app1/policies" in path:
            return httpx.Response(200, json=_cf_result(list(policies)))
        if request.method == "POST" and "/access/apps/app1/policies" in path:
            body = json.loads(request.content.decode())
            policy_record = {**body, "id": "p1"}
            policies.append(policy_record)
            return httpx.Response(200, json=_cf_result(policy_record))
        if request.method == "PUT" and "/access/apps/app1/policies/p1" in path:
            body = json.loads(request.content.decode())
            policies[0] = {**body, "id": "p1"}
            return httpx.Response(200, json=_cf_result(policies[0]))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    ops.create_access_app("x.example.com", "My App", allowed_idps=["idp-1"])
    assert created_apps[0]["allowed_idps"] == ["idp-1"]
    by_domain = ops.get_access_app_by_domain("x.example.com")
    assert by_domain is not None and by_domain["id"] == "app1"
    created_policy = ops.create_access_policy("app1", {"name": "allow", "decision": "allow"})
    assert created_policy["id"] == "p1"
    listed = ops.list_access_policies("app1")
    assert len(listed) == 1
    ops.update_access_policy("app1", "p1", {"name": "allow-updated", "decision": "allow"})
    assert ops.list_access_policies("app1")[0]["name"] == "allow-updated"
    ops.delete_access_policy("app1", "p1")
    ops.delete_access_app("app1")


def test_http_ops_kv_namespace_create_when_missing() -> None:
    """kv_get/kv_put/kv_delete + namespace creation path."""
    stored: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(200, json=_cf_result([]))
        if request.method == "POST" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(200, json=_cf_result({"id": "ns1", "title": "cloudflare-forwarding-defaults"}))
        if "/storage/kv/namespaces/ns1/values/" in path:
            key = path.rsplit("/", 1)[-1]
            if request.method == "GET":
                if key not in stored:
                    return httpx.Response(404)
                return httpx.Response(200, text=stored[key])
            if request.method == "PUT":
                stored[key] = request.content.decode()
                return httpx.Response(200, json=_cf_result(None))
            if request.method == "DELETE":
                stored.pop(key, None)
                return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    assert ops.kv_get("missing") is None
    ops.kv_put("alice--a1", '{"default": "allow"}')
    assert ops.kv_get("alice--a1") == '{"default": "allow"}'
    ops.kv_delete("alice--a1")
    assert ops.kv_get("alice--a1") is None


def test_http_ops_kv_namespace_reuses_existing() -> None:
    """cf_kv_ensure_namespace returns the existing namespace's id without creating a new one."""
    create_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        path = request.url.path
        if request.method == "GET" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(
                200,
                json=_cf_result([{"id": "ns-existing", "title": "cloudflare-forwarding-defaults"}]),
            )
        if request.method == "POST" and path.endswith("/storage/kv/namespaces"):
            create_calls += 1
            return httpx.Response(200, json=_cf_result({"id": "ns-new", "title": "cloudflare-forwarding-defaults"}))
        if "/storage/kv/namespaces/ns-existing/values/" in path and request.method == "PUT":
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    ops.kv_put("k", "v")
    assert create_calls == 0


def test_http_ops_service_token_roundtrip() -> None:
    """create_service_token, list_service_tokens, delete_service_token."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("POST", "/client/v4/accounts/acc/access/service_tokens"): httpx.Response(
            200, json=_cf_result({"id": "svc1", "client_id": "cid", "client_secret": "sec"})
        ),
        ("GET", "/client/v4/accounts/acc/access/service_tokens"): httpx.Response(
            200, json=_cf_result([{"id": "svc1"}])
        ),
        ("DELETE", "/client/v4/accounts/acc/access/service_tokens/svc1"): httpx.Response(200, json=_cf_result(None)),
    }
    ops = _build_http_ops_with_routes(routes)
    token = ops.create_service_token("name")
    assert token["id"] == "svc1"
    assert len(ops.list_service_tokens()) == 1
    ops.delete_service_token("svc1")


def test_cf_access_calls_retry_transient_500s() -> None:
    """A Cloudflare Access 5xx (e.g. its internal error while a just-deleted app
    for the same hostname is still tearing down) is retried and succeeds."""
    call_counter = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_counter["count"] += 1
        if call_counter["count"] == 1:
            return httpx.Response(
                500,
                json={
                    "success": False,
                    "errors": [{"code": 10001, "message": "access.api.error.internal_server_error"}],
                },
            )
        return httpx.Response(200, json={"success": True, "result": {"id": "app-1"}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.cloudflare.example")
    result = cloudflare_mod.cf_create_access_app(client, "acct", "web.example.com", "cf-fwd-test")
    assert result == {"id": "app-1"}
    assert call_counter["count"] == 2


def test_cf_access_calls_do_not_retry_client_errors() -> None:
    call_counter = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_counter["count"] += 1
        return httpx.Response(400, json={"success": False, "errors": [{"code": 1001, "message": "bad request"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.cloudflare.example")
    with pytest.raises(CloudflareApiError):
        cloudflare_mod.cf_create_access_app(client, "acct", "web.example.com", "cf-fwd-test")
    assert call_counter["count"] == 1


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
