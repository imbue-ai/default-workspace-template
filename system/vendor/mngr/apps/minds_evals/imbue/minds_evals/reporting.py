"""What the reports in this package share.

Three renderers write about the same runs: `check_run`'s per-trial table, `ci_matrix`'s decision
table, and `ci_report`'s Slack message. All three shorten a SHA to the same width, so that two of
them can be read side by side; the two that render markdown tables also escape free text into a cell
and write only the reports they were asked for. The rules live here so they cannot drift apart.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure

# How much of a SHA a report prints. Long enough to name a commit unambiguously in this repo, short
# enough to read at a glance, and the same width in every report so two of them can be compared.
SHORT_SHA_LENGTH: Final[int] = 12


@pure
def as_table_cell(text: str) -> str:
    """Free text in a markdown table cell. A pipe or a newline in it would end the cell, and every
    cell these reports render carries free text: exception messages carry anything, and case ids,
    trial names, refs, config paths and criterion names are authored strings held to no vocabulary
    the renderer knows."""
    return text.replace("|", "\\|").replace("\n", " ")


def write_reports(reports: Sequence[tuple[Path | None, str]]) -> None:
    """Write each report that was asked for, creating the directory it goes in.

    A None path is a report this run was not asked to write, which is how every command here makes
    its summary outputs optional.
    """
    for path, content in reports:
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        logger.info("Wrote {}", path)
