"""Pure renderers for a Bugsink error-tracker instance host's on-disk config.

Same split-plane shape as the OpenObserve renderers in ``config_render``: one
Django monolith (gunicorn) behind a caddy ingest gate that terminates TLS with
the tier's Cloudflare origin certificate and exposes ONLY the Sentry-protocol
ingest routes, so the login page, UI, and REST API never exist publicly
(operators and provisioning reach them over an SSH tunnel to loopback).
"""

from typing import Final

from imbue.imbue_common.pure import pure
from imbue.observability.config_render import ORIGIN_CERTIFICATE_PATH
from imbue.observability.config_render import ORIGIN_PRIVATE_KEY_PATH
from imbue.observability.config_render import env_file_quoted
from imbue.observability.config_render import render_origin_firewall_conf
from imbue.observability.data_types import BugsinkInstanceConfig
from imbue.observability.primitives import BUGSINK_HTTP_PORT

# Where the rendered EnvironmentFile lands on the instance host (the caddy /
# TLS / nftables artifacts reuse the paths in ``config_render``).
BUGSINK_ENV_FILE_PATH: Final[str] = "/etc/bugsink/bugsink.env"


@pure
def render_bugsink_env(config: BugsinkInstanceConfig) -> str:
    """Render the systemd EnvironmentFile configuring the Bugsink (Django) process.

    All values are read by the vendored ``bugsink_conf.py`` settings module
    (see ``deploy_assets/``). BASE_URL carries the public ingest hostname --
    Django builds the project DSNs from it -- while ALLOWED_HOSTS additionally
    admits loopback so the SSH-tunneled UI works. BEHIND_HTTPS_PROXY matches
    caddy terminating TLS in front of gunicorn; PHONEHOME stays off on every
    instance; MAX_EVENT_AGE_DAYS is the digest-time retention control.
    """
    return f"""\
# Rendered by imbue.observability -- do not edit on the host; re-render and redeploy.
SECRET_KEY={env_file_quoted(config.secret_key.get_secret_value())}
DATABASE_URL={env_file_quoted(config.database_url.get_secret_value())}
CREATE_SUPERUSER={env_file_quoted(config.create_superuser.get_secret_value())}
BASE_URL={env_file_quoted(config.base_url)}
ALLOWED_HOSTS={env_file_quoted(f"{config.errors_hostname},localhost,127.0.0.1")}
BEHIND_HTTPS_PROXY="True"
PHONEHOME="false"
MAX_EVENT_AGE_DAYS="{config.max_event_age_days}"
"""


@pure
def render_bugsink_caddyfile(config: BugsinkInstanceConfig) -> str:
    """Render the caddy ingest gate: Sentry-protocol DSN routes only, everything else 404.

    The exposed paths are exactly the endpoints a DSN points the sentry SDKs
    at (``/api/<project_id>/envelope/`` and the legacy ``/store/`` variant);
    the project-id segment is a wildcard because project ids are minted at
    provisioning time. The login page, UI, and canonical REST API stay
    loopback-only behind the 404 -- provisioning drives the API through an
    SSH tunnel. X-Real-IP is stamped from Cloudflare's CF-Connecting-IP so
    Django records the true client address (caddy itself only adds
    X-Forwarded-For, and the vendored conf reads X-Real-IP behind a proxy).
    """
    upstream = f"127.0.0.1:{BUGSINK_HTTP_PORT}"
    return f"""\
# Rendered by imbue.observability -- do not edit on the host; re-render and redeploy.
# Machine-ingest gate for {config.errors_hostname} (tier {config.tier}): only the
# Sentry-protocol DSN ingest routes exist publicly. The UI, login page, and
# REST API are reachable ONLY via an SSH tunnel to {upstream}.
{{
    admin off
    auto_https off
}}

https://{config.errors_hostname}:443 {{
    tls {ORIGIN_CERTIFICATE_PATH} {ORIGIN_PRIVATE_KEY_PATH}

    @ingest path /api/*/envelope/ /api/*/store/
    handle @ingest {{
        reverse_proxy {upstream} {{
            header_up X-Real-IP {{header.CF-Connecting-IP}}
        }}
    }}

    handle {{
        respond 404
    }}
}}
"""


@pure
def render_all_bugsink_artifacts(config: BugsinkInstanceConfig) -> dict[str, str]:
    """All rendered Bugsink instance config artifacts, keyed by their on-disk basename.

    The single source of truth for which rendered config files the instance
    host carries; the SSH deploy stages exactly these plus the committed
    deploy assets (the vendored ``bugsink_conf.py`` settings module and the
    hash-locked ``bugsink_requirements.txt``) -- the basenames must appear in
    ``bugsink_remote_install.BUGSINK_REMOTE_ARTIFACT_PATHS``.
    """
    return {
        "bugsink.env": render_bugsink_env(config),
        "Caddyfile": render_bugsink_caddyfile(config),
        "nftables.conf": render_origin_firewall_conf(str(config.errors_hostname), str(config.tier)),
        "origin.pem": config.origin_tls_certificate_pem,
        "origin.key": config.origin_tls_private_key_pem.get_secret_value(),
    }
