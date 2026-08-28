"""Tests for the account store.

Every test passes an explicit `home` so nothing touches the real `~/.minds`.
"""

import shutil
from pathlib import Path

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.accounts import Account
from imbue.system_interface.accounts import AccountError
from imbue.system_interface.accounts import INDEX_VERSION
from imbue.system_interface.accounts import REAUTH_BACKUP_DIRNAME
from imbue.system_interface.accounts import account_dir
from imbue.system_interface.accounts import accounts_root
from imbue.system_interface.accounts import claim_first_chat
from imbue.system_interface.accounts import clear_reauth_backup
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import delete_account
from imbue.system_interface.accounts import discard_account_dir
from imbue.system_interface.accounts import index_path
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.accounts import read_index
from imbue.system_interface.accounts import reconcile
from imbue.system_interface.accounts import rename_account
from imbue.system_interface.accounts import resolve_account
from imbue.system_interface.accounts import save_reauth_backup
from imbue.system_interface.accounts import set_mru


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


def test_resolve_answers_an_explicit_id_only(tmp_path: Path) -> None:
    """"Which account should a new agent use" is `binding.resolve_binding`'s question, not
    this one -- it needs to know which lanes the build has. Two functions answering it
    differently is how the launcher and a new project's starter chat diverged."""
    first = _add(tmp_path, "anthropic", "Anthropic")
    second = _add(tmp_path, "google", "Google")

    assert resolve_account(first.id, tmp_path) == first
    assert resolve_account(second.id, tmp_path) == second


def test_resolve_raises_when_nothing_exists_or_the_id_is_unknown(tmp_path: Path) -> None:
    with pytest.raises(AccountError):
        resolve_account("whatever", tmp_path)
    _add(tmp_path, "anthropic", "Anthropic")
    with pytest.raises(AccountError):
        resolve_account("nope", tmp_path)


def test_committing_the_same_folder_twice_keeps_its_id_and_position(tmp_path: Path) -> None:
    """Re-authenticating commits the same folder again.

    The id and the seq must not move -- agents hold the id by label, and renumbering would
    orphan them. The DISPLAY may: re-keying the bring-your-own-key lane can name a different
    provider, and leaving the old noun there makes every label say something untrue.
    """
    other = _add(tmp_path, "anthropic", "Anthropic")
    account_id, _ = mint_account_dir(tmp_path)
    first = commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    set_mru(other.id, tmp_path)

    second = commit_account(account_id, "anthropic", "Renamed", tmp_path)

    assert (second.id, second.seq, second.lane) == (first.id, first.seq, first.lane)
    assert second.display == "Renamed"
    index = read_index(tmp_path)
    assert [a.id for a in index.accounts] == [other.id, account_id]
    assert [a.display for a in index.accounts] == ["Anthropic", "Renamed"]
    assert index.mru == account_id


def test_committing_a_folder_that_does_not_exist_raises(tmp_path: Path) -> None:
    with pytest.raises(AccountError):
        commit_account("never-minted", "anthropic", "Anthropic", tmp_path)


def test_reconcile_removes_unreachable_folders_and_keeps_committed_ones(tmp_path: Path) -> None:
    """A folder with no row is debris; one with a row is an account."""
    kept = _add(tmp_path, "anthropic", "Anthropic")
    abandoned, abandoned_path = mint_account_dir(tmp_path)

    removed, dropped = reconcile(tmp_path)

    assert removed == (abandoned,)
    assert dropped == ()
    assert not abandoned_path.exists()
    assert account_dir(kept.id, tmp_path).is_dir()


def test_reconcile_drops_a_row_whose_folder_is_gone(tmp_path: Path) -> None:
    """The dangerous direction: a row with no folder LOOKS usable and is not.

    Seen in a real workspace -- codex reported `CODEX_HOME points to "..." but that path does
    not exist` on every call, which reaches the user as an empty model bar rather than as a
    signed-out account. Dropping the row is what turns it back into "sign in again".
    """
    survivor = _add(tmp_path, "anthropic", "Anthropic")
    broken = _add(tmp_path, "openai", "OpenAI")
    shutil.rmtree(account_dir(broken.id, tmp_path))

    removed, dropped = reconcile(tmp_path)

    assert removed == ()
    assert dropped == (broken.id,)
    assert [a.id for a in read_index(tmp_path).accounts] == [survivor.id]


def test_reconcile_clears_an_mru_pointing_at_a_dropped_row(tmp_path: Path) -> None:
    """Otherwise the next chat resolves an account that reconcile just removed."""
    broken = _add(tmp_path, "openai", "OpenAI")
    set_mru(broken.id, tmp_path)
    shutil.rmtree(account_dir(broken.id, tmp_path))

    reconcile(tmp_path)

    assert read_index(tmp_path).mru is None


