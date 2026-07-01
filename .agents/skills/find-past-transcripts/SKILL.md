---
name: find-past-transcripts
description: "Use whenever the user wants to recall, recover, or look up a PAST, earlier, previous, or DELETED chat / conversation / session / agent on this workspace -- e.g. 'what did I say in that chat I deleted', 'what did the sub-agent that set up auth do', 'find the conversation where we worked on X', 'pull up that earlier session', 'don't you remember what we discussed?'. Do NOT answer that you can't access other conversations: agents that ran on this host keep their transcripts locally under /mngr/agents/ (still present) or /mngr/preserved/ (destroyed), and this skill reads them. (This is for reading PAST chats on THIS host; acting on OTHER live workspaces is the separate minds-api skill.)"
compatibility: Covers agents that ran on this host (sub-agents you launched, prior sessions). Uses find/cat/jq.
---

# Find past transcripts

**Do not tell the user you can't see earlier or deleted conversations before you
check.** Every agent that ran on this host leaves its transcript behind locally,
so past chats on this host are recoverable -- refusing without looking is wrong.

An agent's conversation is stored under its state dir as
`events/<source>/common_transcript/events.jsonl` (source is the agent type, e.g.
`claude`). On **this** host that state dir is in one of two places, depending on
whether the agent still exists:

- **Still present** (running, or **STOPPED** but not destroyed):
  `/mngr/agents/<agent_id>/events/*/common_transcript/events.jsonl`.
  A finished `launch-task` worker is usually left STOPPED here -- it is **not**
  in `/mngr/preserved/` until it is actually destroyed.
- **Destroyed:**
  `/mngr/preserved/<agent_name>--<agent_id>/events/*/common_transcript/events.jsonl`.

(Use `$MNGR_HOST_DIR` in place of `/mngr` if this host's mngr root is elsewhere.)
**Always check both** -- a past agent could be in either.

**Scope:** this only covers agents that lived on **this** host. Agents from
*other* workspaces are preserved on the user's machine, not here, and are not
reachable from this skill.

## 1. See what's on this host

```bash
mngr list                        # agents still present (running / stopped), with names + ids
ls -1t /mngr/preserved 2>/dev/null   # destroyed agents (<agent_name>--<agent_id>), newest first
```

Match the user's description to an agent by its name (and, for preserved dirs,
the mtime -- roughly when it was destroyed: `ls -lt /mngr/preserved`).

## 2. Find every transcript on this host (present OR destroyed)

```bash
find /mngr/agents /mngr/preserved -path '*/common_transcript/events.jsonl' 2>/dev/null
```

## 3. Read one

For a still-present agent, the easiest is the rendered view:

```bash
mngr transcript <agent-name-or-id>          # works for running/stopped agents; NOT for destroyed ones
```

For any agent (present or destroyed), read the file directly (pick a path from
step 2):

```bash
cat "/mngr/agents/<agent_id>/events/claude/common_transcript/events.jsonl"
# or, if destroyed:
cat "/mngr/preserved/<agent_name>--<agent_id>/events/claude/common_transcript/events.jsonl"
```

## 4. Render a raw file readably

```bash
F="<path from step 2>"
jq -r '
  if .type=="user_message" then "USER: \(.content)"
  elif .type=="assistant_message" then "ASSISTANT: \([.parts[]?|select(.type=="text").content]|join(" "))"
  elif .type=="tool_result" then "TOOL(\(.tool_name)): \(.output[0:300])"
  else .type end' "$F"
```

## Notes

- **`launch-task` workers:** a worker is only moved to `/mngr/preserved/` when it
  is destroyed (`mngr destroy`). Interactively-launched workers are often left
  STOPPED instead, so look in `/mngr/agents/` first; `mngr transcript <name>`
  reads a stopped worker fine.
- `<source>` in the path is the agent type (`claude`); the `events/*/...` glob in
  step 2 covers other types.
- `system-services--*` and infra agents may have no common transcript -- look at
  the named agents.
- A transcript only exists if that agent actually produced one; a brand-new agent
  with no turns won't have one.
