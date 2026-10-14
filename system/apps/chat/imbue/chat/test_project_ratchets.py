"""Project-specific ratchets confining how the chat package builds an mngr context.

Every operation that reaches mngr in-process builds its context through
``agent_discovery.mngr_context``, which closes the providers mngr caches against that context
on exit; a context loaded any other way stays in that cache for the life of the server. The scan
is an AST walk over every module in the package, tests included, so no test models a second way
for the next one to copy. Lives outside ``test_ratchets.py`` because that file must define the
same test set across every project.
"""

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import LineNumber
from imbue.imbue_common.ratchet_testing.core import RatchetMatchChunk

_PACKAGE = Path(__file__).parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

_LOADER_MODULE: Final[str] = "imbue.mngr.config.loader"
_LOADER_FUNCTION: Final[str] = "load_config"
# The helper ``mngr_context`` replaced: it handed back a context nothing released.
_RETIRED_HELPER: Final[str] = "_get_mngr_context"

# The one place the loader may be called: the body of ``mngr_context`` itself.
_PERMITTED_FILE: Final[Path] = _PACKAGE / "agent_discovery.py"
_PERMITTED_FUNCTION: Final[str] = "mngr_context"

_UNMANAGED_CONTEXT_RULE = RatchetRuleInfo(
    rule_name="mngr contexts built outside agent_discovery.mngr_context",
    rule_description=(
        "Build an mngr context only with `with mngr_context() as mngr_ctx:` (imbue.chat.agent_discovery). "
        "mngr caches every provider it builds against a context until something closes them, and "
        "mngr_context does that on exit; a context loaded with mngr's load_config directly, or through "
        "the retired _get_mngr_context, keeps its providers and the output of every process its group "
        "ran in that cache for the life of the server."
    ),
)


def _loader_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """The names a module binds to the loader function (or the retired helper), and the names it binds to the
    loader module, whose ``.load_config`` counts. Relative imports are not resolved: the package has none."""
    function_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound_name = alias.asname or alias.name
                if node.module == _LOADER_MODULE and alias.name == _LOADER_FUNCTION:
                    function_names.add(bound_name)
                elif f"{node.module}.{alias.name}" == _LOADER_MODULE:
                    module_names.add(bound_name)
                elif alias.name == _RETIRED_HELPER:
                    function_names.add(bound_name)
                else:
                    pass
        elif isinstance(node, ast.Import):
            module_names.update(alias.asname or alias.name for alias in node.names if alias.name == _LOADER_MODULE)
        else:
            pass
    return frozenset(function_names), frozenset(module_names)


def _dotted_name(node: ast.expr) -> str | None:
    """``a.b.c`` for a callee spelled as a name or a chain of attributes on one, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _calls_with_enclosing_function(node: ast.AST, function_name: str | None) -> Iterator[tuple[ast.Call, str | None]]:
    """Every call under ``node``, with the name of its nearest enclosing function (None at module level)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Call):
            yield child, function_name
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _calls_with_enclosing_function(child, child.name)
        else:
            yield from _calls_with_enclosing_function(child, function_name)


def _is_context_build(call: ast.Call, function_names: frozenset[str], module_names: frozenset[str]) -> bool:
    callee = _dotted_name(call.func)
    if callee is None:
        return False
    module_name, _, attribute = callee.rpartition(".")
    if callee in function_names or attribute == _RETIRED_HELPER:
        return True
    return attribute == _LOADER_FUNCTION and module_name in module_names


def _unmanaged_context_builds(
    source_file: Path, permitted_file: Path = _PERMITTED_FILE
) -> tuple[RatchetMatchChunk, ...]:
    tree = ast.parse(source_file.read_text())
    function_names, module_names = _loader_bindings(tree)
    return tuple(
        RatchetMatchChunk(
            file_path=source_file,
            matched_content=ast.unparse(call),
            start_line=LineNumber(call.lineno),
            end_line=LineNumber(call.end_lineno or call.lineno),
        )
        for call, enclosing_function in _calls_with_enclosing_function(tree, None)
        if _is_context_build(call, function_names, module_names)
        and not (source_file == permitted_file and enclosing_function == _PERMITTED_FUNCTION)
    )


def test_prevent_mngr_contexts_built_outside_mngr_context() -> None:
    chunks = tuple(
        chunk for source_file in sorted(_PACKAGE.rglob("*.py")) for chunk in _unmanaged_context_builds(source_file)
    )
    assert len(chunks) <= snapshot(0), _UNMANAGED_CONTEXT_RULE.format_failure(chunks)


_LOADER_IMPORT = "from imbue.mngr.config.loader import load_config\n"


@pytest.mark.parametrize(
    ("file_name", "source", "is_flagged"),
    [
        ("server.py", _LOADER_IMPORT + "load_config(pm, cg)\n", True),
        ("server.py", "from imbue.mngr.config.loader import load_config as load\nload(pm, cg)\n", True),
        ("server.py", "from imbue.mngr.config import loader\nloader.load_config(pm, cg)\n", True),
        ("server.py", "from imbue.mngr.config import loader as mngr_loader\nmngr_loader.load_config(pm, cg)\n", True),
        ("server.py", "import imbue.mngr.config.loader\nimbue.mngr.config.loader.load_config(pm, cg)\n", True),
        ("server.py", "import imbue.mngr.config.loader as mngr_loader\nmngr_loader.load_config(pm, cg)\n", True),
        ("server.py", "from imbue.chat.agent_discovery import _get_mngr_context\n_get_mngr_context()\n", True),
        ("server.py", "from imbue.chat import agent_discovery\nagent_discovery._get_mngr_context()\n", True),
        ("server.py", "from imbue.chat.agent_discovery import _get_mngr_context as get\nget()\n", True),
        ("server.py", "from imbue.chat.config import load_config\nload_config()\n", False),
        ("server.py", _LOADER_IMPORT, False),
        ("agent_discovery.py", _LOADER_IMPORT + "def mngr_context():\n    load_config(pm, cg)\n", False),
        ("agent_discovery.py", _LOADER_IMPORT + "def read_plugin_config():\n    load_config(pm, cg)\n", True),
        ("server.py", _LOADER_IMPORT + "def mngr_context():\n    load_config(pm, cg)\n", True),
    ],
)
def test_the_scan_sees_every_spelling_of_a_context_build_outside_mngr_context(
    tmp_path: Path, file_name: str, source: str, is_flagged: bool
) -> None:
    module = tmp_path / file_name
    module.write_text(source)

    chunks = _unmanaged_context_builds(module, permitted_file=tmp_path / "agent_discovery.py")

    assert len(chunks) == (1 if is_flagged else 0)
