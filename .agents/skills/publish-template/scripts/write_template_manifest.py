#!/usr/bin/env python3
"""Generate `template.toml` for a snapshot being assembled.

Called by `build_template.sh` after it has assembled the tree. Writes the
machine-readable half of the manifest: identity, the derivation recipe, the
lineage inherited from whatever manifest this snapshot overrides, and the
`[[requirements.secret]]` entries aggregated from what the included apps and
skills declare.

Everything the publishing worker must still supply -- the recipe's `exclude`
and `modification_rules`, the rest of the structured `[requirements]`, and the
`[environment]` declarations -- is emitted as an empty section with a prompting
comment. Those all have empty defaults, so the generated file is valid TOML the
moment it is written and `validate_template.py` can gate on it immediately; the
forcing function for filling them in is the task instructions plus the
markdown/TOML agreement check, which fails if the worker writes a `requires_`
line in `template.md` without its counterpart here.

Secrets are the one requirement derived rather than written: an app declares
the env files it needs in its `app.toml` (`[[secrets]]`: `file`, `variables`,
`note`) and a skill under `secrets:` in its SKILL.md front matter, and the
snapshot's `.mcp.template.json` and supervisord programs name the files they
run under. Every referenced `data/.secrets/<file>.env` must be declared, and
every declared variable must exist in the publishing workspace's own file, or
the publish stops here: an adopter is asked for exactly what the declarations
say, so a declaration that lies breaks adoption silently. The aggregated lines
are also written to `--secret-lines-output` so `template.md` carries the matching
`requires_secret:` lines.

Pure standard library: it runs in the assembly worker's post-reset worktree,
where there is no venv, so it must work under a bare `python3`. Strings are
emitted with `json.dumps`, whose escaping is a valid subset of TOML's
basic-string escaping -- and any mistake is caught immediately, because
`build_template.sh` validates the file it just wrote.

Usage (from the assembled repo root):

    python3 write_template_manifest.py --slug S --title T --description D \\
        --version v1 --include PATH [--include PATH ...] \\
        [--data-include PATH ...] [--previous-manifest PATH] \\
        [--workspace-dir PATH] [--secret-lines-output PATH] --output PATH
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

APP_MANIFEST_NAME = "app.toml"
SKILL_FILE_NAME = "SKILL.md"
SECRETS_DIRECTORY = "data/.secrets"
MCP_TEMPLATE_FILE = ".mcp.template.json"
MCP_FILE = ".mcp.json"
SUPERVISORD_DROPIN_DIRECTORY = "system/supervisord.conf.d"

# A reference to a secret file anywhere in a config: the file's slug is what a
# declaration is keyed by.
_SECRET_REFERENCE_RE = re.compile(r"data/\.secrets/([a-z0-9][a-z0-9-]*)\.env")
_SECRET_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The names an env file sets: what `system/scripts/with_secrets.py` reads, names only.
_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE
)


class WriteTemplateManifestError(Exception):
    """Base exception for manifest generation failures."""


class PreviousManifestUnreadableError(WriteTemplateManifestError, ValueError):
    """Raised when the manifest being overridden cannot be parsed.

    Fatal rather than ignored: silently dropping an unreadable predecessor
    would lose the lineage chain, which is the only record of what this
    snapshot replaced.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"Cannot read the previous manifest at {path}: {reason}")


class SecretDeclarationError(WriteTemplateManifestError, ValueError):
    """Raised when a declaration is malformed, a referenced file is undeclared, or a
    declared variable is missing from the publishing workspace's own file."""


