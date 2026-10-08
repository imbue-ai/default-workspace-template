from workspace_layout.primitives import WindowId

from imbue.system_interface.shell.primitives import mint_window_id


def test_minted_window_ids_are_distinct_and_in_the_fixed_shape() -> None:
    minted = mint_window_id()
    assert WindowId(str(minted)) == minted
    assert minted != mint_window_id()
