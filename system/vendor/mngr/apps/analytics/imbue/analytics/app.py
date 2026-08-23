"""Minds analytics: the Modal deployment entrypoint.

A cron-only app (no web endpoint): the hourly gold-table aggregation and the
daily lake maintenance pass. The job bodies live in ``imbue.analytics.jobs``;
this file holds ONLY what Modal needs at deploy time: the image, the app, the
secrets, and the function definitions.

This file is deployed by file path (``modal deploy app.py``), so Modal ships
just this file (as top-level module ``app``) plus the packages added via
``add_local_python_source`` below (this package and ``imbue.modal_app_kit``).
Anything else from the monorepo must NOT be imported by the shipped modules --
it would work locally and crash the container at import time. This file itself
is excluded from the package source mount, so package modules can never import
``imbue.analytics.app``. See libs/modal_app_kit/README.md for the full
deployment model.
"""

import os
from pathlib import Path

import modal

from imbue.analytics.jobs import run_aggregation_job
from imbue.analytics.jobs import run_collection_poll_job
from imbue.analytics.jobs import run_lake_maintenance_job
from imbue.modal_app_kit.deploy import deploy_metadata_secret
from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.deploy import read_deploy_id
from imbue.modal_app_kit.deploy import stamped_secret
from imbue.modal_app_kit.image import locate_image_requirements
from imbue.modal_app_kit.image import pinned_image
from imbue.modal_app_kit.source_mount import shipped_python_source_ignore

_DEPLOY_ENV = read_deploy_env()

# Per-deploy timestamp baked into the deployed function spec by ``minds-admin env
# deploy`` so the app pins to the matching ``analytics-<tier>-<id>`` Modal
# Secret. See ``read_deploy_id`` for the unset-sentinel safety property.
_MINDS_DEPLOY_ID = read_deploy_id()

image = pinned_image(locate_image_requirements(Path(__file__))).add_local_python_source(
    "imbue.analytics",
    "imbue.modal_app_kit",
    ignore=shipped_python_source_ignore,
)
app = modal.App(name=f"analytics-{_DEPLOY_ENV}", image=image)


def _analytics_secrets() -> list[modal.Secret]:
    return [
        stamped_secret("analytics", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        deploy_metadata_secret(_DEPLOY_ENV, _MINDS_DEPLOY_ID),
    ]


@app.function(
    name="aggregation",
    secrets=_analytics_secrets(),
    # Hourly gold-table rewrite, offset from the top of the hour so it reads
    # settled log parquet (OpenObserve flushes its WAL within a minute) and
    # avoids the other tiers' top-of-hour cron herd.
    schedule=modal.Cron("20 * * * *"),
    cpu=1.0,
    memory=2048,
    timeout=900,
)
def aggregation() -> dict[str, int]:
    return run_aggregation_job()


@app.function(
    name="lake_maintenance",
    secrets=_analytics_secrets(),
    # Daily maintenance: flush inlined data, merge small files, expire
    # snapshots past the retention window, clean up unreferenced files.
    schedule=modal.Cron("50 3 * * *"),
    cpu=1.0,
    memory=2048,
    timeout=1800,
)
def lake_maintenance() -> dict[str, int]:
    return run_lake_maintenance_job()


# The poll cadence is baked into the function spec at deploy time; export
# ANALYTICS_COLLECTION_POLL_CRON before `minds-admin env deploy` to change a tier's
# cadence (the per-workspace interval is the runtime knob in the analytics
# secret). An ops-DB advisory lock makes overlapping runs skip cleanly.
_COLLECTION_POLL_CRON = os.environ.get("ANALYTICS_COLLECTION_POLL_CRON", "*/15 * * * *")


@app.function(
    name="collection_poll",
    secrets=[
        *_analytics_secrets(),
        # The pool management key the connector leases with authorizes the
        # workspace hops; attaching the same secret avoids duplicating the
        # key into the analytics Vault entry.
        stamped_secret("pool-ssh", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
    ],
    schedule=modal.Cron(_COLLECTION_POLL_CRON),
    cpu=1.0,
    memory=2048,
    # Under the 15-minute cadence so a full-budget run cannot pile onto the
    # next tick (which the advisory lock would skip anyway).
    timeout=850,
)
def collection_poll() -> dict[str, int]:
    return run_collection_poll_job()
