import re
from typing import Final

from imbue.imbue_common.pure import pure

# Every supervised app is defined by one `[program:<name>]` section in this file,
# which is what actually launches it. That section is part of the app's version --
# it rides along in the app's commits -- so a restore has to put it back too.
SUPERVISORD_CONFIG_PATH: Final[str] = "system/supervisord.conf"

_SECTION_HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")


@pure
def _find_program_block_span(config_lines: list[str], program: str) -> tuple[int, int] | None:
    """The half-open line range of a program's section, from its header to the next section."""
    section_name = f"program:{program}"
    start_idx: int | None = None
    for idx, line in enumerate(config_lines):
        match = _SECTION_HEADER_PATTERN.match(line)
        if match is None:
            continue
        if start_idx is None:
            if match.group("name").strip() == section_name:
                start_idx = idx
        else:
            return (start_idx, idx)
    if start_idx is None:
        return None
    return (start_idx, len(config_lines))


@pure
def extract_program_block(config_text: str, program: str) -> str | None:
    """A program's whole section of a supervisord config, header line included."""
    config_lines = config_text.splitlines(keepends=True)
    span = _find_program_block_span(config_lines, program)
    if span is None:
        return None
    start_idx, end_idx = span
    return "".join(config_lines[start_idx:end_idx])


@pure
def replace_program_block(config_text: str, program: str, replacement_block: str) -> str | None:
    """The config with one program's section swapped out; None when it has no such section.

    Only that one section moves: every other program's definition is left byte for
    byte alone, because they belong to apps this change has nothing to do with.
    """
    config_lines = config_text.splitlines(keepends=True)
    span = _find_program_block_span(config_lines, program)
    if span is None:
        return None
    start_idx, end_idx = span
    # A section taken from the end of another file may have no closing newline, which
    # would otherwise run the next section's header onto the last line spliced in.
    padded_block = replacement_block if replacement_block.endswith("\n") else replacement_block + "\n"
    return "".join(config_lines[:start_idx]) + padded_block + "".join(config_lines[end_idx:])
