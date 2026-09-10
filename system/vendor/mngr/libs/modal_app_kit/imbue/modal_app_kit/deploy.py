"""Deploy-time conventions shared by our Modal apps.

These helpers are evaluated at ``modal deploy`` time, when the app module is
imported locally and its function specs are serialized: the env vars read here
are threaded into the subprocess env by ``minds-admin env deploy`` and baked into
the deployed spec. See libs/modal_app_kit/README.md for the full deployment
model and the reasons behind it.
"""

import os
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

import modal

from imbue.modal_app_kit.log_format import LOG_LEVEL_ENV_VAR

# Tier name ("production", "staging", "dev", ...) selected by the deploy
# wrapper. Defaults to production so a bare `modal deploy` outside the wrapper
# targets production secret names (which then must exist for the deploy to
# succeed -- see read_deploy_id for the matching safety property).
DEPLOY_ENV_VAR: Final[str] = "MNGR_DEPLOY_ENV"

# Per-deploy timestamp minted by ``minds-admin env deploy`` and baked into the
# deployed function spec so the app pins to the matching ``<svc>-<tier>-<id>``
# Modal Secrets. ``modal app rollback`` reverts the captured env and thereby
# re-attaches the previous deploy's secrets in one shot.
DEPLOY_ID_ENV_VAR: Final[str] = "MINDS_DEPLOY_ID"

# Fallback when the deploy id is unset so unit tests can import app modules
# without raising; the resulting ``<svc>-<tier>-MINDS_DEPLOY_ID_UNSET`` secret
# name doesn't exist in any Modal env, so a real ``modal deploy`` outside of
# ``minds-admin env deploy`` fails with "Secret not found" -- the safety property
# the timestamped-secret rollback model needs.
DEPLOY_ID_UNSET_SENTINEL: Final[str] = "MINDS_DEPLOY_ID_UNSET"


def read_deploy_env() -> str:
    return os.environ.get(DEPLOY_ENV_VAR, "production")


def read_deploy_id() -> str:
    return os.environ.get(DEPLOY_ID_ENV_VAR, DEPLOY_ID_UNSET_SENTINEL)


def read_min_containers(env_var: str) -> int:
    """Warm-pool size threaded in by ``minds-admin env deploy``; 0 (the default) means no warm pool."""
    return int(os.environ.get(env_var, "0"))


def read_custom_domains(env_var: str) -> list[str] | None:
    """Comma-separated Modal custom-domain hosts threaded in by ``minds-admin env deploy``.

    None (the default) means the app deploys with no custom domains -- the
    shape ``modal.asgi_app(custom_domains=...)`` expects for "none".
    """
    hosts = [host.strip() for host in os.environ.get(env_var, "").split(",") if host.strip()]
    return hosts or None


def read_scaledown_window(env_var: str) -> int | None:
    """Idle-before-scaledown window (seconds) threaded in by ``minds-admin env deploy``.

    Modal requires the value to be > 0, so 0 (the default, and what the
    ci/test tier uses) is normalized to None, meaning "use Modal's own
    default scaledown window".
    """
    return int(os.environ.get(env_var, "0")) or None


def stamped_secret_name(service: str, tier: str, deploy_id: str) -> str:
    """The ``<service>-<tier>-<deploy_id>`` Modal Secret name minted by ``minds-admin env deploy``."""
    return f"{service}-{tier}-{deploy_id}"


def stamped_secret(service: str, tier: str, deploy_id: str) -> modal.Secret:
    return modal.Secret.from_name(stamped_secret_name(service, tier, deploy_id))


def forwarded_env_secret(env_var_names: Sequence[str]) -> modal.Secret:
    """An inline Secret forwarding deploy-subprocess env vars into the container.

    A deploy-time-only env var that influences a module-level *dependency
    object* (the Modal Proxy attach) must evaluate identically at deploy time
    and inside the container: a Proxy is a function dependency, and a
    local/container dependency-list mismatch fails container startup outright.
    Baking the values into an inline Secret makes the container's import-time
    environment match the deploy's. Unset vars ride along as empty strings
    (the readers' "absent" value).
    """
    return modal.Secret.from_dict({name: os.environ.get(name, "") for name in env_var_names})


def read_modal_proxy(env_var: str, environment_env_var: str) -> modal.Proxy | None:
    """The named Modal Proxy the app's functions egress through, or None for direct egress.

    ``minds-admin env deploy`` threads the tier's proxy name (from its
    ``deploy.toml`` ``[management_plane]`` table) into the deploy subprocess env; an empty or
    unset value means the tier has no proxy and the functions egress from
    Modal's dynamic IPs as before. The named Proxy must already exist in the
    deploying Modal workspace or the deploy fails.

    Proxy lookup is Modal-environment-scoped, and a workspace holds at most
    one proxy -- so a tier whose apps deploy into per-env Modal environments
    (dev) keeps its single shared proxy in one environment (``main``) and
    threads that environment's name via ``environment_env_var``; an empty or
    unset value resolves the proxy in the deploy's own environment.
    """
    proxy_name = os.environ.get(env_var, "").strip()
    if not proxy_name:
        return None
    proxy_environment = os.environ.get(environment_env_var, "").strip()
    return modal.Proxy.from_name(proxy_name, environment_name=proxy_environment or None)


def deploy_metadata_entries(tier: str, deploy_id: str, deployer_environ: Mapping[str, str]) -> dict[str, str | None]:
    """The env entries the deploy metadata secret carries: tier, deploy id, and the log-level knob when set.

    Every entry is a str; the ``str | None`` value type is ``modal.Secret.from_dict``'s parameter type.
    """
    entries: dict[str, str | None] = {DEPLOY_ENV_VAR: tier, DEPLOY_ID_ENV_VAR: deploy_id}
    log_level_name = deployer_environ.get(LOG_LEVEL_ENV_VAR, "")
    if log_level_name:
        entries[LOG_LEVEL_ENV_VAR] = log_level_name
    return entries


def deploy_metadata_secret(tier: str, deploy_id: str) -> modal.Secret:
    """An inline Secret carrying the deploy env + id (and the optional log-level knob) into the container.

    The values are baked into the function spec at deploy time; this makes
    them readable at runtime via ``os.environ`` (e.g. by a ``/version``
    endpoint, or by ``log_format.configure_logging``) without a Vault-backed
    Secret. ``MINDS_LOG_LEVEL`` rides along only when the deployer exported
    it, so an ordinary deploy leaves the runtime default in force.
    """
    return modal.Secret.from_dict(deploy_metadata_entries(tier, deploy_id, dict(os.environ)))
