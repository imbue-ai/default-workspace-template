import posixpath
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.parse import unquote
from xml.etree import ElementTree

import tinycss2
from app_manifest.primitives import AppName
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from PIL import Image, UnidentifiedImageError
from pydantic import Field, ValidationError
from tinycss2 import ast

from workspace_themes.contract import (
    ALLOWED_AT_RULES,
    ALLOWED_FILE_SUFFIXES,
    APP_STYLES_DIRECTORY,
    BUNDLE_FILE_NAME,
    CONTRACT_TOKENS,
    CONTRACT_VERSION,
    CORE_PARTS,
    FALLBACK_ICON_STEM,
    FORBIDDEN_SELECTOR_ATTRIBUTES,
    GROUPING_AT_RULES,
    ICONS_DIRECTORY,
    LICENSE_FILE_PREFIXES,
    MAX_STYLE_FILES_BYTES,
    MAX_THEME_FOLDER_BYTES,
    OPTIONAL_CHROME_SLOTS,
    PIXEL_RANGE_BY_METRIC_TOKEN,
    PRIVATE_TOKEN_PREFIX,
    SERVED_FILE_SUFFIXES,
    STANDARD_THEME_ID,
    STYLE_FILE_SUFFIX,
    THEME_MANIFEST_FILENAME,
    URL_FUNCTION_NAMES,
)
from workspace_themes.data_types import ResolvedChrome, ThemeIconSpec, ThemeManifest
from workspace_themes.primitives import ChromeSlot, IconFormat

_DATA_PART_ATTRIBUTE: Final[str] = "data-part"
_EXACT_MATCH_OPERATOR: Final[str] = "="
_NAMESPACE_SEPARATOR: Final[str] = "|"
# The functions that put a value in at run time, where the validator cannot read it.
_SUBSTITUTION_FUNCTION_NAMES: Final[frozenset[str]] = frozenset(
    {"var", "env", "attr", "if"}
)
# What a browser's URL parser removes from a URL before reading it: tab and newline anywhere, and C0 controls and
# spaces at either end.
_URL_REMOVED_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[\t\n\r]")
_URL_TRIMMED_CHARACTERS: Final[str] = "".join(chr(code) for code in range(0x21))


class StyleSheetContext(FrozenModel):
    """What a style file is checked against."""

    label: str = Field(description="How problems name the file")
    folder: Path = Field(description="The theme folder")
    file_directory: str = Field(
        description="The file's directory inside the folder, '' at its top"
    )
    allowed_parts: frozenset[str] = Field(
        description="The data-part values the file may name"
    )
    scoping_part_prefix: str | None = Field(
        default=None,
        description="For an app's overlay, '<app>.': every selector names one of that app's parts",
    )


@pure
def describe_manifest_validation_error(error: ValidationError) -> list[str]:
    problems: list[str] = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        problems.append(f"theme.toml: {location}: {detail['msg']}")
    return problems


def read_theme_manifest(folder: Path) -> tuple[ThemeManifest | None, list[str]]:
    """The folder's parsed theme.toml, or None and why it could not be read."""
    manifest_path = folder / THEME_MANIFEST_FILENAME
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        return None, [f"theme.toml cannot be read: {error}"]
    except UnicodeDecodeError as error:
        return None, [f"theme.toml is not UTF-8 text: {error}"]
    try:
        data: dict[str, Any] = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as error:
        return None, [f"theme.toml is not valid TOML: {error}"]
    try:
        manifest = ThemeManifest.model_validate(data)
    except ValidationError as error:
        return None, describe_manifest_validation_error(error)
    return manifest, []


