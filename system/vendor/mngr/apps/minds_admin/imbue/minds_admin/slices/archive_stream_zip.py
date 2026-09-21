#!/usr/bin/env python3
"""Stream a zip of one or more directory trees to stdout (run as root inside a slice VM).

The workspace archive (``minds-admin archives create``) uploads this file to
the VM and runs it over the box's loopback SSH, piping its stdout straight into
the tier bucket. It is a standalone stdlib-only script: the VM has the cloud
image's system python3 and nothing of this repository, so it must not import
anything else from here.

Each ``--root <prefix>=<path>`` becomes a top-level folder of the zip. Regular
files, directories and symlinks are archived (symlinks as symlinks, with their
mode bits); sockets, fifos and device nodes (an overlay's whiteouts) are
skipped, as is anything ``--exclude`` names, and a directory that cannot be
listed is reported as skipped rather than dropped. The zip is written with data
descriptors and zip64 so it can stream to a pipe at any size. One summary line,
``MNGR_ARCHIVE_SUMMARY <json>``, goes to stderr at the end.
"""

import argparse
import fnmatch
import json
import logging
import os
import stat
import sys
import time
import zipfile
from collections.abc import Iterator
from collections.abc import Sequence

SUMMARY_MARKER = "MNGR_ARCHIVE_SUMMARY"
# Deflate at the cheapest level: download convenience matters more than ratio.
ZIP_COMPRESS_LEVEL = 1
# The zip format cannot represent timestamps before 1980.
MIN_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
# How many skipped paths the summary names (the count is always complete).
MAX_REPORTED_SKIPS = 50
CHUNK_BYTES = 1024 * 1024
# The MS-DOS directory attribute the stdlib stamps on directory entries beside
# their Unix mode; extractors that key on it rather than the trailing slash
# would otherwise unpack a directory entry as an empty file.
MSDOS_DIRECTORY_FLAG = 0x10


def is_excluded(relative_path: str, excludes: Sequence[str]) -> bool:
    """Whether a path (relative to its root, ``/``-separated) matches an exclude pattern.

    A ``**/`` pattern matches when any trailing run of path components does
    (``**/.venv`` excludes ``a/b/.venv`` and everything under it); any other
    pattern is matched against the whole relative path.
    """
    parts = relative_path.split("/")
    for pattern in excludes:
        if pattern.startswith("**/"):
            tail = pattern[3:]
            is_match = any(fnmatch.fnmatchcase("/".join(parts[start:]), tail) for start in range(len(parts)))
        else:
            is_match = fnmatch.fnmatchcase(relative_path, pattern)
        if is_match:
            return True
    return False


def zip_date_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    try:
        parsed = time.localtime(mtime)[:6]
    except (OSError, ValueError, OverflowError):
        return MIN_ZIP_DATE_TIME
    return parsed if parsed >= MIN_ZIP_DATE_TIME else MIN_ZIP_DATE_TIME


def relative_path_under(root: str, absolute_path: str) -> str:
    """The path of ``absolute_path`` below ``root`` ('' for the root itself)."""
    relative = os.path.relpath(absolute_path, root)
    return "" if relative == "." else relative


def listing_failures_as_entries(root: str, failures: Sequence[OSError]) -> list[tuple[str, str, str]]:
    """The directories os.walk could not list, as entries carrying the failure's reason."""
    return [
        (
            relative_path_under(root, str(failure.filename)),
            str(failure.filename),
            f"listing failed: {failure.strerror}",
        )
        for failure in failures
    ]


def walk_tree(root: str, excludes: Sequence[str]) -> Iterator[tuple[str, str, str | None]]:
    """Yield ``(relative_path, absolute_path, listing_failure)`` for every entry under ``root``, pruning excluded directories.

    ``listing_failure`` is None for an entry; a directory os.walk could not
    list is yielded once more with the failure's reason, so the summary can
    name the subtree the archive is missing.
    """
    listing_failures: list[OSError] = []
    for current_dir, dir_names, file_names in os.walk(root, followlinks=False, onerror=listing_failures.append):
        yield from listing_failures_as_entries(root, listing_failures)
        listing_failures.clear()
        relative_dir = os.path.relpath(current_dir, root)
        kept_dir_names = []
        for dir_name in sorted(dir_names):
            relative = dir_name if relative_dir == "." else f"{relative_dir}/{dir_name}"
            if is_excluded(relative, excludes):
                continue
            kept_dir_names.append(dir_name)
            yield relative, os.path.join(current_dir, dir_name), None
        # os.walk descends into exactly the names left in this list.
        dir_names[:] = kept_dir_names
        for file_name in sorted(file_names):
            relative = file_name if relative_dir == "." else f"{relative_dir}/{file_name}"
            if is_excluded(relative, excludes):
                continue
            yield relative, os.path.join(current_dir, file_name), None
    yield from listing_failures_as_entries(root, listing_failures)


# What writing one entry came to: ``(entries written, bytes written, why it was skipped or None)``.
EntryOutcome = tuple[int, int, str | None]

WRITTEN: EntryOutcome = (1, 0, None)


def skipped(reason: str) -> EntryOutcome:
    return (0, 0, reason)


