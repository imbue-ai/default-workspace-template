# Plan: template environment manifests

> **Give every template a pydantic-validated `template.toml` that declares what its code needs from the environment -- apt / npm / uv / cargo packages and the exotic-install units that have no package database -- validated against the mirrored apt universe at publish time, and installed by the adopting agent at the ADOPTER's pinned snapshot timestamp, so an adopting mind stops discovering atemplate's system dependencies by running into failures.**
>
> ### The gap
> * Atemplate declares its runtime needs only as prose plus `requires_permission:` / `requires_secret:` / `requires_llm:` lines in `template-<slug>.md` (the v1 format). Those cover *permissions and secrets*. They say nothing about **system packages**: atemplate whose code shells out to `pdftotext`, or imports a python package installed as a `uv tool`, ships no record of it. The adopter finds out when the app crashes.
> * Everything machine-readable in the manifest today lives inside markdown -- the Recipe is a fenced **YAML** block (the style guide says never YAML), the prerequisites are hand-written lines matched by grep. Nothing validates either; `build_template.sh` generates placeholders and the only enforcement is a `grep` for un-replaced FILL-IN comments.
>
> ### Recommended mechanism
> * **A sibling `template.toml`** carrying the machine-readable manifest: identity, the recipe, a structured mirror of the requirements, the lineage of what it overrode, and a new `[environment]` section shaped to mirror the env-converge record (`apt` names + the publisher's snapshot timestamp as provenance; `npm_global` / `uv_tools` / `cargo` as name -> version maps; carried `env.d` units by path). Prose and the two append-only history logs stay in the `.md`.
> * **The schema is one pydantic module owned by `env_converge`**, used by both sides: the publish-time gate validates against it, and the adopting agent reads the declaration through it.
> * **Adoption is the adopting agent installing what the manifest lists**, with ordinary `apt-get` / `npm -g` / `uv tool` / `cargo install` commands -- no new engine. Two existing facts make that sufficient: the workspace's apt sources are already pinned, so bare names resolve at the ADOPTER's timestamp; and env-converge already captures whatever gets installed, so it lands in the record without any bookkeeping.
> * **Publish-time validation resolves every declared apt package** against the pinned mirror at the publisher's timestamp, so an unmirrorable third-party package is rejected at the earliest possible moment rather than at some adopter's first boot.
>
> ### Scope note
> * Strictly additive to the publish flow. Both hard gates (§1 scope, §6 confirmation), the CWD / no-merge-back invariant, the bootable-or-nothing rule, and the hard-failing secret scan are preserved verbatim; the new TOML is covered by the existing scan and the new validation is an additional hard gate, never a relaxation of an existing one.

## Overview

- Atemplate is a bootable snapshot of what a mind built, published to a GitHub repo another mind can be created from or adapt. `publish-template` assembles it, `use-template` adopts it, and `update-published-template` / `update-installed-template` move it forward on each side.
- `env_converge` (already in the tree) gives the environment side a principled home for declarations it did not have when the templates flow was designed: a record of everything installed, a convergence pass that replays it, a `package_unavailable` event, an `env.d` unit convention for things with no package database, and an apt universe pinned to a single committed timestamp.
- This plan connects the two. The structuring principle is the one `env_converge` already states: **versions are a function of the pinned snapshot timestamp**, so replaying package *names* at a timestamp yields deterministic *versions*. That is why atemplate declares names rather than versions for apt, and why converging at the **adopter's** timestamp (not the publisher's) is the correct merge: the adopter gets versions consistent with the rest of their environment, and the publisher's timestamp is kept only as provenance to explain a skew.
- The work splits cleanly into a schema, a publish-side gate, the adopt-side install step, and the two update paths that must read the recipe from its new home. Each is independently landable; the phasing at the end reflects that.

## The problem, grounded

Read from the tree as it stands (`a072097f`):

