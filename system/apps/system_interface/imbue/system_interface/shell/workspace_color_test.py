from pathlib import Path
from typing import Any

from workspace_themes.workspace_color import WorkspaceColorRecord

from imbue.system_interface.shell.workspace_color import WorkspaceColorKeeper
from imbue.system_interface.shell.workspace_color import color_label_of


def test_the_color_is_the_first_listed_agent_s_label_normalized() -> None:
    listing = {"agents": [{"labels": {"is_primary": "true"}}, {"labels": {"color": "#0B292B"}}]}

    assert color_label_of(listing) == "#0b292b"
    assert color_label_of({"agents": [{"labels": {"color": "teal"}}]}) is None
    assert color_label_of({"agents": []}) is None
    assert color_label_of(["not", "a", "listing"]) is None


class _Listing:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)

    def __call__(self, repo_root: Path) -> Any:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_changed_label_is_kept_and_announced_once(tmp_path: Path) -> None:
    announced: list[str] = []
    keeper = WorkspaceColorKeeper(
        repo_root=tmp_path,
        state_path=tmp_path / "workspace_color.json",
        on_changed=lambda: announced.append("changed"),
        list_agents=_Listing(
            {"agents": [{"labels": {"color": "#3c3d06"}}]},
            {"agents": [{"labels": {"color": "#3c3d06"}}]},
            OSError("mngr is not installed"),
            {"agents": []},
        ),
    )

    keeper.refresh()
    keeper.refresh()
    assert keeper.read() == WorkspaceColorRecord(hex="#3c3d06")
    assert announced == ["changed"]

    keeper.refresh()
    assert keeper.read() == WorkspaceColorRecord(hex="#3c3d06")

    keeper.refresh()
    assert keeper.read() == WorkspaceColorRecord(hex=None)
    assert announced == ["changed", "changed"]


def test_an_announced_pick_is_worn_at_once_and_kept_once_the_label_says_it(tmp_path: Path) -> None:
    announced: list[str | None] = []
    keeper = WorkspaceColorKeeper(
        repo_root=tmp_path,
        state_path=tmp_path / "workspace_color.json",
        on_changed=lambda: announced.append(keeper.read().hex),
        list_agents=_Listing(
            {"agents": [{"labels": {"color": "#492222"}}]},
            {"agents": [{"labels": {"color": "#9fbbd3"}}]},
        ),
        reread_interval_seconds=0.001,
        reread_window_seconds=1.0,
    )

    keeper.refresh_until("#9FBBD3")

    assert announced == ["#9fbbd3"]
    assert keeper.read() == WorkspaceColorRecord(hex="#9fbbd3")


def test_a_pick_the_label_never_comes_to_say_gives_way_to_the_label(tmp_path: Path) -> None:
    announced: list[str | None] = []
    keeper = WorkspaceColorKeeper(
        repo_root=tmp_path,
        state_path=tmp_path / "workspace_color.json",
        on_changed=lambda: announced.append(keeper.read().hex),
        list_agents=_Listing(*([{"agents": [{"labels": {"color": "#492222"}}]}] * 4)),
        reread_interval_seconds=0.001,
        reread_window_seconds=0.004,
    )

    keeper.refresh_until("#9fbbd3")

    assert announced == ["#9fbbd3", "#492222"]
    assert keeper.read() == WorkspaceColorRecord(hex="#492222")
