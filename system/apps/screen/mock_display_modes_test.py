from pydantic import Field

from screen.data_types import DisplayModes
from screen.data_types import ModeAddition
from screen.interfaces import DisplayModesInterface


class FakeDisplayModes(DisplayModesInterface):
    """An X server advertising the modes the test seeds; added modes join the list."""

    output: str | None = Field(default="DUMMY0", description="The connected output")
    mode_names: list[str] = Field(
        default_factory=lambda: ["1920x1080_60", "1600x900_60", "1280x720_60"], description="The advertised modes"
    )
    added: list[ModeAddition] = Field(default_factory=list, description="Every addition, in order")

    def query(self) -> DisplayModes:
        return DisplayModes(output=self.output, mode_names=tuple(self.mode_names))

    def add_mode(self, addition: ModeAddition) -> None:
        self.added.append(addition)
        self.mode_names.append(addition.modeline.name)
