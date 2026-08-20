A latchkey permission request now has to be filed one per tool call, and the agent is told so when it tries otherwise.

New PreToolUse guard `system/scripts/claude_latchkey_request_standalone.sh` (with the tokenizing in `claude_latchkey_request_check.py`) hard-blocks a Bash call that POSTs to the reserved `latchkey-self.invalid/permission-requests` host when it batches a second request, chains or pipes another command onto it, or redirects its output. The block message explains why and shows the single-request form to re-run.

This closes two ways a request could silently fail to reach the user: the chat renders one permission card per tool call and reads only the first request object echoed in the result, so a batched second request was never shown (it sat unanswered in the minds inbox), and a `> /tmp/req.json` or `| jq .request_id` took the echoed object away, leaving a card with no button to open the approval dialog.

Reading the queue and every other `latchkey curl` are untouched and may still be piped or chained.

The guard is wired for claude in `.claude/settings.json`; codex and pi run the same checker (see `system/scripts/POLICY_HOOKS.md`, where it is now hook 3 and the later hooks are renumbered accordingly).
