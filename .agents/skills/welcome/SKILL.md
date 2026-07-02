---
name: welcome
description: Greet the user with a short, friendly welcome message when a new project/agent is first started. Invoked automatically as the first message from the minds desktop client.
---

# Welcome the user

This skill has two mutually exclusive paths. Which one applies is decided by the
inspiration region at the very bottom of this file (between the
INSPIRATION:BEGIN and INSPIRATION:END HTML-comment marker lines). Check that
region FIRST, before writing anything:

- Region in its default empty state (the single "No inspiration is present"
  sentence) -> follow **Generic welcome** below.
- Region names an inspiration -> follow **Inspiration takeover** below. The
  generic welcome does NOT apply; the takeover replaces it entirely.

## Generic welcome (no inspiration)

This path has two parts: the opening greeting you always send first, and a list
of suggestions you offer only if the user asks for ideas.

### Opening message

Output the following welcome message to the user, verbatim, as your entire
response. Do NOT call any tools, do NOT look at the codebase, and do NOT add
anything else:

---

### Welcome to Minds

I'm an AI operating system built to extend *you* — so you can do your best work.

I can take on tasks for you, build custom AI tools you can easily edit, connect to the tools you already use to pull in information, or just brainstorm ways to make your work better.

**Let's get started**

Already have something in mind? Tell me what you'd like to work on below. If not, I'm happy to suggest a few ways to get started.

---

That is the entire opening message. Stop after printing it.

### If the user asks for suggestions

After the opening message the user replies. If their reply asks for suggestions, says they're not sure, or otherwise signals they don't have something specific in mind, output the following message to the user, verbatim, and nothing else. (If instead they describe something they want to do, ignore this section and help them with that directly.)

---

Here are some popular ways people get started with Minds. Pick whichever fits, and we can build on it as a starting point.

1. **Unify your email & messages:** Bring every conversation into one place and respond from there.
2. **Organize your tasks:** Build a system to track what you need to do and get it done.
3. **Track your team's work:** A dashboard for everything across GitHub, Linear, Slack, and email.
4. **Keep up with what you care about:** Stay current on the products, events, or news that matter to you.

---

## Inspiration takeover (inspiration present)

When the region between the markers names an inspiration, this mind was created
from that inspiration's repo, and the inspiration takes over the welcome. Do
all of the following in your FIRST response, in the same turn, without waiting
to be asked:

1. Do NOT output the generic "Welcome to Minds" message and do NOT offer the
   generic suggestions list. Instead, open with a short CUSTOM welcome that
   names the inspiration's title and gives its one-line description (both are
   written in the region below).
2. Immediately read the inspiration's manifest — the `inspiration-<slug>.md`
   file at the repo root, named in the region.
3. Begin the adaptation conversation: in plain, non-technical language, present
   what the inspiration is and what it needs from the user, then ask the user
   how they want to adapt it. This is the `use-inspiration` skill's template
   path; the manifest's "How to adapt it" section is the script for the
   conversation. Do not start changing anything before having this
   conversation — end your first response on the question.

The generic path's "do NOT call any tools" rule does not apply here: reading
the manifest in the first turn is required.

## Marker contract (for the publishing machinery)

The region between the two markers below is machine-rewritten (a deterministic
awk replacement, never a freeform edit) by the publish-inspiration build script
when a mind is published as an inspiration. The contract that keeps the rewrite
deterministic:

- Each marker is an HTML comment that appears exactly once in this file, as an
  exact whole line, and is never edited, moved, or removed. The build script
  matches the markers as exact whole lines, and the literal marker text must
  not appear anywhere else in this file (prose refers to the markers without
  reproducing them, as above).
- The rewrite replaces everything strictly BETWEEN the markers and touches
  nothing outside them.
- The default (empty) state is the single sentence currently between the
  markers. Any other content means an inspiration is present and the takeover
  path above applies.
- When rewritten, the region carries the inspiration's title, slug, one-line
  description, and manifest path, plus the instruction to take over the
  welcome and immediately start the adaptation conversation.

<!-- INSPIRATION:BEGIN -->
No inspiration is present in this mind yet. This is the default template state.
<!-- INSPIRATION:END -->
