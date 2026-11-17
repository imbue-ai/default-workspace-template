from screen.identity import display_name_for_identity
from workspace_layout.identity import ANONYMOUS_OWNER
from workspace_layout.identity import RequestIdentity


def test_display_name_is_the_email_local_part_else_the_role() -> None:
    assert display_name_for_identity(RequestIdentity(owner=False, user_id="u2", email="mia@example.com")) == "mia"
    assert display_name_for_identity(RequestIdentity(owner=True, user_id="u1", email="josh@imbue.com")) == "josh"
    assert display_name_for_identity(ANONYMOUS_OWNER) == "Owner"
    assert display_name_for_identity(RequestIdentity(owner=False)) == "Guest"
    assert display_name_for_identity(RequestIdentity(owner=False, email="@x")) == "Guest"


def test_a_long_local_part_is_cut_to_what_neko_shows() -> None:
    name = display_name_for_identity(RequestIdentity(owner=True, email=f"{'j' * 80}@imbue.com"))
    assert len(name) == 64
