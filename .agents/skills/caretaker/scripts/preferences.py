#!/usr/bin/env python3
"""Read/write helper for the Caretaker's standing preferences.

The preferences live at ``runtime/caretaker/preferences.toml`` (relative to the
repo root) and follow the frozen schema:

    auto_scan = false       # may scan service logs without asking
    auto_fix = false        # may apply fixes without asking
    fix_scope = "minor_only"   # "minor_only" | "all" -- how big a change is allowed
    introduced = false      # set true only after the first-run welcome is delivered

Consent fields are *absent* until the user answers; an absent field means
"not granted / ask". ``get`` therefore returns the documented default for a
missing key, so the SKILL.md can branch on the value deterministically.

CLI:

    python preferences.py get auto_scan        # -> "false"
    python preferences.py get fix_scope        # -> "minor_only"
    python preferences.py set introduced true
    python preferences.py set fix_scope all
    python preferences.py show                  # dump the whole file as TOML

Reads use stdlib ``tomllib``; writes use ``tomlkit`` (already a dependency of
this repo) so any comments or formatting a human added by hand survive a
programmatic ``set``.
"""

import sys
import tomllib
from pathlib import Path
from typing import Final

import tomlkit

# Repo-root-relative location of the preferences file (frozen by the contract).
PREFERENCES_PATH: Final[Path] = Path("runtime/caretaker/preferences.toml")

# The known keys and their "not granted / ask" defaults. A key absent from the
# file resolves to the default here, so callers never have to special-case a
# missing file or a missing key.
_BOOLEAN_DEFAULTS: Final[dict[str, bool]] = {
    "auto_scan": False,
    "auto_fix": False,
    "introduced": False,
}
_FIX_SCOPE_DEFAULT: Final[str] = "minor_only"
_FIX_SCOPE_CHOICES: Final[tuple[str, ...]] = ("minor_only", "all")

# Every recognized key (booleans plus the fix_scope enum).
KNOWN_KEYS: Final[tuple[str, ...]] = (*_BOOLEAN_DEFAULTS.keys(), "fix_scope")


def _read_raw() -> dict[str, object]:
    """Parse the preferences file, returning an empty dict when it is absent."""
    if not PREFERENCES_PATH.exists():
        return {}
    with PREFERENCES_PATH.open("rb") as f:
        return tomllib.load(f)


def get(key: str) -> str:
    """Return the stored value for ``key`` as a string, or its default if absent.

    Booleans render as ``"true"`` / ``"false"`` so shell callers can compare
    them with a plain string test.
    """
    if key not in KNOWN_KEYS:
        raise SystemExit(f"unknown preference key: {key!r} (known: {', '.join(KNOWN_KEYS)})")
    data = _read_raw()
    if key == "fix_scope":
        value = data.get(key, _FIX_SCOPE_DEFAULT)
        if not isinstance(value, str) or value not in _FIX_SCOPE_CHOICES:
            raise SystemExit(f"invalid fix_scope in {PREFERENCES_PATH}: {value!r} (expected one of {_FIX_SCOPE_CHOICES})")
        return value
    raw = data.get(key, _BOOLEAN_DEFAULTS[key])
    if not isinstance(raw, bool):
        raise SystemExit(f"invalid boolean for {key} in {PREFERENCES_PATH}: {raw!r}")
    return "true" if raw else "false"


def _coerce_value(key: str, value: str) -> object:
    """Validate and convert a string CLI value into the typed value to store."""
    if key == "fix_scope":
        if value not in _FIX_SCOPE_CHOICES:
            raise SystemExit(f"fix_scope must be one of {_FIX_SCOPE_CHOICES}, got {value!r}")
        return value
    normalized = value.strip().lower()
    if normalized in ("true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    raise SystemExit(f"{key} must be a boolean (true/false), got {value!r}")


def set_value(key: str, value: str) -> None:
    """Persist ``key = value`` into the preferences file, preserving formatting."""
    if key not in KNOWN_KEYS:
        raise SystemExit(f"unknown preference key: {key!r} (known: {', '.join(KNOWN_KEYS)})")
    typed = _coerce_value(key, value)
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PREFERENCES_PATH.exists():
        document = tomlkit.parse(PREFERENCES_PATH.read_text())
    else:
        document = tomlkit.document()
    document[key] = typed
    PREFERENCES_PATH.write_text(tomlkit.dumps(document))


def _show() -> str:
    """Render the full preferences file (or an empty document) as TOML text."""
    if not PREFERENCES_PATH.exists():
        return ""
    return PREFERENCES_PATH.read_text()


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "get":
        if len(argv) != 3:
            raise SystemExit("usage: preferences.py get <key>")
        print(get(argv[2]))
        return 0
    if len(argv) >= 2 and argv[1] == "set":
        if len(argv) != 4:
            raise SystemExit("usage: preferences.py set <key> <value>")
        set_value(argv[2], argv[3])
        return 0
    if len(argv) == 2 and argv[1] == "show":
        sys.stdout.write(_show())
        return 0
    raise SystemExit("usage: preferences.py {get <key> | set <key> <value> | show}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
