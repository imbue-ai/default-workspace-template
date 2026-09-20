"""The operator-side state dir of the workspace archive (``~/.minds-<env>/archives/``).

The bucket is the authority on which archives exist (each carries its
manifest beside its zip); this dir keeps a copy of every manifest this
operator machine wrote, the per-workspace locks, and the reports the link
command renders.
"""

from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds_admin.slices.archive_types import ARCHIVE_STAMP_FORMAT
from imbue.minds_admin.slices.archive_types import WorkspaceArchiveManifest
from imbue.minds_admin.slices.archive_types import render_manifest_json
from imbue.minds_admin.slices.cutover_state import acquire_exclusive_flocks

ARCHIVE_STATE_DIRNAME: Final[str] = "archives"
_MANIFESTS_DIRNAME: Final[str] = "manifests"
_REPORTS_DIRNAME: Final[str] = "reports"
_LOCKS_DIRNAME: Final[str] = "locks"


class ArchiveStateStore(MutableModel):
    """Reads and writes the archive state dir for one env."""

    root: Path = Field(frozen=True, description="The state dir (``~/.minds-<env>/archives``)")

    def ensure_layout(self) -> None:
        for subdir in (_MANIFESTS_DIRNAME, _REPORTS_DIRNAME, _LOCKS_DIRNAME):
            (self.root / subdir).mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    @contextmanager
    def acquire_workspace_lock(self, host_db_id: str) -> Iterator[None]:
        """Hold the workspace's lock for the block; a second archive of the same workspace is refused, not queued."""
        with acquire_exclusive_flocks(
            self.root / _LOCKS_DIRNAME,
            (f"workspace-{host_db_id}",),
            refusal_hint="wait for the other archive of this workspace to finish",
        ):
            yield

    def _manifest_path(self, manifest: WorkspaceArchiveManifest) -> Path:
        stamp = manifest.created_at.strftime(ARCHIVE_STAMP_FORMAT)
        return self.root / _MANIFESTS_DIRNAME / manifest.host_db_id / f"{stamp}.json"

    def write_manifest(self, manifest: WorkspaceArchiveManifest) -> Path:
        path = self._manifest_path(manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_manifest_json(manifest))
        return path

    def write_report(self, report_name: str, contents_by_extension: Sequence[tuple[str, str]]) -> list[Path]:
        """Write ``reports/<name>-<stamp>.<ext>`` for each rendering; returns the paths in the given order."""
        stamp = datetime.now(timezone.utc).strftime(ARCHIVE_STAMP_FORMAT)
        reports_dir = self.root / _REPORTS_DIRNAME
        reports_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for extension, content in contents_by_extension:
            path = reports_dir / f"{report_name}-{stamp}.{extension}"
            path.write_text(content)
            paths.append(path)
        return paths
