"""OAuth redirector: the Modal deployment entrypoint.

Deployed by file path once per dev/CI tier (``just deploy-oauth-redirector
<tier>``), NOT by ``minds-admin env deploy`` -- the redirector is tier-level and
holds no secrets. See README.md and libs/modal_app_kit/README.md for the
deployment model.
"""

import logging
import os
from pathlib import Path

import modal
from fastapi import FastAPI

from imbue.modal_app_kit.deploy import DEPLOY_ENV_VAR
from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.image import locate_image_requirements
from imbue.modal_app_kit.image import pinned_image
from imbue.modal_app_kit.log_format import configure_logging
from imbue.modal_app_kit.sentry import init_sentry
from imbue.modal_app_kit.source_mount import shipped_python_source_ignore
from imbue.oauth_redirector.web import ALLOWED_HOST_REGEX_ENV
from imbue.oauth_redirector.web import web_app

_DEPLOY_ENV = read_deploy_env()

# The tier's connector hostname pattern, read at module load (= modal-deploy
# serialization time) and baked into the function spec via an inline secret.
# Required: a redirector with no allowlist would refuse every forward.
_ALLOWED_HOST_REGEX = os.environ.get(ALLOWED_HOST_REGEX_ENV, "")
if not _ALLOWED_HOST_REGEX:
    # Loud (but non-fatal: tests import this module without deployment env)
    # so a direct ``modal deploy`` that bypasses the just recipe shows the
    # misconfiguration in its output instead of shipping a redirector that
    # 503s every forward.
    # Named under ``imbue`` because Modal mounts this entrypoint as module ``app``.
    logging.getLogger("imbue.oauth_redirector.app").warning(
        "%s is not set: a deployed redirector will refuse every forward. "
        "Deploy via 'just deploy-oauth-redirector <tier>', which bakes the tier's allowlist.",
        ALLOWED_HOST_REGEX_ENV,
    )

image = pinned_image(locate_image_requirements(Path(__file__))).add_local_python_source(
    "imbue.oauth_redirector",
    "imbue.modal_app_kit",
    ignore=shipped_python_source_ignore,
)
# DSN of the (dev/ci) Bugsink `oauth-redirector` project, read at module
# load like the allowlist above and baked into the same inline secret. The
# redirector keeps its zero-Vault deployment story; empty (a deploy that
# predates provisioning, or a bare `modal deploy`) simply disables reporting.
_SENTRY_DSN = os.environ.get("OAUTH_REDIRECTOR_SENTRY_DSN", "")

app = modal.App(name=f"oauth-redirector-{_DEPLOY_ENV}", image=image)


@app.function(
    name="redirect",
    secrets=[
        modal.Secret.from_dict(
            {
                ALLOWED_HOST_REGEX_ENV: _ALLOWED_HOST_REGEX,
                "OAUTH_REDIRECTOR_SENTRY_DSN": _SENTRY_DSN,
                # Tier name for the running container, so init_sentry tags
                # events with the real environment instead of "unknown" (the
                # redirector has no deploy metadata secret).
                DEPLOY_ENV_VAR: _DEPLOY_ENV,
            }
        )
    ],
    # One always-warm container: the redirector sits mid-flight in every
    # Google sign-in (Google's consent redirect lands here while the browser
    # still shows Google's page), and a scale-to-zero app that is hit once
    # per sign-in is essentially always cold -- measured 4-34s boots that
    # read as "Google is slow". A single tiny warm container is near-free.
    min_containers=1,
)
@modal.concurrent(max_inputs=32)
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    # JSON log lines, then error reporting to the dev/ci Bugsink instance
    # (no-op without a DSN).
    configure_logging()
    init_sentry("oauth-redirector", "OAUTH_REDIRECTOR_SENTRY_DSN")
    return web_app