def deflated_zip_info(archive_name: str, mtime: float, mode: int) -> zipfile.ZipInfo:
    """The entry of a regular file: its mtime and mode bits, deflated at the streamer's level."""
    zip_info = zipfile.ZipInfo(archive_name, date_time=zip_date_time(mtime))
    zip_info.external_attr = (mode & 0xFFFF) << 16
    zip_info.compress_type = zipfile.ZIP_DEFLATED
    # The ZipFile's own level reaches only the entries it builds itself; one
    # handed to ``open`` carries its own. ``compress_level`` is public from
    # 3.13 and this spelling stays its alias there, so it works on the system
    # python3 of every VM image.
    zip_info._compresslevel = ZIP_COMPRESS_LEVEL  # ty: ignore[unresolved-attribute]
    return zip_info


def add_entry(archive: zipfile.ZipFile, archive_name: str, absolute_path: str) -> EntryOutcome:
    """Write one filesystem entry into the zip, reporting what it came to (or why it was skipped)."""
    try:
        info = os.lstat(absolute_path)
    except OSError as exc:
        return skipped(f"lstat failed: {exc.strerror}")
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        zip_info = zipfile.ZipInfo(archive_name + "/", date_time=zip_date_time(info.st_mtime))
        zip_info.external_attr = ((mode & 0xFFFF) << 16) | MSDOS_DIRECTORY_FLAG
        archive.writestr(zip_info, b"")
        return WRITTEN
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(absolute_path)
        except OSError as exc:
            return skipped(f"readlink failed: {exc.strerror}")
        zip_info = zipfile.ZipInfo(archive_name, date_time=zip_date_time(info.st_mtime))
        zip_info.external_attr = (mode & 0xFFFF) << 16
        archive.writestr(zip_info, target)
        return WRITTEN
    if not stat.S_ISREG(mode):
        return skipped("not a regular file, directory or symlink")
    zip_info = deflated_zip_info(archive_name, info.st_mtime, mode)
    zip_info.file_size = info.st_size
    try:
        source = open(absolute_path, "rb")
    except OSError as exc:
        return skipped(f"open failed: {exc.strerror}")
    written_bytes = 0
    with source, archive.open(zip_info, "w", force_zip64=True) as destination:
        for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
            destination.write(chunk)
            written_bytes += len(chunk)
    return (1, written_bytes, None)


def parse_root(argument: str) -> tuple[str, str]:
    prefix, separator, path = argument.partition("=")
    if not separator or not prefix or not path:
        raise argparse.ArgumentTypeError(f"--root must be <prefix>=<path>, got {argument!r}")
    return prefix.strip("/"), path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=parse_root, required=True, help="<prefix>=<path> to archive")
    parser.add_argument("--exclude", action="append", default=[], help="glob to leave out (repeatable)")
    parser.add_argument("--readme", default=None, help="a text file stored as README.txt at the zip root")
    return parser


# The running totals of a stream, folded as entries are written so a large tree
# never holds one record per entry: ``(entries written, bytes written, entries
# skipped, the first skipped paths with their reasons)``.
Totals = tuple[int, int, int, list[str]]


def empty_totals() -> Totals:
    return (0, 0, 0, [])


def with_outcome(totals: Totals, archive_name: str, outcome: EntryOutcome) -> Totals:
    """The totals with one entry's outcome folded in; only the first ``MAX_REPORTED_SKIPS`` skips are named."""
    entry_count, entry_bytes, skipped_count, skipped_paths = totals
    count, size, reason = outcome
    if reason is None:
        return (entry_count + count, entry_bytes + size, skipped_count, skipped_paths)
    is_named = len(skipped_paths) < MAX_REPORTED_SKIPS
    named_paths = [*skipped_paths, f"{archive_name}: {reason}"] if is_named else skipped_paths
    return (entry_count, entry_bytes, skipped_count + 1, named_paths)


def summary_json(totals: Totals) -> str:
    """The summary line's payload."""
    entry_count, entry_bytes, skipped_count, skipped_paths = totals
    return json.dumps(
        {
            "entry_count": entry_count,
            "entry_bytes": entry_bytes,
            "skipped_count": skipped_count,
            "skipped_paths": skipped_paths,
        }
    )


def main() -> None:
    arguments = build_parser().parse_args()
    totals = empty_totals()
    output = sys.stdout.buffer
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESS_LEVEL) as archive:
        if arguments.readme is not None:
            with open(arguments.readme, "rb") as readme:
                archive.writestr("README.txt", readme.read())
            totals = with_outcome(totals, "README.txt", WRITTEN)
        for prefix, root in arguments.root:
            if not os.path.isdir(root):
                totals = with_outcome(totals, prefix, skipped(f"root {root} is not a directory"))
                continue
            for relative, absolute, listing_failure in walk_tree(root, arguments.exclude):
                archive_name = f"{prefix}/{relative}"
                outcome = (
                    skipped(listing_failure)
                    if listing_failure is not None
                    else add_entry(archive, archive_name, absolute)
                )
                totals = with_outcome(totals, archive_name, outcome)
    output.flush()
    # The summary rides stderr (stdout is the zip) through the stdlib logger,
    # the one structured output channel a script with no dependencies has.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    logging.getLogger(__name__).info("%s %s", SUMMARY_MARKER, summary_json(totals))
    sys.stderr.flush()


if __name__ == "__main__":
    main()
