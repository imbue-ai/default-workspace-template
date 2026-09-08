from imbue.mngr_vps.errors import VpsApiError
from scripts.cleanup_old_vultr_test_instances import _is_transient_vps_api_error


def test_server_side_errors_are_transient() -> None:
    assert _is_transient_vps_api_error(VpsApiError(500, "Internal server error."))
    assert _is_transient_vps_api_error(VpsApiError(503, "Service unavailable"))


def test_network_level_failures_are_transient() -> None:
    assert _is_transient_vps_api_error(VpsApiError(0, "Request failed: connection reset"))


def test_client_errors_are_not_transient() -> None:
    assert not _is_transient_vps_api_error(VpsApiError(401, "Invalid API key"))
    assert not _is_transient_vps_api_error(VpsApiError(404, "Not found"))


def test_unrelated_exceptions_are_not_transient() -> None:
    assert not _is_transient_vps_api_error(RuntimeError("VPS API error 500: not actually a VpsApiError"))
