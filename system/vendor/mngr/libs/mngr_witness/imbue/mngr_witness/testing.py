"""Helpers for tests of the witness prompts."""

from pathlib import Path

from imbue.mngr_mapreduce.prompts import render_prompt
from imbue.mngr_witness.prompts import PACKAGED_TEMPLATE_BY_NAME
from imbue.mngr_witness.prompts import RunFacts
from imbue.mngr_witness.prompts import node_prompt_template
from imbue.mngr_witness.prompts import run_parameters


def render_node_prompt(
    packaged_name: str, variant: Path | None, facts: RunFacts, job_variables: dict[str, str]
) -> str:
    """The prompt an agent receives, rendered the way the executor renders it: the node template over the family."""
    return render_prompt(
        node_prompt_template(packaged_name, variant),
        PACKAGED_TEMPLATE_BY_NAME,
        {**run_parameters(facts), **job_variables},
    )
