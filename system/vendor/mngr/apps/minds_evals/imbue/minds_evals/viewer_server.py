"""Serve a harbor job directory with the vendored viewer.

`harbor view` would serve harbor's own frontend, but the wheel-shipped bundle is absent from the
git tag this project pins, and the point of vendoring is to render annotations that the stock
frontend knows nothing about. harbor's `create_app` takes the static directory as an argument,
so the backend stays stock and only the pixels are ours.
"""

from enum import auto
from pathlib import Path
from typing import Final

import click
import uvicorn
from harbor.viewer import create_app
from loguru import logger

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.logging import setup_logging

# The build lands beside the vendored source, two levels above the package (apps/minds_evals).
VIEWER_BUILD_DIR: Final[Path] = Path(__file__).parents[2] / "viewer" / "build" / "client"
BUILD_SCRIPT: Final[str] = "apps/minds_evals/scripts/build_viewer.sh"


class ViewerMode(LowerCaseStrEnum):
    """Which of the two folder layouts harbor's viewer is being pointed at. The values are
    harbor's own, passed straight to `create_app`."""

    JOBS = auto()
    TASKS = auto()


def detect_folder_mode(folder: Path) -> ViewerMode:
    """Which of harbor's two viewer modes a folder wants, by what its subdirectories hold.

    A job directory holds trials, each with a `config.json`; a task directory holds task
    definitions, each with a `task.toml`. This mirrors what `harbor view` does with an
    unqualified folder, so the two agree on the same tree. `harbor view` exits when neither
    marker turns up; a folder of jobs that has not produced a trial yet is the ordinary way to
    reach that, so this falls back to jobs rather than refusing to start.
    """
    for subdir in sorted(d for d in folder.iterdir() if d.is_dir() and not d.name.startswith(".")):
        if (subdir / "config.json").is_file():
            return ViewerMode.JOBS
        if (subdir / "task.toml").is_file():
            return ViewerMode.TASKS
    return ViewerMode.JOBS


@click.command()
@click.option(
    "--folder",
    "--jobs",
    "folder",
    default="apps/minds_evals/jobs",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Harbor job or task directory to browse",
)
@click.option(
    "--mode",
    type=click.Choice(tuple(ViewerMode)),
    default=None,
    help="Force a viewer mode instead of detecting it from the folder",
)
@click.option("--host", default="127.0.0.1", help="Address to bind")
@click.option("--port", default=8080, type=int, help="Port to bind")
def main(folder: Path, mode: ViewerMode | None, host: str, port: int) -> None:
    """Serve a harbor job or task directory with the vendored viewer."""
    setup_logging(level="INFO")
    if not (VIEWER_BUILD_DIR / "index.html").is_file():
        raise click.ClickException(f"The viewer is not built. Run {BUILD_SCRIPT} first.")
    resolved_mode = mode if mode is not None else detect_folder_mode(folder)
    logger.info("Serving {} in {} mode at http://{}:{}", folder, resolved_mode, host, port)
    uvicorn.run(create_app(folder, mode=resolved_mode, static_dir=VIEWER_BUILD_DIR), host=host, port=port)


if __name__ == "__main__":
    main()
