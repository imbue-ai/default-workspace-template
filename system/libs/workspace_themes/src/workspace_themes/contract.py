# The interface's side of the theme contract (docs/system/blueprint/workspace-themes/, section 4), as the validator
# checks a theme against it. The frontend's copy of the parts list is system/libs/workspace_ui/src/themes/parts.ts;
# the shell's theme_contract_test.py keeps the two, and the color tokens here and in base.css, equal.

from collections.abc import Mapping
from typing import Final

from workspace_themes.primitives import ChromeSlot, ThemeId, TitleAlign

CONTRACT_VERSION: Final[int] = 2

STANDARD_THEME_ID: Final[ThemeId] = ThemeId("standard")

THEME_MANIFEST_FILENAME: Final[str] = "theme.toml"
# The generated bundle's name at the top of a theme's served folder (section 5.1), so no style file may take it.
BUNDLE_FILE_NAME: Final[str] = "theme.css"

# The roots a workspace's themes are found in, relative to the repo root, in the order they are scanned.
BUILTIN_THEMES_DIRECTORY: Final[str] = "system/themes"
WORKSPACE_THEMES_DIRECTORY: Final[str] = "themes"
# Where the shell keeps the workspace color it last read from Imbue Studio's label, for every app to serve.
WORKSPACE_COLOR_STATE_PATH: Final[str] = "data/.state/workspace_color.json"
# Where every app serves the keys that color stands for, beside the theme bundle.
WORKSPACE_COLOR_ROUTE: Final[str] = "/_static/workspace_color.css"
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
# The CSS functions whose string arguments are URLs: in image-set() a bare string is an image, as in url().
URL_FUNCTION_NAMES: Final[frozenset[str]] = frozenset(
    {"url", "src", "image-set", "-webkit-image-set"}
)
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
    "avatar",
)

# A color is one key per ramp; every other color is computed from the keys (docs/system/blueprint/brand-design-system/).
RAMP_NAMES: Final[tuple[str, ...]] = (
    "neutral",
    "primary",
    "accent",
    "success",
    "warn",
    "danger",
    "wallpaper",
)
RAMP_STEP_COUNT: Final[int] = 12
# The roles that each wear their ramp's key as a solid surface, with a subtle tint beside it.
SURFACE_ROLE_NAMES: Final[tuple[str, ...]] = (
    "primary",
    "accent",
    "success",
    "warn",
    "danger",
)

KEY_TOKENS: Final[tuple[str, ...]] = tuple(f"--c-{ramp}-key" for ramp in RAMP_NAMES)

RAMP_TOKENS: Final[tuple[str, ...]] = tuple(
    f"--c-{ramp}-{step}"
    for ramp in RAMP_NAMES
    for step in range(1, RAMP_STEP_COUNT + 1)
)

ROLE_TOKENS: Final[tuple[str, ...]] = (
    "--c-body-surface",
    "--c-body-surface-secondary",
    "--c-body-surface-raised",
    "--c-body-content",
    "--c-body-content-secondary",
    "--c-body-content-tertiary",
    "--c-overlay-surface",
    "--c-overlay-scrim",
    "--c-inverse-surface",
    "--c-inverse-content",
    "--c-inverse-content-secondary",
    "--c-inverse-content-tertiary",
    *(
        f"--c-{role}-{suffix}"
        for role in SURFACE_ROLE_NAMES
        for suffix in ("surface", "content", "subtle-surface", "subtle-content")
    ),
    "--c-fill-faint",
    "--c-fill-hover",
    "--c-fill-active",
    "--c-border-default",
    "--c-border-subtle",
    "--c-border-focus",
    "--c-wallpaper-surface",
    "--c-wallpaper-mark",
    "--c-shadow-raised",
    "--c-shadow-overlay",
)

COLOR_TOKENS: Final[tuple[str, ...]] = (*KEY_TOKENS, *RAMP_TOKENS, *ROLE_TOKENS)

# The dials of the computed hover and active shades, which are never tokens themselves: the contrast step a hover
# takes, how far toward black that step is measured from, and how much darker a background window's header is.
STATE_TOKENS: Final[tuple[str, ...]] = (
    "--state-ratio",
    "--state-flare",
    "--state-inactive",
)

