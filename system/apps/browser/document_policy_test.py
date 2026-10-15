from app_manifest.testing import assert_document_policy_admits_page
from browser import runner as _runner


def test_the_viewer_page_is_served_under_a_policy_admitting_its_inline_scripts_and_its_blob_worklet() -> (
    None
):
    page = _runner.application.test_client().get("/")

    assert page.status_code == 200
    assert_document_policy_admits_page(page)
    policy = page.headers["Content-Security-Policy"]
    assert "script-src 'self' blob: 'sha256-" in policy
    assert "worker-src 'self' blob:" in policy
    assert (
        "Content-Security-Policy"
        not in _runner.application.test_client().get("/health").headers
    )
