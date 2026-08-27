from versioning.data_types import TrailerBlock
from versioning.data_types import VersionKind
from versioning.trailers import parse_trailer_block
from versioning.trailers import serialize_trailer_block


def test_parse_trailer_block_reads_full_block_from_commit_message() -> None:
    message = (
        "science-explorer: article pages now list their sources\n\n"
        "Adds a sources section.\n\n"
        "Versioning-App: science-explorer\n"
        "Versioning-Kind: change\n"
        "Versioning-Request: article pages now list their sources\n"
    )

    block = parse_trailer_block(message)

    assert block.app_name == "science-explorer"
    assert block.kind == VersionKind.CHANGE
    assert block.request == "article pages now list their sources"
    assert block.restored_from_sha is None
    assert block.ported_from_sha is None


def test_parse_trailer_block_returns_empty_block_for_plain_message() -> None:
    block = parse_trailer_block("fix a bug\n\nlonger explanation")

    assert block == TrailerBlock()


def test_parse_trailer_block_ignores_unknown_kind_value() -> None:
    block = parse_trailer_block("subject\n\nVersioning-Kind: bananas\n")

    assert block.kind is None


def test_serialize_then_parse_round_trips_every_field() -> None:
    original = TrailerBlock(
        app_name="news",
        request="the digest now arrives at 7am",
        kind=VersionKind.RESTORE,
        restored_from_sha="a" * 40,
        ported_from_sha="b" * 40,
    )

    reparsed = parse_trailer_block("subject\n\n" + serialize_trailer_block(original))

    assert reparsed == original


def test_serialize_trailer_block_omits_absent_fields() -> None:
    rendered = serialize_trailer_block(TrailerBlock(app_name="news", kind=VersionKind.BUILD))

    assert rendered == "Versioning-App: news\nVersioning-Kind: build"
