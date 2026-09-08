# Trim the comment_cruft review category to defer to the /crispy-comments skill

The `comment_cruft` autofix category (landed in #515) re-listed the `/crispy-comments` skill's criteria and examples, duplicating `.claude/skills/crispy-comments/SKILL.md`. It now points at the skill as the single source of truth and keeps only the taxonomy-specific note that change-process / bug-fix comments belong to `user_request_artifacts_left_in_code`.
