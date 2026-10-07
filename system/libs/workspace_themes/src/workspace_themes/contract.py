# The interface's side of the theme contract (docs/system/blueprint/workspace-themes/, section 4), as the validator
# checks a theme against it. The frontend's copy of the parts list is system/libs/workspace_ui/src/themes/parts.ts;
# contract_test.py keeps the two, and the color tokens here and in base.css, equal.

from typing import Final

from workspace_themes.primitives import ChromeSlot, ThemeId, TitleAlign

CONTRACT_VERSION: Final[int] = 1

STANDARD_THEME_ID: Final[ThemeId] = ThemeId("standard")

THEME_MANIFEST_FILENAME: Final[str] = "theme.toml"
# The generated bundle's name at the top of a theme's served folder (section 5.1), so no style file may take it.
BUNDLE_FILE_NAME: Final[str] = "theme.css"

# The roots a workspace's themes are found in, relative to the repo root, in the order they are scanned.
BUILTIN_THEMES_DIRECTORY: Final[str] = "system/themes"
WORKSPACE_THEMES_DIRECTORY: Final[str] = "themes"
# Where icons generated in a workspace for a built-in theme are kept, so the built-in folder stays as shipped.
GENERATED_ICONS_DIRECTORY: Final[str] = "data/.themes"

# The most themes a base chain holds, counting standard and the theme itself.
MAX_BASE_CHAIN_DEPTH: Final[int] = 4
MAX_THEME_FOLDER_BYTES: Final[int] = 16 * 1024 * 1024
MAX_STYLE_FILES_BYTES: Final[int] = 2 * 1024 * 1024

STYLE_FILE_SUFFIX: Final[str] = ".css"
APP_STYLES_DIRECTORY: Final[str] = "apps"
ALLOWED_FILE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".toml",
        ".md",
        ".json",
        ".css",
        ".woff2",
        ".woff",
        ".ttf",
        ".otf",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".jpg",
        ".svg",
        ".txt",
    }
)
LICENSE_FILE_PREFIXES: Final[tuple[str, ...]] = ("LICENSE", "COPYING")
# What a page may load from a theme folder over the wire: style files and what they reference. The manifest, the
# guide, and vendoring maps stay on disk.
SERVED_FILE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".css",
        ".woff2",
        ".woff",
        ".ttf",
        ".otf",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".jpg",
        ".svg",
    }
)

CORE_PARTS: Final[tuple[str, ...]] = (
    "desktop",
    "window",
    "window-frame",
    "title-bar",
    "window-icon",
    "window-title",
    "window-control",
    "window-content",
    "taskbar",
    "taskbar-entry",
    "launcher-field",
    "shortcut",
    "shortcut-label",
    "app-icon",
    "button",
    "field",
    "select",
    "menu",
    "menu-item",
    "menu-separator",
    "dialog",
    "dialog-header",
    "dialog-title",
    "dialog-actions",
    "tooltip",
    "badge",
    "tile",
    "list-row",
)

COLOR_TOKENS: Final[tuple[str, ...]] = (
    "--c-accent",
    "--c-accent-hover",
    "--c-accent-light",
    "--c-bg",
    "--c-bg-chat",
    "--c-bg-code",
    "--c-bg-composer",
    "--c-bg-sidebar",
    "--c-bg-user-bubble",
    "--c-border",
    "--c-border-code",
    "--c-border-faint",
    "--c-border-strong",
    "--c-danger",
    "--c-danger-bg",
    "--c-danger-border",
    "--c-danger-hover",
    "--c-fill-active",
    "--c-fill-hover",
    "--c-inverse-surface",
    "--c-inverse-surface-hover",
    "--c-shadow-overlay",
    "--c-shadow-raised",
    "--c-step-done",
    "--c-step-pending",
    "--c-stop-button",
    "--c-stop-button-hover",
    "--c-success",
    "--c-success-bg",
    "--c-success-border",
    "--c-surface",
    "--c-surface-secondary",
    "--c-text-faint",
    "--c-text-on-accent",
    "--c-text-primary",
    "--c-text-secondary",
    "--c-warning",
    "--c-warning-bg",
)

