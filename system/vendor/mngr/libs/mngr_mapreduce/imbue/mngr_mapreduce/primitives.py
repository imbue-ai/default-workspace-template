from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.primitives import SafeName


class NodeName(SafeName):
    """The name of a pipeline node; placements, bindings and outcomes are all keyed by it.

    A ``SafeName`` because the name becomes a segment of every agent name and
    every branch name the node produces, so a name that cannot be one is refused
    when the pipeline is built rather than when the first agent launches.
    """

    ...


class ArtifactName(NonEmptyStr):
    """The name of a pipeline artifact; nodes declare what they need and what they produce by it."""

    ...


class PartitionKey(NonEmptyStr):
    """The key a partition function assigns an upstream result to; one job runs per distinct key."""

    ...
