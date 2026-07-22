- **Inspiration versioning.** A workspace now keeps a plain, committed
  `VERSION_HISTORY.md` recording where it came from and what it has published:
  a `## Workspace` line for the template version it was created from and one per
  `update-self` landing, and a `## Inspirations` entry per published inspiration
  (`- v1  <date>  first published  <sha>`, then `v2`, `v3`, ... under the same
  heading). Each line ends in the commit it was cut from, and earlier lines are
  never rewritten. The new **`update-version`** skill owns the whole contract --
  the file format, which commit gets recorded, seeding the creation line, and the
  rules that keep a retried step from double-recording -- so both flows write
  identical lines and there is no separate helper program to maintain.
  `update-self` appends its line as part of landing an
  update; `publish-inspiration` appends its entry only after the push has
  succeeded -- the single sanctioned one-file commit back to the live workspace,
  documented as an explicit exception so it is never confused with the
  tree-clobbering pattern the skill forbids. An unpublished inspiration is never
  recorded.

- **Published manifests now carry a version and a recipe.** Each
  `inspiration-<slug>.md` records `version: v1` in its front-matter and a new
  "Recipe" section: the include paths, the deliberate exclusions, and the
  published-version modification RULES (rules only -- never the removed values).
  An inspiration is derived from its workspace by that recipe rather than being a
  fork of it, so a later update re-runs the recipe against the current workspace
  instead of diffing two repos -- which is what keeps anything deliberately
  excluded excluded, even though it still exists in the source workspace.

- A published snapshot no longer carries the publisher's own version history:
  the assembled tree's `VERSION_HISTORY.md` is reset to the pristine template
  file, so the slugs, repo URLs, and source commits of a mind's other
  inspirations never ship inside one it publishes.

- The publish skill now confirms the **adopter's required permissions with the
  publisher**. The manifest's "Prerequisites" list (what the inspiration's user
  must grant for the app to work) is surfaced back in the §6 chat confirmation in
  plain language -- "whoever adopts it will need to connect/grant X, Y; do those
  look right and complete?" -- and the publisher's answer is part of the
  go-ahead. A missing or wrong line is fixed in the worktree before the push,
  since a gap there silently breaks adoption.

- **An inspiration can be anything committable**, not just an app: a skill, a
  chat customization or behavior, a workflow, a service, config, or seed data.
  If the user wants to snapshot something that is not committed to git -- an
  ephemeral chat behavior, conversation history, runtime-only state -- the skill
  now recognizes this and suggests turning it into something committable first
  (most often by crystallizing it into a skill), since an inspiration must be
  reconstructable from the committed tree.

- **LLM access is now a first-class prerequisite.** Any inspiration whose code
  calls Claude records how it reaches it, because that differs per environment:
  the keyed path (`ANTHROPIC_API_KEY` set -> litellm, pay-per-token) or the
  keyless path (`claude -p` -> the subscription credit pool). The manifest gains
  a `requires_llm:` line naming the method the code was built against, so an
  adopter on the other method knows to switch the model calls (per
  `use-ai-integration`), and a hardcoded path is also listed as a Hole. An
  adopter can no longer be surprised by an implicit LLM dependency.

- **Published inspiration repos are locked down on creation.** Right after the
  repo is created, discussions and forking are turned off via the GitHub API,
  unconditionally and without asking. Issues stay enabled so collaborators can
  still file them; private-by-default is what makes issues and PRs
  collaborators-only. This closes the surfaces where arbitrary, non-collaborator
  users could comment on someone's inspiration. (GitHub has no
  collaborators-only-issues setting for a public repo, so if the user chooses
  public visibility the skill tells them so.)

- Added design docs under `blueprint/agent-inspiration-update-awareness/` for
  tracking an inspiration's version/drift and updating a published inspiration:
  the full proposal, plus a short summary of the version-history file and how a
  re-run of the inspiration's recipe (rather than a cross-repo diff) produces an
  update while preserving deliberate exclusions.
