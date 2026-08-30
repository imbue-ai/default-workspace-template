import re
from datetime import datetime
from typing import Final

from imbue.imbue_common.pure import pure

from versioning.data_types import CommitRecord
from versioning.data_types import TrailerBlock
from versioning.data_types import VersionKind

APP_TRAILER: Final[str] = "Versioning-App"
REQUEST_TRAILER: Final[str] = "Versioning-Request"
KIND_TRAILER: Final[str] = "Versioning-Kind"
RESTORED_FROM_TRAILER: Final[str] = "Versioning-Restored-From"
PORTED_FROM_TRAILER: Final[str] = "Versioning-Ported-From"


def _trailer_pattern(trailer_name: str) -> re.Pattern[str]:
    return re.compile(r"^" + re.escape(trailer_name) + r":\s*(.+?)\s*$", re.MULTILINE)


_PATTERN_BY_TRAILER: Final[dict[str, re.Pattern[str]]] = {
    name: _trailer_pattern(name)
    for name in (
        APP_TRAILER,
        REQUEST_TRAILER,
        KIND_TRAILER,
        RESTORED_FROM_TRAILER,
        PORTED_FROM_TRAILER,
    )
}


@pure
def _extract_trailer_value(commit_message: str, trailer_name: str) -> str | None:
    match = _PATTERN_BY_TRAILER[trailer_name].search(commit_message)
    return match.group(1) if match is not None else None


@pure
def parse_trailer_block(commit_message: str) -> TrailerBlock:
    raw_kind = _extract_trailer_value(commit_message, KIND_TRAILER)
    kind: VersionKind | None
    if raw_kind is None:
        kind = None
    else:
        try:
            kind = VersionKind(raw_kind.upper())
        except ValueError:
            kind = None
    return TrailerBlock(
        app_name=_extract_trailer_value(commit_message, APP_TRAILER),
        request=_extract_trailer_value(commit_message, REQUEST_TRAILER),
        kind=kind,
        restored_from_sha=_extract_trailer_value(commit_message, RESTORED_FROM_TRAILER),
        ported_from_sha=_extract_trailer_value(commit_message, PORTED_FROM_TRAILER),
    )


@pure
def serialize_trailer_block(block: TrailerBlock) -> str:
    """Render the block as trailer lines, omitting absent values."""
    lines: list[str] = []
    if block.app_name is not None:
        lines.append(f"{APP_TRAILER}: {block.app_name}")
    if block.request is not None:
        lines.append(f"{REQUEST_TRAILER}: {block.request}")
    if block.kind is not None:
        lines.append(f"{KIND_TRAILER}: {block.kind.value.lower()}")
    if block.restored_from_sha is not None:
        lines.append(f"{RESTORED_FROM_TRAILER}: {block.restored_from_sha}")
    if block.ported_from_sha is not None:
        lines.append(f"{PORTED_FROM_TRAILER}: {block.ported_from_sha}")
    return "\n".join(lines)


@pure
def parse_git_log_output(
    output: str,
    field_separator: str,
    record_separator: str,
) -> list[CommitRecord]:
    records: list[CommitRecord] = []
    for raw_record in output.split(record_separator):
        stripped = raw_record.strip("\n")
        if not stripped.strip():
            continue
        sha, author, authored_iso, message = stripped.split(field_separator, 3)
        message_lines = message.strip("\n").split("\n", 1)
        subject = message_lines[0].strip()
        body = message_lines[1].strip() if len(message_lines) > 1 else ""
        records.append(
            CommitRecord(
                sha=sha.strip(),
                author=author.strip(),
                authored_at=datetime.fromisoformat(authored_iso.strip()),
                subject=subject,
                body=body,
                trailers=parse_trailer_block(message),
            )
        )
    return records
