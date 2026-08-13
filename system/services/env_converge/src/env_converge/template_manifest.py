"""Schema for a template's machine-readable manifest (`template.toml`).

A template publishes three files at its repo root: `template.md` (prose,
requirements, and the two append-only history logs), `template.svg` (the
thumbnail / README hero), and `template.toml` -- this schema. The TOML is
authoritative for everything machine-readable: identity, the derivation recipe,
the requirements an adopter must activate or adapt, the environment they must
converge, and the
lineage of templates this one was built on.

There is exactly one manifest per repo. A newly published or newly adopted
template OVERRIDES the previous one rather than accumulating beside it; what
survives is the `[[lineage]]` chain, which records each predecessor's repo URL
and commit hash so the superseded manifest stays retrievable where it is
authoritative.

**Who imports this, and why it lives here.** One caller: the publish gate,
`validate_template.py`, which loads it *by file path* rather than as a package.
Nothing on the adopt side comes through here at all -- an agent reads the TOML
and makes what it can of it -- and `env_converge` itself does not import it
either. It sits in this package because the declaration is written in
env_converge's vocabulary: `[environment]` is source-for-source the shape of
the environment record -- apt, npm globals, uv tools, cargo -- plus the `env.d`
units for what has no package database to be recorded from. A template declares
its needs in the same terms the machine already records its own.

**Why it imports only pydantic and the standard library.** The gate runs inside
a worker's worktree that `build_template.sh` has just reset with
`git read-tree -u --reset` + `git clean -fdxq`, which deletes the gitignored
`.venv` -- so both the validator and this module are snapshotted out of the
tree before that reset and re-run under `uv run --no-project --with pydantic`.
That deliberately resolves no workspace project (the assembly script documents
why: building the full environment on a cold base is slow and can fail on an
unrelated build error, aborting an otherwise fine publish), which means no
workspace package is importable here.

That is why the frozen base below is declared locally instead of using
`imbue.imbue_common.frozen_model.FrozenModel`, which the style guide would
otherwise call for: `FrozenModel` imports `imbue.imbue_common.model_update`, a
workspace path dependency that is not available under `--no-project`. The
config below is the same one `FrozenModel` sets. `template_manifest_test.py`
asserts this module's import set stays within stdlib + pydantic so the
constraint cannot silently rot.
"""

import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The three files a template publishes at its repo root. No slug in any of
# them: one template per repo, overriding rather than accumulating.
MANIFEST_TOML_NAME = "template.toml"
MANIFEST_MARKDOWN_NAME = "template.md"
MANIFEST_THUMBNAIL_NAME = "template.svg"

# The manifest format this schema describes. `v1` is the pre-split format:
# slug-named `inspiration-<slug>.md` with the recipe as a YAML block inside the
# markdown and no sibling TOML. A v1 repo has no `template.toml` at all, so
# absence of the file -- not a version field inside it -- is what identifies v1.
CURRENT_MANIFEST_FORMAT = "v2"

# Template-carried env.d units live under this prefix and are ordered after
# the template's own units (1000-, 1100-), which is what makes composition a
# file addition rather than an edit to anything shared.
ENV_D_DIRECTORY = "system/scripts/env.d"
ENV_D_TEMPLATE_MINIMUM_ORDER = 2000


class TemplateManifestError(Exception):
    """Base exception for every template-manifest failure."""


class TemplateManifestNotFoundError(TemplateManifestError, FileNotFoundError):
    """Raised when the manifest file does not exist."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"No template manifest at {path}")


class TemplateManifestParseError(TemplateManifestError, ValueError):
    """Raised when the manifest is unreadable, malformed TOML, or fails validation."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Invalid template manifest at {path}: {reason}")


