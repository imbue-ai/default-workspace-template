from pydantic import Field

from screen.band_sweep import ProcessTreeInterface


class FakeProcessTree(ProcessTreeInterface):
    """A recorded process tree: each pid's parent, executable name and ``oom_score_adj``, with every write kept."""

    parents: dict[int, int] = Field(default_factory=dict, description="Each pid's parent pid")
    names: dict[int, str] = Field(default_factory=dict, description="Each pid's executable name")
    adjustments: dict[int, int] = Field(default_factory=dict, description="Each pid's oom_score_adj")
    writes: list[tuple[int, int]] = Field(default_factory=list, description="Every (pid, adj) written, in order")

    def descendants(self, pid: int) -> list[int]:
        found: list[int] = []
        frontier = [pid]
        while frontier:
            current = frontier.pop()
            children = sorted(child for child, parent in self.parents.items() if parent == current)
            found.extend(children)
            frontier.extend(children)
        return found

    def executable_name(self, pid: int) -> str | None:
        return self.names.get(pid)

    def read_adj(self, pid: int) -> int | None:
        return self.adjustments.get(pid)

    def write_adj(self, pid: int, adj: int) -> bool:
        self.adjustments[pid] = adj
        self.writes.append((pid, adj))
        return True