TYPE_TOKENS: Final[tuple[str, ...]] = (
    "--font-sans",
    "--font-mono",
    "--font-display",
    "--font-size-heading-lg",
    "--font-size-heading",
    "--font-size-body",
    "--font-size-row",
    "--font-size-helper",
    "--weight-regular",
    "--weight-semibold",
    "--weight-bold",
)

SHAPE_TOKENS: Final[tuple[str, ...]] = (
    "--radius-sm",
    "--radius-md",
    "--radius-lg",
    "--radius-xl",
)

MOTION_TOKENS: Final[tuple[str, ...]] = ("--dur-fast", "--dur-base", "--dur-slow")

DESKTOP_TOKENS: Final[tuple[str, ...]] = (
    "--desk-window-radius",
    "--desk-window-shadow",
    "--desk-taskbar-surface",
    "--desk-taskbar-entry-shadow",
    "--desk-backdrop",
    "--desk-default-wallpaper",
    "--desk-icon-radius",
    "--desk-icon-shadow",
    "--desk-icon-shadow-lifted",
    "--desk-shortcut-label-shadow",
    "--desk-toast-radius",
    "--desk-phone-sheet-radius",
    "--desk-phone-tile-radius",
)

# The desktop's metrics a theme may set, each to a pixel length within its range: the shell's geometry reads them.
METRIC_TOKEN_PIXEL_RANGES: Final[dict[str, tuple[int, int]]] = {
    "--desk-title-bar-height": (16, 64),
    "--desk-taskbar-height": (24, 96),
    "--desk-window-control-size": (12, 48),
    "--desk-taskbar-entry-size": (16, 64),
}

TERMINAL_TOKENS: Final[tuple[str, ...]] = (
    "--term-background",
    "--term-foreground",
    "--term-cursor",
    "--term-selection",
    *(f"--term-ansi-{index}" for index in range(16)),
)

CONTRACT_TOKENS: Final[frozenset[str]] = frozenset(
    (
        *COLOR_TOKENS,
        *TYPE_TOKENS,
        *SHAPE_TOKENS,
        *MOTION_TOKENS,
        *DESKTOP_TOKENS,
        *METRIC_TOKEN_PIXEL_RANGES,
        *TERMINAL_TOKENS,
    )
)

# A theme's own helper properties (a bevel it reuses, a stripe pattern) carry this prefix.
PRIVATE_TOKEN_PREFIX: Final[str] = "--theme-"

# Attribute selectors a theme may not use: they reach into an app's private markup.
FORBIDDEN_SELECTOR_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {"class", "id", "style"}
)

# The at-rules a style file may contain. @import and @layer are the bundle's business (section 5.1).
ALLOWED_AT_RULES: Final[frozenset[str]] = frozenset(
    {"media", "supports", "container", "font-face", "keyframes", "-webkit-keyframes"}
)
# The at-rules whose block holds further rules rather than declarations or keyframes.
GROUPING_AT_RULES: Final[frozenset[str]] = frozenset({"media", "supports", "container"})

# The standard title bar (section 4.3).
STANDARD_TITLE_ALIGN: Final[TitleAlign] = TitleAlign.START
STANDARD_LEADING_SLOTS: Final[tuple[ChromeSlot, ...]] = (
    ChromeSlot.ICON,
    ChromeSlot.TITLE,
    ChromeSlot.REFRESH,
    ChromeSlot.MENU,
)
STANDARD_TRAILING_SLOTS: Final[tuple[ChromeSlot, ...]] = (
    ChromeSlot.MINIMIZE,
    ChromeSlot.MAXIMIZE,
    ChromeSlot.CLOSE,
)
OPTIONAL_CHROME_SLOTS: Final[frozenset[ChromeSlot]] = frozenset({ChromeSlot.ICON})

# The generic program icon's file name, without its suffix.
FALLBACK_ICON_STEM: Final[str] = "app"
ICONS_DIRECTORY: Final[str] = "icons"
