---
name: crispy-comments
description: Prune code comments down to what helps future maintainers. Reviews comments on the current branch's diff and removes incidental history, defensive justification, correctness arguments, and ASCII banners, which it sweeps from every file the diff touches. Invoke with /crispy-comments.
---

# Crispy Comments

This skill applies only to the current branch of the current repository. If this is invoked on `main` (or `master`), please confirm with the user whether they intend to run this on the entire repository.

Review the comments added or changed in the diff. For each, ask: does this help future maintainers understand the code, or merely restate what the code plainly does or explain today's bug fix? Remove incidental history, defensive justification, and correctness arguments.

Also remove comments that restate facts from the surrounding code that are likely to change — a count of subclasses, a list of variants, the call sites of a function. These pin the comment to a point in time and rot quickly. Follow DRY: Don't Repeat Yourself.

Remove commented-out code outright — version control already remembers it.

Remove ASCII-art banners and box-drawing section dividers. They add visual noise without conveying anything a plain one-line comment does not.

## Do not hide behind consistency

Never keep a comment because the surrounding code has more like it. "The rest of this file does it this way" is not a reason to leave cruft in; it is a reason to take the rest out too. A file full of banners is a file with a problem, not a house style worth matching. Every rule above applies to your addition at full strength no matter what its neighbors look like.

Banners are where this excuse surfaces most, so the rule there is absolute: when a file you touch contains ASCII banners or box-drawing dividers, delete every one in that file, not only the ones your diff introduced. Adding a banner so your new section matches the existing ones, and sparing your own because the file would look uneven without it, are the same mistake — the rule is what changes the file, not the file that relaxes the rule.

The sweep reaches the whole of each file the diff touches. It stops there: leave files the diff does not touch alone.

If sweeping a file would balloon the change beyond what the user asked for, say so and ask them how to proceed. Deciding on your own to leave the banners in place is not one of the options.

## Source

Copied from https://github.com/DanverImbue/crispy-comments (the canonical version of this skill). To update, re-copy SKILL.md from there.