def test_resolving_a_row_whose_folder_is_gone_is_refused(tmp_path: Path) -> None:
    """Binding an agent to a directory that is not there fails every call instead of once."""
    broken = _add(tmp_path, "openai", "OpenAI")
    shutil.rmtree(account_dir(broken.id, tmp_path))

    with pytest.raises(AccountError):
        resolve_account(broken.id, tmp_path)

def test_reconcile_is_a_no_op_on_a_store_that_was_never_used(tmp_path: Path) -> None:
    assert reconcile(tmp_path) == ((), ())


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


def test_discarding_an_account_keeps_its_chat_history(tmp_path: Path) -> None:
    """claude is bound by pointing CLAUDE_CONFIG_DIR at the account, and it writes its
    session transcripts inside. Removing the folder wholesale did not just stop the bound
    chats working -- it made them render empty, permanently."""
    account_id, path = mint_account_dir(tmp_path)
    (path / ".credentials.json").write_text("{}")
    session = path / "projects" / "-home-user-workspace"
    session.mkdir(parents=True)
    (session / "abc.jsonl").write_text('{"type":"user"}\n')

    discard_account_dir(account_id, tmp_path)

    assert not (path / ".credentials.json").exists(), "the credential should be gone"
    assert (session / "abc.jsonl").read_text() == '{"type":"user"}\n'


def test_discarding_an_account_with_no_history_leaves_nothing_behind(tmp_path: Path) -> None:
    account_id, path = mint_account_dir(tmp_path)
    (path / "auth.json").write_text("{}")

    discard_account_dir(account_id, tmp_path)

    assert not path.exists()


def test_the_boot_sweep_leaves_a_kept_history_folder_alone(tmp_path: Path) -> None:
    """Otherwise the transcripts survive the delete and are swept one boot later."""
    account_id, path = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    (path / "projects").mkdir()
    (path / "projects" / "a.jsonl").write_text("{}\n")
    delete_account(account_id, tmp_path)

    removed, dropped = reconcile(tmp_path)

    assert removed == () and dropped == ()
    assert (path / "projects" / "a.jsonl").exists()


def test_an_index_that_is_not_an_object_is_refused_not_crashed_on(tmp_path: Path) -> None:
    """`_write_index` has no fsync, so a hard host kill can leave a truncated file -- and
    boot calls `reconcile`, under a supervisor that restarts a million times."""
    path = index_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]")

    with pytest.raises(AccountError):
        read_index(tmp_path)


def test_the_lock_file_is_not_mistaken_for_an_account(tmp_path: Path) -> None:
    """It lives in the accounts root beside the folders, and the sweep removes everything
    the index does not name."""
    account_id, _ = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)

    removed, dropped = reconcile(tmp_path)

    assert removed == () and dropped == ()
    assert (accounts_root(tmp_path) / "index.lock").exists()


def test_the_first_chat_is_claimed_exactly_once(tmp_path: Path) -> None:
    """It is what stacks the `first` template, and therefore what delivers `/welcome`.

    Bootstrap used to own this by creating a chat at boot. It cannot now: a chat needs a
    provider account and a fresh workspace has none.
    """
    assert claim_first_chat(tmp_path) is True
    assert claim_first_chat(tmp_path) is False
    assert claim_first_chat(tmp_path) is False


def test_the_first_chat_marker_survives_deleting_every_account(tmp_path: Path) -> None:
    """Signing out of everything does not make the next chat a first chat again -- the user
    has already been welcomed, and being welcomed twice reads as the workspace forgetting."""
    account_id, _ = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    assert claim_first_chat(tmp_path) is True
    delete_account(account_id, tmp_path)

    assert claim_first_chat(tmp_path) is False


def test_a_rename_changes_the_name_and_nothing_else(tmp_path: Path) -> None:
    """A rename is display only. If it touched the folder, the lane or the seq, it could
    strand a chat -- so this asserts on the whole row, not just the field that moved."""
    account_id, _ = mint_account_dir(tmp_path)
    before = commit_account(account_id, "anthropic", "Anthropic", tmp_path)

    after = rename_account(account_id, "Work", tmp_path)

    assert after == before.model_copy_update(to_update(before.field_ref().name, "Work"))
    assert account_dir(account_id, tmp_path).is_dir()
    (stored,) = read_index(tmp_path).accounts
    assert stored.name == "Work"


