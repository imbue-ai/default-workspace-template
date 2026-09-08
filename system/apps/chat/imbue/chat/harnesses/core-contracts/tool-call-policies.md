# Tool-call policies (CANONICAL)

The guards every harness must enforce on the commands its agent runs. This is the TIMELESS
half: **what** must hold and **why**. How each harness wires it, and what is currently
unwired, live in `tool-call-policies-state-of-things.md` beside it.

A policy is a checkpoint around a tool call where we can refuse a command, rewrite it, or tell
the agent something. Claude is the reference implementation; the logic lives once, in the
`agent_*` scripts under `system/scripts/`, and every harness reaches those same files.

## What is in scope here, and what is not

In scope: **anything that inspects or alters the command an agent is about to run**, and
**anything that holds the agent to the `tk` step discipline the chat progress view is built
from**. Those are the workspace's own rules, they are enforced identically everywhere, and
they are ours to change without a mngr release.

Out of scope, deliberately -- these look adjacent and are not:

- **Harness tool-approval permissions.** Whether a harness prompts before running a command,
  and how that prompt is suppressed, is mngr's: it provisions the permission config and the
  skip flag per harness. Nothing here.
- **Turn and activity signalling.** Whatever a harness fires when a prompt is submitted or a
  turn ends belongs to the activity indicator and the message lifecycle, not to command
  policy. A policy may *use* such an event as a delivery channel, but the policy is never
  "run something on that event".
- **Session provisioning.** Dependency syncs, PATH setup, plugin updates: each harness's own
  concern, and mngr already does it per harness.

If a rule cannot be stated without naming a harness's event, it is not a policy -- it is
wiring, and it belongs in the state-of-things file.

## The policies

Each is stated as an invariant, with the reason it exists. The scripts named are the single
source of the logic; a harness that cannot run a script must reproduce its behaviour *and its
exact wording*, so an agent gets an identical explanation everywhere.

### P1. A command must not pipe into `tail` or `head`
`agent_block_pipe_tail_head.sh` -- **hard block.**

The pipe truncates output the agent then reasons about as if it were complete. Redirect to a
file and read that instead, so the full output exists and can be re-read.

### P2. A command must not rewrite git history
`agent_prevent_commit_rewrite.sh` -- **hard block.**

Blocks `git rebase`, `git commit --amend|--fixup`, `git pull --rebase`. History the user may
already have pulled must not move under them; a new commit is always available instead.

### P3. A permission request must be the only thing in its tool call
`agent_latchkey_request_standalone.sh` -> `agent_latchkey_request_check.py` -- **hard block.**

This is about the **chat's permission card**, not about harness tool approval. When an agent
asks the user to approve something, it POSTs to the reserved
`latchkey-self.invalid/permission-requests` host, and the chat builds a card from the single
request object echoed back in that call's result. So the call must stand alone: a second
request in the same call is never shown, and a redirect (`> /tmp/req.json`, `| jq .request_id`)
takes the echoed object away and leaves a card with no button.

The redirect half is blunt on purpose -- an input redirect is blocked alongside the output
ones, because a heredoc body re-enters the parse as further commands. Backgrounding is the
same failure by another route: the echo lands in a later call rather than in the card's own
result.

Tokenising lives in the `.py` (it uses `shlex`, so a rationale mentioning `&&` or `>` inside a
quoted argument stays inside it).

### P4. Every command carries the agent's OOM band and git identity
`agent_rewrite_bash_command.py` -- **rewrite**, never a block.

Two prefixes, then the original command verbatim:
- an **OOM self-tag**, so the agent's subprocesses (builds, tests, browsers) are shed first
  under memory pressure rather than the agent itself;
- the agent's **git identity** as `GIT_AUTHOR_*`/`GIT_COMMITTER_*`, so commits are attributed
  to the agent rather than to whatever `user.name` the checkout inherited at create time. The
  name is read live, so a rename is reflected without a restart.

Each prefix ends with `;` rather than `&&`, so the command runs whether or not the prefix
applied. This must remain the ONLY policy that alters the command.

### P5. Substantive work happens under an in-progress step
`agent_require_steps_pretool.sh` -- **soft reminder**, never a block.

The chat progress view is built entirely from `tk` step records. Work done with no step open
is invisible to the user. Read-only tools are exempt, as are `tk` invocations themselves.

Soft on purpose: a block here would fight the agent over a judgement call (whether a given
action is substantive), and the cost of being wrong is a missing timeline node, not a broken
repo.

### P6. A `tk start` or `tk close` must be the only thing in its tool call
`agent_tk_standalone.sh` -> `agent_tk_standalone_check.py` -- **hard block.**

Chaining (`cd x && tk start s1`) or redirecting a step transition drops it out of the progress
view, so the timeline silently disagrees with what happened. `tk create` is exempt -- it may
be batched, because creating the plan up front is the intended usage.

Tokenising again lives in the `.py`, single-sourced with P3.

### P7. Steps left open are reconciled, not abandoned
`agent_open_tickets_reminder.sh`, `agent_open_tickets_stop_nudge.sh` -- **soft reminder.**

An agent that stops with steps still open leaves the user a timeline that says work is in
flight when it is not. The agent must be reminded of those steps and decide, explicitly, to
continue, replace or close them.

**Stated as a policy, not a channel.** *When* the reminder arrives is a harness's choice --
before the next turn, at stop, or riding a tool result -- because harnesses differ in which of
those can reach the model at all. The invariant is only that an agent with open steps is told
about them before it does more work.

## The rule that keeps this honest

**The logic lives once.** A harness that can execute the scripts runs the scripts. A harness
that cannot must reproduce the behaviour *and copy the wording verbatim*, so no agent gets a
different explanation of the same rule.

When a policy changes, it changes in `system/scripts/` -- and every harness that reproduces
rather than executes needs the matching edit in the same commit. The state-of-things file
lists which those are.