- **`build_template.sh` step 6** writes the manifest with front-matter `title` / `description` / `thumbnail` / `version` / `format`, a `## Recipe` block that is fenced **`yaml`**, and FILL-IN comment blocks for every prose section. Step 8.5 writes `README.md`, step 8 writes the template-specific `/welcome`, step 8.6 removes `docs/VERSION_HISTORY.md`.
- **Nothing is validated.** The script's gates are: the base-ref tree check (exit 5), the secret scan (exit 1), the no-diff guard (exit 3), and the supervisord smoke check (exit 4). The manifest itself is checked only by the worker's and the lead's `grep -n -- '<!-- FILL-IN (publishing agent)'`. A manifest whose `requires_permission:` line names a scope that does not exist, or whose Recipe YAML is malformed, publishes cleanly.
- **System packages are entirely absent from the model.** `## Prerequisites` is documented as "one line per **activation** requirement" -- permissions, secrets, LLM access. There is no place to say "this app shells out to `pdftotext`", and `use-template` §3 correspondingly has nothing to install.
- **The recipe's only reader is the publisher's own update flow.** `update-published-template` §2c reads `include` / `data_include` / `exclude` / `modification_rules` out of the fetched tip's `## Recipe` block. `use-template` and `update-installed-template` never read it. This matters for the compatibility story below.
- **`env_converge` already anticipates this work.** Its README calls cargo "a non-critical source" whose record "matters for template manifests and genuinely fresh homes, not ordinary restores" -- the cargo record exists *because* of this use case, and leaving cargo out of the declaration shape would leave that stated purpose unfulfilled.

## One manifest, not an accumulating pile

The manifest files lose the slug from their names and become **`template.md`, `template.toml`, and `template.svg`** at the repo root. There is exactly one of each, and a newly published or newly adopted template **overrides** the previous one rather than landing beside it.

This replaces the accumulation model. Today `build_template.sh` step 1 carries every pre-existing `template-*.md` and `.svg` forward into each new snapshot, `README.md` lists them all, and `use-template` §2 has to pick "the latest slug named in the welcome skill" out of the pile. That pile grows without bound, and every reader needs disambiguation logic for a case that is almost always "just use the newest".

What replaces it is a **lineage chain**: the new manifest records, for every template this mind used on the way to producing it, the git repo URL and the exact commit hash. The prior manifest's content is not copied forward -- its *address* is. Because the address includes a commit hash, the superseded manifest stays fully recoverable: you fetch that repo at that commit and read it there, in the place where it is authoritative.

```toml
# Oldest first. Each entry is one template this mind used.
[[lineage]]
slug = "note-taker"
repo_url = "https://github.com/someone/note-taker"
commit = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
used_on = "2026-08-04"
```

The `.md` mirrors this as a short "Built on" section of plain markdown links, so a human reading the published page can follow the chain without opening the TOML.

Lineage accrues at both moments a manifest is written:

- **On adopt** (`use-template`): the incoming manifest overrides the local one, and gains a lineage entry for whatever it displaced -- repo URL and commit taken from the fetch (`FETCH_HEAD`) on the merge path, or from `system/config/parent.toml` on the template path.
- **On publish** (`build_template.sh`): the pre-existing `template.toml` is read *before* it is overwritten; its identity becomes the newest lineage entry and its own `[[lineage]]` entries are carried through ahead of it. The chain is transitive, so a third-generation template names all three.

**The tradeoff, stated plainly:** when B overrides A, A's *code* is still in the tree -- the merge brought it in and nothing removes it -- but A is no longer documented in place, only linked. That is a deliberate loss of local legibility bought back by the commit hash, which makes A's own manifest retrievable at exactly the state this mind used. It is worth being explicit that a mind can therefore be running an app whose manifest lives one repo away.

**Lineage is provenance, not dependency.** A manifest's `[environment]` declares what *this snapshot's included code* needs, determined at publish time from the tree being published -- never a union of its ancestors' declarations. If B's snapshot includes A's app, B declares A's packages because they are B's packages now; if it does not, it must not.

### What moves, what stays, and why

