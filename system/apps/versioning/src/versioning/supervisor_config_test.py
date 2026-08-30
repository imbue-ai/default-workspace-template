from versioning.supervisor_config import extract_program_block
from versioning.supervisor_config import replace_program_block

_CONFIG = """[supervisord]
nodaemon=true

[program:curio]
command=uv run curio --icon-file icon.svg
directory=/home/user/workspace

[program:files]
command=uv run files
"""


def test_extract_program_block_stops_at_the_next_section() -> None:
    block = extract_program_block(_CONFIG, "curio")

    assert block is not None
    assert block.startswith("[program:curio]\n")
    assert "command=uv run curio --icon-file icon.svg\n" in block
    assert "[program:files]" not in block


def test_extract_program_block_reads_a_trailing_section_to_the_end() -> None:
    block = extract_program_block(_CONFIG, "files")

    assert block == "[program:files]\ncommand=uv run files\n"


def test_extract_program_block_is_none_for_an_unknown_program() -> None:
    assert extract_program_block(_CONFIG, "nonesuch") is None


def test_replace_program_block_leaves_every_other_program_untouched() -> None:
    replaced = replace_program_block(_CONFIG, "curio", "[program:curio]\ncommand=uv run curio\n\n")

    assert replaced is not None
    assert "--icon-file" not in replaced
    assert "command=uv run curio\n" in replaced
    assert "[supervisord]\nnodaemon=true\n" in replaced
    assert "[program:files]\ncommand=uv run files\n" in replaced


def test_replace_program_block_keeps_the_next_section_on_its_own_line() -> None:
    replaced = replace_program_block(_CONFIG, "curio", "[program:curio]\ncommand=uv run curio")

    assert replaced is not None
    assert "\n[program:files]" in replaced


def test_replace_program_block_is_none_for_an_unknown_program() -> None:
    assert replace_program_block(_CONFIG, "nonesuch", "[program:nonesuch]\n") is None
