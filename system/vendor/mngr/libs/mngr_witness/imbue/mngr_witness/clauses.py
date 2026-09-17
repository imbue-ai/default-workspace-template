from typing import Final
from typing import assert_never

from imbue.imbue_common.pure import pure
from imbue.mngr_behaviors.data_types import BehaviorUnit
from imbue.mngr_behaviors.data_types import BehaviorUnitKind

_THEN_KEYWORD: Final[str] = "Then"
_CONTINUATION_KEYWORDS: Final[frozenset[str]] = frozenset({"And", "But"})


@pure
def unit_clauses(unit: BehaviorUnit) -> tuple[str, ...]:
    """The observable claims of a unit, as written: a Rule's name, or a scenario's Then steps and their And/But continuations.

    Given and When steps are setup and action, not claims, so an And after a
    When is not a clause and a Then after that When opens a new run of clauses.
    """
    match unit.kind:
        case BehaviorUnitKind.RULE:
            return (unit.name,)
        case BehaviorUnitKind.SCENARIO | BehaviorUnitKind.SCENARIO_OUTLINE:
            clauses: list[str] = []
            is_in_then_run = False
            for step in unit.steps:
                if step.keyword == _THEN_KEYWORD:
                    is_in_then_run = True
                    clauses.append(f"{step.keyword} {step.text}")
                elif step.keyword in _CONTINUATION_KEYWORDS and is_in_then_run:
                    clauses.append(f"{step.keyword} {step.text}")
                else:
                    is_in_then_run = False
            return tuple(clauses)
        case _ as unreachable:  # pragma: no cover
            assert_never(unreachable)
