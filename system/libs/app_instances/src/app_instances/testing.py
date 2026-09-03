"""Test doubles and scratch servers for the instances library and for the shell's tests in later phases.

``python -m app_instances.testing stub --port <port>`` serves the stub app for manual checks;
``python -m app_instances.testing sidecar --manifest <path> --app-url <url> --instances-url <url> --store <file> -- <child argv>``
runs the sidecar over a JSON store, which is how the integration test drives it as a real process.
"""

import argparse
import socket
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from app_manifest.manifest import MANIFEST_FILENAME, load_manifest
from app_manifest.primitives import ActionId, AppName, AppUrl, InstancesUrl
from flask import Flask
from imbue.imbue_common.model_update import to_update
from pydantic import Field
from werkzeug.serving import make_server

from app_instances.blueprint import build_instances_blueprint
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
    JsonStoreInstanceSource,
    allocate_key,
    instance_number,
)
from app_instances.nudge import ShellNudger, shell_base_url
from app_instances.primitives import (
    InstanceKey,
    InstanceKeyPrefix,
    InstanceTitle,
    InstanceUrl,
    LocationPath,
    TitleTemplate,
)
from app_instances.sidecar import run_sidecar

STUB_ACTION_ID: Final[ActionId] = ActionId("new")
STUB_KEY_PREFIX: Final[InstanceKeyPrefix] = InstanceKeyPrefix("stub")
STUB_APP_NAME: Final[AppName] = AppName("stub")

LOOPBACK_HOST: Final[str] = "127.0.0.1"

_POLL_INTERVAL_SECONDS: Final[float] = 0.05


class StubInstanceSource(InstanceSourceInterface):
    """An in-memory source that records every call, with knobs for each error the API maps."""

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

    def list_instances(self) -> list[InstanceRecord]:
        self.calls.append("list")
        self._require_ready()
        return list(self.records)

    def create_instance(
        self, action: ActionId, params: Mapping[str, str]
    ) -> InstanceRecord:
        self.calls.append(f"create:{action}:{dict(params)}")
        self._require_ready()
        if action != STUB_ACTION_ID:
            raise UnknownActionError(f"unknown action {action!r}")
        if self.create_refusal is not None:
            raise InstanceConflictError(self.create_refusal)
        key = allocate_key(STUB_KEY_PREFIX, {record.key for record in self.records})
        record = InstanceRecord(
            key=key,
            url=InstanceUrl(params.get("path", "/")),
            title=InstanceTitle(f"Stub {instance_number(STUB_KEY_PREFIX, key)}"),
            status=InstanceStatus.IDLE,
            lifetime=InstanceLifetime.EXPLICIT,
            last_active=datetime.now(timezone.utc),
            renameable=self.is_renameable,
        )
        self.records.append(record)
        return record

    def delete_instance(self, key: InstanceKey) -> None:
        self.calls.append(f"delete:{key}")
        self._require_ready()
        self.records = [record for record in self.records if record.key != key]

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        self.calls.append(f"rename:{key}:{title}")
        self._require_ready()
        if not self.is_renameable:
            raise NotRenameableError("stub instances cannot be renamed")
        record = self._find(key)
        if any(other.key != key and other.title == title for other in self.records):
            raise InstanceConflictError(f"another instance is already titled {title!r}")
        renamed = record.model_copy_update(to_update(record.field_ref().title, title))
        self._replace(renamed)
        return renamed

    def set_location(self, key: InstanceKey, path: LocationPath) -> InstanceRecord:
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


def build_stub_app(
    source: InstanceSourceInterface, nudger: InstanceNudgerInterface
) -> Flask:
    app = Flask(__name__)
    app.register_blueprint(build_instances_blueprint(source, nudger))
    return app


def free_port() -> int:
    """A loopback port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


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
        LOOPBACK_HOST, port, build_stub_app(StubInstanceSource(), nudger), threaded=True
    )
    server.serve_forever()


def _run_stub_command(arguments: argparse.Namespace) -> int:
    run_stub_app(arguments.port)
    return 0


def _run_sidecar_command(arguments: argparse.Namespace, child_argv: list[str]) -> int:
    manifest_path = Path(arguments.manifest)
    manifest = load_manifest(manifest_path)
    source = JsonStoreInstanceSource(
        store_path=Path(arguments.store),
        key_prefix=InstanceKeyPrefix(manifest.name),
        title_template=TitleTemplate(f"{manifest.display_name} {{n}}"),
        lifetime=InstanceLifetime.REFERENCED,
        is_renameable=False,
        is_location_tracked=True,
    )
    return run_sidecar(
        manifest_path=manifest_path,
        app_url=AppUrl(arguments.app_url),
        instances_url=InstancesUrl(arguments.instances_url),
        child_argv=child_argv,
        source=source,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scratch servers for the instances library"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    stub_parser = subparsers.add_parser(
        "stub", help="Serve the stub app (in-memory instances) on a loopback port"
    )
    stub_parser.add_argument(
        "--port", type=int, required=True, help="The loopback port to serve on"
    )
    sidecar_parser = subparsers.add_parser(
        "sidecar", help="Run the sidecar over a JSON store around a child command"
    )
    sidecar_parser.add_argument(
        "--manifest", required=True, help="The app.toml to register"
    )
    sidecar_parser.add_argument(
        "--app-url", required=True, help="The URL the child serves the app at"
    )
    sidecar_parser.add_argument(
        "--instances-url", required=True, help="Where to serve the instances API"
    )
    sidecar_parser.add_argument(
        "--store", required=True, help="The instances.json file of the JSON store"
    )
    sidecar_parser.add_argument(
        "child_argv", nargs=argparse.REMAINDER, help="The child command, after --"
    )
    arguments = parser.parse_args()
    if arguments.command == "stub":
        exit_code = _run_stub_command(arguments)
    elif arguments.command == "sidecar":
        child_argv = (
            arguments.child_argv[1:]
            if arguments.child_argv[:1] == ["--"]
            else arguments.child_argv
        )
        exit_code = _run_sidecar_command(arguments, child_argv)
    else:
        parser.error(f"unknown command {arguments.command!r}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
