A latchkey permission request now has to be filed one per tool call, and the agent is told so when it tries otherwise.

New PreToolUse guard `system/scripts/claude_latchkey_request_standalone.sh` (with the tokenizing in `claude_latchkey_request_check.py`) hard-blocks a Bash call that POSTs to the reserved `latchkey-self.invalid/permission-requests` host when it batches a second request, chains or pipes another command onto it, redirects its output, or runs in the background. The block message explains why and shows the single-request form to re-run, and says outright that the next request can follow immediately in its own call -- the rule is one request per call, not one request at a time.

This closes two ways a request could silently fail to reach the user: the chat renders one permission card per tool call and reads only the first request object echoed in the result, so a batched second request was never shown (it sat unanswered in the minds inbox), and a `> /tmp/req.json` or `| jq .request_id` took the echoed object away, leaving a card with no button to open the approval dialog.

"Redirects its output" covers curl's own write-the-body-to-a-file flags, in every spelling that names the flag as a whole word: `-o out.json`, `-oout.json`, `--output`, `--output=`, `-O`, `--remote-name`, and bundled short-flag clusters like `-so out.json` or `-fsSLo out.json`. It also covers redirecting the command's *input* -- a heredoc or `-d @- < body.json` -- so write the body inline with `-d '{...}'`, which is the form every skill documents.

"Runs in the background" is the one violation that is not in the command text: a tool call made with `run_in_background: true` returns a shell id, so the echoed object lands in a later `BashOutput` call rather than in the card's own result -- the same dead card a trailing `&` produces. The hook reads that flag out of its payload and blocks it too, so filing a request is always a plain foreground call.

Reading the queue and every other `latchkey curl` are untouched and may still be piped or chained.

The guard is wired for claude in `.claude/settings.json`; codex and pi run the same checker (see `system/scripts/POLICY_HOOKS.md`, where it is now hook 3 and the later hooks are renumbered accordingly).
