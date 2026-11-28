"""A script that drives the browser in a ``driven_browser`` block, for the integration test to run in its own
process: ``python driven_browser_script.py <finish|raise|hang> <name file>``. It writes the session's name to the
name file once attached, checks one command, then finishes, raises, or waits to be killed."""

import sys
import threading
from pathlib import Path

from screen.browser_session import driven_browser


class ScriptFailedError(Exception):
    """What the script raises on purpose, or when the command it checks answers wrong."""


def main(mode: str, name_file: Path) -> None:
    with driven_browser(purpose="itest") as browser:
        name_file.write_text(browser.session_name)
        result = browser.run("eval", "() => 1 + 1", "--raw").split()
        if not result or result[-1] != "2":
            raise ScriptFailedError(f"eval answered {result!r}")
        if mode == "raise":
            raise ScriptFailedError("the script failed")
        if mode == "hang":
            threading.Event().wait(600)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
