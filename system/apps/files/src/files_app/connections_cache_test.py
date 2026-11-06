import threading
from pathlib import Path

import httpx

from files_app.connections import Share
from files_app.connections_cache import ConnectionsCache
from files_app.testing import DEVICE
from files_app.testing import FakeComputer


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _cache(computer: FakeComputer, tmp_path: Path, clock: _Clock) -> ConnectionsCache:
    return ConnectionsCache(computer.gateway(), "agent-main", tmp_path / "synced", max_age_seconds=2.0, clock=clock)


def _probes(computer: FakeComputer) -> list[str]:
    return [request.url.path for request in computer.requests if request.method == "PROPFIND"]


def _by_path(shares: tuple[Share, ...]) -> dict[str, Share]:
    return {share.path: share for share in shares}


def test_a_read_answers_every_ask_for_a_while_and_a_later_ask_reads_again(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-read-/Users/kim/notes"], folders={"/Users/kim/notes": []})
    clock = _Clock()
    cache = _cache(computer, tmp_path, clock)

    first = cache.current()
    clock.now += 1.5
    assert cache.current() is first
    assert len(computer.asked("/devices")) == 1

    clock.now += 0.5
    assert cache.current() is not first
    clock.now += 2.0
    cache.current()
    assert len(computer.asked("/devices")) == 3
    # Each read asks for the grants and the syncs, and the share was probed only the first time.
    assert len(computer.asked("/permissions/self")) == 3
    assert len(computer.asked("/folder-syncs")) == 3
    assert len(_probes(computer)) == 1


def test_a_new_share_is_probed_and_a_revoked_one_is_dropped(tmp_path: Path) -> None:
    computer = FakeComputer(
        permissions=["minds-file-server-read-/Users/kim/notes"],
        folders={"/Users/kim/notes": [], "/Users/kim/photos": []},
    )
    clock = _Clock()
    cache = _cache(computer, tmp_path, clock)
    cache.current()

    computer.permissions = ["minds-file-server-write-/Users/kim/photos"]
    clock.now += 2.0
    (photos,) = cache.current().shares

    assert photos.path == "/Users/kim/photos"
    assert photos.access == "write"
    assert photos.kind == "folder"
    assert _probes(computer) == [
        "/minds-api-proxy/api/v1/files/Users/kim/notes",
        "/minds-api-proxy/api/v1/files/Users/kim/photos",
    ]


def test_a_computer_that_falls_asleep_keeps_its_shares_listed_unreachable_until_it_wakes(tmp_path: Path) -> None:
    notes_sync = {
        "local_path": "/Users/kim/notes",
        "workspace_path": f"~/synced_folders/{DEVICE}/Users/kim/notes",
        "activity": "ACTIVE",
        "state": "SYNCED",
        "message": None,
        "bytes_done": None,
        "bytes_total": None,
        "direction": "BOTH",
        "conflict": "NEWER",
    }
    computer = FakeComputer(
        permissions=["minds-file-server-read-/Users/kim/notes", "minds-file-server-read-/Users/kim/Atkinson"],
        folders={"/Users/kim/notes": []},
        syncs=[notes_sync],
    )
    (tmp_path / "synced" / DEVICE / "Users/kim/notes").mkdir(parents=True)
    clock = _Clock()
    cache = _cache(computer, tmp_path, clock)
    awake = _by_path(cache.current().shares)
    assert awake["/Users/kim/notes"].is_reachable and awake["/Users/kim/notes"].sync is not None
    # Gone from the computer: it answers, but not for this share.
    assert not awake["/Users/kim/Atkinson"].is_reachable

    computer.is_awake = False
    clock.now += 2.0
    asleep = _by_path(cache.current().shares)
    assert set(asleep) == {"/Users/kim/notes", "/Users/kim/Atkinson"}
    assert not asleep["/Users/kim/notes"].is_reachable
    # Its copy is still there, of a sync no one could say the state of.
    assert asleep["/Users/kim/notes"].synced_copy == f"{tmp_path}/synced/{DEVICE}/Users/kim/notes/"
    assert asleep["/Users/kim/notes"].sync is None
    probes_while_awake = len(_probes(computer))

    computer.is_awake = True
    computer.folders["/Users/kim/Atkinson"] = []
    clock.now += 2.0
    woken = _by_path(cache.current().shares)
    assert woken["/Users/kim/notes"].is_reachable
    assert woken["/Users/kim/notes"].sync is not None
    # On waking, the share that did not answer is asked again, and the one that did is not.
    assert woken["/Users/kim/Atkinson"].is_reachable
    assert _probes(computer)[probes_while_awake:] == ["/minds-api-proxy/api/v1/files/Users/kim/Atkinson"]


def test_a_computer_asleep_from_the_start_lists_nothing_and_the_listing_still_answers(tmp_path: Path) -> None:
    computer = FakeComputer(permissions=["minds-file-server-read-/Users/kim/notes"], folders={}, is_awake=False)
    listed = _cache(computer, tmp_path, _Clock()).current()
    assert [device.id for device in listed.devices] == [DEVICE]
    assert listed.shares == ()
    assert _probes(computer) == []


class _SlowComputer(FakeComputer):
    """A computer whose device list answers only once let, so asks can pile up behind a read."""

    def __init__(self) -> None:
        super().__init__(permissions=["minds-file-server-read-/Users/kim/notes"], folders={"/Users/kim/notes": []})
        self.is_reading = threading.Event()
        self.may_answer = threading.Event()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/devices":
            self.is_reading.set()
            assert self.may_answer.wait(timeout=10)
        return super().handle(request)


def test_asks_that_come_during_a_read_are_answered_by_it(tmp_path: Path) -> None:
    computer = _SlowComputer()
    cache = _cache(computer, tmp_path, _Clock())
    answers = []
    first = threading.Thread(target=lambda: answers.append(cache.current()))
    first.start()
    assert computer.is_reading.wait(timeout=10)
    others = [threading.Thread(target=lambda: answers.append(cache.current())) for _ in range(3)]
    for other in others:
        other.start()
    computer.may_answer.set()
    for thread in [first, *others]:
        thread.join(timeout=10)

    assert len(answers) == 4
    assert all(answer is answers[0] for answer in answers)
    assert len(computer.asked("/devices")) == 1
