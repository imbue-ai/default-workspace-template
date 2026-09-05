import pytest
from app_instances.primitives import InstanceKey
from app_manifest.primitives import AppName

from imbue.system_interface.shell.errors import InvalidAddressError
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import ProjectId
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.primitives import address_for
from imbue.system_interface.shell.primitives import mint_tab_id


def test_an_address_names_an_app_and_optionally_one_instance() -> None:
    single = Address("app:files")
    assert single.app == AppName("files")
    assert single.key is None
    assert single.render() == "app:files"

    keyed = Address("app:terminal?instance=terminal-3")
    assert keyed.app == AppName("terminal")
    assert keyed.key == InstanceKey("terminal-3")
    assert address_for(AppName("terminal"), InstanceKey("terminal-3")) == keyed
    assert address_for(AppName("files"), None) == single


@pytest.mark.parametrize(
    "spelling",
    ["files", "chat:abc", "terminal:terminal-1", "service:files", "app:", "app:files?key=1", "app:files?instance="],
)
def test_the_old_spellings_and_malformed_addresses_are_refused(spelling: str) -> None:
    with pytest.raises(InvalidAddressError):
        Address(spelling)


def test_view_ids_and_project_ids() -> None:
    assert ViewId("everything") == "everything"
    assert ProjectId("research-2") == "research-2"
    with pytest.raises(InvalidShellValueError):
        ProjectId("everything")
    with pytest.raises(InvalidShellValueError):
        ViewId("Not A Slug")


def test_tab_ids_are_minted_in_the_fixed_shape() -> None:
    minted = mint_tab_id()
    assert TabId(str(minted)) == minted
    assert minted != mint_tab_id()
    with pytest.raises(InvalidShellValueError):
        TabId("panel-1")


def test_client_ids_are_filename_safe() -> None:
    assert ClientId("3f2a-desktop.1") == "3f2a-desktop.1"
    with pytest.raises(InvalidShellValueError):
        ClientId("../escape")
    with pytest.raises(InvalidShellValueError):
        ClientId("")