def test_a_name_is_stripped_and_can_be_cleared(tmp_path: Path) -> None:
    account_id, _ = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)

    assert rename_account(account_id, "  Work  ", tmp_path).name == "Work"
    # "" is the way back to the provider's own name, so it is stored rather than refused.
    assert rename_account(account_id, "", tmp_path).name == ""


def test_renaming_an_account_that_is_not_there_raises(tmp_path: Path) -> None:
    with pytest.raises(AccountError):
        rename_account("nope", "Work", tmp_path)


def test_a_rename_leaves_every_other_account_untouched(tmp_path: Path) -> None:
    first, _ = mint_account_dir(tmp_path)
    commit_account(first, "anthropic", "Anthropic", tmp_path)
    second, _ = mint_account_dir(tmp_path)
    commit_account(second, "anthropic", "Anthropic", tmp_path)

    rename_account(first, "Work", tmp_path)

    names = [account.name for account in read_index(tmp_path).accounts]
    assert names == ["Work", ""]


def test_a_non_numeric_index_version_is_an_account_error_not_a_type_error(tmp_path: Path) -> None:
    """`{"version": null}` used to crash boot in a loop.

    `.get`'s default only covers an ABSENT key, so a present-but-null one reached
    `None > INDEX_VERSION` and raised TypeError -- which `main.py` does not catch, so
    supervisord restarted forever. It has to be the error boot already degrades on.
    """
    path = index_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for bad in ('{"version": null, "accounts": []}', '{"version": "2", "accounts": []}'):
        path.write_text(bad)
        with pytest.raises(AccountError):
            read_index(tmp_path)


def test_committing_an_account_onto_a_different_lane_is_refused(tmp_path: Path) -> None:
    """`Account.lane` is immutable, and this is where that is enforced.

    Silently keeping the old value let a re-auth on the wrong lane commit: the row still said
    anthropic while codex's config had been written into its folder.
    """
    account_id = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    with pytest.raises(AccountError):
        commit_account(account_id, "openai", "OpenAI", tmp_path)
    assert resolve_account(account_id, tmp_path).lane == "anthropic"


def test_an_interrupted_reauth_has_its_credential_restored_at_boot(tmp_path: Path) -> None:
    """The window where the only copy was in process memory is now survivable.

    A re-auth deletes the working credential before driving the CLI. If the process dies in
    that window the account keeps a folder -- so nothing notices -- and every chat bound there
    fails its next turn while the picker shows it as healthy.
    """
    account_id = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    folder = account_dir(account_id, tmp_path)
    credential = folder / ".credentials.json"
    credential.write_bytes(b'{"real": true}')

    # What `start()` does: park, then unlink.
    save_reauth_backup(account_id, {credential: credential.read_bytes()}, tmp_path)
    credential.unlink()
    assert not credential.exists()

    reconcile(tmp_path)
    assert credential.read_bytes() == b'{"real": true}'
    # And the park is gone, so a later boot cannot put it back over a newer credential.
    assert not (folder / REAUTH_BACKUP_DIRNAME).exists()


def test_a_reauth_backup_of_a_file_that_did_not_exist_removes_it_again(tmp_path: Path) -> None:
    """Restoring "it was absent" means deleting, not writing an empty file.

    A first sign-in on a freshly minted folder has no credential to save; if the flow then
    dies, boot must not leave a zero-byte file that the harness would try to parse.
    """
    account_id = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    folder = account_dir(account_id, tmp_path)
    credential = folder / ".credentials.json"

    save_reauth_backup(account_id, {credential: None}, tmp_path)
    credential.write_bytes(b"half-written")

    reconcile(tmp_path)
    assert not credential.exists()


def test_clearing_a_reauth_backup_stops_boot_undoing_a_commit(tmp_path: Path) -> None:
    account_id = mint_account_dir(tmp_path)
    commit_account(account_id, "anthropic", "Anthropic", tmp_path)
    folder = account_dir(account_id, tmp_path)
    credential = folder / ".credentials.json"
    credential.write_bytes(b"old")

    save_reauth_backup(account_id, {credential: b"old"}, tmp_path)
    credential.write_bytes(b"new")
    clear_reauth_backup(account_id, tmp_path)

    reconcile(tmp_path)
    assert credential.read_bytes() == b"new"


def test_a_kept_reauth_backup_does_not_make_a_deleted_account_look_like_debris(tmp_path: Path) -> None:
    """`reconcile` removes folders with no row. A parked backup must not save one from that."""
    account_id = mint_account_dir(tmp_path)
    folder = account_dir(account_id, tmp_path)
    (folder / REAUTH_BACKUP_DIRNAME).mkdir()
    reconcile(tmp_path)
    assert not folder.exists()
