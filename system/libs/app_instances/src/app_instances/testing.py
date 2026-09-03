"""Test doubles and scratch servers for the instances library and for the shell's tests in later phases.

``python -m app_instances.testing stub --port <port>`` serves the stub app for manual checks;
``python -m app_instances.testing sidecar --manifest <path> --app-url <url> --instances-url <url> --store <file> -- <child argv>``
runs the sidecar over a JSON store, which is how the integration test drives it as a real process.
"""

import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import click
import pytest
from app_manifest.manifest import MANIFEST_FILENAME, load_manifest
from app_manifest.primitives import ActionId, AppName, AppUrl, InstancesUrl
from app_manifest.registry import ENV_APPS_FILE
from flask import Flask, request
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field, PrivateAttr
from werkzeug.serving import make_server

from app_instances.blueprint import build_instances_app
from app_instances.data_types import InstanceLifetime, InstanceRecord, InstanceStatus
from app_instances.errors import (
    InstanceConflictError,
    LocationNotTrackedError,
    NotReadyError,
    NotRenameableError,
    UnknownActionError,
    UnknownInstanceError,
)
from app_instances.interfaces import InstanceNudgerInterface, InstanceSourceInterface
from app_instances.json_store import (
    DEFAULT_PATH,
    PATH_PARAM,
    JsonStoreInstanceSource,
    allocate_instance_number,
    allocated_key,
)
from app_instances.nudge import ENV_SHELL_URL, ShellNudger, shell_base_url
from app_instances.primitives import (
    InstanceKey,
    InstanceKeyPrefix,
    InstanceTitle,
    InstanceUrl,
    LocationPath,
    TitleTemplate,
)
from app_instances.sidecar import run_sidecar, serve_in_background

STUB_ACTION_ID: Final[ActionId] = ActionId("new")
STUB_KEY_PREFIX: Final[InstanceKeyPrefix] = InstanceKeyPrefix("stub")
STUB_APP_NAME: Final[AppName] = AppName("stub")

LOOPBACK_HOST: Final[str] = "127.0.0.1"

_POLL_INTERVAL_SECONDS: Final[float] = 0.05