@pure
def check_manifest_rules(manifest: ThemeManifest, folder_name: str) -> list[str]:
    problems: list[str] = []
    if manifest.id != folder_name:
        problems.append(
            f"theme.toml: id {str(manifest.id)!r} must equal the folder's name {folder_name!r}"
        )
    if manifest.contract != CONTRACT_VERSION:
        problems.append(
            f"theme.toml: contract {manifest.contract} is not one this workspace reads (it reads {CONTRACT_VERSION})"
        )
    if manifest.id == STANDARD_THEME_ID:
        if manifest.base is not None:
            problems.append("theme.toml: the standard theme has no base")
        if manifest.icons is None:
            problems.append("theme.toml: the standard theme must declare [icons]")
    elif manifest.base == manifest.id:
        problems.append("theme.toml: a theme cannot be its own base")
    else:
        pass
    return problems


@pure
def check_resolved_chrome(chrome: ResolvedChrome) -> list[str]:
    slots = [*chrome.leading, *chrome.trailing]
    problems: list[str] = []
    duplicated = sorted({str(slot) for slot in slots if slots.count(slot) > 1})
    if duplicated:
        problems.append(
            f"chrome: {', '.join(duplicated)} appear more than once across leading and trailing"
        )
    missing = sorted(
        str(slot)
        for slot in ChromeSlot
        if slot not in slots and slot not in OPTIONAL_CHROME_SLOTS
    )
    if missing:
        problems.append(
            f"chrome: every control stays on the title bar, but {', '.join(missing)} is missing"
        )
    return problems


def _is_license_file(name: str) -> bool:
    return name.startswith(LICENSE_FILE_PREFIXES)


def check_folder_files(folder: Path) -> list[str]:
    """The folder's files are kinds a theme may contain, none is a link or takes the bundle's name, and the folder
    is within its size limit."""
    problems: list[str] = []
    total_bytes = 0
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink():
            problems.append(f"{relative}: a theme folder may not contain links")
            continue
        if not path.is_file():
            continue
        # A hidden file, or a file in a hidden folder, of a kind a theme never serves (a .DS_Store, an editor's swap
        # file, a .git folder) cannot reach a page.
        if (
            any(part.startswith(".") for part in path.relative_to(folder).parts)
            and path.suffix.lower() not in ALLOWED_FILE_SUFFIXES
        ):
            continue
        if relative == BUNDLE_FILE_NAME:
            problems.append(
                f"{relative}: {BUNDLE_FILE_NAME} is the bundle's name; call the style file something else"
            )
        if path.suffix.lower() not in ALLOWED_FILE_SUFFIXES and not _is_license_file(
            path.name
        ):
            problems.append(
                f"{relative}: a theme may not contain {path.suffix or 'extensionless'} files"
            )
        total_bytes += path.stat().st_size
    if total_bytes > MAX_THEME_FOLDER_BYTES:
        problems.append(
            f"the folder is {total_bytes} bytes, more than the {MAX_THEME_FOLDER_BYTES} a theme may be"
        )
    return problems


def _resolve_in_folder(
    folder: Path, directory: str, reference: str
) -> tuple[str, Path] | None:
    """A reference made from a file in ``directory`` as a folder-relative path, or None when it leaves the folder."""
    joined = posixpath.normpath(posixpath.join(directory, reference))
    if joined.startswith("../") or joined == ".." or posixpath.isabs(joined):
        return None
    return joined, folder / joined


@pure
def _strip_query_and_fragment(reference: str) -> str:
    for separator in ("?", "#"):
        reference = reference.split(separator, 1)[0]
    return reference


