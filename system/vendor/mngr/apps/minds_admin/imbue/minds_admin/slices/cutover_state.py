"""The operator-side state dir of the gen-1 -> gen-2 cutover (``~/.minds-<env>/cutover/``).

Every stage is re-runnable per workspace from what is written here: the
per-workspace record and ``docker inspect``, the harvested keys (shredded once
the workspace is restored), the per-box record, and the stage reports.
One-time tooling, deleted in phase 6 of blueprint/slice-fleet-cutover.
"""

import fcntl
import json
import os
import shutil
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import IO

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds_admin.slices.cutover_types import CutoverBoxState
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.cutover_types import CutoverWorkspaceState
from imbue.minds_admin.slices.cutover_types import HarvestedKeys
from imbue.minds_admin.slices.cutover_types import StageReport

CUTOVER_STATE_DIRNAME: Final[str] = "cutover"
_WORKSPACES_DIRNAME: Final[str] = "workspaces"
_KEYS_DIRNAME: Final[str] = "keys"
_BOXES_DIRNAME: Final[str] = "boxes"
_REPORTS_DIRNAME: Final[str] = "reports"
_LOCKS_DIRNAME: Final[str] = "locks"
_INSPECT_SUFFIX: Final[str] = ".inspect.json"

# The key files under ``keys/<host_db_id>/``.
_VM_HOST_KEY_FILE: Final[str] = "vm_ssh_host_ed25519_key"
_VM_HOST_KEY_PUB_FILE: Final[str] = "vm_ssh_host_ed25519_key.pub"
_VM_AUTHORIZED_KEYS_FILE: Final[str] = "vm_authorized_keys"
_CONTAINER_HOST_KEY_FILE: Final[str] = "container_ssh_host_ed25519_key"
_CONTAINER_HOST_KEY_PUB_FILE: Final[str] = "container_ssh_host_ed25519_key.pub"
_CONTAINER_AUTHORIZED_KEYS_FILE: Final[str] = "container_authorized_keys"


