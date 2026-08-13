import imbue.oauth_redirector.app as app_mod
from imbue.oauth_redirector.web import web_app


def test_modal_app_is_named_for_the_deploy_env() -> None:
    # The redirector is deployed once per tier, so the tier must be in the app
    # name (a bare shared name would collide across tiers in one workspace).
    assert app_mod.app.name == f"oauth-redirector-{app_mod._DEPLOY_ENV}"


def test_fastapi_entrypoint_serves_the_shared_web_app() -> None:
    assert app_mod.fastapi_app.local() is web_app
