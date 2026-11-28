"""What else in the workspace uses an app: the references its record lists.

A reference names a file or directory outside the app's directory that uses the app, so a change to the app's
footprint reaches it. The record is the only place they are kept, and these two operations the only way to
change them once an app has a record.
"""

from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from pydantic import Field

from app_install.errors import RecordReferenceError
from app_install.interfaces import WorkspaceRuntimeInterface
from app_manifest.records import AppRecord
from app_manifest.records import RecordReference
from app_manifest.records import load_record
from app_manifest.records import write_record
from app_manifest.scope import describe_record_reference_problem


class ReferenceOutcome(FrozenModel):
    """What changing an app's references did."""

    record: AppRecord = Field(description="The app's record as it is now")
    reference: RecordReference = Field(description="The reference that was added or removed")
    problems: tuple[str, ...] = Field(default=(), description="What this run tried and could not do")


def _write_and_commit(
    repo_root: Path, record: AppRecord, message: str, runtime: WorkspaceRuntimeInterface
) -> tuple[str, ...]:
    record_file = write_record(repo_root, record)
    commit_failure = runtime.commit_paths([record_file.relative_to(repo_root).as_posix()], message)
    if commit_failure is None:
        return ()
    return (f"the app's record is written but was not committed: {commit_failure}",)


def add_reference(
    repo_root: Path, name: str, path: str, note: str, runtime: WorkspaceRuntimeInterface
) -> ReferenceOutcome:
    """List ``path`` in the record of the app ``name`` as something that uses the app, and commit that one file.

    Raises AppRecordNotFoundError for an app with no record, and RecordReferenceError for a path the record
    already lists and for one a reference may not name: it is a literal path that exists, outside every app's
    directory, reached through no symlink, and not under ``system/vendor/`` or ``data/``. Nothing is written
    when it raises.
    """
    record = load_record(repo_root, name)
    if any(reference.path == path for reference in record.references):
        raise RecordReferenceError(f"the record of {name} already lists {path!r}")
    reference = RecordReference(path=path, note=note)
    problem = describe_record_reference_problem(repo_root, record.package, reference)
    if problem is not None:
        raise RecordReferenceError(f"{path!r} cannot be listed as using {name}: {problem}")
    updated = record.model_copy_update(to_update(record.field_ref().references, (*record.references, reference)))
    problems = _write_and_commit(repo_root, updated, f"app-install: record that {path} uses {name}", runtime)
    return ReferenceOutcome(record=updated, reference=reference, problems=problems)


def remove_reference(repo_root: Path, name: str, path: str, runtime: WorkspaceRuntimeInterface) -> ReferenceOutcome:
    """Take ``path`` out of the references the record of the app ``name`` lists, and commit that one file.

    Raises AppRecordNotFoundError for an app with no record, and RecordReferenceError when the record does not
    list the path. Nothing is written when it raises.
    """
    record = load_record(repo_root, name)
    removed = [reference for reference in record.references if reference.path == path]
    if not removed:
        raise RecordReferenceError(f"the record of {name} does not list {path!r}")
    remaining = tuple(reference for reference in record.references if reference.path != path)
    updated = record.model_copy_update(to_update(record.field_ref().references, remaining))
    problems = _write_and_commit(repo_root, updated, f"app-install: record that {path} no longer uses {name}", runtime)
    return ReferenceOutcome(record=updated, reference=removed[0], problems=problems)