| Content | Home in v2 | Reason |
|---|---|---|
| Recipe (`include` / `data_include` / `exclude` / `modification_rules`) | **`.toml` only**; the `.md`'s `## Recipe` section becomes a one-line pointer | Its only reader is `update-published-template`, which runs in the *publisher's own* mind -- the same mind that just published v2, hence on a v2-aware template. Nothing older reads it, so a strict move is safe. Also retires a YAML block the style guide forbids. |
| Requirements -- activation half (`requires_*` lines) | **`.md` (unchanged) + structured mirror in `.toml`** | `use-template` reads these, and an adopter may be on an *older* template that knows nothing about the `.toml`. The human-facing lines must keep working verbatim. Validation asserts the two agree one-for-one. |
| Requirements -- adaptation half | **`.md` prose + `[[requirements.adaptation]]` mirror** | Prose on both sides; nothing to cross-check, so validation leaves it alone. |
| Front-matter (`title` / `description` / `thumbnail` / `version`) | **`.md` (unchanged) + mirror in `.toml`**, `format: v2` | Same reason: older adopters and the generated README/welcome read the front-matter. Both are written in one pass by the generator, and validation asserts agreement, so drift is not possible in practice and is caught if it happens. |
| `[environment]` declarations | **`.toml` only** | New; nothing older reads it, and an older adopter simply does not converge it (see compatibility). |
| Lineage (repo URL + commit per ancestor) | **`.toml`**, mirrored as a "Built on" link list in the `.md` | Machine-readable provenance; the `.md` mirror is what a human follows from the GitHub page. |
| Prose (`What it is`, `How it works`, `How to adapt it`) | **`.md` only** | Not machine-readable. |
| `Publication history`, `Adaptation history` | **`.md` only** | The two append-only logs; see the note on enforcement. |

### One "Requirements" list, replacing Holes AND Prerequisites

The manifest had two sections for "things an adopter must deal with":
**Prerequisites** (activation -- permissions, secrets, LLM access) and **Holes**
(adaptation -- stubbed integrations, hardcoded accounts). Renaming Holes to
Requirements, as first planned, put two near-synonyms side by side and left the
publishing worker responsible for filing each item under the right heading,
policed only by an instruction not to get it wrong.

**They merge into one `## Requirements` section**, in the markdown and as one
`[requirements]` table in the TOML.

The distinction that actually matters is preserved -- as the **kind of each
entry** rather than which section it sits in -- because the two are handled at
different times by different mechanisms:

- **Activation** (`[[requirements.permission]]`, `[[requirements.secret]]`,
  `[requirements.llm]`, and the `requires_` lines that mirror them in the
  markdown): the adopting agent acts on these FIRST and BY ITSELF, initiating
  each latchkey request before asking the user anything. They stay
  machine-readable and cross-checked between the two files, because of a real
  incident where an adopter was never prompted for a Slack permission the app
  needed. **This is the property a merge must not lose**, and flattening both
  kinds into prose would have lost it.
- **Adaptation** (`[[requirements.adaptation]]`, prose bullets in the markdown):
  worked through interactively with the user, after activation. Prose on both
  sides, so it is not cross-checked -- there is nothing to compare one-for-one.

So "activate everything typed, then walk the adaptation list" is derivable from
the data instead of from a heading a human had to choose correctly. The merge is
a net simplification *and* removes a failure mode; it is not a loss of rigour.

`Environment` stays its own section: it is not a decision the adopter makes at
all, it is what gets installed, and it is converged rather than resolved.

### Proposed `.toml` shape

```toml
format = "v2"

[template]
slug = "slack-inbox"                # still the identity (repo name, ledger key); no longer in any filename
title = "Slack Inbox"
description = "A daily digest of the channels you actually read."
thumbnail = "template.svg"
version = "v1"

# Every template this mind used on the way to this one, oldest first.
[[lineage]]
slug = "note-taker"
repo_url = "https://github.com/someone/note-taker"
commit = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
used_on = "2026-08-04"

[recipe]
include = ["system/apps/slack_inbox"]
data_include = []
exclude = ["system/apps/slack_inbox/fixtures/personal"]
modification_rules = ["replace the hardcoded team Slack channel with a neutral default"]

[[prerequisites.permission]]
scope = "slack-api"
permission = "slack-read-all"
note = "reads channel history for the digest"

[[prerequisites.secret]]
name = "SLACK_SIGNING_SECRET"
note = "verifies inbound webhooks; set in the app's config"

[prerequisites.llm]
method = "keyed"                    # keyed (ANTHROPIC_API_KEY -> litellm) | keyless (claude -p)
note = "summarization step; an adopter on the keyless path must switch it per use-ai-integration"

[environment]
# Provenance only. Convergence installs at the ADOPTER's timestamp, never this one.
apt_snapshot_timestamp = "20260725T000000Z"
# Name-level pins: versions follow from whichever timestamp converges them.
apt = ["poppler-utils", "ripgrep"]
# Carried env.d units, repo-root-relative, for installs with no package database.
env_d_units = ["system/scripts/env.d/2000-slack-inbox-fonts.sh"]
cargo_default_toolchain = "stable-x86_64-unknown-linux-gnu"

# Name -> version, mirroring the record's version_by_* maps. These sources are
# not snapshot-pinned, so the version IS the pin.
[environment.npm_global]
"@slack/cli" = "2.1.0"

[environment.uv_tools]
yt-dlp = "2026.7.1"

[environment.cargo_crates]
fd-find = "9.0.0"
```

