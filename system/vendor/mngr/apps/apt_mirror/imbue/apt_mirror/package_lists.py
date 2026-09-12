"""Reading committed package list files (one package spec per line; hash-prefixed comments)."""

from collections.abc import Sequence
from pathlib import Path

from imbue.apt_mirror.data_types import PackageSpec
from imbue.apt_mirror.errors import AptMirrorPackageListError
from imbue.imbue_common.pure import pure


@pure
def parse_package_spec(text: str) -> PackageSpec:
    """Parse ``name`` or ``name=version`` (apt's own pin spelling). Raises AptMirrorPackageListError when malformed."""
    name, separator, version = text.partition("=")
    if not name or (separator and not version):
        raise AptMirrorPackageListError(f"Malformed package spec {text!r} (expected 'name' or 'name=version')")
    return PackageSpec(name=name, version=version if separator else None)


@pure
def parse_package_list_text(text: str) -> list[PackageSpec]:
    specs: list[PackageSpec] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            specs.append(parse_package_spec(stripped))
    return specs


def read_package_lists(list_paths: Sequence[Path]) -> list[PackageSpec]:
    """Read and deduplicate package specs from the given list files, preserving order.

    Raises AptMirrorPackageListError when a file is missing, unreadable, or malformed.
    """
    seen: set[str] = set()
    ordered_specs: list[PackageSpec] = []
    for list_path in list_paths:
        try:
            text = list_path.read_text()
        except OSError as e:
            raise AptMirrorPackageListError(f"Cannot read package list: {list_path}") from e
        for spec in parse_package_list_text(text):
            if spec.text not in seen:
                seen.add(spec.text)
                ordered_specs.append(spec)
    return ordered_specs
