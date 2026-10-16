#!/usr/bin/env python3
"""Install Studio Discord into an Imbue workspace from this checkout, or remove it again.

    python3 install.py [--workspace /home/user/workspace] [--port 8086]
    python3 install.py --uninstall [--workspace /home/user/workspace]

For a workspace that does not already carry the app (newer workspaces get it from the default workspace template).
Installing does three things and touches nothing else in the workspace:

1. ``uv tool install --editable`` this checkout, so the ``studio-discord`` command runs this code (edits here take
   effect on the next restart).
2. Writes the one workspace file it needs, ``system/supervisord.conf.d/studio-discord.conf``: the supervisord
   program that has the workspace register the window from this checkout's ``app.toml`` and then runs the app.
3. ``supervisorctl reread && update`` to start it.

Uninstalling stops the program, removes that file and the app's registration, and uninstalls the tool. The app's
data (``data/.apps/studio-discord/``) is kept either way.

Standard library only: it runs before any environment exists.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROGRAM = "studio-discord"
APP_NAME = "studio-discord"
CHECKOUT = Path(__file__).resolve().parent


class InstallError(Exception):
    pass


def _run(argv: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv)} failed:\n{result.stdout}{result.stderr}")
    return result.stdout


def program_block(workspace: Path, tool_bin: Path, port: int) -> str:
    """The supervisord program: the workspace registers the window, then the app runs from this checkout."""
    register = (
        f"python3 system/scripts/forward_port.py --manifest {CHECKOUT / 'app.toml'} --url http://localhost:{port}"
    )
    return f"""\
; Written by studio-discord's install.py ({CHECKOUT}). Remove with: python3 {CHECKOUT / "install.py"} --uninstall
[program:{PROGRAM}]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "{register} && exec {tool_bin}"
directory={workspace}
environment=STUDIO_DISCORD_PORT="{port}"
autostart=true
autorestart=true
startsecs=30
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=/var/log/supervisor/{PROGRAM}-stdout.log
stderr_logfile=/var/log/supervisor/{PROGRAM}-stderr.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_backups=3
"""


def _dropin(workspace: Path) -> Path:
    return workspace / "system" / "supervisord.conf.d" / f"{PROGRAM}.conf"


def install(workspace: Path, port: int) -> None:
    if not (workspace / "system" / "scripts" / "forward_port.py").is_file():
        raise InstallError(f"{workspace} does not look like an Imbue workspace (no system/scripts/forward_port.py)")
    if shutil.which("uv") is None:
        raise InstallError("uv is not on PATH")
    _run(["uv", "tool", "install", "--editable", "--reinstall", str(CHECKOUT)])
    tool_bin = Path(_run(["uv", "tool", "dir", "--bin"]).strip()) / APP_NAME
    if not tool_bin.exists():
        raise InstallError(f"the install did not produce {tool_bin}")
    _dropin(workspace).write_text(program_block(workspace, tool_bin, port))
    _run(["supervisorctl", "reread"])
    _run(["supervisorctl", "update"])
    print(f"studio-discord installed: program {PROGRAM}, port {port}, running {tool_bin} (code in {CHECKOUT})")


def uninstall(workspace: Path) -> None:
    dropin = _dropin(workspace)
    if dropin.exists():
        subprocess.run(["supervisorctl", "stop", PROGRAM], capture_output=True)
        dropin.unlink()
        _run(["supervisorctl", "reread"])
        _run(["supervisorctl", "update"])
    subprocess.run(
        ["python3", "system/scripts/forward_port.py", "--name", APP_NAME, "--remove"],
        cwd=workspace,
        capture_output=True,
    )
    subprocess.run(["uv", "tool", "uninstall", APP_NAME], capture_output=True)
    print("studio-discord removed; its data under data/.apps/studio-discord/ is kept")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Studio Discord into an Imbue workspace, or remove it.")
    parser.add_argument("--workspace", type=Path, default=Path("/home/user/workspace"))
    parser.add_argument("--port", type=int, default=8086)
    parser.add_argument("--uninstall", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.uninstall:
            uninstall(arguments.workspace.absolute())
        else:
            install(arguments.workspace.absolute(), arguments.port)
    except InstallError as error:
        print(f"install.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
