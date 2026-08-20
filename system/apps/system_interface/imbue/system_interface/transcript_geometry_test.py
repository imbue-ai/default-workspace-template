import json
from pathlib import Path

import pytest

from imbue.system_interface.transcript_geometry import TranscriptGeometryKeyError
from imbue.system_interface.transcript_geometry import TranscriptRowGeometry
from imbue.system_interface.transcript_geometry import clear_geometry
from imbue.system_interface.transcript_geometry import read_geometry
from imbue.system_interface.transcript_geometry import write_geometry

_GEOMETRY_FILENAME = "transcript_geometry.json"


def _measured_row(row_key: str, start_offset: int, end_offset: int, height: float) -> dict[str, object]:
    return {"row_key": row_key, "start_offset": start_offset, "end_offset": end_offset, "height": height}


def test_an_unmeasured_workspace_has_no_geometry_and_no_file(tmp_path: Path) -> None:
    assert read_geometry(tmp_path, "agent-7", 760) == ()
    assert not (tmp_path / _GEOMETRY_FILENAME).exists()


def test_write_geometry_round_trips_the_rows_the_client_measured(tmp_path: Path) -> None:
    stored_rows = write_geometry(
        tmp_path,
        "agent-7",
        760,
        [_measured_row("turn-1", 0, 3, 160.5), _measured_row("turn-2", 3, 51, 940.0)],
    )

    assert stored_rows == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=160.5),
        TranscriptRowGeometry(row_key="turn-2", start_offset=3, end_offset=51, height=940.0),
    )
    assert read_geometry(tmp_path, "agent-7", 760) == stored_rows
    assert (tmp_path / _GEOMETRY_FILENAME).exists()


def test_geometry_survives_a_round_trip_through_the_file(tmp_path: Path) -> None:
    # The measurements are the workspace's, so they have to outlive the process
    # that wrote them: every read goes back to the file rather than to anything
    # held in memory.
    write_geometry(tmp_path, "agent-7", 760, [_measured_row("turn-1", 0, 3, 160.0)])
    write_geometry(tmp_path, "agent-9", 760, [_measured_row("turn-1", 0, 12, 480.0)])

    stored = json.loads((tmp_path / _GEOMETRY_FILENAME).read_text())

    assert stored["geometry_by_agent_id"]["agent-7"]["760"]["rows"] == [
        {"row_key": "turn-1", "start_offset": 0, "end_offset": 3, "height": 160.0}
    ]
    assert stored["geometry_by_agent_id"]["agent-7"]["760"]["updated_at_ms"] > 0
    assert read_geometry(tmp_path, "agent-9", 760) == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=12, height=480.0),
    )


def test_a_second_measurement_at_the_same_width_replaces_the_first(tmp_path: Path) -> None:
    # A client measures the whole of what it rendered, so the newer pass is the
    # answer for that width rather than something to merge into the older one.
    write_geometry(tmp_path, "agent-7", 760, [_measured_row("turn-1", 0, 3, 160.0)])

    stored_rows = write_geometry(tmp_path, "agent-7", 760, [_measured_row("turn-1", 0, 3, 210.0)])

    assert stored_rows == (TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=210.0),)
    assert read_geometry(tmp_path, "agent-7", 760) == stored_rows


def test_each_width_bucket_keeps_its_own_rows(tmp_path: Path) -> None:
    # A row's height is a function of the width it wrapped at, so geometry taken
    # in a narrow panel is not an answer for a wide one.
    write_geometry(tmp_path, "agent-7", 480, [_measured_row("turn-1", 0, 3, 320.0)])
    write_geometry(tmp_path, "agent-7", 1200, [_measured_row("turn-1", 0, 3, 160.0)])

    assert read_geometry(tmp_path, "agent-7", 480) == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=320.0),
    )
    assert read_geometry(tmp_path, "agent-7", 1200) == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=160.0),
    )
    assert read_geometry(tmp_path, "agent-7", 900) == ()


def test_each_transcript_keeps_its_own_rows(tmp_path: Path) -> None:
    write_geometry(tmp_path, "agent-7", 760, [_measured_row("turn-1", 0, 3, 160.0)])
    write_geometry(tmp_path, "agent-9", 760, [_measured_row("turn-1", 0, 3, 640.0)])

    assert read_geometry(tmp_path, "agent-7", 760) == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=160.0),
    )
    assert read_geometry(tmp_path, "agent-9", 760) == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=640.0),
    )