The shape deliberately mirrors `env_converge/data_types.py`: `apt` is a name list because `AptState.manual_packages` is the replay input and versions follow from the timestamp; `npm_global` / `uv_tools` / `cargo_crates` are name -> version maps because `NpmGlobalState.version_by_package`, `UvToolState.version_by_tool`, and `CargoState.version_by_crate` are, and their registries are not snapshot-pinned, so the recorded version is the only available pin. `cargo_default_toolchain` mirrors `CargoState.default_toolchain`. Keeping the two shapes isomorphic is what makes "declare what you captured" a mechanical step for the publishing worker rather than a translation exercise.

### Cargo: in, and why

**Cargo joins `apt` / `npm_global` / `uv_tools` in the declaration shape.** The env-converge README states the reason directly: unlike npm globals, `~/.cargo/bin` binaries ride the backup as real files, so the cargo record is irrelevant to ordinary restores and matters for "template manifests and genuinely fresh homes". Atemplate crossing to a different machine is precisely the fresh-home case, and it is the one the README names. Omitting cargo would leave a gap the existing design already anticipated, for the sake of one table and one branch in the converge path -- and `_install_missing_cargo` already exists to reuse.

One consequence to handle explicitly: cargo replay reports `package_unavailable` when rust itself is absent rather than bootstrapping rustup. So a declared crate on an adopter without rust surfaces as unavailable with a *different* remedy than a timestamp skew -- "install rust", not "run the upgrade". The prompt must distinguish these two causes rather than printing one generic message (see "the unavailable prompt" below).

### Schema ownership and the no-venv constraint

The schema is **one pydantic module, `system/services/env_converge/src/env_converge/template_manifest.py`**, used by both sides. There must not be two definitions -- a publish-side validator that drifts from the adopt-side reader is the failure mode this whole change exists to prevent.

The sharp constraint is that the publish-time gate runs in a worktree that **has no virtualenv**: `build_template.sh` step 3 does `git read-tree -u --reset` + `git clean -fdxq`, and `.venv` is gitignored, so it is deleted. The script already avoids `uv run` deliberately -- its own comment explains that resolving the workspace environment costs many seconds on a cold base and "can fail outright on an unrelated build error, spuriously aborting a publish that is otherwise fine."

Resolution:

- The module imports **only `pydantic` and the standard library** (`tomllib`). It is snapshotted out of the worktree *before* the reset, exactly as `scan_secrets.sh` and `betterleaks.toml` already are -- that loop takes a list and this is one more entry -- and invoked as `uv run --no-project --with 'pydantic>=2' python <snapshot>/validate_template.py <toml>`. `--no-project` means uv never touches the workspace project, so the failure mode the script's comment warns about cannot occur.
- **Deviation to flag:** this means the module cannot use `FrozenModel`, because `FrozenModel` imports `imbue.imbue_common.model_update`, which is a workspace path dependency that `--no-project --with pydantic` cannot supply. The module will define a local base with `model_config = ConfigDict(frozen=True, extra="forbid")` -- the same configuration `FrozenModel` sets -- and say why in its docstring. The alternative (snapshotting `imbue_common` too, or `uv sync`ing the worktree) reintroduces exactly the cost and fragility the script avoids. **Called out here because it is a deliberate style-guide deviation; say the word and I will take the heavier alternative instead.**
- A unit test asserts the module's import set stays within stdlib + pydantic, so the constraint cannot silently rot.

## Publish-time validation

Three invocation points, because the manifest is only complete after the worker fills it in -- `build_template.sh` generates a skeleton and exits, so a gate inside the script alone cannot see the finished content:

