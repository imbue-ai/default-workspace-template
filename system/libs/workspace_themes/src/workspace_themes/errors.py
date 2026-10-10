class WorkspaceThemesError(Exception):
    """Base error for everything in the workspace_themes library."""


class InvalidThemeValueError(WorkspaceThemesError, ValueError):
    """A theme manifest value does not satisfy its rule."""


class ThemeNotFoundError(WorkspaceThemesError, KeyError):
    """No available theme has the requested id."""

    def __init__(self, theme_id: str) -> None:
        self.theme_id = theme_id
        super().__init__(f"no available theme {theme_id!r}")


class ThemeFileNotFoundError(WorkspaceThemesError, FileNotFoundError):
    """A theme file was requested that the theme does not have, or may not serve."""


class ThemeFolderExistsError(WorkspaceThemesError, FileExistsError):
    """A new theme's folder is already taken."""


class IconImageError(WorkspaceThemesError, ValueError):
    """An icon image cannot be read, or cannot be made to fit its theme's limits."""
