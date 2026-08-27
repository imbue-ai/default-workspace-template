"""Tests for the account store.

Every test passes an explicit `home` so nothing touches the real `~/.minds`.
"""

from pathlib import Path

import pytest

from imbue.system_interface.accounts import INDEX_VERSION
from imbue.system_interface.accounts import Account
from imbue.system_interface.accounts import AccountError
from imbue.system_interface.accounts import account_dir
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import delete_account
from imbue.system_interface.accounts import discard_account_dir
from imbue.system_interface.accounts import index_path
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.accounts import read_index
from imbue.system_interface.accounts import resolve_account
from imbue.system_interface.accounts import set_mru
from imbue.system_interface.accounts import sweep_orphan_dirs


def _add(home: Path, lane: str, display: str) -> Account:
    account_id, _ = mint_account_dir(home)
    return commit_account(account_id, lane, display, home)


def test_an_empty_store_reads_as_empty(tmp_path: Path) -> None:
    index = read_index(tmp_path)
    assert index.accounts == ()
    assert index.mru is None
    assert index.version == INDEX_VERSION


def test_mint_then_commit_round_trips(tmp_path: Path) -> None:
    account_id, path = mint_account_dir(tmp_path)
    assert path.is_dir()
    # Minting alone commits nothing -- the index write is the commit point.
    assert read_index(tmp_path).accounts == ()

    account = commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    assert account.id == account_id
    assert account.seq == 1
    assert read_index(tmp_path).accounts == (account,)


def test_the_folder_name_carries_no_identity(tmp_path: Path) -> None:
    """The whole point of a random id: nothing on disk claims a lane or a number."""
    first = _add(tmp_path, "opencode-go", "Opencode Go")
    second = _add(tmp_path, "opencode-go", "Opencode Go")
    assert "opencode" not in first.id
    # Same lane, adjacent seq, unrelated ids -- nothing about the folder is derived.
    assert first.id != second.id
    assert not second.id.startswith(first.id[:8])
    assert account_dir(first.id, tmp_path).name == first.id


def test_seq_counts_per_lane(tmp_path: Path) -> None:
    assert _add(tmp_path, "anthropic", "Anthropic").seq == 1
    assert _add(tmp_path, "anthropic", "Anthropic").seq == 2
    # A different lane numbers independently.
    assert _add(tmp_path, "google", "Google").seq == 1


def test_seq_is_never_reused_after_a_delete(tmp_path: Path) -> None:
    """`count + 1` would mint a duplicate of a live account's number."""
    first = _add(tmp_path, "anthropic", "Anthropic")
    second = _add(tmp_path, "anthropic", "Anthropic")
    assert second.seq == 2

    delete_account(first.id, tmp_path)
    assert _add(tmp_path, "anthropic", "Anthropic").seq == 3


def test_committing_sets_the_mru(tmp_path: Path) -> None:
    _add(tmp_path, "anthropic", "Anthropic")
    second = _add(tmp_path, "google", "Google")
    assert read_index(tmp_path).mru == second.id


def test_delete_removes_the_row_the_folder_and_a_stale_mru(tmp_path: Path) -> None:
    account = _add(tmp_path, "anthropic", "Anthropic")
    assert account_dir(account.id, tmp_path).is_dir()

    delete_account(account.id, tmp_path)

    index = read_index(tmp_path)
    assert index.accounts == ()
    assert index.mru is None
    assert not account_dir(account.id, tmp_path).exists()


def test_delete_leaves_an_unrelated_mru_alone(tmp_path: Path) -> None:
    keeper = _add(tmp_path, "anthropic", "Anthropic")
    doomed = _add(tmp_path, "google", "Google")
    set_mru(keeper.id, tmp_path)

    delete_account(doomed.id, tmp_path)
    assert read_index(tmp_path).mru == keeper.id


def test_deleting_an_unknown_account_raises(tmp_path: Path) -> None:
    with pytest.raises(AccountError):
        delete_account("nope", tmp_path)


def test_resolve_prefers_an_explicit_id_then_mru_then_the_first(tmp_path: Path) -> None:
    first = _add(tmp_path, "anthropic", "Anthropic")
    second = _add(tmp_path, "google", "Google")

    assert resolve_account(first.id, tmp_path) == first
    # Committing `second` made it the mru, so an empty id resolves there.
    assert resolve_account("", tmp_path) == second

    # An mru naming a deleted account falls back rather than refusing.
    delete_account(second.id, tmp_path)
    assert resolve_account("", tmp_path) == first


def test_resolve_raises_when_nothing_exists_or_the_id_is_unknown(tmp_path: Path) -> None:
    with pytest.raises(AccountError):
        resolve_account("", tmp_path)
    _add(tmp_path, "anthropic", "Anthropic")
    with pytest.raises(AccountError):
        resolve_account("nope", tmp_path)


def test_committing_the_same_folder_twice_raises(tmp_path: Path) -> None:
    account_id, _ = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    with pytest.raises(AccountError):
        commit_account(account_id, "anthropic", "Anthropic", tmp_path)


def test_committing_a_folder_that_does_not_exist_raises(tmp_path: Path) -> None:
    with pytest.raises(AccountError):
        commit_account("never-minted", "anthropic", "Anthropic", tmp_path)


def test_sweep_removes_uncommitted_folders_and_keeps_committed_ones(tmp_path: Path) -> None:
    """An interrupted sign-in leaves a folder with no row. It may hold real credentials,
    but nothing can reach it -- no row means no id the UI can name."""
    kept = _add(tmp_path, "anthropic", "Anthropic")
    orphan_id, orphan_path = mint_account_dir(tmp_path)
    (orphan_path / ".credentials.json").write_text("{}")

    swept = sweep_orphan_dirs(tmp_path)

    assert swept == (orphan_id,)
    assert not orphan_path.exists()
    assert account_dir(kept.id, tmp_path).is_dir()


def test_sweep_is_a_no_op_on_a_store_that_was_never_used(tmp_path: Path) -> None:
    assert sweep_orphan_dirs(tmp_path) == ()


def test_discard_is_idempotent(tmp_path: Path) -> None:
    account_id, path = mint_account_dir(tmp_path)
    discard_account_dir(account_id, tmp_path)
    discard_account_dir(account_id, tmp_path)
    assert not path.exists()


def test_a_future_index_version_is_refused_rather_than_misread(tmp_path: Path) -> None:
    """A reverted build must not read a newer index as "no accounts" and start minting
    over the top of one."""
    _add(tmp_path, "anthropic", "Anthropic")
    path = index_path(tmp_path)
    path.write_text(path.read_text().replace(f'"version": {INDEX_VERSION}', '"version": 99'))

    with pytest.raises(AccountError, match="version 99"):
        read_index(tmp_path)


def test_an_unreadable_index_raises_rather_than_returning_empty(tmp_path: Path) -> None:
    _add(tmp_path, "anthropic", "Anthropic")
    index_path(tmp_path).write_text("{ not json")
    with pytest.raises(AccountError):
        read_index(tmp_path)