def _check_url(reference: str, context: StyleSheetContext, line: int) -> list[str]:
    stripped = _URL_REMOVED_CHARACTERS.sub("", reference).strip(_URL_TRIMMED_CHARACTERS)
    if stripped.startswith("data:"):
        return []
    # A URL parser reads a backslash as a slash, so \\host/a.png would name another host.
    if "\\" in stripped:
        return [
            f"{context.label}:{line}: url({stripped}) has a backslash; write a theme's paths with /"
        ]
    if (
        "://" in stripped
        or stripped.startswith("//")
        or ":" in stripped.split("/", 1)[0]
    ):
        return [
            f"{context.label}:{line}: url({stripped}) is remote; a theme's files come from its own folder"
        ]
    # A browser reads a percent-encoded dot segment (%2e%2e) as .., and the server decodes the path it serves.
    path_part = unquote(_strip_query_and_fragment(stripped))
    if not path_part:
        return [f"{context.label}:{line}: url() names no file"]
    resolved = _resolve_in_folder(context.folder, context.file_directory, path_part)
    if resolved is None:
        return [f"{context.label}:{line}: url({stripped}) leaves the theme folder"]
    relative, path = resolved
    if not path.is_file():
        return [
            f"{context.label}:{line}: url({stripped}) names {relative}, which does not exist"
        ]
    if PurePosixPath(relative).suffix.lower() not in SERVED_FILE_SUFFIXES:
        return [
            f"{context.label}:{line}: url({stripped}) names {relative}, a kind of file a theme does not serve"
        ]
    return []


def _check_url_function_arguments(
    tokens: Iterable[ast.Node], context: StyleSheetContext
) -> list[str]:
    """A URL a function takes is written out, never put in with var(), env(), attr(), or if(), so it can be checked."""
    problems: list[str] = []
    for token in tokens:
        if isinstance(token, ast.FunctionBlock):
            if token.lower_name in URL_FUNCTION_NAMES:
                problems.extend(
                    f"{context.label}:{argument.source_line}: {token.lower_name}() takes {argument.lower_name}(); "
                    "write the URL out, so it can be checked"
                    for argument in token.arguments
                    if isinstance(argument, ast.FunctionBlock)
                    and argument.lower_name in _SUBSTITUTION_FUNCTION_NAMES
                )
            problems.extend(_check_url_function_arguments(token.arguments, context))
        elif isinstance(
            token,
            (ast.ParenthesesBlock, ast.SquareBracketsBlock, ast.CurlyBracketsBlock),
        ):
            problems.extend(_check_url_function_arguments(token.content, context))
        else:
            continue
    return problems