def _write_private_file(path: Path, content: str) -> None:
    """Write ``content`` 0600 (created 0600, never briefly world-readable) and atomically.

    The restore's per-box threads list every workspace record while their
    siblings write theirs, so a record is replaced by rename rather than
    truncated in place: a reader sees the old or the new file, never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.chmod(0o600)
    os.replace(temp_path, path)


def _shred_file(path: Path) -> None:
    """Overwrite a key file with zeros before unlinking it (a best-effort shred without the coreutil)."""
    if not path.exists():
        return
    size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.write(b"\0" * size)
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()


class CutoverStateStore(MutableModel):
    """Reads and writes the cutover state dir for one env."""

    root: Path = Field(frozen=True, description="The state dir (``~/.minds-<env>/cutover``)")

    def ensure_layout(self) -> None:
        for subdir in (_WORKSPACES_DIRNAME, _KEYS_DIRNAME, _BOXES_DIRNAME, _REPORTS_DIRNAME, _LOCKS_DIRNAME):
            (self.root / subdir).mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    @contextmanager
    def acquire_locks(self, names: Sequence[str]) -> Iterator[None]:
        """Hold one exclusive flock per name for the block; refuse (never wait) when any is already held.

        Concurrent migrate invocations parallelize across disjoint target
        boxes: the target-box lock is what serializes a box (one restore at a
        time, one management dial), and the per-workspace locks keep two
        invocations from touching the same workspace. Locks are advisory and
        process-scoped (flock), so a crashed invocation never leaves a stale
        lock behind.
        """
        handles: list[IO[str]] = []
        try:
            for name in names:
                path = self.root / _LOCKS_DIRNAME / f"{name}.lock"
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("w")
                handles.append(handle)
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise CutoverError(
                        f"another cutover invocation holds the {name} lock ({path}); "
                        "run against a disjoint target box / workspace set, or wait for it"
                    ) from exc
            yield
        finally:
            # Closing a handle releases its flock; a partially acquired set
            # unwinds the same way.
            for handle in handles:
                handle.close()

    def _workspace_path(self, host_db_id: str) -> Path:
        return self.root / _WORKSPACES_DIRNAME / f"{host_db_id}.json"

    def _inspect_path(self, host_db_id: str) -> Path:
        return self.root / _WORKSPACES_DIRNAME / f"{host_db_id}{_INSPECT_SUFFIX}"

    def _keys_dir(self, host_db_id: str) -> Path:
        return self.root / _KEYS_DIRNAME / host_db_id

    def _box_path(self, server_id: str) -> Path:
        return self.root / _BOXES_DIRNAME / f"{server_id}.json"

    def read_workspace(self, host_db_id: str) -> CutoverWorkspaceState | None:
        path = self._workspace_path(host_db_id)
        if not path.exists():
            return None
        return CutoverWorkspaceState.model_validate_json(path.read_text())

    def write_workspace(self, state: CutoverWorkspaceState) -> None:
        _write_private_file(self._workspace_path(state.host_db_id), state.model_dump_json(indent=2))

    def list_workspaces(self) -> list[CutoverWorkspaceState]:
        workspaces_dir = self.root / _WORKSPACES_DIRNAME
        if not workspaces_dir.is_dir():
            return []
        states: list[CutoverWorkspaceState] = []
        for path in sorted(workspaces_dir.glob("*.json")):
            if path.name.endswith(_INSPECT_SUFFIX):
                continue
            states.append(CutoverWorkspaceState.model_validate_json(path.read_text()))
        return states

    def read_inspect(self, host_db_id: str) -> dict[str, Any] | None:
        path = self._inspect_path(host_db_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def write_inspect(self, host_db_id: str, inspect_entry: dict[str, Any]) -> None:
        _write_private_file(self._inspect_path(host_db_id), json.dumps(inspect_entry, indent=2))

    def write_keys(self, host_db_id: str, keys: HarvestedKeys) -> None:
        keys_dir = self._keys_dir(host_db_id)
        keys_dir.mkdir(parents=True, exist_ok=True)
        keys_dir.chmod(0o700)
        _write_private_file(keys_dir / _VM_HOST_KEY_FILE, keys.vm_host_private_key.get_secret_value())
        _write_private_file(keys_dir / _VM_HOST_KEY_PUB_FILE, keys.vm_host_public_key)
        _write_private_file(keys_dir / _VM_AUTHORIZED_KEYS_FILE, keys.vm_authorized_keys)
        _write_private_file(keys_dir / _CONTAINER_HOST_KEY_FILE, keys.container_host_private_key.get_secret_value())
        _write_private_file(keys_dir / _CONTAINER_HOST_KEY_PUB_FILE, keys.container_host_public_key)
        _write_private_file(keys_dir / _CONTAINER_AUTHORIZED_KEYS_FILE, keys.container_authorized_keys)

    def read_keys(self, host_db_id: str) -> HarvestedKeys | None:
        keys_dir = self._keys_dir(host_db_id)
        if not (keys_dir / _VM_HOST_KEY_FILE).exists():
            return None
        return HarvestedKeys(
            vm_host_private_key=SecretStr((keys_dir / _VM_HOST_KEY_FILE).read_text()),
            vm_host_public_key=(keys_dir / _VM_HOST_KEY_PUB_FILE).read_text(),
            vm_authorized_keys=(keys_dir / _VM_AUTHORIZED_KEYS_FILE).read_text(),
            container_host_private_key=SecretStr((keys_dir / _CONTAINER_HOST_KEY_FILE).read_text()),
            container_host_public_key=(keys_dir / _CONTAINER_HOST_KEY_PUB_FILE).read_text(),
            container_authorized_keys=(keys_dir / _CONTAINER_AUTHORIZED_KEYS_FILE).read_text(),
        )

    def shred_keys(self, host_db_id: str) -> None:
        keys_dir = self._keys_dir(host_db_id)
        if not keys_dir.exists():
            return
        for path in keys_dir.iterdir():
            _shred_file(path)
        shutil.rmtree(keys_dir, ignore_errors=True)

    def read_box(self, server_id: str) -> CutoverBoxState | None:
        path = self._box_path(server_id)
        if not path.exists():
            return None
        return CutoverBoxState.model_validate_json(path.read_text())

    def write_box(self, state: CutoverBoxState) -> None:
        _write_private_file(self._box_path(state.server_id), state.model_dump_json(indent=2))

    def list_boxes(self) -> list[CutoverBoxState]:
        boxes_dir = self.root / _BOXES_DIRNAME
        if not boxes_dir.is_dir():
            return []
        return [CutoverBoxState.model_validate_json(path.read_text()) for path in sorted(boxes_dir.glob("*.json"))]

    def write_report(self, stage_name: str, report_json: str, report_text: str) -> Path:
        """Write ``reports/<stage>-<timestamp>.json`` + ``.txt``; returns the JSON path."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        reports_dir = self.root / _REPORTS_DIRNAME
        reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = reports_dir / f"{stage_name}-{stamp}.json"
        json_path.write_text(report_json)
        (reports_dir / f"{stage_name}-{stamp}.txt").write_text(report_text)
        return json_path

    def write_stage_report(self, report: StageReport, report_text: str) -> Path:
        return self.write_report(report.stage_name, report.model_dump_json(indent=2), report_text)
