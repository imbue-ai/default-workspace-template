from latchkey_access.gateway import (
    ENV_GATEWAY,
    ENV_GATEWAY_PASSWORD,
    ENV_GATEWAY_PERMISSIONS_OVERRIDE,
    HEADER_GATEWAY_PASSWORD,
    HEADER_GATEWAY_PERMISSIONS_OVERRIDE,
    GatewayAccess,
)


def test_the_gateway_is_read_from_the_environment_with_its_override_jwt_when_present() -> (
    None
):
    access = GatewayAccess.from_environ(
        {
            ENV_GATEWAY: "http://127.0.0.1:1989/",
            ENV_GATEWAY_PASSWORD: "pw-4412",
            ENV_GATEWAY_PERMISSIONS_OVERRIDE: "jwt-8831",
        }
    )
    assert access is not None
    assert (
        access.url("/permission-requests")
        == "http://127.0.0.1:1989/permission-requests"
    )
    assert access.headers() == {
        HEADER_GATEWAY_PASSWORD: "pw-4412",
        HEADER_GATEWAY_PERMISSIONS_OVERRIDE: "jwt-8831",
    }


def test_a_vps_gateway_without_the_override_jwt_sends_only_the_password() -> None:
    access = GatewayAccess.from_environ(
        {ENV_GATEWAY: "http://host.docker.internal:1989", ENV_GATEWAY_PASSWORD: "pw"}
    )
    assert access is not None
    assert access.permissions_override is None
    assert access.headers() == {HEADER_GATEWAY_PASSWORD: "pw"}


def test_a_missing_address_or_password_means_no_gateway() -> None:
    assert GatewayAccess.from_environ({}) is None
    assert GatewayAccess.from_environ({ENV_GATEWAY: "http://127.0.0.1:1989"}) is None
    assert GatewayAccess.from_environ({ENV_GATEWAY_PASSWORD: "pw"}) is None
    assert (
        GatewayAccess.from_environ({ENV_GATEWAY: "", ENV_GATEWAY_PASSWORD: "pw"})
        is None
    )


def test_secrets_never_appear_in_the_models_text() -> None:
    access = GatewayAccess.from_environ(
        {ENV_GATEWAY: "http://127.0.0.1:1989", ENV_GATEWAY_PASSWORD: "pw-secret-9"}
    )
    assert access is not None
    assert "pw-secret-9" not in repr(access)
