"""The plain-python3 scripts insert package paths into sys.path by hand.

Those inserted paths are invisible to the venv-backed pytest runs (the same
packages are installed in the workspace venv), so a stale path only explodes
at runtime under a bare ``python3`` -- which is exactly how every claude
launch runs ``claude_oom_launch.py``. Assert that every inserted path in
every script resolves to a real directory so a package move cannot silently
break agent startup again.
"""

import re
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent

# Matches the argument of the conventional insert:
#   sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "a" / "b"))
_PATH_INSERT_RE = re.compile(
    r"sys\.path\.insert\(\s*0,\s*str\(Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]((?:\s*/\s*\"[^\"]+\")+)\s*\)\s*\)"
)
_COMPONENT_RE = re.compile(r"\"([^\"]+)\"")


def test_every_script_sys_path_insert_points_at_an_existing_directory() -> None:
    checked_count = 0
    missing: list[str] = []
    for script in sorted(_SCRIPTS_DIR.glob("*.py")):
        source = script.read_text()
        for match in _PATH_INSERT_RE.finditer(source):
            parents_idx = int(match.group(1))
            components = _COMPONENT_RE.findall(match.group(2))
            target = script.resolve().parents[parents_idx].joinpath(*components)
            checked_count += 1
            if not target.is_dir():
                missing.append(f"{script.name}: {target}")
    assert not missing, "sys.path inserts pointing at missing directories:\n" + "\n".join(
        f"  - {m}" for m in missing
    )
    # The convention is load-bearing (claude_oom_launch, the oom tag scripts);
    # if this ever matches nothing the regex has rotted, not the scripts.
    assert checked_count >= 5