def _urls_in(tokens: Iterable[ast.Node]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for token in tokens:
        if isinstance(token, ast.URLToken):
            found.append((token.value, token.source_line))
        elif isinstance(token, ast.FunctionBlock):
            if token.lower_name in URL_FUNCTION_NAMES:
                strings = [
                    argument
                    for argument in token.arguments
                    if isinstance(argument, ast.StringToken)
                ]
                found.extend(
                    (argument.value, argument.source_line) for argument in strings
                )
            found.extend(_urls_in(token.arguments))
        elif isinstance(
            token,
            (ast.ParenthesesBlock, ast.SquareBracketsBlock, ast.CurlyBracketsBlock),
        ):
            found.extend(_urls_in(token.content))
        else:
            continue
    return found


@pure
def _significant(tokens: Iterable[ast.Node]) -> list[ast.Node]:
    return [
        token
        for token in tokens
        if not isinstance(token, (ast.WhitespaceToken, ast.Comment))
    ]


def _check_metric_value(
    name: str, value: Sequence[ast.Node], context: StyleSheetContext, line: int
) -> list[str]:
    low, high = PIXEL_RANGE_BY_METRIC_TOKEN[name]
    significant = _significant(value)
    number = significant[0] if len(significant) == 1 else None
    if not isinstance(number, ast.DimensionToken) or number.lower_unit != "px":
        return [
            f"{context.label}:{line}: {name} must be a length in px, from {low}px to {high}px"
        ]
    if not low <= number.value <= high:
        return [
            f"{context.label}:{line}: {name} is {number.value:g}px, outside {low}px to {high}px"
        ]
    return []


def _check_declarations(
    content: Sequence[ast.Node], context: StyleSheetContext
) -> list[str]:
    problems: list[str] = []
    for node in tinycss2.parse_declaration_list(
        content, skip_comments=True, skip_whitespace=True
    ):
        if isinstance(node, ast.ParseError):
            problems.append(
                f"{context.label}:{node.source_line}: {node.message} (nested rules are not supported; write each "
                "rule at the top level)"
            )
            continue
        if not isinstance(node, ast.Declaration):
            problems.append(
                f"{context.label}:{node.source_line}: only declarations may sit inside a rule"
            )
            continue
        name = node.name
        if name.startswith("--"):
            if name in PIXEL_RANGE_BY_METRIC_TOKEN:
                problems.extend(
                    _check_metric_value(name, node.value, context, node.source_line)
                )
            elif name not in CONTRACT_TOKENS and not name.startswith(
                PRIVATE_TOKEN_PREFIX
            ):
                problems.append(
                    f"{context.label}:{node.source_line}: {name} is not a contract token; a theme's own properties "
                    f"are named {PRIVATE_TOKEN_PREFIX}*"
                )
            else:
                pass
        for reference, line in _urls_in(node.value):
            problems.extend(_check_url(reference, context, line))
        problems.extend(_check_url_function_arguments(node.value, context))
    return problems


@pure
def _is_namespace_separator(token: ast.Node) -> bool:
    return isinstance(token, ast.LiteralToken) and token.value == _NAMESPACE_SEPARATOR


@pure
def _attribute_name_index(significant: Sequence[ast.Node]) -> int:
    """Where an attribute selector's name is, past a namespace prefix (``|``, ``*|``, or ``<ns>|``)."""
    if significant and _is_namespace_separator(significant[0]):
        return 1
    if len(significant) > 1 and _is_namespace_separator(significant[1]):
        return 2
    return 0


def _check_attribute_selector(
    block: ast.SquareBracketsBlock, context: StyleSheetContext
) -> list[str]:
    significant = _significant(block.content)
    name_index = _attribute_name_index(significant)
    name_token = significant[name_index] if name_index < len(significant) else None
    if not isinstance(name_token, ast.IdentToken):
        return []
    attribute = name_token.lower_value
    line = block.source_line
    if attribute in FORBIDDEN_SELECTOR_ATTRIBUTES:
        return [
            f"{context.label}:{line}: [{attribute}] selects an app's private markup; select a data-part instead"
        ]
    if attribute != _DATA_PART_ATTRIBUTE:
        return []
    operator = "".join(
        token.value
        for token in significant[name_index + 1 : -1]
        if isinstance(token, ast.LiteralToken)
    )
    value_token = significant[-1] if len(significant) > 1 else None
    if operator != _EXACT_MATCH_OPERATOR or not isinstance(
        value_token, (ast.StringToken, ast.IdentToken)
    ):
        return [
            f'{context.label}:{line}: select a part exactly, as [data-part="<part>"]'
        ]
    if value_token.value not in context.allowed_parts:
        return [
            f"{context.label}:{line}: {value_token.value!r} is not a part this file may style"
        ]
    return []


@pure
def _exact_part_name(block: ast.SquareBracketsBlock) -> str | None:
    """The part an attribute selector names, when it is ``[data-part="<part>"]``."""
    significant = _significant(block.content)
    name_index = _attribute_name_index(significant)
    if len(significant) != name_index + 3:
        return None
    name_token, operator_token, value_token = significant[name_index:]
    if (
        not isinstance(name_token, ast.IdentToken)
        or name_token.lower_value != _DATA_PART_ATTRIBUTE
        or not isinstance(operator_token, ast.LiteralToken)
        or operator_token.value != _EXACT_MATCH_OPERATOR
        or not isinstance(value_token, (ast.StringToken, ast.IdentToken))
    ):
        return None
    return value_token.value


@pure
def _split_selector_list(prelude: Sequence[ast.Node]) -> list[list[ast.Node]]:
    """A rule's selectors, split at its top-level commas, each without whitespace or comments."""
    selectors: list[list[ast.Node]] = [[]]
    for token in _significant(prelude):
        if isinstance(token, ast.LiteralToken) and token.value == ",":
            selectors.append([])
        else:
            selectors[-1].append(token)
    return [selector for selector in selectors if selector]


def _check_overlay_scope(
    prelude: Sequence[ast.Node], context: StyleSheetContext, prefix: str
) -> list[str]:
    """Every selector of an app's overlay names one of the app's parts outside any selector function, so the
    overlay, which is in every app's bundle, matches nothing on another app's page."""
    return [
        f"{context.label}:{selector[0].source_line}: an app's overlay styles only that app; each selector names "
        f'one of its parts, [data-part="{prefix}<part>"]'
        for selector in _split_selector_list(prelude)
        if not any(
            (_exact_part_name(token) or "").startswith(prefix)
            for token in selector
            if isinstance(token, ast.SquareBracketsBlock)
        )
    ]


def _check_selector(
    prelude: Sequence[ast.Node], context: StyleSheetContext
) -> list[str]:
    problems: list[str] = []
    tokens = list(prelude)
    for index, token in enumerate(tokens):
        line = token.source_line
        if isinstance(token, ast.LiteralToken) and token.value == ".":
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if isinstance(following, ast.IdentToken):
                problems.append(
                    f"{context.label}:{line}: .{following.value} is a class: a class is an app's private markup; "
                    "select a data-part instead"
                )
        elif isinstance(token, ast.HashToken):
            problems.append(
                f"{context.label}:{line}: #{token.value} is an id selector; select a data-part instead"
            )
        elif isinstance(token, ast.SquareBracketsBlock):
            problems.extend(_check_attribute_selector(token, context))
        elif isinstance(token, ast.FunctionBlock):
            # Every function's arguments are read: :is() and :not() take selectors, and so do :nth-child(... of)
            # and ::slotted().
            problems.extend(_check_selector(token.arguments, context))
        else:
            continue
    return problems


def _check_keyframes(
    content: Sequence[ast.Node], context: StyleSheetContext
) -> list[str]:
    problems: list[str] = []
    for node in tinycss2.parse_rule_list(
        content, skip_comments=True, skip_whitespace=True
    ):
        if isinstance(node, ast.QualifiedRule):
            problems.extend(_check_declarations(node.content, context))
        elif isinstance(node, ast.ParseError):
            problems.append(f"{context.label}:{node.source_line}: {node.message}")
        elif isinstance(node, ast.AtRule):
            problems.append(
                f"{context.label}:{node.source_line}: @{node.lower_at_keyword} is not allowed inside @keyframes"
            )
        else:
            continue
    return problems


def _check_rules(rules: Iterable[ast.Node], context: StyleSheetContext) -> list[str]:
    problems: list[str] = []
    for node in rules:
        if isinstance(node, ast.ParseError):
            problems.append(f"{context.label}:{node.source_line}: {node.message}")
        elif isinstance(node, ast.AtRule):
            problems.extend(_check_at_rule(node, context))
        elif isinstance(node, ast.QualifiedRule):
            problems.extend(_check_selector(node.prelude, context))
            if context.scoping_part_prefix is not None:
                problems.extend(
                    _check_overlay_scope(
                        node.prelude, context, context.scoping_part_prefix
                    )
                )
            problems.extend(_check_declarations(node.content or [], context))
        else:
            continue
    return problems


def _check_at_rule(node: ast.AtRule, context: StyleSheetContext) -> list[str]:
    name = node.lower_at_keyword
    line = node.source_line
    if name not in ALLOWED_AT_RULES:
        return [
            f"{context.label}:{line}: @{name} is not allowed in a theme's style file"
        ]
    if node.content is None:
        return [f"{context.label}:{line}: @{name} needs a block"]
    if context.scoping_part_prefix is not None and name not in GROUPING_AT_RULES:
        return [
            f"{context.label}:{line}: @{name} names something every app's page shares; put it in the theme's own "
            "style files, not an app's overlay"
        ]
    if name in GROUPING_AT_RULES:
        return _check_rules(
            tinycss2.parse_rule_list(
                node.content, skip_comments=True, skip_whitespace=True
            ),
            context,
        )
    if name == "font-face":
        return _check_declarations(node.content, context)
    return _check_keyframes(node.content, context)


def check_style_sheet(css_text: str, context: StyleSheetContext) -> list[str]:
    """Every way the style sheet steps outside the contract (docs/system/blueprint/workspace-themes/, section 3.2)."""
    rules = tinycss2.parse_stylesheet(
        css_text, skip_comments=True, skip_whitespace=True
    )
    return _check_rules(rules, context)


@pure
def app_part_selector_names(app_name: str, part_names: Iterable[str]) -> frozenset[str]:
    return frozenset(f"{app_name}.{part}" for part in part_names)


def _check_style_file(
    folder: Path,
    relative: str,
    allowed_parts: frozenset[str],
    scoping_part_prefix: str | None,
) -> tuple[list[str], int]:
    path = folder / relative
    if not relative.endswith(STYLE_FILE_SUFFIX):
        return [f"{relative}: a style file is a .css file"], 0
    if not path.is_file():
        return [f"{relative}: listed as a style file, but it does not exist"], 0
    # utf-8-sig drops a leading byte-order mark as a browser's decoding does, so the rules checked are the rules the
    # browser reads: kept, the mark would hide a leading @import inside a rule's selector.
    try:
        css_text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        return [f"{relative}: cannot be read as UTF-8 text: {error}"], 0
    context = StyleSheetContext(
        label=relative,
        folder=folder,
        file_directory=posixpath.dirname(relative),
        allowed_parts=allowed_parts,
        scoping_part_prefix=scoping_part_prefix,
    )
    return check_style_sheet(css_text, context), len(css_text.encode("utf-8"))


def check_style_files(
    folder: Path,
    manifest: ThemeManifest,
    app_parts_by_app: Mapping[AppName, frozenset[str]],
) -> list[str]:
    """The manifest's style files and app overlays exist and obey the contract, within the size limit."""
    problems: list[str] = []
    total_bytes = 0
    core_parts = frozenset(CORE_PARTS)
    for relative in manifest.styles.files:
        file_problems, size = _check_style_file(
            folder, relative, core_parts, scoping_part_prefix=None
        )
        problems.extend(file_problems)
        total_bytes += size
    for app_name in manifest.styles.apps:
        app_parts = app_parts_by_app.get(app_name)
        # An app this workspace does not have leaves its overlay out: there is nothing of it to style.
        if app_parts is None:
            continue
        allowed = core_parts | app_part_selector_names(app_name, app_parts)
        file_problems, size = _check_style_file(
            folder,
            f"{APP_STYLES_DIRECTORY}/{app_name}.css",
            allowed,
            scoping_part_prefix=f"{app_name}.",
        )
        problems.extend(file_problems)
        total_bytes += size
    if total_bytes > MAX_STYLE_FILES_BYTES:
        problems.append(
            f"the style files are {total_bytes} bytes, more than the {MAX_STYLE_FILES_BYTES} allowed"
        )
    return problems


def _check_png_size(path: Path, relative: str, size: int) -> list[str]:
    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        return [f"{relative}: cannot be read as an image: {error}"]
    if image_format != "PNG":
        return [f"{relative}: is {image_format}, not PNG"]
    if (width, height) != (size, size):
        return [
            f"{relative}: is {width}x{height}, but the theme's icons are {size}x{size}"
        ]
    return []


def check_icon_files(folder: Path, theme_id: str, icons: ThemeIconSpec) -> list[str]:
    """The icon guide, references, fallback, and curated icons exist and match the declared format and size."""
    problems: list[str] = []
    if not (folder / icons.guide).is_file():
        problems.append(f"icons.guide: {icons.guide} does not exist")
    for reference in icons.references:
        if not (folder / reference).is_file():
            problems.append(f"icons.references: {reference} does not exist")
    suffix = f".{icons.format}"
    fallback = folder / ICONS_DIRECTORY / f"{FALLBACK_ICON_STEM}{suffix}"
    if theme_id != STANDARD_THEME_ID and not fallback.is_file():
        problems.append(
            f"icons: the generic program icon {ICONS_DIRECTORY}/{FALLBACK_ICON_STEM}{suffix} is missing"
        )
    problems.extend(check_icon_file_formats(folder, icons))
    return problems


def check_icon_file_formats(folder: Path, icons: ThemeIconSpec) -> list[str]:
    """The folder's icons in the spec's format match it: a png icon's format and size, an svg icon's rules. A theme
    that inherits its base's [icons] draws its own icons to that spec, so they are held to it too."""
    problems: list[str] = []
    icons_directory = folder / ICONS_DIRECTORY
    if not icons_directory.is_dir():
        return problems
    for path in sorted(icons_directory.glob(f"*.{icons.format}")):
        label = path.relative_to(folder).as_posix()
        if icons.format == IconFormat.PNG:
            problems.extend(_check_png_size(path, label, icons.size))
        else:
            problems.extend(f"{label}: {problem}" for problem in check_svg_icon(path))
    return problems


def check_attribution_files(folder: Path, manifest: ThemeManifest) -> list[str]:
    return [
        f"attribution {str(attribution.name)!r}: {path} does not exist"
        for attribution in manifest.attribution
        for path in attribution.files
        if not (folder / path).exists()
    ]


SVG_NAMESPACE_TAG: Final[str] = "{http://www.w3.org/2000/svg}svg"
# What an svg icon may not hold, as docs/system/app-icons.md says of app icons: nothing that executes or embeds
# foreign content, and no reference to anything outside the icon itself.
_FORBIDDEN_SVG_ELEMENTS: Final[frozenset[str]] = frozenset({"script", "foreignobject"})
_SVG_REFERENCE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"href", "src"})
_EVENT_HANDLER_PREFIX: Final[str] = "on"
_FRAGMENT_PREFIX: Final[str] = "#"


