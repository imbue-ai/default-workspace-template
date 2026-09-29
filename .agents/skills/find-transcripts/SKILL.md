---
name: find-transcripts
description: "Find, read, or search through any chat message, transcript, or conversation content from this host -- whether from an active agent, a past session, a deleted agent, a sub-agent, or a worker. Use this skill any time a user asks about chat histories or you otherwise want to access them. NOTE: this skill only covers Imbue Studio agents -- not other services (ChatGPT, claude.ai, etc.)."
compatibility: Covers agents that ran on this host (active, stopped, or destroyed). Uses find/jq/mngr.
metadata:
  author: imbue
---

# Find transcripts

**Do not tell the user you can't see a conversation before you check.** Every
agent that has run on this host leaves its transcript behind locally -- past,
active, or destroyed. Refusing without looking is wrong.

An agent's conversation is stored under its state dir as
`events/<source>/common_transcript/events.jsonl` (source is the agent type, e.g.
`claude`). On **this** host that state dir is in one of two places, depending on
whether the agent still exists:

- **Still present** (running, or **STOPPED** but not destroyed):
  `/home/user/.mngr/agents/<agent_id>/events/*/common_transcript/events.jsonl`.
  A finished `launch-task` worker is usually left STOPPED here -- it is **not**
  in `/home/user/.mngr/preserved/` until it is actually destroyed.
- **Destroyed:**
  `/home/user/.mngr/preserved/<agent_name>--<agent_id>/events/*/common_transcript/events.jsonl`.

(Use `$MNGR_HOST_DIR` in place of `/home/user/.mngr` if this host's mngr root is elsewhere.)
**Always check both** -- a past agent could be in either.

**Note on agent types:** transcripts here include the user-facing chat agent
*and* any worker/sub-agents launched via `launch-task` or similar. Workers often
have short, task-focused transcripts. `mngr list` shows agent names and labels to
help you identify which is which.

**One chat, several agents.** A chat that the chat app moved to another harness
has run on more than one agent, and the user sees it as one conversation. Every
agent of such a chat carries the labels `chat_id=<chat id>` (the id of the
chat's first agent) and `chat_seq=<n>` (its position, 1 for the first). The
agents the chat has left are stopped and renamed `archived-<seq>-<name>-<id>`
with `display_name` "<title> (archived <seq>)"; only the current agent keeps
the chat's name. To read the whole conversation, list the chat's agents and
read their transcripts in `chat_seq` order:

```bash
mngr list --include 'labels.chat_id == "<chat id>"'     # every agent of one chat, archived ones included
mngr list --include 'labels.chat_id == "$MINDS_CHAT_ID"'   # the agents of the chat you are running in
```

An archived agent is still present (not destroyed), so `mngr transcript <id>`
and the still-present path below both work for it.

## What this skill does NOT cover

- **Other services** (ChatGPT, claude.ai, other AI tools): their chats are not
  stored on this host. To access them you'd need to pull in that data separately
  via their own export features.

- **Other Imbue Studio workspaces**: each workspace is a separate host with its own
  `/home/user/.mngr/`. Transcripts from agents in another workspace live there, not here.
  To read them, SSH into that workspace via the Imbue Studio API: use the `minds-api`
  skill to request the `minds-workspaces-ssh` latchkey permission, then run this
  skill's read commands over SSH on that host.

## 1. See what's on this host

```bash
mngr list                        # agents still present (running / stopped), with names + ids
ls -1t /home/user/.mngr/preserved 2>/dev/null   # destroyed agents (<agent_name>--<agent_id>), newest first
```

Match the user's description to an agent by its name (and, for preserved dirs,
the mtime -- roughly when it was destroyed: `ls -lt /home/user/.mngr/preserved`).

## 2. Find every transcript on this host (present OR destroyed)

```bash
find /home/user/.mngr/agents /home/user/.mngr/preserved -path '*/events/*/common_transcript/events.jsonl' -not -path '*/events/logs/*' 2>/dev/null
```

(`events/logs/common_transcript/` is the transcript converter's own log, not a
conversation, so it is excluded.)

## 3. Read one

```bash
mngr transcript <agent-name-or-id>                    # still-present agent (running or stopped)
mngr transcript <agent-name-or-id> --preserved-only   # destroyed agent, read from /home/user/.mngr/preserved
```

The rendered view cuts long tool inputs, tool outputs, and thinking short; add
`--full` to see them whole. `--role user --role agent` hides tool output,
`--tail N` shows the last N events, and `--format jsonl` prints the raw records.

An agent from before the transcript format change has an old-format file, which
`mngr transcript` refuses with an error naming the "retired pre-ATIF format".
Read those with step 4.

## 4. Render or search a raw file

This handles both the current format (`step` / `observation` records) and the
old one (`user_message` / `assistant_message` / `tool_result`):

```bash
F="<path from step 2>"
jq -r '
  def text: if type == "string" then . else [.[]? | select(.type == "text").text] | join(" ") end;
  if .type == "step" then
    "\(.source | ascii_upcase): \(.message)"
    + ([.tool_calls[]? | "\n  -> \(.function_name)(\(.arguments | tojson | .[0:300]))"] | join(""))
    + ([.observation.results[]? | "\n  \(.content | text)"] | join(""))
  elif .type == "observation" then
    [.results[] | "TOOL(\(.extra.tool_name // "?")): \(.content | text | .[0:300])"] | join("\n")
  elif .type == "user_message" then "USER: \(.content)"
  elif .type == "assistant_message" then
    "AGENT: \([.parts[]? | select(.type == "text").content] | join(" "))"
    + ([.tool_calls[]? | "\n  -> \(.tool_name)(\(.input_preview))"] | join(""))
  elif .type == "tool_result" then "TOOL(\(.tool_name)): \(.output[0:300])"
  else empty end' "$F"
```

Drop the `[0:300]` slices to see tool inputs and outputs whole. An old-format
file stored only the first 200 characters of each tool input (`input_preview`)
and the first 2,000 of each tool output; for a claude agent the complete record is the raw transcript
`logs/claude_transcript/events.jsonl` in the same agent directory (kept for
destroyed agents too).

## Notes

- `system-services--*` and infra agents may have no common transcript -- look at
  the named agents.
- A transcript only exists if that agent actually produced one; a brand-new agent
  with no turns won't have one.