class StubInstanceSource(InstanceSourceInterface):
    """An in-memory source that records every call, with knobs for each error the API maps.

    Every method holds one lock, so the stub is safe under the threaded server ``run_stub_app``
    serves it through, as the interface requires.
    """

    records: list[InstanceRecord] = Field(
        default_factory=list, description="The current instances"
    )
    calls: list[str] = Field(
        default_factory=list, description="Every call made, as 'method:argument'"
    )
    is_ready: bool = Field(
        default=True, description="False makes every call raise NotReadyError"
    )
    is_renameable: bool = Field(default=True, description="Whether rename is accepted")
    is_location_tracked: bool = Field(
        default=True, description="Whether location reports are accepted"
    )
    create_refusal: str | None = Field(
        default=None,
        description="When set, create raises InstanceConflictError with this detail",
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def list_instances(self) -> list[InstanceRecord]:
        with self._lock:
            self.calls.append("list")
            self._require_ready()
            return list(self.records)

    def create_instance(
        self, action: ActionId, params: Mapping[str, str]
    ) -> InstanceRecord:
        with self._lock:
            self.calls.append(f"create:{action}:{dict(params)}")
            self._require_ready()
            if action != STUB_ACTION_ID:
                raise UnknownActionError(f"unknown action {action!r}")
            if self.create_refusal is not None:
                raise InstanceConflictError(self.create_refusal)
            number = allocate_instance_number(
                STUB_KEY_PREFIX, {record.key for record in self.records}
            )
            record = InstanceRecord(
                key=allocated_key(STUB_KEY_PREFIX, number),
                url=InstanceUrl(params.get(PATH_PARAM, DEFAULT_PATH)),
                title=InstanceTitle(f"Stub {number}"),
                status=InstanceStatus.IDLE,
                lifetime=InstanceLifetime.EXPLICIT,
                last_active=datetime.now(timezone.utc),
                renameable=self.is_renameable,
            )
            self.records.append(record)
            return record

    def delete_instance(self, key: InstanceKey) -> None:
        with self._lock:
            self.calls.append(f"delete:{key}")
            self._require_ready()
            self.records = [record for record in self.records if record.key != key]

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        with self._lock:
            self.calls.append(f"rename:{key}:{title}")
            self._require_ready()
            if not self.is_renameable:
                raise NotRenameableError("stub instances cannot be renamed")
            record = self._find(key)
            if any(other.key != key and other.title == title for other in self.records):
                raise InstanceConflictError(
                    f"another instance is already titled {title!r}"
                )
            renamed = record.model_copy_update(
                to_update(record.field_ref().title, title)
            )
            self._replace(renamed)
            return renamed

    def set_location(self, key: InstanceKey, path: LocationPath) -> InstanceRecord:
        with self._lock:
            self.calls.append(f"location:{key}:{path}")
            self._require_ready()
            if not self.is_location_tracked:
                raise LocationNotTrackedError("the stub does not track locations")
            record = self._find(key)
            relocated = record.model_copy_update(
                to_update(record.field_ref().url, InstanceUrl(path))
            )
            self._replace(relocated)
            return relocated

    def _require_ready(self) -> None:
        if not self.is_ready:
            raise NotReadyError("the stub is still initialising")

    def _find(self, key: InstanceKey) -> InstanceRecord:
        for record in self.records:
            if record.key == key:
                return record
        raise UnknownInstanceError(f"no instance has the key {key!r}")

    def _replace(self, replacement: InstanceRecord) -> None:
        self.records = [
            replacement if record.key == replacement.key else record
            for record in self.records
        ]


class RecordingNudger(InstanceNudgerInterface):
    """Counts nudges instead of posting them."""

    nudge_count: int = Field(default=0, description="How many times nudge was called")

    def nudge(self) -> None:
        self.nudge_count += 1


class SidecarEnvironment(FrozenModel):
    """Where a registration or sidecar under test keeps its files, and the registry it registers in."""

    scratch_dir: Path = Field(
        description="The test's own directory for manifests, stores, and logs"
    )
    registry_path: Path = Field(description="The apps.toml registrations land in")


def prepare_sidecar_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> SidecarEnvironment:
    """What a fixture builds for a registration or sidecar under test: cwd at ``repo_root`` (the registration script is cwd-relative), a scratch registry, and a shell URL nothing listens on."""
    monkeypatch.chdir(repo_root)
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry_path))
    monkeypatch.setenv(ENV_SHELL_URL, f"http://{LOOPBACK_HOST}:{free_port()}")
    return SidecarEnvironment(scratch_dir=tmp_path, registry_path=registry_path)


def free_port() -> int:
    """A loopback port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


class RecordedShellRequest(FrozenModel):
    """One request a fake shell received."""

    method: str = Field(description="The HTTP method")
    path: str = Field(description="The request path")
    body: Any = Field(
        description="The parsed JSON body, or None when the body was empty or not JSON"
    )


class RecordedShellRequests(MutableModel):
    """What a fake shell received, and where it listens."""

    base_url: str = Field(frozen=True, description="Where the fake shell listens")
    requests: list[RecordedShellRequest] = Field(
        default_factory=list, description="Every request received, in order"
    )

    def paths(self) -> list[tuple[str, str]]:
        """Every (method, path) received, in order."""
        return [(received.method, received.path) for received in self.requests]


@contextmanager
def serve_recording_shell() -> Iterator[RecordedShellRequests]:
    """A loopback server that records every request and answers 404, as the shell does before phase 7."""
    port = free_port()
    recorded = RecordedShellRequests(base_url=f"http://{LOOPBACK_HOST}:{port}")
    app = Flask(__name__)

    @app.route("/<path:_anything>", methods=["GET", "POST"])
    def record(_anything: str) -> tuple[str, int]:
        recorded.requests.append(
            RecordedShellRequest(
                method=request.method,
                path=request.path,
                body=request.get_json(force=True, silent=True),
            )
        )
        return "", 404

    with serve_in_background(LOOPBACK_HOST, port, app):
        yield recorded


def is_port_accepting(port: int) -> bool:
    try:
        with socket.create_connection((LOOPBACK_HOST, port), timeout=0.2):
            return True
    except OSError:
        return False


def wait_until(condition: Callable[[], bool], timeout_seconds: float) -> bool:
    """Poll ``condition`` until it holds or the deadline passes, reporting which."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return condition()


