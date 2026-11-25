"""Pages an app's server writes whole when it cannot show what was asked for, and the remedies they offer.

Such a page is drawn without the workspace's components, so its remedies come from the app contract module's
``mountFailureRemedies`` (``system/libs/workspace_ui/src/app_contract.ts``), which follows the rules the components'
``FailureRemedies`` follow: once a shell frames the page and greets it, an agent asked to fix the failure, then a
draft in a chat and a bug report where the ask is refused. ``failure_remedies_script`` is the one script that mounts
them, and it is the only thing a page says about the ask: the agent's task is worded by the module (``agentFixTask``).

Every app's "not built" page, served at its pages' routes while its frontend's build is missing, is
``not_built_page_html``: one sentence and one ask for every app, from the app's display name and build command. It
checks the app's health route on its own and reloads once the build is there. The page imports the contract module
from the route every app serves it at (``APP_CONTRACT_ROUTE``), which is the shell's build output, so it keeps working
while the app's own build is missing; while the shell's is missing too, the ask stays hidden.
"""

import html
import json
from typing import Final

from flask import Response
from pydantic import Field

from app_manifest.registry import APP_CONTRACT_ROUTE
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The element a page draws its remedies into, and the rules its stylesheet carries for them: the ask beside the
# page's own buttons in one row (``.actions``), and a refused ask's sentence, details and buttons on a row of their own.
FAILURE_REMEDIES_ROOT_ID: Final[str] = "failure-remedies"
FAILURE_REMEDIES_STYLE: Final[str] = (
    ".actions{display:flex;flex-wrap:wrap;gap:0.5rem;align-items:flex-start}"
    ".failure-remedies{display:contents}.ask-agent-failure{flex-basis:100%}.ask-agent-failure-message{margin:0.5rem 0}"
    ".ask-agent-failure .see-details{margin:0 0 0.5rem}"
    ".ask-agent-failure-actions{display:flex;flex-wrap:wrap;gap:0.5rem}[hidden]{display:none!important}"
)

NOT_BUILT_POLL_SECONDS: Final[int] = 10
_REBUILD_INSTRUCTION: Final[str] = (
    "Rebuild it so the page comes back, then tell the user what was wrong and what you changed."
)
_REBUILD_ASK_LABEL: Final[str] = "Ask an agent to rebuild it"


class AgentFixRequest(FrozenModel):
    """What an agent asked to fix a failure is told, in the shape the app contract's ``AgentFixRequest`` takes."""

    what_failed: str = Field(description="What the person was shown")
    details: str | None = Field(description="What went wrong underneath it, handed on verbatim; None when nothing")
    instruction: str | None = Field(
        description="What the agent is to do where that is not to find and fix a fault; None for the contract's own"
    )


class FailureRemedies(FrozenModel):
    """What a page offers for the failure it shows, which owes the person help."""

    message: str = Field(description="The failure's sentence, as the page shows it")
    details: str | None = Field(description="What the page's own 'See details' holds; None when nothing")
    request: AgentFixRequest = Field(description="What the agent is asked")
    ask_label: str = Field(description="The ask button's words")


class FrontendBuild(FrozenModel):
    """How an app's frontend is built: the command, and the directory it is run in."""

    command: str = Field(description="The command that builds it")
    directory: str = Field(description="Where the command is run, relative to the repo root")


# Every built-in app's frontend is a package of the npm workspace under system/, built by one command there.
WORKSPACE_FRONTEND_BUILD: Final[FrontendBuild] = FrontendBuild(command="npm run build", directory="system/")


@pure
def script_literal(value: object) -> str:
    """``value`` as JSON a script element can hold: nothing in it can close the element."""
    return json.dumps(value).replace("<", "\\u003c")


