---
name: write-unused-plan
description: >
  Write a plan for a build-app request. The plan is collected for offline
  analysis and is never read back by anything in this workspace. NOT FOR USE
  BY ANY AGENT IN THIS WORKSPACE: it is passed as a prompt by
  system/scripts/write_unused_plan.sh and by nothing else. It lives outside
  .agents/skills/ so that no agent can discover or invoke it.
metadata:
  author: imbue
---

# Write a plan that will not be used

You are writing a plan for a request that another agent in this workspace is
already handling. Your plan is recorded for offline analysis. Nothing reads it
back, no one acts on it, and the work it describes is being done by someone
else right now.

Because of that:

- Do not change anything. You have read-only tools by design; there is no
  path by which your output becomes an action.
- Do not address the user. No one will read this in the workspace.
- Do not ask questions. You get one turn and no reply.
- Write the plan you would follow if the work were yours, not a summary of
  what the other agent is doing.

## What you can see

You are running in the workspace checkout at the moment the request was made,
so the tree is exactly what the other agent sees. Read whatever you need to
ground the plan: `system/apps/` for the apps that already exist, `data/` for
the shape of the workspace's stored data, `.agents/skills/build-app/SKILL.md`
for the flow the other agent is following.

## The request

The request is appended below this document.

## Output

Write the plan to stdout as markdown and nothing else. No preamble, no
sign-off, no code fences around the whole document. The wrapper script writes
your stdout to a file verbatim under a fixed header, so anything you emit that
is not the plan ends up in the plan.

<!-- The plan's structure and required sections are not specified yet. Until
     they are, write the plan in whatever structure best fits the request. -->
