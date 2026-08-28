import imbue.analytics.app as app_module


def test_app_is_named_for_the_deploy_env() -> None:
    # conftest pins MNGR_DEPLOY_ENV=dev-analytics-tests for every test; the
    # module was imported under it (or a sibling test env) either way.
    assert app_module.app.name is not None
    assert app_module.app.name.startswith("analytics-")


def test_app_registers_the_three_cron_functions() -> None:
    registered_names = set(app_module.app.registered_functions.keys())

    assert "aggregation" in registered_names
    assert "lake_maintenance" in registered_names
    assert "collection_poll" in registered_names