@pure
def _local_name(qualified_name: str) -> str:
    """A tag or attribute name without ElementTree's ``{namespace}`` prefix."""
    return qualified_name.rpartition("}")[2]


def _svg_content_problems(root: ElementTree.Element) -> list[str]:
    problems: list[str] = []
    for element in root.iter():
        tag = _local_name(str(element.tag)).lower()
        if tag in _FORBIDDEN_SVG_ELEMENTS:
            problems.append(f"the icon has a <{tag}> element, which an icon may not")
        for qualified_name, value in element.attrib.items():
            name = _local_name(qualified_name).lower()
            if name.startswith(_EVENT_HANDLER_PREFIX):
                problems.append(
                    f"the icon's <{tag}> has an event handler, {name}, which an icon may not"
                )
            elif name in _SVG_REFERENCE_ATTRIBUTES and not value.strip().startswith(
                _FRAGMENT_PREFIX
            ):
                problems.append(
                    f"the icon's <{tag}> {name} points at {value!r}; an icon refers only to itself, by #fragment"
                )
            else:
                continue
    return problems


def check_svg_icon(path: Path) -> list[str]:
    """Every way an svg icon is not one the workspace can draw: well-formed SVG with a viewBox, holding nothing that
    executes or loads from elsewhere."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as error:
        return [f"the icon is not well-formed SVG: {error}"]
    if root.tag not in (SVG_NAMESPACE_TAG, "svg"):
        return ["the icon's root element is not <svg>"]
    problems = _svg_content_problems(root)
    if "viewBox" not in root.attrib:
        problems.append("the icon has no viewBox, so it cannot scale")
    return problems
