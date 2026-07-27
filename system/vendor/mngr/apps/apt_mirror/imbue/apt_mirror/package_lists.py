"""Reading committed package list files (one package name per line; hash-prefixed comments)."""

from collections.abc import Sequence
from pathlib import Path

from imbue.apt_mirror.errors import AptMirrorPackageListError
from imbue.imbue_common.pure import pure


@pure
def parse_package_list_text(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            names.append(stripped)
    return names


def read_package_lists(list_paths: Sequence[Path]) -> list[str]:
    """Read and deduplicate package names from the given list files, preserving order.

    Raises AptMirrorPackageListError when a file is missing or unreadable.
    """
    seen: set[str] = set()
    ordered_names: list[str] = []
    for list_path in list_paths:
        try:
            text = list_path.read_text()
        except OSError as e:
            raise AptMirrorPackageListError(f"Cannot read package list: {list_path}") from e
        for name in parse_package_list_text(text):
            if name not in seen:
                seen.add(name)
                ordered_names.append(name)
    return ordered_names
