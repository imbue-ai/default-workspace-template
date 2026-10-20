#!/usr/bin/env python3
"""Entry point for the guard hooks' checks: ``agent_hook_check.py <check> [args...]``.

Each guard's ``.sh`` hook runs its check by name; the check, a module of
:mod:`workspace_bare_scripts`, reads the arguments after the name. Only the named check
is imported, so a hook pays for one check's imports, as it would running that check alone.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

CHECK_NAMES = (
    "block_pipe_tail_head",
    "latchkey_request",
    "secrets_guard",
    "tk_standalone",
)


def main(argv: list[str]) -> int:
    check = argv[1] if len(argv) > 1 else ""
    check_argv = [argv[0], *argv[2:]]
    if check == "block_pipe_tail_head":
        from workspace_bare_scripts import agent_block_pipe_tail_head_check

        return agent_block_pipe_tail_head_check.main(check_argv)
    if check == "latchkey_request":
        from workspace_bare_scripts import agent_latchkey_request_check

        return agent_latchkey_request_check.main(check_argv)
    if check == "secrets_guard":
        from workspace_bare_scripts import agent_secrets_guard_check

        return agent_secrets_guard_check.main()
    if check == "tk_standalone":
        from workspace_bare_scripts import agent_tk_standalone_check

        return agent_tk_standalone_check.main(check_argv)
    sys.stderr.write(
        f"agent_hook_check.py: unknown check {check!r}; expected one of {', '.join(CHECK_NAMES)}\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
