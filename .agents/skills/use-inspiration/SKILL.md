---
name: use-inspiration
description: Former name of the use-template skill. Use when someone says "/use-inspiration <git url>", or refers to adopting an "inspiration" -- what used to be called an inspiration is now called a template.
---

# use-inspiration (renamed to `use-template`)

**Inspirations are now called templates.** This skill exists only so the old
name keeps working; it has no behaviour of its own.

Load the **`use-template`** skill and follow it exactly, treating the git URL
the user gave you as the template to adopt.

## Why this alias exists

Two things in the wild still say `/use-inspiration`, and both would break
without it:

- **The minds desktop app.** Its "Create from Inspiration" add flow copies
  `/use-inspiration <git-url>` to the user's clipboard
  (`CreateInspirationPage.ts`). Until the app ships the new wording, that paste
  lands here.
- **Already-published templates.** Every README published before the rename
  carries a copyable `/use-inspiration <url>` fallback line under its "Open in
  Minds" button. Those repos are on other people's accounts and cannot be
  edited.

Remove this alias only once both are updated -- the app has shipped the new
string, and enough time has passed that pre-rename READMEs are not worth
supporting.
