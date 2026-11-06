"""``app-store-launch <slug>``: what an installed app's supervisord program runs.

It reads the app's install record and catalog entry, registers the app's generated manifest and port with the
workspace (``forward_port.py --manifest``, which must come before the app listens), reads back the origin label the
registration minted, and then replaces itself with the app's own launch command, so supervisord supervises the app
directly. The command is an argv from the reviewed catalog with its placeholders filled in; it runs in the app's data
folder.
"""

import os
import sys
from pathlib import Path

import click
from loguru import logger
from pydantic import Field

from app_manifest.errors import AppRegistrationError
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import read_origin_label
from app_manifest.registry import register_app
from app_manifest.registry_location import registry_path
from app_store.added_apps import AddedAppStore
from app_store.added_apps import offered_catalog
from app_store.catalog import load_app_catalog
from app_store.catalog import render_argv
from app_store.config import load_config
from app_store.errors import AppStoreError
from app_store.layout import StoreLayout
from app_store.records import RecordStore
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The exit status for an app the store cannot launch: supervisord's retries end in FATAL rather than looping.
LAUNCH_FAILED_EXIT_CODE = 2


@pure
def launch_environment(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    return {**base, **extra}


class LaunchPlan(FrozenModel):
    """What launching an installed app takes: what to register, and what to run once registered."""

    manifest_path: Path = Field(description="The generated app.toml to register")
    app_url: AppUrl = Field(description="Where the app is registered as answering")
    argv: tuple[str, ...] = Field(description="The catalog's launch argv, unrendered")
    env: dict[str, str] = Field(description="The catalog's extra environment, unrendered")
    port: int = Field(description="The app's port")
    data_dir: Path = Field(description="The app's data folder, where it runs")
    env_dir: Path = Field(description="The app's environment")

    def render(self, origin_label: str) -> tuple[list[str], dict[str, str]]:
        """The argv and the extra environment with every placeholder filled in."""
        argv = render_argv(self.argv, self.port, self.data_dir, self.env_dir, origin_label)
        values = render_argv(tuple(self.env.values()), self.port, self.data_dir, self.env_dir, origin_label)
        return argv, dict(zip(self.env.keys(), values, strict=True))


def plan_launch(slug: str, layout: StoreLayout) -> LaunchPlan:
    """How to launch ``slug``; raises AppStoreError when the app is not installed."""
    app = offered_catalog(load_app_catalog(), AddedAppStore(path=layout.added_apps_path).read()).find(slug)
    record = RecordStore(path=layout.records_path).read().find(slug)
    if app is None or record is None or record.port is None:
        raise AppStoreError(f"{slug} is not an app the App Store has installed")
    return LaunchPlan(
        manifest_path=layout.manifest_path(slug),
        app_url=AppUrl(f"http://localhost:{record.port}"),
        argv=app.launch.argv,
        env=app.launch.env,
        port=record.port,
        data_dir=layout.app_data_dir(slug),
        env_dir=layout.env_dir(slug),
    )


def registered_origin_label(slug: str, registry: Path) -> str:
    """The origin label the registration minted for ``slug``; raises AppStoreError when there is none."""
    label = read_origin_label(registry, AppName(slug))
    if label == "":
        raise AppStoreError(f"the app registry {registry} holds no origin label for {slug}")
    return label


@click.command()
@click.argument("slug")
def main(slug: str) -> None:
    """Register an installed App Store app, then run it."""
    config = load_config()
    layout = StoreLayout(data_dir=config.app_store_data_dir.absolute(), repo_root=Path.cwd())
    try:
        plan = plan_launch(slug, layout)
        register_app(plan.manifest_path, plan.app_url)
        argv, extra_env = plan.render(registered_origin_label(slug, registry_path()))
    except (AppStoreError, AppRegistrationError) as e:
        logger.error("Cannot launch {}: {}", slug, e)
        sys.exit(LAUNCH_FAILED_EXIT_CODE)
    plan.data_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(plan.data_dir)
    logger.info("Launching {}: {}", slug, " ".join(argv))
    os.execve(argv[0], argv, launch_environment(dict(os.environ), extra_env))
