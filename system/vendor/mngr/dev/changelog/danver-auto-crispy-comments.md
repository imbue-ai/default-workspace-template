# Auto-run /crispy-comments as part of finishing work

Agents now prune comments per the `/crispy-comments` skill as a normal part of finishing, two ways:

- The imbue-code-guardian autofix pass gains a `comment_cruft` category (`.reviewer/code-issue-categories.md`) that applies the skill's criteria: remove correctness arguments, defensive justification, DRY restatement of volatile facts, commented-out code, and banners, keeping only comments that explain a non-obvious "why". Change-process / bug-fix comments stay covered by the existing `user_request_artifacts_left_in_code` category. It is deliberately left out of `.reviewer/autofix/auto-accept.md`, so the autofix agent deliberates each proposed removal rather than deleting comments unattended.

- `CLAUDE.md` gains a finish-checklist line instructing agents to run `/crispy-comments` on their branch diff before finishing.
