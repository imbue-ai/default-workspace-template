# Sending to the pane we meant, and saying so when we cannot

Folds `conversation-contexts/tmux-lockdown` into the send path for the harnesses driven through `send-keys` (claude and antigravity), and gives the workspace enough to offer the right button when it goes wrong.

## The two failures being fixed

Both are tmux-level, below any harness.

**A pane in a mode swallows `send-keys`.** copy-mode, clock-mode, choose-tree and customize-mode all intercept keys. Measured: into a pane in copy-mode, `send-keys` delivers nothing while `paste-buffer` delivers normally. That asymmetry is what makes it nasty rather than merely broken -- a long message pasted via `load-buffer`/`paste-buffer` lands in the input box, and the `Enter` sent afterwards by `send-keys` is eaten, so the text sits there unsent. With `mouse on`, one wheel-up enters copy-mode with no obvious indicator.

**`session:window` resolves to the window's ACTIVE pane.** One split and that is no longer the agent: the message goes to whatever pane is active now, silently, with no error. `session:window.0` is no better -- panes renumber when one closes. Only the pane ID (`%0`) is stable for the pane's life, and it fails loudly rather than misrouting.

## The fix

Capture the agent pane's ID once at session creation into a tmux session user-option, and send to that:

```bash
# at session creation
PANE=$(tmux display -p -t '=s:agent' '#{pane_id}')
tmux set-option -t '=s:' @mngr_agent_pane "$PANE"

# before every send
P=$(tmux show-options -v -t '=s:' @mngr_agent_pane)
tmux copy-mode -q -t "$P"    # clears every mode; silent no-op when not in one
tmux send-keys  -t "$P" ...
```

`@`-prefixed options are tmux's user namespace, stored and never interpreted. The scope is exactly right: the value dies with the session, so there is no file to go stale and a restarted agent sets it fresh.

`copy-mode -q` clears all four mode types in one call and exits 0 silently when the pane is not in a mode, so it needs no `#{pane_in_mode}` check. `send-keys -X cancel` is *not* usable here -- it errors with `not in a mode`.

The preflight must target the same pane the send targets, or it clears one pane and types into another.

**Applies to claude and antigravity, and needs no opt-in to do so.** They are the only agents that inherit the send-keys pipeline: codex subclasses `BaseAgent` directly and drives its app-server ("This class subclasses ``BaseAgent`` directly (not ``SendKeysAgent`` / ``InteractiveTuiAgent``)", `mngr_codex/plugin.py`), and pi likewise "uses none of the latter's paste/Enter pipeline" (`mngr_pi_coding/plugin.py`). So putting the fix in the shared send path lands it on exactly the harnesses that send keys, and on nothing else, with no flag to set or forget. A harness that later adopts the pipeline gets it for free, which is the right default -- the bug is in the pipeline, not in a harness.

**Rollout is non-breaking.** Reading the option on a session created before this change fails with `invalid option: @mngr_agent_pane`; on that signal, fall back to today's `session:window` target. Existing agents keep working and pick the new behaviour up when they restart.

**Gotcha:** `=s` alone is not a valid session target for `set-option` (`no such session: =s`). Use `=s:`. `TmuxSessionTarget.as_shell_arg()` emits `=name`, so it needs the trailing colon or a `TmuxWindowTarget`.

## The part that reaches the user

A stored pane that no longer exists fails with `can't find pane: %0`, exit 1. That is not a dialog, not a busy agent, and not something the user can fix from the chat: **the pane is gone, so the only thing that helps is restarting the agent.**

That matters for the workspace's failure notice, which today offers Cancel, Retry and Force for every send failure. Retry against a dead pane is guaranteed to fail again -- it is a button that cannot work.

### Where the decision belongs

**mngr reports what happened; the workspace decides what to offer.** mngr is the terminal interface: it knows the pane is gone, and that is a fact about the agent. Which buttons a user gets is a chat-UI question -- Force, Cancel, and "returned to the composer" are workspace concepts that mean nothing to `mngr message` on a terminal.

