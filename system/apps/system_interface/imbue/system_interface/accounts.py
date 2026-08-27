"""The account store: one folder per signed-in provider account, plus an index.

An account IS a folder. Nothing about which provider it belongs to, who signed in, or what
it is called lives on disk -- the folder name is random and carries no meaning, so a folder
can never drift out of sync with, or be mistaken for, the identity the UI shows. All of that
lives in one index file, which is the sole source of truth.

Signing in twice to the same real account is fine and expected: two folders that look
identical from outside. When a login expires the user makes a new one and deletes the old,
so there is deliberately no dedupe, no identity scraping, and no liveness probing anywhere
in this module.

The commit point is the INDEX WRITE, not the folder. A flow mints a folder, drives the CLI
into it, and only then commits a row. That ordering means an interrupted sign-in leaves a
folder with no row -- which `sweep_orphan_dirs` removes at boot -- rather than a row
pointing at a half-authenticated folder the UI would offer as usable.

Concurrency: three operations mutate the index (commit, delete, set-mru) and they are served
concurrently by Flask. An atomic rename prevents a *torn* file, not a *lost update*, so every
mutation takes `_INDEX_LOCK` across the whole read-modify-write.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update

logger = _loguru_logger

# Bumped when the on-disk shape changes. Code that finds a higher version than it knows
# should refuse rather than guess -- without this there is no way for an older build (a
# revert, a rolled-back workspace) to tell "no accounts yet" from "accounts it cannot read".
INDEX_VERSION: Final = 1

_INDEX_FILENAME: Final = "index.json"
_ACCOUNTS_RELATIVE_PATH: Final = (".minds", "accounts")

_INDEX_LOCK = threading.Lock()


class AccountError(RuntimeError):
    """An account could not be created, resolved, or removed."""


class Account(FrozenModel):
    """One signed-in provider account.

    `id` IS the folder name. Deriving it from the lane and a sequence number would put
    identity back on disk -- and would force a choice, on delete, between renumbering every
    stored reference and leaving gaps. A random id makes delete "drop the row and remove the
    folder" and nothing else.
    """

    id: str
    # Which (AI provider + harness) pairing this account signs into. Immutable: re-signing
    # into a folder may change *which* account it holds, never which harness runs it.
    lane: str
    # 1-based, per lane, for display only. Never reused -- see `_next_seq`.
    seq: int
    # The provider noun shown to the user. Equal to the lane's provider name except on the
    # bring-your-own-key lane, where it is the key provider the user picked, so two keys
    # read "OpenRouter (Pi)" and "Groq (Pi)" rather than "API key (Pi)" and "API key (Pi) 2".
    display: str


class AccountIndex(FrozenModel):
    version: int = INDEX_VERSION
    accounts: tuple[Account, ...] = ()
    # The account a new chat uses when none is named. Updated on every chat create.
    mru: str | None = None


def accounts_root(home: Path | None = None) -> Path:
    """Where account folders live. Never under /tmp -- codex refuses a home there."""
    return (home or Path.home()).joinpath(*_ACCOUNTS_RELATIVE_PATH)


def account_dir(account_id: str, home: Path | None = None) -> Path:
    return accounts_root(home) / account_id


def index_path(home: Path | None = None) -> Path:
    return accounts_root(home) / _INDEX_FILENAME


def read_index(home: Path | None = None) -> AccountIndex:
    """Read the index, treating absence as empty and refusing a future version."""
    path = index_path(home)
    if not path.exists():
        return AccountIndex()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise AccountError(f"account index at {path} is unreadable: {e}") from e
    version = payload.get("version", 0)
    if version > INDEX_VERSION:
        raise AccountError(
            f"account index at {path} is version {version}, but this build understands "
            f"{INDEX_VERSION}. Refusing to read it rather than silently dropping accounts."
        )
    return AccountIndex.model_validate(payload)


def _write_index(index: AccountIndex, home: Path | None = None) -> None:
    """Serialize the index through a temp file and rename it into place.

    Callers must already hold `_INDEX_LOCK`; the rename only buys atomicity of the file's
    contents, not of the read-modify-write around it.
    """
    path = index_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index.model_dump(), indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _next_seq(index: AccountIndex, lane: str) -> int:
    """One past the highest seq this lane has ever shown.

    Deliberately not `count + 1`: deleting "Anthropic 1" while "Anthropic 2" is live would
    make the next account a second "Anthropic 2".
    """
    return max((a.seq for a in index.accounts if a.lane == lane), default=0) + 1


def mint_account_dir(home: Path | None = None) -> tuple[str, Path]:
    """Create an empty folder for a sign-in that has not happened yet.

    Returns its id and path. No index row is written -- until the flow succeeds this folder
    is invisible to everything, and `sweep_orphan_dirs` will remove it if the flow never
    finishes.
    """
    account_id = uuid.uuid4().hex
    path = account_dir(account_id, home)
    path.mkdir(parents=True, exist_ok=False)
    return account_id, path


def commit_account(account_id: str, lane: str, display: str, home: Path | None = None) -> Account:
    """Write the index row that makes a minted folder into a real account.

    This is the commit point of a sign-in. Everything before it is provisional.
    Idempotent: committing an id that is already indexed returns the existing row.
    """
    path = account_dir(account_id, home)
    if not path.is_dir():
        raise AccountError(f"cannot commit account {account_id}: {path} does not exist")
    with _INDEX_LOCK:
        index = read_index(home)
        # Re-authenticating writes the same folder a second time. The row already exists and
        # agents hold its id by label, so keep it as-is rather than renumbering; only the
        # credential on disk changed. That makes this a no-op commit, not a conflict.
        existing = next((a for a in index.accounts if a.id == account_id), None)
        if existing is not None:
            _write_index(index.model_copy_update(to_update(index.field_ref().mru, account_id)), home)
            return existing
        account = Account(id=account_id, lane=lane, seq=_next_seq(index, lane), display=display)
        _write_index(
            index.model_copy_update(
                to_update(index.field_ref().accounts, (*index.accounts, account)),
                to_update(index.field_ref().mru, account_id),
            ),
            home,
        )
    logger.info("Committed account {} on lane {} (seq {})", account_id, lane, account.seq)
    return account


def discard_account_dir(account_id: str, home: Path | None = None) -> None:
    """Remove a minted-but-uncommitted folder after a failed or abandoned sign-in."""
    shutil.rmtree(account_dir(account_id, home), ignore_errors=True)


def delete_account(account_id: str, home: Path | None = None) -> None:
    """Drop the row, remove the folder, and clear the mru if it pointed here.

    Agents bound to this account keep their transcripts and fail on their next turn with
    their harness's own error. Nothing rebinds them: their `account` label becomes a
    dangling reference, which is the cost of delete-and-re-add over re-authenticating in
    place.
    """
    with _INDEX_LOCK:
        index = read_index(home)
        remaining = tuple(a for a in index.accounts if a.id != account_id)
        if len(remaining) == len(index.accounts):
            raise AccountError(f"no such account: {account_id}")
        mru = None if index.mru == account_id else index.mru
        _write_index(
            index.model_copy_update(
                to_update(index.field_ref().accounts, remaining),
                to_update(index.field_ref().mru, mru),
            ),
            home,
        )
    discard_account_dir(account_id, home)
    logger.info("Deleted account {}", account_id)


def set_mru(account_id: str, home: Path | None = None) -> None:
    with _INDEX_LOCK:
        index = read_index(home)
        if not any(a.id == account_id for a in index.accounts):
            raise AccountError(f"no such account: {account_id}")
        _write_index(index.model_copy_update(to_update(index.field_ref().mru, account_id)), home)


def resolve_account(account_id: str, home: Path | None = None) -> Account:
    """Resolve an explicit id, or the mru, or the only sensible fallback.

    An empty id means "whatever a new chat should use". That is the mru when it still
    resolves, else the first account -- an mru can name a deleted account only if something
    raced, but falling back beats refusing.
    """
    index = read_index(home)
    if not index.accounts:
        raise AccountError("no provider accounts exist yet")
    if account_id:
        for account in index.accounts:
            if account.id == account_id:
                return account
        raise AccountError(f"no such account: {account_id}")
    if index.mru:
        for account in index.accounts:
            if account.id == index.mru:
                return account
    return index.accounts[0]


def sweep_orphan_dirs(home: Path | None = None) -> tuple[str, ...]:
    """Remove folders with no index row. Called at boot.

    These are the debris of sign-ins that died between minting a folder and committing it --
    a crash, a restart, or a flow the user abandoned. They may hold real credentials, but
    nothing can reach them: no row means no id the UI can name.
    """
    root = accounts_root(home)
    if not root.is_dir():
        return ()
    known = {a.id for a in read_index(home).accounts}
    swept = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in known:
            shutil.rmtree(child, ignore_errors=True)
            swept.append(child.name)
    if swept:
        logger.info("Swept {} orphaned account folder(s): {}", len(swept), ", ".join(swept))
    return tuple(swept)
