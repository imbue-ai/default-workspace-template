from routines_app.fix_chat import chat_id_from_output


def test_the_chat_id_is_read_from_the_last_json_line_of_the_create() -> None:
    stdout = 'warming up\n{"chat_id": "agent-1", "name": "Fix News sync", "display_name": "Fix News sync"}\n'
    assert chat_id_from_output(stdout) == "agent-1"


def test_a_create_that_named_no_chat_answers_none() -> None:
    assert chat_id_from_output("nothing useful\n") is None
    assert chat_id_from_output('{"name": "x"}\n') is None
    assert chat_id_from_output('{"chat_id": ""}\n') is None
