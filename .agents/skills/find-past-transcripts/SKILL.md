---
name: find-past-transcripts
description: "Use whenever the user wants to recall, recover, or look up a PAST, earlier, previous, or DELETED chat / conversation / session / agent on this workspace -- e.g. 'what did I say in that chat I deleted', 'what did the sub-agent that set up auth do', 'find the conversation where we worked on X', 'pull up that earlier session', 'don't you remember what we discussed?'. Do NOT answer that you can't access other conversations: agents that ran on this host and were destroyed have their transcripts preserved locally under /mngr/preserved/, and this skill reads them. (This is for reading PAST/destroyed chats on THIS host; acting on OTHER live workspaces is the separate minds-api skill.)"
compatibility: Covers agents that ran on this host (sub-agents you launched, prior sessions). Uses find/cat/jq.
---

# Find past transcripts

**Do not tell the user you can't see earlier or deleted conversations before you
check `/mngr/preserved/`.** Agents that ran on this host and were destroyed leave
their transcript behind locally, so past chats on this host are recoverable --
refusing without looking is wrong.

When an agent that ran on **this** workspace host is destroyed -- a sub-agent you
launched via the `launch-task` skill, a sibling agent, or an earlier session --
mngr keeps a copy of its conversation transcript on this host under
`/mngr/preserved/` (more precisely `$MNGR_HOST_DIR/preserved/`). This skill finds
and reads those, so you can recover "old stuff" the user refers to.

**Scope:** this only covers agents that lived on **this** host. Agents from
*other* workspaces are preserved on the user's machine, not here, and are not
reachable from this skill.

## 1. List the destroyed agents preserved on this host

```bash
ls -1t /mngr/preserved          # each entry is <agent_name>--<agent_id>, newest first
```

If `/mngr` isn't this host's mngr root, use `"$MNGR_HOST_DIR/preserved"` instead.
Match the user's description to an agent by its `<agent_name>` and the directory's
time (`ls -lt /mngr/preserved` -- the mtime is roughly when it was destroyed).

## 2. Find every preserved transcript on this host

```bash
find /mngr/preserved -path '*/common_transcript/events.jsonl'
```

## 3. Read one (raw JSONL, one event per line)

```bash
cat "/mngr/preserved/<agent_name>--<agent_id>/events/claude/common_transcript/events.jsonl"
```

## 4. Render it readably

```bash
F="/mngr/preserved/<agent_name>--<agent_id>/events/claude/common_transcript/events.jsonl"
jq -r '
  if .type=="user_message" then "USER: \(.content)"
  elif .type=="assistant_message" then "ASSISTANT: \([.parts[]?|select(.type=="text").content]|join(" "))"
  elif .type=="tool_result" then "TOOL(\(.tool_name)): \(.output[0:300])"
  else .type end' "$F"
```

## Notes

- `<source>` in the path is the agent type (`claude`); the `events/*/...` glob in
  step 2 covers other types.
- `system-services--*` entries are infra agents and have no common transcript --
  look at the named agents.
- A transcript only exists if that agent actually produced one before it was
  destroyed; a brand-new agent with no turns won't have one.
