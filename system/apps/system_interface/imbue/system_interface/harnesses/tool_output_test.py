"""Unit tests for the shared tool-output rules.

The two tk rules live here rather than in any harness: every harness asks the same two
questions of a command it has already located, so the answers must not be able to differ
between them.
"""

from imbue.system_interface.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.system_interface.harnesses.tool_output import is_tk_lifecycle_anywhere

# --- the two tk rules, and the asymmetry between them -------------------------------------
# Both used to be reimplemented per harness (four copies of the verb set, the parser import
# and the segment walk). These pin the property those copies were free to drift on.


def test_the_hide_rule_is_strict_and_the_truncation_rule_is_broad() -> None:
    """A batched command still does real work, so it must RENDER (not hide) -- but its input
    must survive truncation so the progress view can read the plan out of it. Over-preserving
    input is harmless; over-hiding work silently swallows it."""
    batched = "cd /code && tk start s1"
    assert is_pure_tk_lifecycle_command(batched) is False, "must not hide: it also runs cd"
    assert is_tk_lifecycle_anywhere(batched) is True, "must not truncate: it carries step data"


def test_a_pure_invocation_satisfies_both_rules() -> None:
    command = 'tk create --step "Build the thing"'
    assert is_pure_tk_lifecycle_command(command) is True
    assert is_tk_lifecycle_anywhere(command) is True


def test_a_tk_verb_quoted_inside_another_command_is_neither() -> None:
    """Shell-aware, not a substring match: the shared shlex parser keeps a mention inside a
    quoted argument from being read as a real lifecycle call."""
    command = 'echo "remember to tk close s1"'
    assert is_pure_tk_lifecycle_command(command) is False
    assert is_tk_lifecycle_anywhere(command) is False