So mngr should not name buttons. It should name **what kind of failure this is**, precisely enough that the workspace can decide without parsing prose.

### Retry and Force must mean what the UI already means

The two buttons the workspace offers are not new behaviours invented for this notice, and they must not drift into being:

- **Retry is the send button.** The same `sendMessage(agentId, text)` the composer calls, hitting the same endpoint and re-running preflight. Not a special path, no bypass -- which is why a Retry can fail again with a different reason, and should.
- **Force is a restart and then that same send.** Its restart is deliberately not what the Stop button does: Stop calls `drainToComposer`, which dispatches to the harness's own interrupt-to-composer, and for claude that can be a native chord that never restarts the process. Force is for a wedged agent, so it needs the guaranteed restart (`mngr start --restart --no-resume`). It takes Stop's queue rescue first -- a best-effort drain, since the agent being forced is often the stuck one -- and then restarts regardless.

That ordering is the queue fix: the restart SIGKILLs the agent and takes anything queued inside the harness with it, so a feature whose rule is "never lose the message" must not have a button that loses other people's.

### What that looks like

Today the reason crosses as a free string, which the notice prints. Add a machine-readable kind alongside it -- an enum on the error, carried through the send endpoint's response next to `detail`:

| kind | means | workspace offers |
| --- | --- | --- |
| `agent_unreachable` | the pane is gone; nothing in the terminal to talk to | Cancel, **Force** -- Retry withheld, it cannot work |
| `input_blocked` | a dialog or shell mode holds the input | Cancel, Retry, Force |
| `not_ready` | the harness is still coming up | Cancel, Retry |
| `unknown` | anything not classified | Cancel, Retry, Force (today's behaviour) |

The mapping table lives in the workspace, not in mngr. `unknown` is the default so an unclassified failure keeps working exactly as it does now, and a new kind is additive rather than breaking.

**The obstacle, stated plainly.** There is no channel for a kind today. `MessageResult.failed_agents` is `list[tuple[str, str]]` -- an agent name and an error *string* -- and that is the only way a failure leaves mngr's send, so a kind would be flattened into prose on the way out. Three ways through it:

1. Widen the tuple to carry the kind. Honest, but `MessageResult` is public and `mngr message`'s exit code reads it.
2. Add a record per failure (`MessageResult.failures`, carrying agent/reason/kind), keeping `failed_agents` as a derived view so `mngr message`'s exit code and output are unchanged. **Built.**
3. Have the workspace pattern-match the prose. No mngr change, and exactly the fragility removed when the reason stopped being flattened to a bool. Rejected.

This is the one part of the plan that touches an mngr public type, so it is worth deciding before building rather than during.

The frontend already models this: `NoticeDialog` takes a list of actions, and the notice builds that list per failure. Choosing the list from a kind is a small step from choosing it from "is this repeatable".

## Implementation order

1. **tmux lockdown, mngr side.** Capture the pane ID at session creation; read it before each send with the `invalid option` fallback; `copy-mode -q` before every `send-keys`. This alone stops silent misrouting and swallowed Enters, and is independently shippable.
2. **A kind on the failure.** `agent_unreachable` when the pane is missing, `input_blocked` from the dialog registry's refusals, `not_ready` from the readiness path. Carried to the workspace beside the detail.
3. **The workspace maps kind to buttons.** Default `unknown`, so nothing regresses while kinds are filled in.

## Testing

- tmux, measured: `send-keys` into a pane in each of the four modes is swallowed before the preflight and delivered after; `copy-mode -q` exits 0 silently on a pane not in a mode.
- A split makes `session:window` misroute while the pane ID still delivers.
- The option is absent on an old session and the send falls back rather than failing.
- A killed pane produces `agent_unreachable`, and the notice offers Cancel and Force but not Retry.
- Not yet verified anywhere: all of the above was measured against `cat` as the pane process, not a live claude or antigravity TUI. The mechanism sits below the harness so it should hold, but it has not been exercised end to end.