@pytest.mark.parametrize(
    "unusable_row",
    [
        _measured_row("", 0, 3, 160.0),
        _measured_row("turn-1", -1, 3, 160.0),
        _measured_row("turn-1", 3, 3, 160.0),
        _measured_row("turn-1", 5, 3, 160.0),
        _measured_row("turn-1", 0, 3, 0.0),
        _measured_row("turn-1", 0, 3, -160.0),
        {"row_key": "turn-1", "start_offset": 0, "end_offset": 3},
        {"row_key": "turn-1", "start_offset": "0", "end_offset": 3, "height": 160.0},
        {"row_key": "turn-1", "start_offset": True, "end_offset": 3, "height": 160.0},
        {"row_key": "turn-1", "start_offset": 0, "end_offset": 3, "height": True},
        {"row_key": 7, "start_offset": 0, "end_offset": 3, "height": 160.0},
        {"row_key": "turn-1", "start_offset": 0, "end_offset": 3, "height": 160.0, "measured_by": "a client"},
        "not a row at all",
        None,
    ],
)
def test_a_row_that_is_not_a_measurement_is_skipped_rather_than_failing_the_write(
    tmp_path: Path,
    unusable_row: object,
) -> None:
    # This data outlives the code that wrote it, so one unusable row costs only
    # itself: the rest of the transcript keeps its geometry and the skipped row
    # is measured again as it renders.
    stored_rows = write_geometry(
        tmp_path,
        "agent-7",
        760,
        [unusable_row, _measured_row("turn-2", 3, 51, 940.0)],
    )

    assert stored_rows == (TranscriptRowGeometry(row_key="turn-2", start_offset=3, end_offset=51, height=940.0),)
    assert read_geometry(tmp_path, "agent-7", 760) == stored_rows


def test_a_row_measured_at_a_whole_number_of_pixels_is_a_measurement(tmp_path: Path) -> None:
    # JSON has one number type, so a row that happened to settle on an exact
    # pixel arrives as an integer and is still a height.
    stored_rows = write_geometry(
        tmp_path,
        "agent-7",
        760,
        [{"row_key": "turn-1", "start_offset": 0, "end_offset": 3, "height": 160}],
    )

    assert stored_rows == (TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=160.0),)
    assert read_geometry(tmp_path, "agent-7", 760) == stored_rows


def test_only_the_rows_up_to_the_cap_are_kept(tmp_path: Path) -> None:
    # The whole file is read and rewritten on every write, so a conversation
    # long enough to exceed the cap keeps the leading rows rather than growing
    # that cost without limit.
    measured_rows = [_measured_row(f"turn-{index}", index, index + 1, 160.0) for index in range(5_001)]

    stored_rows = write_geometry(tmp_path, "agent-7", 760, measured_rows)

    assert len(stored_rows) == 5_000
    assert stored_rows[0].row_key == "turn-0"
    assert stored_rows[-1].row_key == "turn-4999"
    assert len(read_geometry(tmp_path, "agent-7", 760)) == 5_000


def test_only_the_most_recently_updated_transcripts_are_kept(tmp_path: Path) -> None:
    # Geometry is a cache: an evicted transcript is measured again the next time
    # it is opened, which is what keeps the file bounded on a workspace that has
    # shown thousands of them.
    for index in range(51):
        write_geometry(tmp_path, f"agent-{index}", 760, [_measured_row("turn-1", 0, 3, 160.0)])

    assert read_geometry(tmp_path, "agent-0", 760) == ()
    assert read_geometry(tmp_path, "agent-1", 760) != ()
    assert read_geometry(tmp_path, "agent-50", 760) != ()
    assert len(json.loads((tmp_path / _GEOMETRY_FILENAME).read_text())["geometry_by_agent_id"]) == 50


def test_only_the_most_recently_updated_width_buckets_of_a_transcript_are_kept(tmp_path: Path) -> None:
    for width_bucket in range(400, 400 + 9 * 40, 40):
        write_geometry(tmp_path, "agent-7", width_bucket, [_measured_row("turn-1", 0, 3, 160.0)])

    assert read_geometry(tmp_path, "agent-7", 400) == ()
    assert read_geometry(tmp_path, "agent-7", 440) != ()
    assert read_geometry(tmp_path, "agent-7", 720) != ()
    stored = json.loads((tmp_path / _GEOMETRY_FILENAME).read_text())
    assert len(stored["geometry_by_agent_id"]["agent-7"]) == 8


def test_measuring_a_transcript_again_keeps_its_other_widths_alive(tmp_path: Path) -> None:
    # A transcript's recency is its most recent write across every width, so the
    # one being read keeps the width it was read at earlier, and it is the
    # transcript nobody has come back to that goes when the cap bites.
    write_geometry(tmp_path, "agent-7", 480, [_measured_row("turn-1", 0, 3, 320.0)])
    for index in range(49):
        write_geometry(tmp_path, f"agent-{index}-other", 760, [_measured_row("turn-1", 0, 3, 160.0)])

    write_geometry(tmp_path, "agent-7", 1200, [_measured_row("turn-1", 0, 3, 160.0)])
    write_geometry(tmp_path, "agent-newest", 760, [_measured_row("turn-1", 0, 3, 160.0)])

    assert read_geometry(tmp_path, "agent-0-other", 760) == ()
    assert read_geometry(tmp_path, "agent-7", 480) != ()
    assert read_geometry(tmp_path, "agent-7", 1200) != ()


