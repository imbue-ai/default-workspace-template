"""Rendering a node's prompt template, and finding what it reads.

A node carries the Jinja source its jobs' prompts are rendered from, and the
pipeline carries the shared sources that source may extend or include. Both live
in the pipeline, so a template family composes without a loader reaching outside
the object, and every name a family reads can be found before anything runs.
"""

from collections.abc import Mapping

from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import StrictUndefined
from jinja2 import TemplateError
from jinja2 import meta

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError


class PromptRenderError(MngrError):
    """Raised when a node's prompt template cannot be rendered for a job."""

    ...


class MissingTemplateError(PromptRenderError):
    """Raised when a template extends or includes a name the pipeline does not carry."""

    ...


@pure
def make_environment(template_by_name: Mapping[str, str]) -> Environment:
    """An environment that resolves ``extends`` and ``include`` against the pipeline's own templates."""
    return Environment(
        loader=DictLoader(dict(template_by_name)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


@pure
def find_template_variables(template_source: str, template_by_name: Mapping[str, str]) -> frozenset[str]:
    """Every variable name the template reads, including through what it extends or includes.

    Raises PromptRenderError when a source is not valid Jinja and MissingTemplateError
    when it reaches for a template the pipeline does not carry, so both are caught
    where the pipeline is built rather than where an agent would have been launched.
    """
    environment = make_environment(template_by_name)
    variables: set[str] = set()
    pending = [template_source]
    seen_names: set[str] = set()
    while pending:
        source = pending.pop()
        try:
            parsed = environment.parse(source)
        except TemplateError as exc:
            raise PromptRenderError(f"Prompt template is not valid Jinja: {exc}") from exc
        variables.update(meta.find_undeclared_variables(parsed))
        for name in meta.find_referenced_templates(parsed):
            if name is None:
                raise MissingTemplateError(
                    "A prompt template extends or includes a computed name, which cannot be checked before a run"
                )
            if name in seen_names:
                continue
            if name not in template_by_name:
                raise MissingTemplateError(f"A prompt template extends or includes '{name}', which the pipeline lacks")
            seen_names.add(name)
            pending.append(template_by_name[name])
    return frozenset(variables)


def render_prompt(
    template_source: str, template_by_name: Mapping[str, str], variable_by_name: Mapping[str, str]
) -> str:
    """Render the template against the given variables.

    Raises PromptRenderError when a variable the template reads was not
    supplied, rather than letting the hole reach an agent as empty text.
    """
    try:
        return make_environment(template_by_name).from_string(template_source).render(dict(variable_by_name))
    except TemplateError as exc:
        raise PromptRenderError(f"Could not render the prompt template: {exc}") from exc
