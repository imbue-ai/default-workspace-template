"""The plain-python3 scripts insert package paths into sys.path by hand.

Those inserted paths are invisible to the venv-backed pytest runs (the same
packages are installed in the workspace venv), so a stale path only explodes
at runtime under a bare ``python3`` -- which is exactly how every claude
launch runs ``claude_oom_launch.py``. Assert that every inserted path in
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
# splits the call across several lines, and a pattern that only recognised the
# one-line rendering silently matched nothing in those scripts.
_PATH_INSERT_RE = re.compile(
    r"sys\.path\.insert\(\s*0,\s*str\(\s*Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]"
    r"((?:\s*/\s*\"[^\"]+\")+)\s*\)\s*,?\s*\)"
)
_COMPONENT_RE = re.compile(r"\"([^\"]+)\"")


def test_every_script_sys_path_insert_points_at_an_existing_directory() -> None:
    checked_per_dir: dict[Path, int] = {}
    missing: list[str] = []
    for scanned_dir in _SCANNED_DIRS:
        checked_per_dir[scanned_dir] = 0
        for script in sorted(scanned_dir.glob("*.py")):
            source = script.read_text()
            for match in _PATH_INSERT_RE.finditer(source):
                parents_idx = int(match.group(1))
                components = _COMPONENT_RE.findall(match.group(2))
                target = script.resolve().parents[parents_idx].joinpath(*components)
                checked_per_dir[scanned_dir] += 1
                if not target.is_dir():
                    missing.append(f"{script.name}: {target}")
    assert not missing, (
        "sys.path inserts pointing at missing directories:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )
    # Per directory, not in total: a directory is on the list because a script
    # in it inserts a path, so one that yields nothing means the regex no longer
    # recognises how that script writes the insert -- which reads as a pass while
    # checking nothing. That is how the reveal script went unguarded for a
    # formatter's line break.
    unchecked = [str(d) for d, count in checked_per_dir.items() if count == 0]
    assert not unchecked, (
        "scanned directories with no recognized sys.path insert:\n"
        + "\n".join(f"  - {d}" for d in unchecked)
    )
