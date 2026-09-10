from imbue.imbue_common.primitives import NonEmptyStr


class ArtifactName(NonEmptyStr):
    """The name of a pipeline artifact; steps and outputs refer to artifacts by it."""

    ...