@pure
def failure_remedies_script(remedies: FailureRemedies, contract_module_source: str | None) -> str:
    """The body of the ``<script type="module">`` that mounts ``remedies`` into the page's ``FAILURE_REMEDIES_ROOT_ID``
    element once a frame holds the page. It imports the contract module from the route every app serves it at, or,
    given ``contract_module_source`` (the built module's text), from that copy, for a page whose origin serves
    nothing beside it; that page's policy must then admit ``blob:`` scripts."""
    request: dict[str, str | None] = {
        "whatFailed": remedies.request.what_failed,
        "details": remedies.request.details,
    }
    if remedies.request.instruction is not None:
        request["instruction"] = remedies.request.instruction
    mount = {
        "failure": {"message": remedies.message, "details": remedies.details, "isHelpOwed": True},
        "request": request,
        "askLabel": remedies.ask_label,
    }
    if contract_module_source is None:
        module = script_literal(APP_CONTRACT_ROUTE)
    else:
        module = (
            f'URL.createObjectURL(new Blob([{script_literal(contract_module_source)}], {{ type: "text/javascript" }}))'
        )
    return f"""
if (window.parent !== window) {{
  import({module})
    .then(({{ connectToShell, mountFailureRemedies }}) => {{
      let remedies = null;
      const shell = connectToShell({{ onHandshake: () => remedies?.redraw() }});
      remedies = mountFailureRemedies(document.getElementById({script_literal(FAILURE_REMEDIES_ROOT_ID)}), shell,
        {script_literal(mount)});
    }})
    .catch((error) => console.warn("Could not load the app contract module", error));
}}
"""


@pure
def not_built_sentence(display_name: str) -> str:
    return f"{display_name} isn't available right now."


@pure
def not_built_details(build: FrontendBuild) -> str:
    return f"Its built files are missing. They are rebuilt with {build.command} in {build.directory}."


@pure
def not_built_remedies(display_name: str, build: FrontendBuild) -> FailureRemedies:
    """What every not-built page offers: an agent asked to rebuild the app, told what the page says and why."""
    return FailureRemedies(
        message=not_built_sentence(display_name),
        details=not_built_details(build),
        request=AgentFixRequest(
            what_failed=not_built_sentence(display_name),
            details=not_built_details(build),
            instruction=_REBUILD_INSTRUCTION,
        ),
        ask_label=_REBUILD_ASK_LABEL,
    )


_NOT_BUILT_STYLE: Final[str] = (
    "body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;"
    "margin:0;color:#334155}main{max-width:32rem;padding:2rem}p{line-height:1.5}button{font:inherit;"
    "padding:0.4rem 1rem;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:inherit;cursor:pointer}"
    "button:hover{background:#f1f5f9}details{margin-top:1.5rem;font-size:0.85em;color:#64748b}summary{cursor:pointer}"
    "pre{white-space:pre-wrap;word-break:break-word}" + FAILURE_REMEDIES_STYLE
)


@pure
def not_built_page_html(display_name: str, build: FrontendBuild, health_path: str) -> str:
    """The page an app serves in place of its own while its frontend is not built: what is wrong, a Reload, an agent to
    rebuild it once a shell greets the page, and the build command behind "See details". It asks ``health_path``
    (whose answer carries ``is_frontend_built``) every ``NOT_BUILT_POLL_SECONDS`` and reloads once the build is
    there, so a rebuild by any route brings the page back."""
    name = html.escape(display_name)
    sentence = html.escape(not_built_sentence(display_name), quote=False)
    command = html.escape(build.command, quote=False)
    directory = html.escape(build.directory, quote=False)
    remedies = failure_remedies_script(not_built_remedies(display_name, build), None)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{name}</title><style>{_NOT_BUILT_STYLE}</style></head>
<body><main>
<p>{sentence} It comes back on its own once it is rebuilt.</p>
<div class="actions"><button type="button" id="reload">Reload</button><div id="{FAILURE_REMEDIES_ROOT_ID}"
class="failure-remedies"></div></div>
<details><summary>See details</summary>
<p>Its built files are missing. They are rebuilt with <code>{command}</code> in <code>{directory}</code>.</p></details>
</main>
<script type="module">
document.getElementById("reload").addEventListener("click", () => window.location.reload());
setInterval(() => {{
  fetch({script_literal(health_path)}, {{ cache: "no-store" }})
    .then((response) => response.json())
    .then((health) => {{
      if (health.is_frontend_built === true) window.location.reload();
    }})
    // The app restarting is the expected way for this to fail, and a moment the page may be arriving: ask again.
    .catch(() => {{}});
}}, {NOT_BUILT_POLL_SECONDS * 1000});
</script>
<script type="module">{remedies}</script>
</body></html>
"""


def not_built_page_response(display_name: str, build: FrontendBuild, health_path: str) -> Response:
    """``not_built_page_html`` as an answer no cache keeps, so the built page replaces it the moment it exists."""
    return Response(
        not_built_page_html(display_name, build, health_path),
        mimetype="text/html",
        headers={"Cache-Control": "no-store"},
    )
