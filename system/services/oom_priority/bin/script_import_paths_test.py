"""The plain-python3 scripts insert package paths into sys.path by hand.

Those inserted paths are invisible to the venv-backed pytest runs (the same
packages are installed in the workspace venv), so a stale path only explodes
at runtime under a bare ``python3`` -- which is exactly how every claude
launch runs ``agent_oom_launch.py``. Assert that every inserted path in
every plain-python3 script -- the oom_priority entry points here in ``bin/``
and the remaining hook scripts in ``system/scripts/`` -- resolves to a real
directory, so a package move cannot silently break agent startup again.
"""

import re
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BIN_DIR.parents[3]
_SCANNED_DIRS = (
    _BIN_DIR,
    _REPO_ROOT / "system" / "scripts",
    # Skill scripts run under a bare python3 for the same reason, and the reveal
    # one bands itself out of the agent-subprocess range before it starts.
    _REPO_ROOT / ".agents" / "skills" / "update-system-interface" / "scripts",
)

# Matches the argument of the conventional insert:
#   sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "a" / "b"))
# Whitespace is allowed everywhere the formatter may put a line break, and the
# outer call may carry a trailing comma: past a certain path length ruff-format
# splits the call across several lines, and a pattern that recognises only the
# one-line rendering matches nothing in those scripts.
_PATH_INSERT_RE = re.compile(
    r"sys\.path\.insert\(\s*0,\s*str\(\s*Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]"
    r"((?:\s*/\s*\"[^\"]+\")+)\s*\)\s*,?\s*\)"
)
_COMPONENT_RE = re.compile(r"\"([^\"]+)\"")


def test_every_script_sys_path_insert_points_at_an_existing_directory() -> None:
    missing: list[str] = []
    unrecognized: list[str] = []
    for scanned_dir in _SCANNED_DIRS:
        for script in sorted(scanned_dir.glob("*.py")):
            # The scan is about the scripts run under a bare ``python3``; a test
            # file is never one, and this file's own worked example of the
            # convention would read as an insert to check.
            if script.name.endswith("_test.py"):
                continue
            source = script.read_text()
            matches = list(_PATH_INSERT_RE.finditer(source))
            for match in matches:
                parents_idx = int(match.group(1))
                components = _COMPONENT_RE.findall(match.group(2))
                target = script.resolve().parents[parents_idx].joinpath(*components)
                if not target.is_dir():
                    missing.append(f"{script.name}: {target}")
            # Per script, because that is the granularity the rot happens at: one
            # script written in a form the regex does not recognise contributes
            # no checks, which reads as a pass over an insert nobody looked at.
            # A count across the directory hides it, since its neighbours still
            # match.
            if not matches and "sys.path.insert(" in source:
                unrecognized.append(str(script))
    assert not missing, (
        "sys.path inserts pointing at missing directories:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )
    assert not unrecognized, (
        "scripts whose sys.path insert the pattern above does not recognize:\n"
        + "\n".join(f"  - {s}" for s in unrecognized)
    )
