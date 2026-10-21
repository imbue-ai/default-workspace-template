class SkillsCatalogError(Exception):
    """Base exception for the skills catalog."""


class InvalidSkillNameError(SkillsCatalogError, ValueError):
    """A skill name that cannot be a directory directly under .agents/skills."""


class SkillNotFoundError(SkillsCatalogError, LookupError):
    """No skill of that name is in .agents/skills."""


class GitHistoryError(SkillsCatalogError, RuntimeError):
    """The workspace's git history could not be read."""