class SecretDeclaration:
    """One `data/.secrets/<file>.env` a template needs, merged across everything that declares it."""

    def __init__(
        self, file: str, variables: list[str], note: str, sources: list[str]
    ) -> None:
        self.file = file
        self.variables = variables
        self.note = note
        self.sources = sources


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_string_array(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def read_inherited_lineage(previous_manifest_path: Path) -> list[dict[str, str]]:
    """The lineage entries a new manifest inherits from the one it overrides.

    Mirrors `env_converge.template_manifest.lineage_after_override`, in
    stdlib form because this script cannot import the schema module: the
    predecessor's own chain first (oldest first), then the predecessor itself
    when its `[origin]` gives an address to record.
    """
    if not previous_manifest_path.is_file():
        return []
    try:
        previous = tomllib.loads(previous_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PreviousManifestUnreadableError(previous_manifest_path, str(e)) from e

    inherited: list[dict[str, str]] = []
    for entry in previous.get("lineage", []):
        inherited.append(
            {
                key: str(entry[key])
                for key in ("slug", "repo_url", "commit", "used_on")
                if key in entry
            }
        )

    origin = previous.get("origin")
    identity = previous.get("template", {})
    if isinstance(origin, dict) and "repo_url" in origin and "commit" in origin:
        entry = {
            "slug": str(identity.get("slug", "unknown")),
            "repo_url": str(origin["repo_url"]),
            "commit": str(origin["commit"]),
        }
        if "adopted_on" in origin:
            entry["used_on"] = str(origin["adopted_on"])
        inherited.append(entry)
    return inherited


# --- secret declarations -----------------------------------------------------


def _validated_declaration(raw: object, source: str) -> tuple[str, list[str], str]:
    """One declaration's (file, variables, note), refusing anything malformed."""
    if not isinstance(raw, dict):
        raise SecretDeclarationError(
            f"{source}: a secret declaration must be a table with file, variables, note"
        )
    declaration: dict[str, object] = {str(key): value for key, value in raw.items()}
    file = declaration.get("file")
    variables = declaration.get("variables")
    note = declaration.get("note", "")
    if not isinstance(file, str) or not _SECRET_FILE_RE.match(file):
        raise SecretDeclarationError(
            f"{source}: secret file {file!r} must be lowercase letters, digits and hyphens"
        )
    names = (
        [name for name in variables if isinstance(name, str)]
        if isinstance(variables, list)
        else []
    )
    if (
        not isinstance(variables, list)
        or not names
        or len(names) != len(variables)
        or not all(_VARIABLE_NAME_RE.match(name) for name in names)
    ):
        raise SecretDeclarationError(
            f"{source}: secret {file!r} must list at least one variable name (letters, digits, underscores)"
        )
    if not isinstance(note, str):
        raise SecretDeclarationError(f"{source}: secret {file!r} note must be a string")
    return file, names, note


def _parse_flow_list(text: str) -> list[str]:
    """`[A, B]` (items optionally quoted) as a list of strings."""
    inner = text.strip()[1:-1]
    return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]


def parse_skill_secrets(skill_text: str) -> list[dict[str, object]]:
    """The `secrets:` list of a SKILL.md front matter, in the one shape it may take::

        secrets:
          - file: example
            variables: [EXAMPLE_API_KEY]
            note: an Example API key from the account's settings page

    Only that subset of YAML is read: a list of maps with scalar or flow-list
    values. The front matter is what skill validators already parse, so an
    unrelated key never trips this.
    """
    lines = skill_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    entries: list[dict[str, object]] = []
    is_in_secrets = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.startswith(" ") and line.strip():
            is_in_secrets = line.split(":", 1)[0].strip() == "secrets"
            continue
        if not is_in_secrets or not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            entries.append({})
            stripped = stripped[2:].strip()
        if not entries or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        value = raw_value.strip()
        if value.startswith("["):
            entries[-1][key.strip()] = _parse_flow_list(value)
        else:
            entries[-1][key.strip()] = value.strip("'\"")
    return entries


def collect_declarations(
    repo_root: Path, include_paths: list[str]
) -> dict[str, SecretDeclaration]:
    """Every secret the included apps and skills declare, merged by file."""
    declaration_by_file: dict[str, SecretDeclaration] = {}

    def add(file: str, variables: list[str], note: str, source: str) -> None:
        existing = declaration_by_file.get(file)
        if existing is None:
            declaration_by_file[file] = SecretDeclaration(
                file, list(variables), note, [source]
            )
            return
        for name in variables:
            if name not in existing.variables:
                existing.variables.append(name)
        if note and note not in existing.note:
            existing.note = f"{existing.note}; {note}" if existing.note else note
        existing.sources.append(source)

    for include in include_paths:
        base = repo_root / include
        candidates = (
            [base]
            if base.is_file()
            else sorted(base.rglob("*"))
            if base.is_dir()
            else []
        )
        for path in candidates:
            relative = path.relative_to(repo_root).as_posix()
            if path.name == APP_MANIFEST_NAME:
                try:
                    manifest = tomllib.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                    raise SecretDeclarationError(
                        f"{relative}: cannot read the app manifest: {e}"
                    ) from e
                for raw in manifest.get("secrets", []):
                    file, variables, note = _validated_declaration(raw, relative)
                    add(file, variables, note, relative)
            elif path.name == SKILL_FILE_NAME:
                for raw in parse_skill_secrets(
                    path.read_text(encoding="utf-8", errors="replace")
                ):
                    file, variables, note = _validated_declaration(raw, relative)
                    add(file, variables, note, relative)
    return declaration_by_file


def collect_references(repo_root: Path) -> dict[str, list[str]]:
    """Every secret file the snapshot's MCP config and supervisord programs name, with where."""
    sources_by_file: dict[str, list[str]] = {}
    candidates = [repo_root / MCP_TEMPLATE_FILE, repo_root / MCP_FILE]
    dropins = repo_root / SUPERVISORD_DROPIN_DIRECTORY
    if dropins.is_dir():
        candidates.extend(sorted(dropins.glob("*.conf")))
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(repo_root).as_posix()
        for match in _SECRET_REFERENCE_RE.finditer(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            sources_by_file.setdefault(match.group(1), []).append(relative)
    return sources_by_file


def env_file_variable_names(text: str) -> set[str]:
    return set(_ENV_ASSIGNMENT_RE.findall(text))


def check_declarations(
    declaration_by_file: dict[str, SecretDeclaration],
    sources_by_file: dict[str, list[str]],
    workspace_dir: Path | None,
) -> list[str]:
    """The two lint rules: a referenced file needs a declaration, and a declared
    variable must exist in the publishing workspace's own file."""
    problems: list[str] = []
    for file, sources in sorted(sources_by_file.items()):
        if file not in declaration_by_file:
            problems.append(
                f"{', '.join(sources)} runs under {SECRETS_DIRECTORY}/{file}.env, but no included app.toml "
                f"[[secrets]] or SKILL.md secrets: entry declares that file"
            )
    if workspace_dir is not None:
        for file, declaration in sorted(declaration_by_file.items()):
            env_file = workspace_dir / SECRETS_DIRECTORY / f"{file}.env"
            present = (
                env_file_variable_names(
                    env_file.read_text(encoding="utf-8", errors="replace")
                )
                if env_file.is_file()
                else set()
            )
            missing = [name for name in declaration.variables if name not in present]
            if missing:
                problems.append(
                    f"{', '.join(declaration.sources)} declares {', '.join(missing)} in "
                    f"{SECRETS_DIRECTORY}/{file}.env, which this workspace's file does not set; "
                    "a declaration an adopter will be asked for must match what the app actually runs with"
                )
    return problems


def render_secret_requirements(
    declaration_by_file: dict[str, SecretDeclaration],
) -> list[str]:
    lines: list[str] = []
    for file, declaration in sorted(declaration_by_file.items()):
        lines.extend(
            [
                "",
                "[[requirements.secret]]",
                f"file = {_toml_string(file)}",
                f"variables = {_toml_string_array(declaration.variables)}",
                f"note = {_toml_string(declaration.note)}",
            ]
        )
    return lines


def render_requires_secret_lines(
    declaration_by_file: dict[str, SecretDeclaration],
) -> str:
    """The `requires_secret:` lines template.md carries, one per aggregated file."""
    lines = []
    for file, declaration in sorted(declaration_by_file.items()):
        note = f" ({declaration.note})" if declaration.note else ""
        lines.append(
            f"- requires_secret: {SECRETS_DIRECTORY}/{file}.env with {', '.join(declaration.variables)}{note}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def render_manifest(
    slug: str,
    title: str,
    description: str,
    version: str,
    manifest_format: str,
    thumbnail: str,
    include: list[str],
    data_include: list[str],
    lineage: list[dict[str, str]],
    secret_declarations: dict[str, SecretDeclaration],
) -> str:
    lines = [
        "# Machine-readable manifest for this template. The sibling",
        "# template.md holds the prose, the Requirements list, and the two",
        "# append-only history logs; this file is authoritative for everything",
        "# below. Both are generated together -- validate_template.py fails if",
        "# they disagree.",
        f"format = {_toml_string(manifest_format)}",
        "",
        "[template]",
        f"slug = {_toml_string(slug)}",
        f"title = {_toml_string(title)}",
        f"description = {_toml_string(description)}",
        f"thumbnail = {_toml_string(thumbnail)}",
        f"version = {_toml_string(version)}",
        "",
        "# How this template is DERIVED from the workspace it came from. An",
        "# update re-runs this recipe rather than diffing two repos, which is what",
        "# keeps an exclusion excluded even though the thing still exists upstream.",
        "[recipe]",
        f"include = {_toml_string_array(include)}",
        f"data_include = {_toml_string_array(data_include)}",
        "# FILL IN: one entry per deliberate exclusion (paths left out, and",
        "# features stripped from an included path). Leave [] if nothing was.",
        "exclude = []",
        "# FILL IN: one entry per published-version modification, stated as a RULE",
        "# and NEVER the removed value -- the point of a modification is that the",
        '# value does not ship. e.g. "replace the hardcoded team channel with a',
        '# neutral default". Leave [] if there were none.',
        "modification_rules = []",
        "",
        "# Everything an adopter must deal with before this is really theirs.",
        "# One list -- but each entry's KIND says how it is handled, because the",
        "# two are handled at different times by different mechanisms:",
        "#",
        "#   permission / secret / llm = ACTIVATION. The adopting agent acts on",
        "#   these FIRST and BY ITSELF, initiating each latchkey permission",
        "#   request and each secret request before asking the user anything.",
        "#   Every one of them must have a matching requires_ line in template.md",
        "#   and vice versa -- the validator checks that, because an adopter once",
        "#   never got prompted for a permission the app needed.",
        "#",
        "#   The [[requirements.secret]] entries below were AGGREGATED from the",
        "#   included apps' app.toml [[secrets]] and skills' SKILL.md secrets:",
        "#   declarations, plus every data/.secrets/<file>.env the snapshot's",
        "#   .mcp.template.json and supervisord programs run under. Fix a wrong one",
        "#   at its declaration, not here.",
        "#",
        "#   adaptation = worked through INTERACTIVELY with the user afterwards.",
        "#   Prose on both sides, so it is not cross-checked.",
        "#",
        "# FILL IN, e.g.:",
        "#   [[requirements.permission]]",
        '#   scope = "slack-api"',
        '#   permission = "slack-read-all"',
        "#",
        "#   [requirements.llm]",
        '#   method = "keyed"   # keyed (an API key, e.g. ANTHROPIC_API_KEY)'
        " | keyless (a subscription CLI, e.g. claude -p)",
        "#",
        "#   [[requirements.adaptation]]",
        '#   summary = "the digest channel is hardcoded"',
        '#   resolution = "ask the user which channel to watch"',
        "[requirements]",
        *render_secret_requirements(secret_declarations),
        "",
        "# What the included code needs INSTALLED. apt takes bare names: versions",
        "# are a function of the apt snapshot timestamp, so replaying names at the",
        "# adopter's timestamp yields versions consistent with the rest of their",
        "# environment. The other sources are not snapshot-pinned, so for them the",
        "# recorded version IS the pin (name = version).",
        "#",
        "# FILL IN from what the included code actually needs, e.g.:",
        '#   apt = ["poppler-utils"]',
        "#   [environment.npm_global]",
        '#   "@slack/cli" = "2.1.0"',
        "#   [environment.uv_tools]",
        '#   yt-dlp = "2026.7.1"',
        "#   [environment.cargo_crates]",
        '#   fd-find = "9.0.0"',
        "#",
        "# For an install with no package database (a URL-fetched binary, a browser),",
        "# ship a system/scripts/env.d/<NNNN>-<slug>-<name>.sh unit with NNNN >= 2000,",
        "# include it in the recipe above, and list it in env_d_units.",
        "[environment]",
        "apt = []",
        "env_d_units = []",
    ]

    if lineage:
        lines.extend(
            [
                "",
                "# Templates this mind used on the way to this one, oldest first.",
                "# A new manifest overrides its predecessor rather than accumulating",
                "# beside it; the commit hash is what keeps the superseded manifest",
                "# retrievable in the repo where it is authoritative.",
            ]
        )
        for entry in lineage:
            lines.append("")
            lines.append("[[lineage]]")
            for key in ("slug", "repo_url", "commit", "used_on"):
                if key in entry:
                    lines.append(f"{key} = {_toml_string(entry[key])}")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--format", dest="manifest_format", required=True)
    parser.add_argument("--thumbnail", required=True)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--data-include", action="append", default=[])
    parser.add_argument(
        "--previous-manifest",
        default="",
        help="The template.toml being overridden, staged before the reset",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="The assembled tree the declarations and references are read from (default: cwd)",
    )
    parser.add_argument(
        "--workspace-dir",
        default="",
        help="The publishing workspace, whose data/.secrets/ files every declared variable must exist in",
    )
    parser.add_argument(
        "--secret-lines-output",
        default="",
        help="Where to write the requires_secret: lines template.md carries (one per aggregated file)",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    lineage: list[dict[str, str]] = []
    if args.previous_manifest:
        lineage = read_inherited_lineage(Path(args.previous_manifest))

    repo_root = Path(args.repo_root).resolve()
    try:
        declarations = collect_declarations(repo_root, list(args.include))
        problems = check_declarations(
            declarations,
            collect_references(repo_root),
            Path(args.workspace_dir) if args.workspace_dir else None,
        )
    except SecretDeclarationError as e:
        print(f"write_template_manifest: {e}", file=sys.stderr)
        return 1
    if problems:
        print(
            f"write_template_manifest: {len(problems)} problem(s) with the template's secrets:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    Path(args.output).write_text(
        render_manifest(
            slug=args.slug,
            title=args.title,
            description=args.description,
            version=args.version,
            manifest_format=args.manifest_format,
            thumbnail=args.thumbnail,
            include=args.include,
            data_include=args.data_include,
            lineage=lineage,
            secret_declarations=declarations,
        ),
        encoding="utf-8",
    )
    if args.secret_lines_output:
        Path(args.secret_lines_output).write_text(
            render_requires_secret_lines(declarations), encoding="utf-8"
        )
    if lineage:
        print(
            f"write_template_manifest: carried {len(lineage)} lineage entr"
            f"{'y' if len(lineage) == 1 else 'ies'} forward",
            file=sys.stderr,
        )
    if declarations:
        print(
            f"write_template_manifest: aggregated {len(declarations)} secret declaration(s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
