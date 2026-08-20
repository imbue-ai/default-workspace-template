"""Deploy-time conventions shared by our Modal apps.

These helpers are evaluated at ``modal deploy`` time, when the app module is
imported locally and its function specs are serialized: the env vars read here
are threaded into the subprocess env by ``minds env deploy`` and baked into
the deployed spec. See libs/modal_app_kit/README.md for the full deployment
model and the reasons behind it.
"""

import os
from typing import Final

import modal

# Tier name ("production", "staging", "dev", ...) selected by the deploy
# wrapper. Defaults to production so a bare `modal deploy` outside the wrapper
# targets production secret names (which then must exist for the deploy to
# succeed -- see read_deploy_id for the matching safety property).
DEPLOY_ENV_VAR: Final[str] = "MNGR_DEPLOY_ENV"

# Per-deploy timestamp minted by ``minds env deploy`` and baked into the
# deployed function spec so the app pins to the matching ``<svc>-<tier>-<id>``
# Modal Secrets. ``modal app rollback`` reverts the captured env and thereby
# re-attaches the previous deploy's secrets in one shot.
DEPLOY_ID_ENV_VAR: Final[str] = "MINDS_DEPLOY_ID"

# Fallback when the deploy id is unset so unit tests can import app modules
# without raising; the resulting ``<svc>-<tier>-MINDS_DEPLOY_ID_UNSET`` secret
# name doesn't exist in any Modal env, so a real ``modal deploy`` outside of
# ``minds env deploy`` fails with "Secret not found" -- the safety property
# the timestamped-secret rollback model needs.
DEPLOY_ID_UNSET_SENTINEL: Final[str] = "MINDS_DEPLOY_ID_UNSET"


def read_deploy_env() -> str:
    return os.environ.get(DEPLOY_ENV_VAR, "production")


def read_deploy_id() -> str:
    return os.environ.get(DEPLOY_ID_ENV_VAR, DEPLOY_ID_UNSET_SENTINEL)


def read_min_containers(env_var: str) -> int:
    """Warm-pool size threaded in by ``minds env deploy``; 0 (the default) means no warm pool."""
    return int(os.environ.get(env_var, "0"))


def read_custom_domains(env_var: str) -> list[str] | None:
    """Comma-separated Modal custom-domain hosts threaded in by ``minds env deploy``.

    None (the default) means the app deploys with no custom domains -- the
    shape ``modal.asgi_app(custom_domains=...)`` expects for "none".
    """
    hosts = [host.strip() for host in os.environ.get(env_var, "").split(",") if host.strip()]
    return hosts or None


def read_scaledown_window(env_var: str) -> int | None:
    """Idle-before-scaledown window (seconds) threaded in by ``minds env deploy``.

    Modal requires the value to be > 0, so 0 (the default, and what the
    ci/test tier uses) is normalized to None, meaning "use Modal's own
    default scaledown window".
    """
    return int(os.environ.get(env_var, "0")) or None


def stamped_secret_name(service: str, tier: str, deploy_id: str) -> str:
    """The ``<service>-<tier>-<deploy_id>`` Modal Secret name minted by ``minds env deploy``."""
    return f"{service}-{tier}-{deploy_id}"


def stamped_secret(service: str, tier: str, deploy_id: str) -> modal.Secret:
    return modal.Secret.from_name(stamped_secret_name(service, tier, deploy_id))


def deploy_metadata_secret(tier: str, deploy_id: str) -> modal.Secret:
    """An inline Secret carrying the deploy env + id into the running container.

    Both values are baked into the function spec at deploy time; this makes
    them readable at runtime via ``os.environ`` (e.g. by a ``/version``
    endpoint) without a Vault-backed Secret.
    """
    return modal.Secret.from_dict({DEPLOY_ENV_VAR: tier, DEPLOY_ID_ENV_VAR: deploy_id})
