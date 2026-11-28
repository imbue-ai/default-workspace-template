"""Recording that an app's own checks pass on the template version the workspace is on.

The record's ``verified_template`` is what an adopter's agent compares with its own workspace's version when an
app misbehaves there, so it is written only after the checks have run and passed, never by hand.
"""

from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from pydantic import Field

from app_install.errors import AppChecksFailedError
from app_install.errors import InvalidTemplateVersionError
from app_install.git import workspace_template_version
from app_install.interfaces import WorkspaceRuntimeInterface
from app_manifest.manifest import TEMPLATE_VERSION_PATTERN
from app_manifest.records import AppRecord
from app_manifest.records import load_record
from app_manifest.records import write_record


class VerifyOutcome(FrozenModel):
    """What verifying an app did."""

    record: AppRecord = Field(description="The app's record as it is now")
    verified_template: str = Field(description="The template version the record now names, empty when none was set")
    problems: tuple[str, ...] = Field(default=(), description="What this run tried and could not do")


def verify_app(
    repo_root: Path, name: str, template_version: str | None, runtime: WorkspaceRuntimeInterface
) -> VerifyOutcome:
    """Run the checks of the app ``name`` (its environment syncs from its lock and its tests pass) and, when they
    pass, write ``template_version`` to its record as ``verified_template`` and commit that one file.

    With no version given, the one the checkout's history names is used: an update's worker names the version it
    is bringing in, which its worktree's history does not hold a tag for yet.

    Raises AppRecordNotFoundError for an app with no record, InvalidTemplateVersionError for a version that is no
    template version, and AppChecksFailedError when the checks fail; nothing is written when it raises.
    """
    record = load_record(repo_root, name)
    if template_version is not None and not TEMPLATE_VERSION_PATTERN.fullmatch(template_version):
        raise InvalidTemplateVersionError(
            f"{template_version!r} is no template version: one is written minds-v<major>.<minor>.<patch>"
        )
    failure = runtime.verify_app(repo_root, record.package)
    if failure is not None:
        raise AppChecksFailedError(f"the checks of {name} fail: {failure}")
    version = template_version or workspace_template_version(repo_root)
    if not version:
        return VerifyOutcome(
            record=record,
            verified_template="",
            problems=(
                "the checkout's history holds no template release tag and no version was given, so "
                "verified_template was left as it was",
            ),
        )
    if record.verified_template == version:
        return VerifyOutcome(record=record, verified_template=version)
    updated = record.model_copy_update(to_update(record.field_ref().verified_template, version))
    record_file = write_record(repo_root, updated)
    commit_failure = runtime.commit_paths(
        [record_file.relative_to(repo_root).as_posix()], f"app-install: {name} passes its checks on {version}"
    )
    problems = (
        () if commit_failure is None else (f"the app's record is written but was not committed: {commit_failure}",)
    )
    return VerifyOutcome(record=updated, verified_template=version, problems=problems)
