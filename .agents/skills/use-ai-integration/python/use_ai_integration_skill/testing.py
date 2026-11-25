import json
from collections.abc import Sequence
from pathlib import Path


def write_ai_accounts(
    root: Path,
    rows: Sequence[tuple[str, str, str]],
    *,
    mru: str | None = None,
    default_account: str | None = None,
) -> Path:
    """Write a version-2 account index of ``(id, lane, display)`` rows, in index order, and a folder per row."""
    root.mkdir(parents=True, exist_ok=True)
    for account_id, _, _ in rows:
        (root / account_id).mkdir()
    index = {
        "version": 2,
        "accounts": [
            {"id": account_id, "lane": lane, "seq": seq, "display": display, "name": ""}
            for seq, (account_id, lane, display) in enumerate(rows, start=1)
        ],
        "mru": mru,
        "default_account": default_account,
    }
    (root / "index.json").write_text(json.dumps(index))
    return root


def write_stub_executable(bin_dir: Path, name: str, python_source: str) -> Path:
    """Write an executable Python script called ``name`` into ``bin_dir``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/usr/bin/env python3\n" + python_source)
    path.chmod(0o755)
    return path


def write_pi_key(account_dir: Path, provider_id: str) -> Path:
    """Write a pi ``auth.json`` holding a placeholder key for ``provider_id``, as the chat's sign-in writes it."""
    path = account_dir / "auth.json"
    path.write_text(
        json.dumps({provider_id: {"type": "api_key", "key": f"fake-{provider_id}-key"}})
    )
    path.chmod(0o600)
    return path


def write_agy_sign_in(account_dir: Path) -> Path:
    """Write a placeholder Antigravity sign-in token into ``account_dir``, agy's home."""
    path = account_dir / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("placeholder-token")
    return path
