import base64
import hashlib
from pathlib import Path

import pytest
from flask import Blueprint, Flask, Response, send_file

from app_manifest.content_security_policy import (
    attach_document_policy,
    build_document_policy,
    inline_script_hashes,
    inline_source,
    url_origin,
)
from app_manifest.errors import PolicySourceError
from app_manifest.testing import assert_document_policy_admits_page

_PAGE_WITH_SCRIPTS = (
    "<!doctype html><html><head><script src=/app.js></script></head><body>"
    '<script type="module">\n  import { a } from "/a.js";\n</script>'
    "<script></script>"
    "<SCRIPT>console.log(1)</SCRIPT>"
    "<script>console.log(1)</script>"
    '<script data-src="/lazy.js">lazy()</script>'
    "</body></html>"
)


def _sha256_source(body: str) -> str:
    return (
        "'sha256-"
        + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        + "'"
    )


def test_inline_script_hashes_cover_each_inline_body_once_and_skip_external_and_empty_scripts() -> (
    None
):
    assert inline_script_hashes(_PAGE_WITH_SCRIPTS) == (
        _sha256_source('\n  import { a } from "/a.js";\n'),
        _sha256_source("console.log(1)"),
        _sha256_source("lazy()"),
    )
    assert inline_script_hashes("<p>no scripts</p>") == ()
    assert inline_source("console.log(1)") == _sha256_source("console.log(1)")


def test_url_origin_keeps_the_scheme_host_and_port_and_refuses_a_relative_url() -> None:
    assert url_origin("https://raw.githubusercontent.com/imbue-ai/x/main/catalog.json") == "https://raw.githubusercontent.com"
    assert url_origin("http://localhost:8030/api?x=1") == "http://localhost:8030"
    with pytest.raises(PolicySourceError):
        url_origin("catalog/new-tab-templates.json")


def test_build_document_policy_appends_hashes_to_script_src_and_applies_overrides() -> (
    None
):
    policy = build_document_policy(
        _PAGE_WITH_SCRIPTS, {"script-src": "'self' blob:", "worker-src": "blob:"}
    )

    directives = dict(part.split(" ", 1) for part in policy.split("; "))
    assert directives["script-src"] == " ".join(
        (
            "'self' blob:",
            _sha256_source('\n  import { a } from "/a.js";\n'),
            _sha256_source("console.log(1)"),
            _sha256_source("lazy()"),
        )
    )
    assert directives["worker-src"] == "blob:"
    assert directives["default-src"] == "'self'"
    # No cross-origin fetch by default: an app that renders content from elsewhere overrides the directive.
    assert directives["img-src"] == "'self' data: blob:"
    assert "media-src" not in directives
    assert directives["object-src"] == "'none'"
    assert "frame-ancestors" not in directives
    assert build_document_policy("<p>plain</p>").startswith(
        "default-src 'self'; script-src 'self'; "
    )


def _app(tmp_path: Path) -> Flask:
    app = Flask("under-test", static_folder=None)
    document = tmp_path / "built.html"
    document.write_text(
        "<!doctype html><html><body><script src=/bundle.js></script></body></html>"
    )
    blueprint = Blueprint("pages", __name__)

    @blueprint.get("/")
    def page() -> Response:
        return Response(
            "<html><body><script>work()</script></body></html>", mimetype="text/html"
        )

    @blueprint.get("/built")
    def built() -> Response:
        return send_file(document, mimetype="text/html")

    @blueprint.get("/own-policy")
    def own_policy() -> Response:
        return Response(
            "<html></html>",
            mimetype="text/html",
            headers={"Content-Security-Policy": "default-src 'none'"},
        )

    @blueprint.get("/data")
    def data() -> Response:
        return Response('{"ok": true}', mimetype="application/json")

    attach_document_policy(blueprint)
    app.register_blueprint(blueprint)
    return app


def test_attach_document_policy_stamps_html_responses_only_and_keeps_a_routes_own_policy(
    tmp_path: Path,
) -> None:
    client = _app(tmp_path).test_client()

    page = client.get("/")
    assert_document_policy_admits_page(page)
    assert _sha256_source("work()") in page.headers["Content-Security-Policy"]
    # A streamed file is not read: it gets the policy without hashes, which admits its external bundle.
    built = client.get("/built")
    assert built.headers["Content-Security-Policy"] == build_document_policy("")
    assert_document_policy_admits_page(built)
    assert (
        client.get("/own-policy").headers["Content-Security-Policy"]
        == "default-src 'none'"
    )
    assert "Content-Security-Policy" not in client.get("/data").headers
