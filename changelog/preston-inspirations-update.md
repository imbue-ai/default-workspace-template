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