_MINIMAL_ICON: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 3h18v18H3z"/></svg>'
)


def write_sidecar_manifest(
    directory: Path, app_name: AppName, instances_url: InstancesUrl
) -> Path:
    """Write a valid multi-instance app.toml (and the icon it names) into ``directory``, returning the manifest path."""
    (directory / "icon.svg").write_text(_MINIMAL_ICON)
    manifest_path = directory / MANIFEST_FILENAME
    manifest_path.write_text(
        f'name = "{app_name}"\n'
        f'display_name = "Sidecar {app_name}"\n'
        'icon = "icon.svg"\n'
        "instances = true\n"
        f'instances_url = "{instances_url}"\n'
        "\n"
        "[[actions]]\n"
        'id = "new"\n'
        'label = "New sidecar page"\n'
    )
    return manifest_path


def run_stub_app(port: int) -> None:
    """Serve the stub app on the loopback port, nudging the real shell, until the process is interrupted."""
    nudger = ShellNudger(app_name=STUB_APP_NAME, shell_url=shell_base_url())
    server = make_server(
        LOOPBACK_HOST,
        port,
        build_instances_app(StubInstanceSource(), nudger),
        threaded=True,
    )
    server.serve_forever()


def run_sidecar_over_json_store(
    manifest_path: Path,
    app_url: AppUrl,
    instances_url: InstancesUrl,
    store_path: Path,
    child_argv: Sequence[str],
) -> int:
    """Run the sidecar with a referenced, location-tracked JSON store keyed by the manifest's name."""
    manifest = load_manifest(manifest_path)
    source = JsonStoreInstanceSource(
        store_path=store_path,
        key_prefix=InstanceKeyPrefix(manifest.name),
        title_template=TitleTemplate(f"{manifest.display_name} {{n}}"),
        lifetime=InstanceLifetime.REFERENCED,
        is_renameable=False,
        is_location_tracked=True,
    )
    return run_sidecar(
        manifest_path=manifest_path,
        app_url=app_url,
        instances_url=instances_url,
        child_argv=child_argv,
        source=source,
    )


@click.group()
def testing_cli() -> None:
    """Scratch servers for the instances library."""


@testing_cli.command("stub")
@click.option("--port", type=int, required=True, help="The loopback port to serve on")
def _serve_stub_command(port: int) -> None:
    """Serve the stub app (in-memory instances) on a loopback port."""
    run_stub_app(port)


@testing_cli.command("sidecar")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    required=True,
    help="The app.toml to register",
)
@click.option("--app-url", required=True, help="The URL the child serves the app at")
@click.option("--instances-url", required=True, help="Where to serve the instances API")
@click.option(
    "--store",
    "store_path",
    type=click.Path(path_type=Path),
    required=True,
    help="The instances.json file of the JSON store",
)
@click.argument("child_argv", nargs=-1, required=True)
def _run_sidecar_command(
    manifest_path: Path,
    app_url: str,
    instances_url: str,
    store_path: Path,
    child_argv: tuple[str, ...],
) -> None:
    """Run the sidecar over a JSON store around CHILD_ARGV (the child command, given after --)."""
    sys.exit(
        run_sidecar_over_json_store(
            manifest_path=manifest_path,
            app_url=AppUrl(app_url),
            instances_url=InstancesUrl(instances_url),
            store_path=store_path,
            child_argv=child_argv,
        )
    )


def main() -> None:
    testing_cli()


if __name__ == "__main__":
    main()
