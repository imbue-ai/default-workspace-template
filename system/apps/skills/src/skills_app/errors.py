class SkillsAppError(Exception):
    """Base error for the Skills app."""


class SkillIndexUnavailableError(SkillsAppError, ConnectionError):
    """The online skills index could not be asked, or did not answer a search."""


class MalformedSkillIndexAnswerError(SkillIndexUnavailableError, ValueError):
    """The online skills index answered something that is not a search result."""
