#!/usr/bin/env python3
"""Render a declarative mngr_mapreduce pipeline to a deterministic SVG.

Usage, from the repo root:

    uv run python libs/mngr_mapreduce/scripts/render_pipeline_svg.py \\
        <module>:<PIPELINE_ATTRIBUTE> <output>.svg

PIPELINE_REF is a dotted ``module:attribute`` reference to a ``Pipeline``
instance, so any pipeline built on the model can be drawn. The rendering is a
pure function of the model, so re-running it is how a checked-in diagram is
brought back into agreement with the pipeline it was drawn from.
"""

import pkgutil
from pathlib import Path

import click
from loguru import logger

from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline_svg import render_pipeline_svg


@click.command()
@click.argument("pipeline_ref")
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
def main(pipeline_ref: str, output_path: Path) -> None:
    """Render PIPELINE_REF (a module:attribute reference to a Pipeline) to OUTPUT_PATH."""
    try:
        pipeline = pkgutil.resolve_name(pipeline_ref)
    except (ImportError, AttributeError, ValueError) as e:
        raise click.BadParameter(f"Could not resolve {pipeline_ref!r}: {e}", param_hint="PIPELINE_REF") from e
    if not isinstance(pipeline, Pipeline):
        raise click.BadParameter(
            f"{pipeline_ref!r} is not a {Pipeline.__module__}.Pipeline instance", param_hint="PIPELINE_REF"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_pipeline_svg(pipeline), encoding="utf-8")
    logger.info("Wrote {}", output_path)


if __name__ == "__main__":
    main()