# Contract 1's color tokens and what each became in contract 2; None for the computed hover and active shades,
# which are no longer tokens. The migration rewrites a contract-1 theme with it, and the validator names the new
# token when a contract-2 theme still sets an old one.
CONTRACT_1_RENAMES: Final[Mapping[str, str | None]] = {
    "--c-bg": "--c-body-surface",
    "--c-bg-sidebar": "--c-body-surface-secondary",
    "--c-bg-chat": "--c-body-surface-raised",
    "--c-bg-composer": "--c-body-surface-raised",
    "--c-bg-code": "--c-body-surface-secondary",
    "--c-bg-user-bubble": "--c-primary-subtle-surface",
    "--c-bubble-other": "--c-body-surface-secondary",
    "--c-surface": "--c-body-surface-raised",
    "--c-surface-secondary": "--c-body-surface-secondary",
    "--c-text-primary": "--c-body-content",
    "--c-text-secondary": "--c-body-content-secondary",
    "--c-text-faint": "--c-body-content-tertiary",
    "--c-text-on-accent": "--c-accent-content",
    "--c-border": "--c-border-default",
    "--c-border-faint": "--c-border-subtle",
    "--c-border-strong": "--c-border-default",
    "--c-border-code": "--c-border-subtle",
    "--c-accent": "--c-accent-surface",
    "--c-accent-hover": None,
    "--c-accent-light": "--c-accent-subtle-surface",
    "--c-stop-button": "--c-inverse-surface",
    "--c-stop-button-hover": None,
    "--c-danger": "--c-danger-surface",
    "--c-danger-hover": None,
    "--c-danger-bg": "--c-danger-subtle-surface",
    "--c-danger-border": "--c-danger-4",
    "--c-warning": "--c-warn-surface",
    "--c-warning-bg": "--c-warn-subtle-surface",
    "--c-success": "--c-success-surface",
    "--c-success-bg": "--c-success-subtle-surface",
    "--c-success-border": "--c-success-4",
    "--c-step-done": "--c-accent-subtle-content",
    "--c-step-pending": "--c-neutral-6",
    "--c-inverse-surface-hover": None,
}

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
    "--desk-avatar-color",
    "--desk-avatar-shadow-color",
)

# The desktop's metrics a theme may set, each to a pixel length within its range: the shell's geometry reads them.
PIXEL_RANGE_BY_METRIC_TOKEN: Final[dict[str, tuple[int, int]]] = {
    "--desk-title-bar-height": (16, 64),
    "--desk-window-radius": (0, 32),
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
        *STATE_TOKENS,
        *TYPE_TOKENS,
        *SHAPE_TOKENS,
        *MOTION_TOKENS,
        *DESKTOP_TOKENS,
        *PIXEL_RANGE_BY_METRIC_TOKEN,
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

# The attribute the page kit's script sets on <html>: a page built as plain HTML, whose bare controls take their
# parts' styles (section 5.3).
PAGE_KIT_ROOT_ATTRIBUTE: Final[str] = "data-page-kit"
# The bare elements on a page-kit page that take each part's styles, as selectors of zero specificity beyond the
# element itself: an app's own class rules still win over the theme's, and a widget's own controls keep their look
# wherever its stylesheet says otherwise.
_TEXT_INPUT_TYPES: Final[str] = (
    '[type="text"], [type="search"], [type="email"], [type="url"], [type="password"], [type="number"], '
    '[type="tel"], :not([type])'
)
BARE_CONTROLS_BY_PART: Final[Mapping[str, tuple[str, ...]]] = {
    "button": (":is(button)",),
    "field": (f":is(input):where({_TEXT_INPUT_TYPES})", ":is(textarea)"),
    "select": (":is(select)",),
}

# The standard title bar (section 4.3; the multiplayer spec, section 9.1): the window menu and Refresh lead, the
# icon and title sit centred over the bar.
STANDARD_TITLE_ALIGN: Final[TitleAlign] = TitleAlign.CENTER
STANDARD_LEADING_SLOTS: Final[tuple[ChromeSlot, ...]] = (
    ChromeSlot.MENU,
    ChromeSlot.REFRESH,
    ChromeSlot.ICON,
    ChromeSlot.TITLE,
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
