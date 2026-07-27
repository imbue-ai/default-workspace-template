"""The on-disk environment record: independent per-source JSON state files.

The record lives at `$MNGR_HOST_DIR/plugin/env-converge/` (i.e. inside the
persistent, backed-up home tree) as one JSON file per source -- `base.json`,
`apt.json`, `npm.json`, `uv.json`, `cargo.json` -- each rewritten atomically.
JSON (not toml) so shell scripts can consume the state with jq.

The rootfs identity stamp lives OUTSIDE the record, on the container rootfs
(`/var/lib/minds/env-converge/rootfs-id`): its presence means "this rootfs has
been converged/captured before", which decides the capture-first (known
rootfs: deliberate removals stick) vs converge-first (fresh rootfs after a
rebuild or restore: the record wins) ordering.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel

from env_converge.data_types import (
    AptState,
    BaseIdentity,
    CargoState,
    NpmGlobalState,
    UvToolState,
)


class EnvConvergeError(Exception):
    """Base exception for env-converge failures."""


class RecordDirUnavailableError(EnvConvergeError, RuntimeError):
    """Raised when the record location cannot be determined (MNGR_HOST_DIR unset)."""

    def __init__(self) -> None:
        super().__init__("MNGR_HOST_DIR is unset; the environment record has no home")


ROOTFS_STAMP_PATH: Final[Path] = Path("/var/lib/minds/env-converge/rootfs-id")

_BASE_FILE: Final[str] = "base.json"
_APT_FILE: Final[str] = "apt.json"
_NPM_FILE: Final[str] = "npm.json"
_UV_FILE: Final[str] = "uv.json"
_CARGO_FILE: Final[str] = "cargo.json"


def default_record_dir() -> Path:
    """The record directory under the host's mngr data dir. Raises when unlocatable."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if not host_dir:
        raise RecordDirUnavailableError()
    return Path(host_dir) / "plugin" / "env-converge"


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp_path.replace(path)


def _read_model(path: Path, model_type: type[FrozenModel]) -> FrozenModel | None:
    if not path.exists():
        return None
    return model_type.model_validate_json(path.read_text())


def write_base_identity(record_dir: Path, identity: BaseIdentity) -> None:
    _write_json_atomic(record_dir / _BASE_FILE, json.loads(identity.model_dump_json()))


def read_base_identity(record_dir: Path) -> BaseIdentity | None:
    model = _read_model(record_dir / _BASE_FILE, BaseIdentity)
    assert model is None or isinstance(model, BaseIdentity)
    return model


def write_apt_state(record_dir: Path, state: AptState) -> None:
    _write_json_atomic(record_dir / _APT_FILE, json.loads(state.model_dump_json()))


def read_apt_state(record_dir: Path) -> AptState | None:
    model = _read_model(record_dir / _APT_FILE, AptState)
    assert model is None or isinstance(model, AptState)
    return model


def write_npm_state(record_dir: Path, state: NpmGlobalState) -> None:
    _write_json_atomic(record_dir / _NPM_FILE, json.loads(state.model_dump_json()))


def read_npm_state(record_dir: Path) -> NpmGlobalState | None:
    model = _read_model(record_dir / _NPM_FILE, NpmGlobalState)
    assert model is None or isinstance(model, NpmGlobalState)
    return model


def write_uv_tool_state(record_dir: Path, state: UvToolState) -> None:
    _write_json_atomic(record_dir / _UV_FILE, json.loads(state.model_dump_json()))


def read_uv_tool_state(record_dir: Path) -> UvToolState | None:
    model = _read_model(record_dir / _UV_FILE, UvToolState)
    assert model is None or isinstance(model, UvToolState)
    return model


def write_cargo_state(record_dir: Path, state: CargoState) -> None:
    _write_json_atomic(record_dir / _CARGO_FILE, json.loads(state.model_dump_json()))


def read_cargo_state(record_dir: Path) -> CargoState | None:
    model = _read_model(record_dir / _CARGO_FILE, CargoState)
    assert model is None or isinstance(model, CargoState)
    return model


def is_rootfs_stamped(stamp_path: Path = ROOTFS_STAMP_PATH) -> bool:
    return stamp_path.exists()


def stamp_rootfs(stamp_path: Path = ROOTFS_STAMP_PATH) -> None:
    """Mark this rootfs as converged/captured (idempotent; keeps the first id)."""
    if stamp_path.exists():
        return
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(uuid.uuid4().hex + "\n")
