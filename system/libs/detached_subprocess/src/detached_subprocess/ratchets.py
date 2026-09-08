"""The ratchet rules that keep a consuming app's spawns detached.

Every app that shells out from a supervisord service reintroduces the whole failure mode with
one attached spawn, so the rules are allowlist-by-file rather than counts. They live here
because the two apps that adopt them share the reasoning and the exemption mechanics; each app
applies them to its own source tree, with its own allowlist, from its own
``subprocess_ratchets_test.py``.

There are two rules because there are two ways to spawn. Run-to-completion commands go through
``run_detached_command``. Long-running background processes cannot -- they need
``ConcurrencyGroup`` -- so they ask it for the detachment themselves, and a rule looking only
for the raw runner call would not see them.
"""

from __future__ import annotations

import ast
from pathlib import Path

from imbue.imbue_common.ratchet_testing.common_ratchets import RegexRatchetRule
from imbue.imbue_common.ratchet_testing.core import get_ast_nodes_of_type

# Every ConcurrencyGroup entry point that reaches the subprocess runner. Shared by the rule and
# by the guard standing behind an allowlisted file's exemption from it: the exemption waives all
# of these at once, so a guard that knew about fewer would leave the rest unchecked in that file.
BACKGROUND_SPAWN_NAMES = ("run_process_in_background", "run_process_to_completion", "run_background")

RAW_SPAWN_RULE = RegexRatchetRule(
    rule_name="subprocess spawns outside the detached runner",
    rule_description=(
        "A workspace service must spawn every subprocess written in its own source through "
        "detached_subprocess.runner.run_detached_command, which puts the child in its own "
        "session. A child that inherits the service's controlling terminal can stop the whole "
        "service just by touching that terminal when it is killed (the kernel answers a read or a "
        "mode change from a background process group with SIGTTIN / SIGTTOU addressed to the whole "
        "group), which wedges the workspace: the socket keeps accepting and nothing answers. Do "
        "not call run_local_command_modern_version, subprocess.Popen/run, or os.system directly "
        "-- extend run_detached_command instead. Importing the attached runner counts as much as "
        "calling it: handing it to something else as a value (a default argument, a callback) "
        "spawns just as attached."
    ),
    # The import alternatives are anchored to the start of a line so they read code and not
    # prose: the modules that spawn also name the runner in comments, which a bare name pattern
    # would misfire on. One is anchored to the `from ... import` prefix, leaving what follows
    # `import` open (up to a trailing `#`) so an alias spelling -- `... as _run` -- still
    # matches; the other takes a line holding nothing but the name, which is the continuation
    # line of a parenthesized import.
    pattern_string=(
        r"^from \S+ import [^#\n]*\brun_local_command_modern_version\b"
        r"|^\s*run_local_command_modern_version,?\s*$"
        r"|run_local_command_modern_version\(|subprocess\.(?:Popen|run|call|check_call|check_output)\(|os\.system\("
    ),
    is_multiline=True,
)

BACKGROUND_SPAWN_RULE = RegexRatchetRule(
    rule_name="ConcurrencyGroup process spawns outside the allowlisted file",
    rule_description=(
        "A long-running background process started through ConcurrencyGroup reaches the same "
        "subprocess runner as a direct call, so it inherits the service's controlling terminal "
        "unless it asks not to, and terminating it can then stop the whole service. A spawn that "
        "genuinely has to outlive the call must pass is_detached_from_terminal=True; anything "
        "else should prefer run_detached_command."
    ),
    pattern_string="|".join(rf"{name}\(" for name in BACKGROUND_SPAWN_NAMES),
)


def called_name(call: ast.Call) -> str | None:
    """The bare name of what ``call`` invokes, for both ``f(...)`` and ``obj.f(...)``."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def find_undetached_background_spawns(module_path: Path) -> tuple[list[ast.Call], list[ast.Call]]:
    """The ConcurrencyGroup spawns in ``module_path``, and those of them not asking to detach.

    Used by the guard standing behind a file's exemption from :data:`BACKGROUND_SPAWN_RULE`:
    nothing else would notice a spawn there going back to attached.
    """
    spawns = [
        call for call in get_ast_nodes_of_type(module_path, ast.Call) if called_name(call) in BACKGROUND_SPAWN_NAMES
    ]
    undetached = []
    for spawn in spawns:
        detachment = {keyword.arg: keyword.value for keyword in spawn.keywords}.get("is_detached_from_terminal")
        if not (isinstance(detachment, ast.Constant) and detachment.value is True):
            undetached.append(spawn)
    return spawns, undetached
