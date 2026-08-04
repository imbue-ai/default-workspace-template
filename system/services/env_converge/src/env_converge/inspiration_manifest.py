"""Schema for an inspiration's machine-readable manifest (`inspiration.toml`).

An inspiration publishes three files at its repo root: `inspiration.md` (prose,
requirements, and the two append-only history logs), `inspiration.svg` (the
thumbnail / README hero), and `inspiration.toml` -- this schema. The TOML is
authoritative for everything machine-readable: identity, the derivation recipe,
the activation prerequisites, the environment an adopter must converge, and the
lineage of inspirations this one was built on.

There is exactly one manifest per repo. A newly published or newly adopted
inspiration OVERRIDES the previous one rather than accumulating beside it; what
survives is the `[[lineage]]` chain, which records each predecessor's repo URL
and commit hash so the superseded manifest stays retrievable where it is
authoritative.

**Why this module imports only pydantic and the standard library.** It is used
from two very different places. At converge time it is imported normally, from
the workspace venv. At publish time it runs inside a worker's worktree that
`build_inspiration.sh` has just reset with `git read-tree -u --reset` +
`git clean -fdxq`, which deletes the gitignored `.venv` -- so the validator is
snapshotted out of the tree before that reset and re-run under
`uv run --no-project --with pydantic`. That deliberately resolves no workspace
project (the assembly script documents why: building the full environment on a
cold base is slow and can fail on an unrelated build error, aborting an
otherwise fine publish), which means no workspace package is importable here.

That is why the frozen base below is declared locally instead of using
`imbue.imbue_common.frozen_model.FrozenModel`, which the style guide would
otherwise call for: `FrozenModel` imports `imbue.imbue_common.model_update`, a
workspace path dependency that is not available under `--no-project`. The
config below is the same one `FrozenModel` sets. `inspiration_manifest_test.py`
asserts this module's import set stays within stdlib + pydantic so the
constraint cannot silently rot.
"""

import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The three files an inspiration publishes at its repo root. No slug in any of
# them: one inspiration per repo, overriding rather than accumulating.
MANIFEST_TOML_NAME = "inspiration.toml"
MANIFEST_MARKDOWN_NAME = "inspiration.md"
MANIFEST_THUMBNAIL_NAME = "inspiration.svg"

# The manifest format this schema describes. `v1` is the pre-split format:
# slug-named `inspiration-<slug>.md` with the recipe as a YAML block inside the
# markdown and no sibling TOML. A v1 repo has no `inspiration.toml` at all, so
# absence of the file -- not a version field inside it -- is what identifies v1.
CURRENT_MANIFEST_FORMAT = "v2"

# Inspiration-carried env.d units live under this prefix and are ordered after
# the template's own units (1000-, 1100-), which is what makes composition a
# file addition rather than an edit to anything shared.
ENV_D_DIRECTORY = "system/scripts/env.d"
ENV_D_INSPIRATION_MINIMUM_ORDER = 2000


class InspirationManifestError(Exception):
    """Base exception for every inspiration-manifest failure."""


class InspirationManifestNotFoundError(InspirationManifestError, FileNotFoundError):
    """Raised when the manifest file does not exist."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"No inspiration manifest at {path}")


class InspirationManifestParseError(InspirationManifestError, ValueError):
    """Raised when the manifest is unreadable, malformed TOML, or fails validation."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Invalid inspiration manifest at {path}: {reason}")


