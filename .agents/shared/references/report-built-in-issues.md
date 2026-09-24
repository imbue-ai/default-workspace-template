# Reporting built-in issues to imbue

How to hand imbue a diagnosis of a defect in built-in code (mngr, vendored code,
the initial template, or anything an `update-self:` merge brought in). You do not
submit it yourself: the POST below makes the desktop app open a pre-filled "report
a bug" modal, and the user reviews and submits it, so the human gates the send.

**Send exactly one report, once, covering every built-in issue you found.** Each POST pops a modal over the user's workspace, so a report per issue means a queue of modals to read and submit one at a time, for a single session's work. Collect the issues as you go and send them together at the end of the pass, numbered, each with its own root cause and classification -- they can be split apart upstream, but they cannot be un-popped here. If you have already sent a report this session and then find something else, say so in chat and ask before sending a second one.

POST your diagnosis to the minds report route through the latchkey gateway:

```bash
DESCRIPTION="$(cat <<'EOF'
<one-paragraph summary of what you were doing and what you found>

---- 1. <short title of the first issue> ----
Root cause: <file:line and what is wrong>
Classification: built-in (<mngr / template / update-self>), <fixable here | needs a change in mngr | needs a new desktop-app version>
Fix: <what you changed, or why it cannot be fixed from here>

---- 2. <short title of the second issue> ----
<the same three lines; drop the numbering when there is only one issue>
EOF
)"

# Report against the workspace's PRIMARY agent id, not your own ($MNGR_AGENT_ID).
# The desktop app pops the modal in the window showing that workspace, and it
# identifies the window by the primary (is_primary) agent id. If you are an
# /assist chat (a sub-agent spawned in this workspace), $MNGR_AGENT_ID is your
# own id, not the workspace's -- reporting under it would pop the modal in
# whatever window is focused instead of this one. Resolve the primary id from
# the local agent list (only this workspace's agents are visible from here, so
# exactly one agent carries is_primary); fall back to your own id if the lookup
# comes up empty.
WORKSPACE_AGENT_ID="$(mngr ls --include 'has(labels.is_primary)' --ids)"
WORKSPACE_AGENT_ID="${WORKSPACE_AGENT_ID:-$MNGR_AGENT_ID}"

latchkey curl -sS -X POST \
  "http://latchkey-self.invalid/minds-api-proxy/api/v1/agents/$WORKSPACE_AGENT_ID/report" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg d "$DESCRIPTION" '{description: $d}')"
```

A successful call returns `{"ok": true}` and pops the pre-filled modal in the app. Tell the user a report has been opened for their review, and name the issues it covers.
