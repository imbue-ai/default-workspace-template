import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from imbue.mngr.api.preservation import PRESERVATION_MANIFEST_FILENAME
from imbue.mngr.primitives import AgentId

from history_sweep.sweep import (
    RETENTION,
    RemovalReason,
    SweepRefusedError,
    SweepRoots,
    apply_sweep,
    plan_sweep,
    read_session_ids,
)
from history_sweep.testing import write_archive, write_session

_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def roots(tmp_path: Path) -> SweepRoots:
    return SweepRoots(
        host_dir=tmp_path / "mngr",
        accounts_root=tmp_path / "accounts",
        chats_root=tmp_path / "chats",
    )


def _new_agent_id() -> str:
    return str(AgentId.generate())


def _write_present_agent(
    roots: SweepRoots, agent_id: str, session_ids: list[str]
) -> None:
    agent_dir = roots.host_dir / "agents" / agent_id
    agent_dir.mkdir(parents=True)
    (agent_dir / "claude_session_id_history").write_text(
        "".join(f"{sid} hook\n" for sid in session_ids)
    )


def _write_chat_record(roots: SweepRoots, agent_ids: list[str]) -> None:
    chat_dir = roots.chats_root / agent_ids[0]
    chat_dir.mkdir(parents=True)
    record = {
        "chat_id": agent_ids[0],
        "agents": [{"seq": i + 1, "agent_id": a} for i, a in enumerate(agent_ids)],
    }
    (chat_dir / "record.json").write_text(json.dumps(record))


def _planned(roots: SweepRoots) -> dict[Path, RemovalReason]:
    return {removal.path: removal.reason for removal in plan_sweep(roots, now=_NOW)}


def test_recently_deleted_chat_loses_only_its_account_session_copy(
    roots: SweepRoots,
) -> None:
    session_id = str(uuid4())
    copies = write_session(roots, session_id)
    archive = write_archive(
        roots, _new_agent_id(), [session_id], preserved_at=_NOW - timedelta(days=1)
    )

    planned = _planned(roots)

    assert planned == {path: RemovalReason.ORPHANED_SESSION_COPY for path in copies}
    assert archive not in planned


def test_archive_past_retention_is_removed_with_its_session_copies(
    roots: SweepRoots,
) -> None:
    session_id = str(uuid4())
    copies = write_session(roots, session_id)
    archive = write_archive(
        roots, _new_agent_id(), [session_id], preserved_at=_NOW - RETENTION
    )

    planned = _planned(roots)

    assert planned == {
        **{path: RemovalReason.ORPHANED_SESSION_COPY for path in copies},
        archive: RemovalReason.EXPIRED_ARCHIVE,
    }


def test_session_copies_stay_while_the_archive_has_no_raw_transcript(
    roots: SweepRoots,
) -> None:
    session_id = str(uuid4())
    write_session(roots, session_id)
    write_archive(
        roots,
        _new_agent_id(),
        [session_id],
        preserved_at=_NOW - timedelta(days=1),
        has_raw_transcript=False,
    )

    assert _planned(roots) == {}


def test_archive_of_a_present_agent_is_never_touched(roots: SweepRoots) -> None:
    agent_id = _new_agent_id()
    session_id = str(uuid4())
    write_session(roots, session_id)
    _write_present_agent(roots, agent_id, [session_id])
    write_archive(roots, agent_id, [session_id], preserved_at=_NOW - timedelta(days=90))

    assert _planned(roots) == {}


def test_archive_of_an_agent_a_chat_record_lists_is_never_touched(
    roots: SweepRoots,
) -> None:
    agent_id = _new_agent_id()
    session_id = str(uuid4())
    write_session(roots, session_id)
    _write_chat_record(roots, [_new_agent_id(), agent_id])
    write_archive(roots, agent_id, [session_id], preserved_at=_NOW - timedelta(days=90))

    assert _planned(roots) == {}


def test_session_a_present_agent_still_names_is_kept(roots: SweepRoots) -> None:
    shared_session = str(uuid4())
    own_session = str(uuid4())
    shared_copies = write_session(roots, shared_session)
    own_copies = write_session(roots, own_session)
    _write_present_agent(roots, _new_agent_id(), [shared_session])
    write_archive(
        roots,
        _new_agent_id(),
        [shared_session, own_session],
        preserved_at=_NOW - timedelta(days=1),
    )

    planned = _planned(roots)

    assert set(planned) == set(own_copies)
    assert not set(shared_copies) & set(planned)