1. **In `build_template.sh`, right after generation.** Validates the generated skeleton parses and is internally consistent, and runs the apt-resolution check over any declared packages. Fails the script with a **new exit code 6** (manifest validation / dependency resolution), documented in `publish-template` §5 alongside the existing 1/2/3/4/5.
2. **By the worker, after filling in the FILL-INs** (§3 step 6's self-check, next to the existing greps). This is the invocation that actually sees real declarations, so it is where the apt-resolution check earns its keep.
3. **By the lead, as part of §8's pre-push checklist**, next to the placeholder-thumbnail gate. Same script, same exit code; verification, never a substitute for the §6 user confirmation.

### The apt-resolution check

Every name in `[environment].apt` must resolve in the mirrored universe at the publisher's pinned timestamp. The worker's container already has the pinned sources written by `write_apt_sources.sh`, so this is a local index query:

```bash
apt-get install --no-install-recommends --dry-run -qq <pkg>...
```

A package that does not resolve fails the gate with the offending names, so an unmirrorable third-party source is rejected at publish rather than at some adopter's first boot. Following the secret-scan precedent, a **missing `apt-get` is a hard failure, not a skip** -- the publish flow only ever runs inside the workspace container, and a silent skip would turn a broken environment into a publish that ships unverifiable declarations. (This does mean the shell gate cannot run on a macOS dev box; the pydantic validator and its tests can, and that is where the unit tests live.)

### Secret-scan coverage

The new `.toml` is generated at the repo root **after** the scan, exactly like the `.md` manifest, the `.svg`, the `/welcome`, and `README.md` -- all of which are generated post-scan today. The scan covers the *staged overlay* (content coming out of the live mind), which is where a secret can actually ride in; generated files are written from arguments the lead already resolved with the user. Two additions keep that reasoning honest rather than assumed:

- Any `env.d` unit atemplate carries is an **included path**, so it is staged and therefore scanned like any other overlaid file. This is stated explicitly so nobody later "optimizes" unit declaration into a generated file.
- The worker's §3 step 2 already re-runs `scan_secrets.sh` over every file it modifies after applying published-version modifications; filling in the `.toml` puts it in that set.

## Adoption: the agent installs what the manifest declares

The declaration is a **to-do list for the adopting agent**, not an input to a new
engine. `use-template` reads `[environment]` and runs the ordinary
`apt-get` / `npm -g` / `uv tool` / `cargo install` commands itself.

This is deliberately less machinery than the first draft of this plan proposed
(a "declared source" inside env-converge, with a `declared.json` recording which
`(slug, version)` sets had been applied). That design solved problems this one
does not have:

- **Converging at the adopter's timestamp is already true.** The workspace's apt
  sources are pinned by `write_apt_sources.sh`, so a bare `apt-get install <name>`
  resolves at THIS mind's snapshot. No code needed to arrange it -- it is why the
  manifest declares names rather than versions.
- **Capture is already automatic.** env-converge's stated model is "agents install
  things normally; nothing needs to be declared" -- a dpkg post-invoke hook plus
  boot-time probes record whatever appears. Anything the agent installs lands in
  the record and survives a rebuild or restore with no extra bookkeeping.
- **Apply-once stops being a problem.** A one-time install during adoption cannot
  fight removal stickiness, because there is no re-application loop to fight it.
  `declared.json` existed only to stop a boot-time replay resurrecting a package
  the user deliberately removed; with no replay, the whole mechanism is moot.
- **env.d units need nothing at all.** They are files in the tree; env-converge
  already runs them on the next boot.

So the model is unchanged rather than extended: the manifest tells the agent what
to install, the agent installs it normally, and the existing capture makes it
durable. What the skill must do well is the *failure* case -- naming the package
and distinguishing a timestamp skew (offer `env-converge upgrade`) from cargo
entries with rust absent (an upgrade will not help), instead of pressing on into
a setup that cannot work.

## Composition by file addition

The spec's "file-addition rather than file-merge where cheap" applies directly to `env.d`:

- Atemplate carrying an exotic install ships `system/scripts/env.d/<NNNN>-<slug>-<name>.sh` as a normal included path. The adopter's merge lands it as a new file; `env_converge` runs it on the next slow phase. **No new machinery is required at all** -- only a convention and a declaration.
- **Convention: `NNNN >= 2000` for template-carried units**, with the slug in the filename. The template's own units are 1000 (playwright) and 1100 (secret scanners), so 2000+ keeps template units ordered after the template's and makes cross-template collisions structurally unlikely.
- `[environment].env_d_units` lists them so publish-time validation can assert each declared path actually exists in the assembled tree and sits under `system/scripts/env.d/`, and so the adopting agent can tell the user what will run. Validation also asserts each is inside the recipe's include set -- a declared unit that was not included would be a manifest that lies.
- Supervisord `[include]` drop-ins are the same idea for services, but `system/supervisord.conf` has no `[include]` section today and adding one is a separate change with its own boot-risk surface. **Out of scope here**, noted so the pattern is not forgotten.

## Compatibility

The constraint is that a manifest with no sibling `.toml` (`format: v1`) keeps working on the adopt path. v1 is recognised by its *filenames* as well as its front-matter: a v1 repo has one or more slug-named `template-<slug>.md` files and no `template.toml`. Both directions:

- **Old manifest, new code.** `use-template` §2 looks for `template.toml` first; absent, it falls back to globbing `template-*.md` and reads exactly what it reads today -- including the existing "pick the latest slug named in the welcome skill" disambiguation, which stays in the skill purely as the v1 read path. It skips the environment step entirely. `env_converge` finds no `template.toml` and contributes nothing, so a v1 template converges precisely as it does now. `update-published-template` §2c falls back to the `.md`'s `## Recipe` YAML block when the tip carries no `.toml`.
- **Renaming on migration.** When the publisher's update migrates v1 -> v2, it `git mv`s `template-<slug>.md` / `.svg` to `template.md` / `template.svg` and writes `template.toml`. Any *other* accumulated `template-*.md` in that repo is not carried forward -- it becomes a lineage entry instead, which is the whole point of the chain. The commit hash in each entry is what makes that non-destructive.
- **New manifest, old code.** The `.md` keeps its front-matter and its `## Prerequisites` lines verbatim, which is everything an older `use-template` reads. It gets the app and the setup agenda; it simply does not converge the environment -- degraded, not broken. This is the whole reason Prerequisites and front-matter are mirrored rather than moved.
- **Migrating a v1 template to v2 -- always, never optional.** The governing rule is **read the old format, always write the new one**: nothing this flow touches is left on v1. `update-published-template` writes the `.toml` whenever it cuts the next version, lifting the recipe out of the `.md`, and states it at the §2e scope gate as a fact rather than an offer. There is no "stay on v1" path and no flag to request one -- carrying two live write-formats is exactly the drift this change exists to remove.
- **The one place migration is deliberately NOT automatic: the adopt path.** `use-template` reads a v1 manifest natively and does not synthesize a `.toml` from it. Synthesizing would mean parsing hand-written markdown prose (`requires_permission:` lines a human typed) into a validated schema -- reintroducing precisely the fragility the `.toml` exists to eliminate, and doing it silently on someone else's published content. A v1 template becomes v2 when its **publisher** next updates it, which is the moment someone with authority over that content is in the loop.
- `format: v2` in the `.md` front-matter is the discriminator; absent `format` continues to mean v1 exactly as `use-template` §2 already says.

## The generated README

The published repo's `README.md` is its GitHub landing page -- the thing that decides whether a person boots the template at all. Today `build_template.sh` step 8.5 generates a title, the one-line description, one FILL-IN overview, and a "Use it" section. The recipe it should follow instead, in order:

1. **A hero graphic** at the top -- a hand-authored SVG scales best. The bespoke thumbnail the worker already designs (`template.svg`) is exactly this asset, so the README references it rather than commissioning a second one.
2. **The "Open in Minds" call-to-action** -- no prose, just a large centered button plus a one-line copyable fallback beneath it. The button points at the HTTPS trampoline (`https://boweiliu.github.io/open-in-minds/?git_url=<repo url>`); a bare `minds://` link is never used, because GitHub renders it dead. The fallback line is `/use-template <repo url>`.
3. **Why you care** -- one or two plain sentences on the problem it solves.
4. **How to use it** -- how someone actually uses the thing once it is running: the commands, endpoints, workflow, or setup steps it exposes. This is the heart of the page; concise and readable beats exhaustive (a short list or two worked examples beats a wall of prose).
5. **Ideas for making it yours** -- three to five concrete, specific changes someone could make after adopting it ("point it at a different channel", "swap the digest for a weekly summary", "add a second source alongside Slack"). This is the section that turns a reader into an adopter: it shows the thing is a starting point rather than a finished artifact.
6. **Anything else** the app needs -- screenshots with captions, config, security notes, architecture.

**Ideas are not Requirements.** The manifest's Requirements section is the must-decide list -- what is stubbed or hardcoded and *has* to be resolved for the thing to be the adopter's. Ideas are optional invitations: things that already work fine but could be taken somewhere else. Keeping them distinct matters, because a reader who cannot tell "you must fix this" from "you could try this" will read the whole page as a list of defects. The worker fills both, from the same knowledge of the app, and is told explicitly not to duplicate an item across them.

### The repo-URL bootstrapping problem

Both the button and the fallback line need the repo URL -- **which does not exist when the README is generated.** `build_template.sh` runs in §3; the repo name is only confirmed by the user in §6 and the owner is only known from the create call's `.owner.login` in §8 step 1. Resolution, matching the design language already in the flow:

- The script generates the CTA with a distinctive placeholder token, exactly as it already does for the thumbnail's `minds-placeholder-thumbnail` marker.
- The lead resolves `owner` in §7 (the `latchkey curl -sf https://api.github.com/user` probe it already runs returns the login) and `repo_name` in §6, substitutes both, and commits **before** §8's push -- so the existing "never push then fix up with a second commit" rule holds.
- §8's pre-push checklist gains a grep for the leftover token, alongside the placeholder-thumbnail gate it already runs. A README that still carries the placeholder blocks the push.

### Verification

The instructions require an agent to verify the rendered page rather than hand the user raw markdown. Bound to tooling that actually exists in this repo:

- **After pushing, verify the live GitHub page** in the embedded Chromium via the `agentic-browser-fleet` skill: the hero graphic and screenshots render with no broken images (a relative path that is right in the source tree can 404 on github.com -- this is the only check that catches it), the Open in Minds badge loads and links to the trampoline, and the copyable `/use-template` line and every other link are correct. Fix and re-push until clean.
- **Before shipping, render the README and open it in a tab.** The tooling the instructions name did not exist in this repo, so it is added here: `system/scripts/render_markdown_preview.py` renders a markdown file the way GitHub will (raw HTML, the centered hero, the badge, tables) and `markdown_preview_server.py` serves it as the `markdown-preview` service, which `layout.py open service:markdown-preview` puts in a tab. Crucially it serves the previewed file's OWN directory alongside the page, so a relative `template.svg` resolves -- a preview that showed every local image broken would hide the exact class of bug it exists to catch. No new dependency: markdown-it-py's `js-default` preset gives tables plus raw HTML without `mdit_py_plugins` or `linkify-it-py` (the `gfm-like` preset raises without the latter).

## What this does not touch

Stated explicitly because each exists because of a real incident:

- **`publish-template` §1's scope gate and §6's confirmation gate** are unchanged. The new environment declarations are *surfaced* at §6 alongside the permissions and secrets already recapped there ("this will also install: ...") -- more information at the same gate, never a new path around it.
- **One sentence is added at §6's visibility line: if the user chooses public, the template ships under the MIT license.** It is stated at the gate where visibility is already being confirmed, so the licensing consequence of going public is in front of the user at the moment they make that choice. (Note for later: this *states* the license rather than shipping one -- no `LICENSE` file is generated. If it should be enforced rather than announced, that is a small follow-up, but it is deliberately not smuggled into this change.)
- **The CWD invariant and the no-merge-back rule** are unchanged. Nothing in this plan adds a write to `/home/user/workspace`; §8 step 4's version-history entry remains the single sanctioned exception.
- **The secret scan** stays the hard-failing, no-fallback, authoritative blocker. The new validation is an *additional* gate with its own exit code.
- **Bootable-or-nothing.** A validation failure is a "fix and relaunch" situation like every other non-zero exit -- never a reason to publish something smaller.

## Open questions

- **The MIT license is stated, not shipped.** §6 tells the user a public template is MIT-licensed; no `LICENSE` file is generated into the snapshot. Enforcing it is a small follow-up, deliberately not folded in here.
- **Append-only enforcement -- deliberately out of scope.** Four logs are documented append-only (`## Templates` and `## Adopted templates` in `docs/VERSION_HISTORY.md`, `Publication history` / `Adaptation history` in the manifest) and nothing enforces it: the only script that touches `VERSION_HISTORY.md` is `build_template.sh`, and only to `rm -f` it out of the snapshot, leaving `publish-template` §8 step 4's per-slug idempotence test as the single weak check. Enforcement was scoped alongside this work and **the user's decision is to leave it for now**, so nothing here builds it. Recorded so the gap stays known rather than looking closed.
- **Nothing uninstalls.** Packages atemplate brought in stay installed if the user later drops the template, and a newer version dropping a declaration does not remove anything. That is deliberate -- something else may have come to depend on them, and an uninstall is not reversible from a manifest -- but it means the environment only accretes.

## Phasing

Each phase is independently landable and independently useful.

- **Phase 1 -- schema.** `template_manifest.py` in `env_converge`: identity, recipe, prerequisites, environment, lineage. The stdlib+pydantic import constraint and its test, plus unit tests over the model (valid manifests, every rejection case, the v1/v2 discrimination). No flow changes; nothing observable yet.
- **Phase 2 -- the naming sweep.** Slug-free `template.md` / `.toml` / `.svg` with override semantics and the lineage chain; `Holes` -> `Requirements`. Mechanical but wide: `build_template.sh` (carry-forward becomes lineage capture, README listing, `/welcome`, summary) and all four skills. Landing it before the feature work means everything after it is written once, against the final names.
- **Phase 3 -- publish side.** `build_template.sh` generates the `.toml`, snapshots the validator with the scan tools, runs validation + the apt-resolution check, exits 6 on failure. `publish-template` SKILL: FILL-IN instructions for the environment section, §5's new exit code, §6's recap of what will be installed plus the MIT-if-public sentence, §8's pre-push gates. The full README recipe (hero, Open in Minds CTA, why, how to use, ideas) and its post-push browser verification land here.
- **Phase 4 -- adopt side.** `use-template` §2 reads the `.toml`; §3 installs
  the declared `[environment]` with ordinary commands and handles the two
  failure modes. `update-installed-template` §2b installs whatever a newer
  version added. No env-converge change: capture already covers it.
- **Phase 5 -- env.d units and the update paths.** The `2000+`-with-slug convention; validation that declared units exist and are inside the include set. `update-published-template` reads the recipe from the `.toml` (with the `.md` fallback) and performs the v1 -> v2 migration including the file renames; `update-installed-template` re-converges after pulling a newer version whose declarations may have changed.

## Grounding and assumptions

- Every "today it does X" claim is read from the tree at `a072097f`: `build_template.sh` (generation steps 6 / 7 / 8 / 8.5 / 8.6, gates and exit codes 1-5), `publish-template/SKILL.md` (§1 and §6 gates, §3 worker task, §5 exit-code map, §8 push and version-history entry), `use-template/SKILL.md` (§2 manifest read, §3 activation), both update skills, `env_converge/README.md` + `data_types.py` + `converge.py` + `events.py`, `system/scripts/env.d/1100-secret-scanners.sh`, and `system/scripts/write_apt_sources.sh`.
- **Verified against the env-converge README rather than assumed:** versions are a function of `.mngr/apt-snapshot-timestamp` (currently `20260725T000000Z`); apt replay uses the recorded *manual* name set while npm/uv/cargo replay uses name@version; `package_unavailable` exists and `env-converge run` exits 3 when recorded packages were unavailable; `env-converge upgrade` advances to the repo's committed timestamp; env.d units are plain idempotent bash with no marker files, receiving `ENV_CONVERGE_WORKSPACE_DIR` / `ENV_CONVERGE_OVERLAY_DIR`; the record lives at `$MNGR_HOST_DIR/plugin/env-converge/`.
- **Assumption, now measured:** `uv run --no-project --with 'pydantic>=2'` was the plan's sharpest risk -- if it were slow or fragile it would take the single-schema property down with it. Exercised against a snapshotted `validate_template.py` + `template_manifest.py` pair outside any project, warm: **0.8s wall, "Installed 5 packages in 8ms"**. Comfortably inside a publish gate, and it resolves no workspace project, so the cold-base build failure the assembly script warns about cannot occur. The fallback (a vendored pydantic-free validator) is not needed.
- **Defect found and fixed here:** `docs/system/style_guide.md` was a symlink to `vendor/mngr/style_guide.md`, which resolves relative to `docs/system/` and so pointed at a path that does not exist -- a stale target from the `system/` relayout. Repointed to `../../system/vendor/mngr/style_guide.md`, which resolves. A sweep for other broken symlinks outside `system/vendor/` found none.