# Matches the slug rule the publish flow and `build_template.sh` enforce:
# ^[A-Za-z0-9._-]+$ with no leading '-'. Encoded as one pattern so the
# no-leading-dash rule is part of the type rather than a separate check.
Slug = Annotated[str, Field(pattern=r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")]

# A template's published version: v1 for a first publish, then v2, v3, ...
TemplateVersion = Annotated[str, Field(pattern=r"^v[1-9][0-9]*$")]

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


class TemplateIdentity(FrozenManifestModel):
    """Who this template is.

    The slug remains the identity -- it names the repo and keys the version
    ledger -- it just no longer appears in any filename.
    """

    slug: Slug = Field(
        description="Identity of the template; also the default repo name"
    )
    title: NonEmptyString = Field(description="Display title shown to a user")
    description: NonEmptyString = Field(
        description="One-line description of what it does"
    )
    thumbnail: NonEmptyString = Field(
        default=MANIFEST_THUMBNAIL_NAME,
        description="Repo-root-relative thumbnail path (the README's hero graphic)",
    )
    version: TemplateVersion = Field(
        description="Published version of this template (v1 for a first publish)"
    )


class Recipe(FrozenManifestModel):
    """How this template is DERIVED from the workspace it came from.

    A template is not a fork: an update re-runs this recipe against the
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
    """How the included code reaches its model.

    Recorded explicitly because the route is per-environment: an adopter may be
    on the other method than the one this code was written against, and must
    know to switch it. The value is a free string rather than an enum so a
    harness other than the one this workspace ships can name its own route.
    """

    method: NonEmptyString = Field(
        description="'keyed' (an API key, such as ANTHROPIC_API_KEY, via litellm) "
        "or 'keyless' (a subscription-backed CLI, such as claude -p)"
    )
    note: str = Field(
        default="", description="What an adopter on the other method must change"
    )


class AdaptationRequirement(FrozenManifestModel):
    """One thing the adapter must DECIDE or REWIRE to make this theirs.

    Prose rather than machine-readable, because it is worked through
    interactively with the user: a stubbed integration, a hardcoded account or
    channel, data that was deliberately not included.
    """

    summary: NonEmptyString = Field(description="What is missing or hardcoded")
    resolution: str = Field(
        default="", description="What a working replacement looks like"
    )


class Requirements(FrozenManifestModel):
    """Everything an adopter must deal with before this is really theirs.

    One list, deliberately -- an earlier split into "Prerequisites" and
    "Requirements" put two near-synonyms next to each other and made the
    publishing worker responsible for filing each item under the right heading,
    policed only by an instruction not to get it wrong.

    The distinction that actually matters is preserved, as the KIND of each
    item rather than which section it sits in, because the two are handled at
    different times by different mechanisms:

    - `permission`, `secret`, and `llm` are ACTIVATION requirements. The
      adopting agent acts on them FIRST and BY ITSELF -- it initiates each
      latchkey permission request before asking the user anything. They are
      machine-readable precisely because of a real incident where an adopter
      was never prompted for a Slack permission the app needed.
    - `adaptation` entries are worked through INTERACTIVELY afterwards, one at
      a time, with the user.

    So "activate everything typed, then walk the adaptation list" is derivable
    from the data instead of from a heading a human had to choose correctly.
    """

    permission: tuple[PermissionRequirement, ...] = Field(
        default=(),
        description="ACTIVATION: latchkey permissions the adopting agent initiates itself, first",
    )
    secret: tuple[SecretRequirement, ...] = Field(
        default=(), description="ACTIVATION: secrets the adopter must supply"
    )
    llm: LlmRequirement | None = Field(
        default=None,
        description="ACTIVATION: present whenever the included code calls an LLM",
    )
    adaptation: tuple[AdaptationRequirement, ...] = Field(
        default=(),
        description="ADAPTATION: design gaps resolved interactively with the user, after activation",
    )

    def has_activation_requirements(self) -> bool:
        """Whether anything must be activated before the template runs."""
        return bool(self.permission or self.secret or self.llm is not None)


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
        description=f"Repo-root-relative {ENV_D_DIRECTORY}/<NNNN>-<slug>-<name>.sh units carried by this template",
    )

    def is_empty(self) -> bool:
        """Whether this template declares no environment needs at all."""
        return not (
            self.apt
            or self.npm_global
            or self.uv_tools
            or self.cargo_crates
            or self.cargo_default_toolchain
            or self.env_d_units
        )


class LineageEntry(FrozenManifestModel):
    """One template this mind used on the way to producing this one.

    The commit hash is what makes overriding non-destructive: the superseded
    manifest stays readable by fetching that repo at that commit.
    """

    slug: Slug = Field(description="Identity of the predecessor template")
    repo_url: NonEmptyString = Field(
        description="Git repo URL the predecessor was used from"
    )
    commit: CommitHash = Field(
        description="Exact commit of the predecessor that was used"
    )
    used_on: date | None = Field(
        default=None, description="Date this mind adopted or built on it"
    )


class ManifestOrigin(FrozenManifestModel):
    """Where THIS COPY of the manifest was obtained from.

    Written by the adopt paths -- `use-template` knows the fetch URL and
    `FETCH_HEAD`, and a mind created from a template repo knows its parent
    -- and read by the publish flow, which turns it into the newest
    `[[lineage]]` entry when a new manifest overrides this one. Without it an
    override would lose the address of what it replaced, which is the one thing
    that makes overriding safe.

    Absent in a manifest a mind published itself (nothing was adopted) and in
    any v1 manifest (the field did not exist), so consumers treat absence as
    "unknown provenance", never as an error.
    """

    repo_url: NonEmptyString = Field(
        description="Git repo URL this copy was fetched from"
    )
    commit: CommitHash = Field(description="Exact commit this copy was taken at")
    adopted_on: date | None = Field(
        default=None, description="Date this mind adopted it"
    )


class TemplateManifest(FrozenManifestModel):
    """The whole of `template.toml`."""

    format: NonEmptyString = Field(
        default=CURRENT_MANIFEST_FORMAT, description="Manifest format version"
    )
    template: TemplateIdentity = Field(description="Identity of this template")
    origin: ManifestOrigin | None = Field(
        default=None,
        description="Where this copy came from; becomes a lineage entry when overridden",
    )
    recipe: Recipe = Field(
        description="How this template is derived from its source workspace"
    )
    requirements: Requirements = Field(
        default_factory=Requirements,
        description="Everything an adopter must activate or adapt",
    )
    environment: EnvironmentDeclaration = Field(
        default_factory=EnvironmentDeclaration,
        description="Packages and units an adopter must converge",
    )
    lineage: tuple[LineageEntry, ...] = Field(
        default=(), description="Templates this one was built on, oldest first"
    )


def find_manifest_path(workspace_dir: Path) -> Path | None:
    """The workspace's `template.toml`, or None when there is none.

    Absence is the normal case and is not an error: an ordinary workspace has
    no template, and a v1 template repo has slug-named markdown manifests
    and no TOML at all.
    """
    candidate = workspace_dir / MANIFEST_TOML_NAME
    return candidate if candidate.is_file() else None


def load_template_manifest(path: Path) -> TemplateManifest:
    """Parse and validate one `template.toml`, strictly.

    Strict is the right setting because the only thing that parses a manifest
    is the publish gate, and everything it reads it is about to write back out.
    An unrecognised key there is a typo in something we just wrote -- dropping
    `apt_packages` for `apt` quietly would mean quietly installing nothing.

    A manifest declaring a format this workspace does not write is refused
    outright, rather than read for the parts we happen to recognise. Reading it
    leniently would mean re-publishing a file we did not fully understand: the
    unknown tables are still in it, so stamping our own format on it produces
    something the next reader cannot parse, and stripping them deletes what its
    author declared. Neither is a thing to do to someone's published template.
    Nothing on the ADOPT path comes through here -- an agent reads the TOML and
    makes what it can of it -- so this refusal costs no one a template they
    could otherwise have used.

    Raises TemplateManifestNotFoundError when the file is absent, and
    TemplateManifestParseError when it cannot be read, is not valid TOML,
    declares a format this workspace does not write, or does not satisfy the
    schema.
    """
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as e:
        raise TemplateManifestNotFoundError(path) from e
    except OSError as e:
        raise TemplateManifestParseError(path, f"cannot read the file: {e}") from e
    try:
        raw_manifest = tomllib.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise TemplateManifestParseError(path, f"not valid UTF-8: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise TemplateManifestParseError(path, f"not valid TOML: {e}") from e
    declared_format = raw_manifest.get("format", CURRENT_MANIFEST_FORMAT)
    if declared_format != CURRENT_MANIFEST_FORMAT:
        raise TemplateManifestParseError(
            path,
            f"declares format {declared_format!r}, but this workspace writes "
            f"{CURRENT_MANIFEST_FORMAT!r}. Update this workspace before "
            "publishing a new version of this template, or it would lose what "
            "the newer format declares.",
        )
    try:
        return TemplateManifest.model_validate(raw_manifest)
    except ValidationError as e:
        raise TemplateManifestParseError(path, str(e)) from e


def lineage_after_override(
    previous: TemplateManifest | None, used_on: date | None = None
) -> tuple[LineageEntry, ...]:
    """The lineage a new manifest inherits when it overrides `previous`.

    The chain is transitive: the predecessor's own lineage comes through first
    (oldest first), then the predecessor itself. A predecessor with no
    `[origin]` -- one this mind published rather than adopted, or a v1 manifest
    from before the field existed -- contributes no link of its own, because
    there is no address to record; its inherited chain still carries through
    rather than being dropped.
    """
    if previous is None:
        return ()
    if previous.origin is None:
        return previous.lineage
    return previous.lineage + (
        LineageEntry(
            slug=previous.template.slug,
            repo_url=previous.origin.repo_url,
            commit=previous.origin.commit,
            used_on=used_on if used_on is not None else previous.origin.adopted_on,
        ),
    )


# Markdown HTML comments, including an unterminated trailing one.
_HTML_COMMENT_PATTERN = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)


def _strip_html_comments(markdown_text: str) -> str:
    """The markdown with `<!-- ... -->` blocks removed.

    The generated FILL-IN instructions are HTML comments and they quote example
    `requires_permission:` / `requires_secret:` / `requires_llm:` lines to show
    the form. Counting those as declarations makes a freshly-generated skeleton
    look like it declares three activation requirements it does not have.
    """
    return _HTML_COMMENT_PATTERN.sub("", markdown_text)


def _parse_front_matter_scalar(raw_value: str) -> str:
    """One YAML scalar from a front-matter line, unquoted if it was quoted.

    Front-matter values are the user's own words -- a title like
    `The "Daily" Digest: v2` has to be emitted double-quoted or YAML reads it
    as something else entirely. Parsing has to undo exactly that, or every
    generated manifest would look like it disagreed with its TOML.

    Only the flat scalar forms front matter actually uses are handled: a
    double-quoted string with backslash escapes, a single-quoted string with
    YAML's doubled-quote escape, or a plain scalar.
    """
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        body = value[1:-1]
        result: list[str] = []
        is_escaped = False
        for character in body:
            if is_escaped:
                result.append({"n": "\n", "t": "\t"}.get(character, character))
                is_escaped = False
            elif character == "\\":
                is_escaped = True
            else:
                result.append(character)
        return "".join(result)
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


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
            front_matter[key.strip()] = _parse_front_matter_scalar(value)
    return front_matter


def check_env_d_units(manifest: TemplateManifest) -> tuple[str, ...]:
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
        elif int(order_digits) < ENV_D_TEMPLATE_MINIMUM_ORDER:
            problems.append(
                f"env.d unit {unit!r} must sort at or after {ENV_D_TEMPLATE_MINIMUM_ORDER}, "
                "which is reserved for template-carried units"
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
    manifest: TemplateManifest, markdown_text: str
) -> tuple[str, ...]:
    """Problems where `template.md` and `template.toml` disagree.

    The markdown keeps a human-readable front matter and `requires_` lines --
    both of which an adopter on an older template still reads -- while the TOML
    is authoritative. They are generated together, so any disagreement means
    one of them was hand-edited afterwards. Only the ACTIVATION requirements are
    cross-checked: the adaptation entries are prose in both files by design, so
    there is nothing to compare one-for-one.
    """
    problems: list[str] = []
    front_matter = _parse_markdown_front_matter(markdown_text)
    expected_front_matter = {
        "title": manifest.template.title,
        "description": manifest.template.description,
        "thumbnail": manifest.template.thumbnail,
    }
    for key, expected in expected_front_matter.items():
        actual = front_matter.get(key)
        if actual is None:
            problems.append(f"template.md front matter is missing {key!r}")
        elif actual != expected:
            problems.append(
                f"template.md front matter {key!r} is {actual!r} but template.toml says {expected!r}"
            )

    declared_permissions = {
        f"{item.scope} / {item.permission}" for item in manifest.requirements.permission
    }
    markdown_permission_count = 0
    markdown_secret_count = 0
    has_markdown_llm_line = False
    for line in _strip_html_comments(markdown_text).splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith("requires_permission:"):
            markdown_permission_count += 1
        elif stripped.startswith("requires_secret:"):
            markdown_secret_count += 1
        elif stripped.startswith("requires_llm:"):
            has_markdown_llm_line = True
    if markdown_permission_count != len(declared_permissions):
        problems.append(
            f"template.md lists {markdown_permission_count} requires_permission line(s) but "
            f"template.toml declares {len(declared_permissions)}"
        )
    if markdown_secret_count != len(manifest.requirements.secret):
        problems.append(
            f"template.md lists {markdown_secret_count} requires_secret line(s) but "
            f"template.toml declares {len(manifest.requirements.secret)}"
        )
    if has_markdown_llm_line != (manifest.requirements.llm is not None):
        problems.append(
            "template.md and template.toml disagree about whether this template needs LLM access"
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


def validate_template_tree(
    repo_root: Path, is_unfinished_allowed: bool = False
) -> tuple[str, ...]:
    """Every problem with the template published at `repo_root`, or ().

    `is_unfinished_allowed` is for the one caller that runs immediately after
    generation: `build_template.sh` writes the FILL-IN blocks and the
    placeholder thumbnail on purpose and then checks that the skeleton it just
    produced is well-formed, so flagging its own placeholders would fail every
    publish at step one. Every later run -- the worker's, and the lead's before
    the push -- leaves it False, which is what actually blocks an unfinished
    manifest from being published.

    Raises TemplateManifestNotFoundError / TemplateManifestParseError when
    the TOML itself is missing or unparseable -- those are failures to even
    reach the checks, not findings from them.
    """
    manifest_path = repo_root / MANIFEST_TOML_NAME
    manifest = load_template_manifest(manifest_path)

    problems: list[str] = []
    markdown_path = repo_root / MANIFEST_MARKDOWN_NAME
    if not markdown_path.is_file():
        problems.append(f"{MANIFEST_MARKDOWN_NAME} is missing")
        markdown_text = ""
    else:
        markdown_text = markdown_path.read_text(encoding="utf-8", errors="replace")
        problems.extend(check_markdown_agreement(manifest, markdown_text))

    thumbnail_path = repo_root / manifest.template.thumbnail
    if not thumbnail_path.is_file():
        problems.append(f"thumbnail {manifest.template.thumbnail!r} is missing")
        thumbnail_text = ""
    else:
        thumbnail_text = thumbnail_path.read_text(encoding="utf-8", errors="replace")

    problems.extend(check_env_d_units(manifest))
    # `check_env_d_units` proves a unit is named right and WOULD ship if it
    # existed; only the assembled tree can prove it does. A declared unit with
    # a typo in its path passes every name check and then simply never runs on
    # the adopter's machine, which is the failure this manifest exists to stop.
    for unit in manifest.environment.env_d_units:
        if not (repo_root / unit).is_file():
            problems.append(f"env.d unit {unit!r} is declared but is not in the tree")

    if not is_unfinished_allowed:
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
