---
name: blueprint-generate
description: Close a blueprint Q&A session and write the plan file from what it gathered. Use when the user says the questions are done, or invokes this directly after a blueprint session. Only the user ends the Q&A phase, so do not reach for this on your own mid-session.
metadata:
  author: imbue
---

## Blueprint — Generate plan

Generate an implementation plan from the Q&A session.

### Step 1: Resolve template

Recall the template name from the blueprint Q&A conversation. Read [references/templates.json](references/templates.json) and find the matching template.

### Step 2: Generate slug

Create a concise 2-5 word kebab-case slug (max 50 chars) for the feature. Sanitize: lowercase, alphanumeric + hyphens only, no leading/trailing hyphens. If `docs/system/blueprint/<slug>` already exists, append `-2`, `-3`, etc.

### Step 3: Create plan directory

```bash
mkdir -p docs/system/blueprint/<slug>
```

### Step 4: Refine the prompt

Use the most recent refined prompt from the Q&A conversation. If for any reason it wasn't shown during Q&A, generate it now — see [references/refine-prompt.md](references/refine-prompt.md).

### Step 5: Write the plan

See [references/write-plan.md](references/write-plan.md).

Append this progress line at the end of **every** message during the Write phase:

```
✓ Explore  ✓ Plan  ● Write  ○ Refine
```

Place the line after all other content, separated by a blank line.

### Step 6: Refinement

See [references/refinement.md](references/refinement.md).