def test_session_a_listed_absent_agent_names_is_kept(roots: SweepRoots) -> None:
    shared_session = str(uuid4())
    shared_copies = write_session(roots, shared_session)
    listed_agent_id = _new_agent_id()
    _write_chat_record(roots, [_new_agent_id(), listed_agent_id])
    write_archive(
        roots,
        listed_agent_id,
        [shared_session],
        preserved_at=_NOW - timedelta(days=90),
    )
    write_archive(
        roots,
        _new_agent_id(),
        [shared_session],
        preserved_at=_NOW - timedelta(days=1),
    )

    assert not set(shared_copies) & set(_planned(roots))


def test_session_two_deleted_agents_name_is_planned_once(roots: SweepRoots) -> None:
    shared_session = str(uuid4())
    copies = write_session(roots, shared_session)
    for _ in range(2):
        write_archive(
            roots,
            _new_agent_id(),
            [shared_session],
            preserved_at=_NOW - timedelta(days=1),
        )

    planned_paths = [removal.path for removal in plan_sweep(roots, now=_NOW)]

    assert sorted(planned_paths) == sorted(copies)


def test_session_copies_are_found_in_every_account(roots: SweepRoots) -> None:
    session_id = str(uuid4())
    copies = write_session(roots, session_id, account="a") + write_session(
        roots, session_id, account="b"
    )
    write_archive(
        roots, _new_agent_id(), [session_id], preserved_at=_NOW - timedelta(days=1)
    )

    assert set(_planned(roots)) == set(copies)


def test_unreadable_chat_record_refuses_the_whole_sweep(roots: SweepRoots) -> None:
    session_id = str(uuid4())
    write_session(roots, session_id)
    write_archive(
        roots, _new_agent_id(), [session_id], preserved_at=_NOW - timedelta(days=90)
    )
    (roots.chats_root / "broken").mkdir(parents=True)
    (roots.chats_root / "broken" / "record.json").write_text("{not json")

    with pytest.raises(SweepRefusedError):
        plan_sweep(roots, now=_NOW)


def test_nothing_to_sweep_on_an_empty_host(roots: SweepRoots) -> None:
    assert plan_sweep(roots, now=_NOW) == ()


def test_apply_removes_planned_paths_and_reports_their_sizes(roots: SweepRoots) -> None:
    session_id = str(uuid4())
    copies = write_session(roots, session_id)
    archive = write_archive(
        roots, _new_agent_id(), [session_id], preserved_at=_NOW - RETENTION
    )
    planned = plan_sweep(roots, now=_NOW)

    removed, failed = apply_sweep(planned)

    assert failed == ()
    assert {removal.path for removal in removed} == {*copies, archive}
    assert not any(path.exists() for path in [*copies, archive])
    assert all(removal.size_bytes > 0 for removal in removed)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_apply_keeps_the_archive_when_a_session_copy_cannot_be_removed(
    roots: SweepRoots,
) -> None:
    session_id = str(uuid4())
    jsonl = write_session(roots, session_id)[0]
    archive = write_archive(
        roots, _new_agent_id(), [session_id], preserved_at=_NOW - RETENTION
    )
    planned = plan_sweep(roots, now=_NOW)
    jsonl.parent.chmod(0o500)
    try:
        _removed, failed = apply_sweep(planned)
    finally:
        jsonl.parent.chmod(0o700)

    assert {removal.path for removal, _error in failed} >= {jsonl, archive}
    assert archive.exists()
    assert jsonl.exists()


def test_read_session_ids_takes_the_first_word_and_skips_unsafe_ids(
    tmp_path: Path,
) -> None:
    history = tmp_path / "claude_session_id_history"
    history.write_text("abc-123 hook\n\n../../etc reset\nabc-123 resume\ndef-456\n")

    assert read_session_ids(history) == {"abc-123", "def-456"}
    assert read_session_ids(tmp_path / "missing") == set()


def test_legacy_archive_without_manifest_ages_by_its_folder_time(
    roots: SweepRoots,
) -> None:
    old_session, recent_session = str(uuid4()), str(uuid4())
    old_copies = write_session(roots, old_session)
    recent_copies = write_session(roots, recent_session)
    old_archive = write_archive(
        roots, _new_agent_id(), [old_session], preserved_at=_NOW
    )
    recent_archive = write_archive(
        roots, _new_agent_id(), [recent_session], preserved_at=_NOW
    )
    for archive, age in ((old_archive, RETENTION), (recent_archive, timedelta(days=1))):
        (archive / PRESERVATION_MANIFEST_FILENAME).unlink()
        mtime = (_NOW - age).timestamp()
        os.utime(archive, (mtime, mtime))

    assert _planned(roots) == {
        **{path: RemovalReason.ORPHANED_SESSION_COPY for path in old_copies},
        old_archive: RemovalReason.EXPIRED_ARCHIVE,
        **{path: RemovalReason.ORPHANED_SESSION_COPY for path in recent_copies},
    }
