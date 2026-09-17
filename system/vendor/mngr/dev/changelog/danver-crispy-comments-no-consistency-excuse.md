# Close the "but the rest of the file does it" loophole in /crispy-comments

The `/crispy-comments` skill (`.claude/skills/crispy-comments/SKILL.md`) gains a "Do not hide behind consistency" section. Matching the surrounding file is no longer a reason to keep any comment the skill says to remove, and the ASCII-banner rule now sweeps every file the diff touches instead of only the lines the diff added -- an agent that finds banners in a file it edits removes all of them rather than adding one more to fit in. The sweep stops at the files the diff touches; if it would balloon the change, the agent asks the user instead of quietly leaving the banners.

`CLAUDE.md` and the `comment_cruft` category in `.reviewer/code-issue-categories.md` are updated to match the widened scope.
