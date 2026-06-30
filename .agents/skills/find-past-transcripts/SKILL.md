---
name: find-past-transcripts
description: "Use when the user refers to past work, an old conversation, or something a previous/earlier agent or workspace did -- including ones that were destroyed -- e.g. 'what did the agent that set up auth do', 'find the chat where we discussed X', 'pull up that old workspace's history'. Lists past agents whose transcripts were preserved and reads any agent's transcript through the Minds API."
compatibility: Requires latchkey (the standard agent gateway) and curl. See the minds-api skill for the gateway/permission mechanics this skill builds on.
---

# Find past transcripts

When a Minds agent is destroyed, the hub keeps a durable copy of its
conversation transcript even after the agent's host is gone. This skill lets you
find that "old stuff" the user is referring to: list the past agents whose
transcripts were preserved, then read any agent's transcript.

These two routes are part of the Minds API, reached the same way every other
Minds API call is -- through the **latchkey gateway's `minds-api-proxy`**, using
`latchkey curl` (never plain curl). They are gated by the same
`minds-workspaces-read` permission used to list workspaces, so if you have not
been granted it yet you will get a 403; see the **`minds-api` skill** for the
gateway address, the permission table, and the `type: "workspace"` permission
request flow (request `minds-workspaces-read`).

## 1. Find which past agents have a preserved transcript

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces/preserved \
  | jq '.agents[] | {agent_name, agent_id, preserved_at}'
```

Each entry has the agent's `agent_name`, its `agent_id`, and `preserved_at`
(roughly when the agent was destroyed). The list is newest-first, so the most
recently destroyed agents are at the top. Use the `agent_name` and timing to
match the user's description ("the one that set up auth", "last week's
workspace") to a specific `agent_id`.

This is the authoritative set of preserved transcripts on the hub -- it includes
agents that are long gone from the live workspace list, which is exactly the
"old stuff" you usually want.

## 2. Read a chosen agent's transcript

```bash
latchkey curl "http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces/<AGENT_ID>/transcript" \
  | jq -r '.content'
```

The response has `agent_id`, `format`, `is_preserved` (true when it came from the
destroyed-agent copy, false when read from a live agent), and `content` (the
rendered transcript). It serves both destroyed agents (from the preserved copy)
and live agents.

Optional query params mirror `mngr transcript`, so you can keep large
transcripts focused:

- `format=human` (default), `json`, or `jsonl`
- `role=user` / `role=assistant` / `role=tool` (repeatable) to filter by speaker
- `head=N` or `tail=N` to take the first or last N events (not both)

```bash
# Just the user messages, last 40 events, as JSONL:
latchkey curl "http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces/<AGENT_ID>/transcript?role=user&tail=40&format=jsonl" \
  | jq -r '.content'
```

## Notes

- An unknown `agent_id` (never preserved and not a live workspace) returns 404.
- Reading a transcript is allowed under `minds-workspaces-read`, the same grant
  that lets you list workspaces -- no separate permission. If the call is
  rejected, file the `minds-workspaces-read` request per the `minds-api` skill
  and retry once approved.
- To find your *own* current agent id, see `$MNGR_AGENT_ID`; you rarely need it
  here, since this skill is about *other*, past agents.