# Matches the slug rule the publish flow and `build_inspiration.sh` enforce:
# ^[A-Za-z0-9._-]+$ with no leading '-'. Encoded as one pattern so the
# no-leading-dash rule is part of the type rather than a separate check.
Slug = Annotated[str, Field(pattern=r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")]

# An inspiration's published version: v1 for a first publish, then v2, v3, ...
InspirationVersion = Annotated[str, Field(pattern=r"^v[1-9][0-9]*$")]

# apt archive snapshot timestamp, matching .mngr/apt-snapshot-timestamp.
SnapshotTimestamp = Annotated[str, Field(pattern=r"^[0-9]{8}T[0-9]{6}Z$")]

# An abbreviated-or-full git commit hash.
CommitHash = Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$")]

NonEmptyString = Annotated[str, Field(min_length=1)]


class FrozenManifestModel(BaseModel):
    """Immutable, strict base for every manifest model.

    Mirrors `imbue.imbue_common.frozen_model.FrozenModel`'s configuration; see
    the module docstring for why it is not imported.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=False,
    )


class InspirationIdentity(FrozenManifestModel):
    """Who this inspiration is.

    The slug remains the identity -- it names the repo and keys the version
    ledger -- it just no longer appears in any filename.
    """

    slug: Slug = Field(
        description="Identity of the inspiration; also the default repo name"
    )
    title: NonEmptyString = Field(description="Display title shown to a user")
    description: NonEmptyString = Field(
        description="One-line description of what it does"
    )
    thumbnail: NonEmptyString = Field(
        default=MANIFEST_THUMBNAIL_NAME,
        description="Repo-root-relative thumbnail path (the README's hero graphic)",
    )
    version: InspirationVersion = Field(
        description="Published version of this inspiration (v1 for a first publish)"
    )


class Recipe(FrozenManifestModel):
    """How this inspiration is DERIVED from the workspace it came from.

    An inspiration is not a fork: an update re-runs this recipe against the
    current source workspace rather than diffing two repos, which is what keeps
    a deliberate exclusion excluded even though the thing still exists upstream.
    """

    include: tuple[NonEmptyString, ...] = Field(
        description="Repo-root-relative paths overlaid onto the clean template base"
    )
    data_include: tuple[NonEmptyString, ...] = Field(
        default=(),
        description="Non-personal data paths explicitly opted into the snapshot",
    )
    exclude: tuple[NonEmptyString, ...] = Field(
        default=(),
        description="Deliberate exclusions: paths left out, and features stripped from an included path",
    )
    modification_rules: tuple[NonEmptyString, ...] = Field(
        default=(),
        description="Published-version edits as RULES, never the removed values (which must not ship)",
    )


class PermissionRequirement(FrozenManifestModel):
    """One latchkey permission the adopting agent must initiate during setup."""

    scope: NonEmptyString = Field(description="Latchkey scope, e.g. slack-api")
    permission: NonEmptyString = Field(
        description="Permission schema, e.g. slack-read-all"
    )
    note: str = Field(default="", description="What the app needs it for")


class SecretRequirement(FrozenManifestModel):
    """One secret the adopter must supply."""

    name: NonEmptyString = Field(description="Environment variable or config key")
    note: str = Field(default="", description="What it is for and where it goes")


class LlmRequirement(FrozenManifestModel):
    """How the included code reaches Claude.

    Recorded explicitly because the route is per-environment: an adopter may be
    on the other method than the one this code was written against, and must
    know to switch it.
    """

    method: NonEmptyString = Field(
        description="'keyed' (ANTHROPIC_API_KEY via litellm) or 'keyless' (claude -p subscription)"
    )
    note: str = Field(
        default="", description="What an adopter on the other method must change"
    )


class Prerequisites(FrozenManifestModel):
    """The SETUP agenda: what must be ACTIVATED before the inspiration runs.

    Distinct from the markdown's Requirements section, which is the ADAPTATION
    agenda -- what an adapter must decide or rewire to make it theirs.
    """

    permission: tuple[PermissionRequirement, ...] = Field(
        default=(), description="Latchkey permissions the adopting agent initiates"
    )
    secret: tuple[SecretRequirement, ...] = Field(
        default=(), description="Secrets the adopter must supply"
    )
    llm: LlmRequirement | None = Field(
        default=None, description="Present whenever the included code calls an LLM"
    )


class EnvironmentDeclaration(FrozenManifestModel):
    """What the included code needs installed, mirroring the env-converge record.

    apt is a bare name list because versions are a function of the pinned apt
    snapshot timestamp -- replaying names at a timestamp yields deterministic
    versions, so names are the portable pin and the publisher's timestamp is
    recorded only as provenance. The other sources are not snapshot-pinned, so
    for them the recorded version IS the pin, and they mirror the record's
    `version_by_*` maps.
    """

    apt_snapshot_timestamp: SnapshotTimestamp | None = Field(
        default=None,
        description="Publisher's pinned timestamp; provenance only -- convergence uses the ADOPTER's",
    )
    apt: tuple[NonEmptyString, ...] = Field(
        default=(),
        description="apt package names; versions follow from the converging timestamp",
    )
    npm_global: dict[str, str] = Field(
        default_factory=dict,
        description="Globally-installed npm packages, name -> version",
    )
    uv_tools: dict[str, str] = Field(
        default_factory=dict, description="uv-installed tools, name -> version"
    )
    cargo_crates: dict[str, str] = Field(
        default_factory=dict, description="cargo registry crates, name -> version"
    )
    cargo_default_toolchain: str | None = Field(
        default=None,
        description="rustup default toolchain to install before the crates",
    )
    env_d_units: tuple[NonEmptyString, ...] = Field(
        default=(),
        description=f"Repo-root-relative {ENV_D_DIRECTORY}/<NNNN>-<slug>-<name>.sh units carried by this inspiration",
    )

    def is_empty(self) -> bool:
        """Whether this inspiration declares no environment needs at all."""
        return not (
            self.apt
            or self.npm_global
            or self.uv_tools
            or self.cargo_crates
            or self.cargo_default_toolchain
            or self.env_d_units
        )


class LineageEntry(FrozenManifestModel):
    """One inspiration this mind used on the way to producing this one.

    The commit hash is what makes overriding non-destructive: the superseded
    manifest stays readable by fetching that repo at that commit.
    """

    slug: Slug = Field(description="Identity of the predecessor inspiration")
    repo_url: NonEmptyString = Field(
        description="Git repo URL the predecessor was used from"
    )
    commit: CommitHash = Field(
        description="Exact commit of the predecessor that was used"
    )
    used_on: date | None = Field(
        default=None, description="Date this mind adopted or built on it"
    )


class InspirationManifest(FrozenManifestModel):
    """The whole of `inspiration.toml`."""

    format: NonEmptyString = Field(
        default=CURRENT_MANIFEST_FORMAT, description="Manifest format version"
    )
    inspiration: InspirationIdentity = Field(description="Identity of this inspiration")
    recipe: Recipe = Field(
        description="How this inspiration is derived from its source workspace"
    )
    prerequisites: Prerequisites = Field(
        default_factory=Prerequisites, description="The activation (SETUP) agenda"
    )
    environment: EnvironmentDeclaration = Field(
        default_factory=EnvironmentDeclaration,
        description="Packages and units an adopter must converge",
    )
    lineage: tuple[LineageEntry, ...] = Field(
        default=(), description="Inspirations this one was built on, oldest first"
    )


def find_manifest_path(workspace_dir: Path) -> Path | None:
    """The workspace's `inspiration.toml`, or None when there is none.

    Absence is the normal case and is not an error: an ordinary workspace has
    no inspiration, and a v1 inspiration repo has slug-named markdown manifests
    and no TOML at all.
    """
    candidate = workspace_dir / MANIFEST_TOML_NAME
    return candidate if candidate.is_file() else None


def load_inspiration_manifest(path: Path) -> InspirationManifest:
    """Parse and validate one `inspiration.toml`.

    Raises InspirationManifestNotFoundError when the file is absent, and
    InspirationManifestParseError when it cannot be read, is not valid TOML, or
    does not satisfy the schema.
    """
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as e:
        raise InspirationManifestNotFoundError(path) from e
    except OSError as e:
        raise InspirationManifestParseError(path, f"cannot read the file: {e}") from e
    try:
        raw_manifest = tomllib.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise InspirationManifestParseError(path, f"not valid UTF-8: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise InspirationManifestParseError(path, f"not valid TOML: {e}") from e
    try:
        return InspirationManifest.model_validate(raw_manifest)
    except ValidationError as e:
        raise InspirationManifestParseError(path, str(e)) from e


def _parse_markdown_front_matter(markdown_text: str) -> dict[str, str]:
    """The `key: value` pairs of a leading `---`-delimited front-matter block."""
    lines = markdown_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    front_matter: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator:
            front_matter[key.strip()] = value.strip()
    return front_matter


def check_env_d_units(manifest: InspirationManifest) -> tuple[str, ...]:
    """Problems with the declared env.d units, if any.

    A declared unit must live under the env.d directory, be a shell script,
    sort after the template's own units, and be carried by the recipe -- a unit
    the snapshot does not actually include is a manifest that lies about what
    an adopter will run.
    """
    problems: list[str] = []
    include_paths = manifest.recipe.include + manifest.recipe.data_include
    for unit in manifest.environment.env_d_units:
        if not unit.startswith(f"{ENV_D_DIRECTORY}/"):
            problems.append(f"env.d unit {unit!r} must live under {ENV_D_DIRECTORY}/")
            continue
        unit_name = unit[len(ENV_D_DIRECTORY) + 1 :]
        if not unit_name.endswith(".sh"):
            problems.append(f"env.d unit {unit!r} must be a .sh script")
        order_digits = unit_name.split("-", 1)[0]
        if not order_digits.isdigit():
            problems.append(
                f"env.d unit {unit!r} must be named <NNNN>-<slug>-<name>.sh (missing numeric order prefix)"
            )
        elif int(order_digits) < ENV_D_INSPIRATION_MINIMUM_ORDER:
            problems.append(
                f"env.d unit {unit!r} must sort at or after {ENV_D_INSPIRATION_MINIMUM_ORDER}, "
                "which is reserved for inspiration-carried units"
            )
        if not any(
            unit == path or unit.startswith(f"{path}/") for path in include_paths
        ):
            problems.append(
                f"env.d unit {unit!r} is declared but not covered by the recipe's include paths, "
                "so it would not ship in the snapshot"
            )
    return tuple(problems)


def check_markdown_agreement(
    manifest: InspirationManifest, markdown_text: str
) -> tuple[str, ...]:
    """Problems where `inspiration.md` and `inspiration.toml` disagree.

    The markdown keeps a human-readable front matter and Prerequisites list --
    both of which an adopter on an older template still reads -- while the TOML
    is authoritative. They are generated together, so any disagreement means
    one of them was hand-edited afterwards.
    """
    problems: list[str] = []
    front_matter = _parse_markdown_front_matter(markdown_text)
    expected_front_matter = {
        "title": manifest.inspiration.title,
        "description": manifest.inspiration.description,
        "thumbnail": manifest.inspiration.thumbnail,
    }
    for key, expected in expected_front_matter.items():
        actual = front_matter.get(key)
        if actual is None:
            problems.append(f"inspiration.md front matter is missing {key!r}")
        elif actual != expected:
            problems.append(
                f"inspiration.md front matter {key!r} is {actual!r} but inspiration.toml says {expected!r}"
            )

    declared_permissions = {
        f"{item.scope} / {item.permission}"
        for item in manifest.prerequisites.permission
    }
    markdown_permission_count = 0
    markdown_secret_count = 0
    has_markdown_llm_line = False
    for line in markdown_text.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith("requires_permission:"):
            markdown_permission_count += 1
        elif stripped.startswith("requires_secret:"):
            markdown_secret_count += 1
        elif stripped.startswith("requires_llm:"):
            has_markdown_llm_line = True
    if markdown_permission_count != len(declared_permissions):
        problems.append(
            f"inspiration.md lists {markdown_permission_count} requires_permission line(s) but "
            f"inspiration.toml declares {len(declared_permissions)}"
        )
    if markdown_secret_count != len(manifest.prerequisites.secret):
        problems.append(
            f"inspiration.md lists {markdown_secret_count} requires_secret line(s) but "
            f"inspiration.toml declares {len(manifest.prerequisites.secret)}"
        )
    if has_markdown_llm_line != (manifest.prerequisites.llm is not None):
        problems.append(
            "inspiration.md and inspiration.toml disagree about whether this inspiration needs LLM access"
        )
    return tuple(problems)


def check_unfinished_placeholders(*texts: str) -> tuple[str, ...]:
    """Problems where a generated FILL-IN block was never replaced.

    The same condition the publish flow's greps catch, folded in so one command
    is a complete gate rather than one of several a caller must remember.
    """
    problems: list[str] = []
    for text in texts:
        if "<!-- FILL-IN (publishing agent)" in text:
            problems.append(
                "an unfinished '<!-- FILL-IN (publishing agent)' block is still present"
            )
        if "minds-placeholder-thumbnail" in text:
            problems.append("the generated placeholder thumbnail was never replaced")
    return tuple(problems)


def validate_inspiration_tree(repo_root: Path) -> tuple[str, ...]:
    """Every problem with the inspiration published at `repo_root`, or ().

    Raises InspirationManifestNotFoundError / InspirationManifestParseError when
    the TOML itself is missing or unparseable -- those are failures to even
    reach the checks, not findings from them.
    """
    manifest_path = repo_root / MANIFEST_TOML_NAME
    manifest = load_inspiration_manifest(manifest_path)

    problems: list[str] = []
    markdown_path = repo_root / MANIFEST_MARKDOWN_NAME
    if not markdown_path.is_file():
        problems.append(f"{MANIFEST_MARKDOWN_NAME} is missing")
        markdown_text = ""
    else:
        markdown_text = markdown_path.read_text(encoding="utf-8", errors="replace")
        problems.extend(check_markdown_agreement(manifest, markdown_text))

    thumbnail_path = repo_root / manifest.inspiration.thumbnail
    if not thumbnail_path.is_file():
        problems.append(f"thumbnail {manifest.inspiration.thumbnail!r} is missing")
        thumbnail_text = ""
    else:
        thumbnail_text = thumbnail_path.read_text(encoding="utf-8", errors="replace")

    problems.extend(check_env_d_units(manifest))
    problems.extend(check_unfinished_placeholders(markdown_text, thumbnail_text))

    readme_path = repo_root / "README.md"
    if readme_path.is_file():
        problems.extend(
            check_unfinished_placeholders(
                readme_path.read_text(encoding="utf-8", errors="replace")
            )
        )

    if not manifest.recipe.include:
        problems.append("the recipe includes no paths, so there is nothing to publish")

    return tuple(problems)