def test_clearing_a_transcript_that_was_never_measured_is_a_noop(tmp_path: Path) -> None:
    assert clear_geometry(tmp_path, "agent-7") is False
    assert read_geometry(tmp_path, "agent-7", 760) == ()
    assert not (tmp_path / _GEOMETRY_FILENAME).exists()


def test_clear_geometry_drops_every_width_of_a_destroyed_transcript(tmp_path: Path) -> None:
    write_geometry(tmp_path, "agent-7", 480, [_measured_row("turn-1", 0, 3, 320.0)])
    write_geometry(tmp_path, "agent-7", 1200, [_measured_row("turn-1", 0, 3, 160.0)])

    assert clear_geometry(tmp_path, "agent-7") is True

    assert read_geometry(tmp_path, "agent-7", 480) == ()
    assert read_geometry(tmp_path, "agent-7", 1200) == ()


def test_clearing_one_transcript_leaves_the_others_alone(tmp_path: Path) -> None:
    write_geometry(tmp_path, "agent-7", 760, [_measured_row("turn-1", 0, 3, 160.0)])
    write_geometry(tmp_path, "agent-9", 760, [_measured_row("turn-1", 0, 3, 640.0)])

    assert clear_geometry(tmp_path, "agent-7") is True

    assert read_geometry(tmp_path, "agent-9", 760) == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=640.0),
    )


def test_an_agent_id_is_trimmed(tmp_path: Path) -> None:
    write_geometry(tmp_path, "  agent-7  ", 760, [_measured_row("turn-1", 0, 3, 160.0)])

    assert read_geometry(tmp_path, "agent-7", 760) != ()


def test_a_blank_agent_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TranscriptGeometryKeyError):
        read_geometry(tmp_path, "   ", 760)
    with pytest.raises(TranscriptGeometryKeyError):
        write_geometry(tmp_path, "", 760, [_measured_row("turn-1", 0, 3, 160.0)])
    with pytest.raises(TranscriptGeometryKeyError):
        clear_geometry(tmp_path, "")


def test_a_width_bucket_that_is_no_viewport_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TranscriptGeometryKeyError):
        write_geometry(tmp_path, "agent-7", 0, [_measured_row("turn-1", 0, 3, 160.0)])
    with pytest.raises(TranscriptGeometryKeyError):
        read_geometry(tmp_path, "agent-7", -760)

    assert not (tmp_path / _GEOMETRY_FILENAME).exists()


def test_a_corrupt_file_reads_as_nothing_measured(tmp_path: Path) -> None:
    write_geometry(tmp_path, "agent-7", 760, [_measured_row("turn-1", 0, 3, 160.0)])
    (tmp_path / _GEOMETRY_FILENAME).write_text("garbage{")

    assert read_geometry(tmp_path, "agent-7", 760) == ()

    # And the store is usable again from there rather than stuck.
    stored_rows = write_geometry(tmp_path, "agent-7", 760, [_measured_row("turn-1", 0, 3, 160.0)])
    assert read_geometry(tmp_path, "agent-7", 760) == stored_rows


def test_a_hand_edited_file_keeps_only_the_entries_that_are_measurements(tmp_path: Path) -> None:
    (tmp_path / _GEOMETRY_FILENAME).write_text(
        json.dumps(
            {
                "geometry_by_agent_id": {
                    "agent-7": {
                        "760": {
                            "rows": [
                                {"row_key": "turn-1", "start_offset": 0, "end_offset": 3, "height": 160.0},
                                {"row_key": "turn-2", "start_offset": 3, "end_offset": 3, "height": 160.0},
                            ],
                            "updated_at_ms": 1700000000000,
                        },
                        "1200": {"rows": "all of them", "updated_at_ms": 1700000000000},
                        "480": {"rows": [], "updated_at_ms": "yesterday"},
                    },
                    "agent-9": "measured, honest",
                }
            }
        )
    )

    assert read_geometry(tmp_path, "agent-7", 760) == (
        TranscriptRowGeometry(row_key="turn-1", start_offset=0, end_offset=3, height=160.0),
    )
    assert read_geometry(tmp_path, "agent-7", 1200) == ()
    assert read_geometry(tmp_path, "agent-7", 480) == ()
    assert read_geometry(tmp_path, "agent-9", 760) == ()


def test_a_file_that_is_not_this_store_reads_as_nothing_measured(tmp_path: Path) -> None:
    (tmp_path / _GEOMETRY_FILENAME).write_text('{"geometry_by_agent_id": []}')
    assert read_geometry(tmp_path, "agent-7", 760) == ()
    (tmp_path / _GEOMETRY_FILENAME).write_text("[]")
    assert read_geometry(tmp_path, "agent-7", 760) == ()
