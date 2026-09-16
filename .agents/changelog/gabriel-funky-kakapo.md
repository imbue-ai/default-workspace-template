Chat agents were still talking to users about git (commits, branches, pushes), test commands and counts, and file paths, even though the workspace's users are mostly non-technical. The "Engineering Subordinate" output style only asked for plain language in one late paragraph, and its own examples modeled technical talk.

- The output style now opens with a "your reader is not an engineer" principle: a list of plumbing topics the user never hears about by default (version control, tests, code locations, build tools, commands, agent machinery, error text), each with the plain-language phrase to use instead, plus rewrites. The technical "Good" examples were rewritten in plain terms, the "keep technical terms verbatim" rule now only applies to terms the reader rule lets through, and the switch to technical language happens only on the user's signal (they used the terms first, asked, or must act on it).

- New shared reference `.agents/shared/references/user-facing-language.md` holds the full vocabulary and rewrites, so the output style, `AGENTS.md`, and skills point at one list.

- Progress-view titles follow a skill's rough shape, not its exact step list: several skill steps may collapse into one plain-English title, and step names never become titles. Delegation steps are named for the outcome ("Rebuild the login flow (in the background)") rather than "delegate ... to a sub-agent"; `launch-task` and `publish-template` examples updated to match.

- `github-sync`'s final report step now describes the backup in plain terms (where the copy lives, that every saved change is copied automatically, what is covered by the other backup) instead of commits, pushes, and hooks.
