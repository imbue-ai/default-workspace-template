# Tool-call policy parity: audit and fix plan

Three independent deep audits (codex, pi, agy), each verified against running code rather than
against the docs. Claude is the reference. Every claim below was reproduced; where a finding is
measured rather than reasoned, the measurement is shown.

The headline: **P1–P7 are broadly live everywhere, but two harnesses have a total bypass, one
harness hard-blocks legitimate work today, and three claims the docs make about the mechanism
are simply false.**

## Severity-ranked findings

| # | Harness | Finding | Status |
|---|---|---|---|
| 1 | codex | **P1 hard-blocks file edits.** `apply_patch` carries the patch body in `.tool_input.command`, and 2 of 4 guards have no tool-name gate | reproduced |
| 2 | agy | **All four guards defeated by adding a file.** The shim dir is first on PATH and the shim calls `jq` bare; fail-open does the rest | reproduced |
| 3 | codex | **`write_stdin` fires no hook at all.** Type into a shell started by a guarded call and P1/P2/P3/P6 are gone | reproduced live |
| 4 | codex | **P3/P6 unenforceable**, and the display layer silently drops work because of it | reproduced live |
| 5 | agy | **P5 not wired at all** — the doc calls it `n/a` on reasoning that is half wrong | verified |
| 6 | agy | **P7a not wired**, though a live channel to the model exists and the same doc documents it | verified |
| 7 | pi | **Block wording drifts** from the shared scripts, which the contract forbids in as many words | reproduced |
| 8 | all | **P7b is decorative on every harness, claude included** | verified |
| 9 | all | **"P4 runs last so blockers see the original" is a myth** on claude and codex | verified |

## 1. codex: P1 hard-blocks file edits — FIX FIRST

Two of the four guards read `.tool_input.command` with no tool-name gate:

```
agent_block_pipe_tail_head.sh      tool_name refs = 0
agent_prevent_commit_rewrite.sh    tool_name refs = 0
agent_latchkey_request_standalone.sh   tool_name refs = 2
agent_tk_standalone.sh                 tool_name refs = 2
```

Under codex's code mode, PreToolUse fires **per inner delegated call**, `exec_command` is renamed
to `Bash`, and **`apply_patch` also carries `.tool_input.command`** — holding the patch body.
Reproduced with the real script:

```
$ printf '{"tool_name":"apply_patch","tool_input":{"command":"*** Begin Patch\n*** Update File:
   README.md\n+Run: cat foo | head -5\n*** End Patch"}}' | bash agent_block_pipe_tail_head.sh
exit=2
Do not pipe commands through tail or head. Instead, redirect output to a temp file...
```

So a codex agent editing any file whose content contains `| head` or `| tail` — a shell script, a
README, **the files in this repo** — is hard-blocked, and the entire JS program aborts. This
cannot happen on claude, whose `Edit`/`Write` carry `file_path`/`old_string`/`new_string` and
never `command`. P2 is inert only by luck: its `^git[[:space:]]+` anchor does not match a patch
body.

**Fix.** Add the gate its two siblings already have, right after `input=$(cat)`:

```bash
tool_name=$(echo "$input" | jq -r '.tool_name // empty')
[[ "$tool_name" == "Bash" ]] || exit 0
```

Safe for all four harnesses: claude's shell tool is `Bash`, codex renames `exec_command` to
`Bash`, agy's shim synthesises `{"tool_name":"Bash",...}`, and pi never runs these two scripts
(they are re-expressed in `mngr_pi_lifecycle.ts`). Apply to both ungated scripts for symmetry.

## 2. agy: the shim's own directory is a bypass — FIX SECOND

`plugin.py:1491` puts the shim directory **first** on PATH; `agy_shim/bash:64` calls `jq` bare;
and any failure to build the payload falls open by design. Reproduced:

```
baseline                                     exit=2   (git rebase blocked)
after dropping a 2-line `jq` in the shim dir exit=0   (ALL FOUR GUARDS DEFEATED)
log: "payload build failed (jq missing or refused input); running unguarded"
```

The shim's own header concedes the agent could `rm` it — that is the accepted posture. This is
different in kind: the shim dir is a **repo path the agent edits normally**, so defeating every
guard requires adding a file rather than deleting the seatbelt.

**Fix.** Resolve helpers from a fixed PATH, not the agent's:

```bash
PAYLOAD=$(printf '%s' "$COMMAND" | PATH=/usr/bin:/bin jq -Rs '...' 2>/dev/null)
PREFIX=$(PATH=/usr/bin:/bin python3 "$SCRIPTS_DIR/agent_rewrite_bash_command.py" --prefix-only ...)
```

and invoke the guards with the same sanitised PATH (they call `jq` bare too).

**Related, cheap:** the fail-open path for a missing/incorrect `MNGR_AGENT_WORK_DIR` writes **no
log line** — the broadest failure is the only silent one. The `note` helper is defined above the
check; adding one is free.

## 3. codex: `write_stdin` is a total bypass

Measured: one JS program does `exec_command({cmd:'bash', tty:true})` then
`write_stdin({session_id, chars:'...\n'})`. The hook log contains **one** entry — `Bash |
{"command":"bash"}`. `write_stdin` fires **no PreToolUse event at all**, unlike `apply_patch` and
`update_plan`, which do. So:

```js
const d = await tools.exec_command({cmd:'bash', tty:true});
await tools.write_stdin({session_id: d.session_id, chars:'git rebase -i HEAD~3\n'});
```

defeats P1, P2, P3 and P6 outright. P4 survives, because its `export` prefix is inherited by the
child shell. Not fixable in a guard — codex emits nothing to hook.

**The obvious fix does not work.** `write_stdin = { enabled = false }` in the `tools` table is
silently ignored: `ToolsToml` accepts exactly three keys — `web_search`,
`experimental_request_user_input`, `update_plan`. Verified live: with that key set, codex still
reported *"Command sent successfully via `write_stdin`"* and the marker was still written.

**The fix that does work is `[features] unified_exec = false`.** `write_stdin` and the tty option
are part of the unified-exec surface, not the tools table. Verified against production's exact
feature set (`code_mode_only`, `code_mode_host`, `goals = false`, `memories = false`, the two
disabled tools) plus `unified_exec = false`:

```
bypass marker: (NOT written - bypass closed)
hook fired:    Bash 'echo still-works'      <- ordinary shell still works AND still hooks
```

and codex itself reports *"`write_stdin` does not exist in the available exec tools, and the
shell tool has no TTY option."* Note the older spelling `experimental_use_unified_exec_tool` is
deprecated in 0.147.0 — it warns and tells you to use `[features].unified_exec`.

Add it to `agent_types.codex.config_overrides` in `.mngr/settings.toml`, plus a sentence in
`append_system_prompt__extend` as the seatbelt. The cost is the interactive-tty / type-into-a-
running-shell affordance, which nothing in Minds uses today.

## 4. codex: P3 and P6 are unenforceable, and the display layer pays for it

Both policies say the guarded thing must be *"the only thing in its **tool call**"*. On codex a
tool call is a JS program that may hold N `exec_command` calls, each of which passes the
standalone check individually. Measured: one `custom_tool_call` containing three
`tools.exec_command` calls produced **three PreToolUse events with three unrelated
`tool_use_id`s and no field naming the outer call**. No stateful guard can count per call.

The harm is not theoretical — it lands on the renderer, which reads only the **first** call in the
program (`codex/tool_labels.py:275-293`). So:

```js
tools.exec_command({cmd:'tk start s1'}); tools.exec_command({cmd:'sed -i ...'})
```

classifies as pure-tk → `DisplayKind.HIDDEN` → **the `sed` work silently disappears from the
chat.** That is exactly the failure class P6 exists to prevent. Same shape for P3: the permission
card is built from the first request object, so a second request in the same program is never
shown — literally the `_MULTIPLE` case P3 blocks.

**Fix the harm, not the unenforceable guard.** In `codex/session_parser.py::_labelled_tool_call`,
refuse `HIDDEN`/`PERMISSION_REQUEST` when the program contains more than one `tools.<fn>(`
match. A batched call then renders as ordinary work: nothing vanishes and no buttonless card is
built.

## 5. agy P5: MISSING, not `n/a`

The doc justifies `n/a` with: *"The check skips on claude TOOL NAMES, and agy reaches the guards
through a shell shim where every call is `Bash`... wrong in both directions."*

The second direction is true and unfixable — `write_to_file` and `replace_file_content` never
touch the shim, and no agy hook carries tool identity. The first is false:
`agent_require_steps_pretool.sh:47-54` **already has a command-shaped branch**. The false-positive
risk is also *lower* on agy than claude, because agy has `view_file`/`list_dir`/`grep_search`/
`find_by_name` as first-class tools, so its `run_command` skews substantive — claude nudges on a
plain `cat foo` today.

Of agy's 17 tools, ~10 would be nudge-worthy under claude's own rules; exactly one reaches the
shim.

**Fix (two small edits).** Widen the Bash skip in `agent_require_steps_pretool.sh` from `tk` to a
read-only allowlist (`cat|ls|grep|rg|find|wc|head|tail|git status|git diff|git log`), which
improves claude and codex in the same edit; then invoke the script from the shim, reading
**stdout** (the guard loop discards stdout) and re-emitting `additionalContext` on stderr.

**Ceiling, stated honestly:** file edits still never nudge. Correct verdict is **PARTIAL after the
fix, MISSING today** — never `n/a`.

## 6. agy P7a: MISSING, and the doc contradicts itself

Line 27 of the state-of-things: *"agy has no prompt-submit event, and its stop-time stderr goes to
a tmux pane nobody reads."* Line 41 of the **same file**, in its own channel table:

> `| agy | exit 2 + stderr (becomes the tool result) | prepend before exec | stderr on the same result |`

That third column *is* the "tell the agent something" capability. Both clauses on line 27 are true
and neither is the relevant channel. The canonical doc is explicit that P7 is *"stated as a
policy, not a channel — when the reminder arrives is a harness's choice... or riding a tool
result."* Riding a tool result is precisely what agy can do, and the shim already proves the
channel works (a block reason was observed verbatim in agy's own transcript).

Worse: `AGENTS.md:53` tells agy *"At the start of each new user message you'll get a system
reminder listing your still-open steps"* — a reminder no code path can deliver.

**Fix (~6 lines in the shim).** Once per turn, fire `agent_open_tickets_reminder.sh` and put its
output on stderr. A turn key exists without touching mngr: `statusline.sh` removes the `active`
marker on the idle edge and re-`touch`es it on busy, so its **inode changes once per turn** (mtime
does not — it is touched every sample). Stamp the inode and fire on change.

## 7. pi: block wording drifts from the shared scripts

The contract requires a harness that cannot run the scripts to *"reproduce the behaviour **and
copy the wording verbatim**"*. pi re-expresses P1/P2 in TypeScript and all four messages differ:

| shared script | `mngr_pi_lifecycle.ts` |
|---|---|
| "...**Instead, redirect output to a temp file (e.g. cmd > /tmp/output.txt) and then read from that file separately using the Read tool or a separate tail/head command on the file.**" | "...**Redirect to a temp file (e.g. cmd > /tmp/out.txt) and read that instead.**" |
| "**Blocked: git rebase commands are not allowed**" | "**git rebase is not allowed.**" |
| "**Blocked:** git commit with --amend or --fixup is not allowed" | "git commit with --amend or --fixup is not allowed**.**" |
| "**Blocked:** git pull --rebase **commands are** not allowed (...)" | "git pull --rebase is not allowed (...)**.**" |

*Behaviour* is at parity: 34/36 adversarial commands agree, and the two that differ (`git pullx
--rebase`, `git commitx --amend`) are cases where **pi's `\b` is more correct than claude's prefix
match**. Neither is a real command; do not "fix" pi to match.

**Fix:** replace the four literals with the verbatim text. Vendored, so it needs a mngr PR plus a
re-vendor.

## 8. P7b is decorative on every harness

Verified three ways: `agent_open_tickets_stop_nudge.sh:5-6` says so itself (*"mainly for
orchestrator log / human visibility"*) and exits 0 unconditionally; on codex a sentinel written at
Stop appears in **no** transcript item; on pi it is `process.stderr.write` and the runner discards
handler results.

So the doc's framing of agy's P7 as `n/a` because *"its stop-time stderr goes to a tmux pane
nobody reads"* implies the other harnesses' stop nudges do something. **They do not.** The half
that works is P7a, which reaches the model via `additionalContext` (claude/codex) or a chained
`systemPrompt` (pi).

## 9. "P4 runs last so blockers see the original" is a myth

- **claude** runs a matcher's hooks **in parallel** — `agent_rewrite_bash_command.py:36-40` says
  so in its own comment.
- **codex** does not thread `updatedInput` into later hooks of the same event. Measured: a
  rewriting hook placed first, a logging hook second — the logger saw the **original**. And
  `.codex/hooks.json` already has P1/P2 swapped relative to `.claude/settings.json`, which nobody
  noticed, because it never mattered.
- **pi**'s order is deterministic and is the *unsafe* one: `mergePaths(primary, additional)` =
  `[...primary, ...additional]` with CLI `-e` as primary, so **mngr's rewrite always runs before
  the guards**. This makes `mngrOriginalCommand` load-bearing on every bash call, not a fallback.
- **agy** is the only harness where the ordering is real, because the shim runs the guards and the
  rewrite as statements in one script.

The doc describes a mechanism that exists on exactly one of four harnesses. Ordering is harmless
belt-and-braces; the thing that actually protects the guards on pi is `mngrOriginalCommand`.

## Per-harness verdicts

### codex
| Policy | Verdict | Note |
|---|---|---|
| P1 | **PARTIAL** | over-broad: hard-blocks `apply_patch` |
| P2 | PARITY | inert on patch bodies only by anchor luck |
| P3 | **PARTIAL** | per-`exec_command`, not per tool call; `--backgrounded` arm dead |
| P4 | PARITY | verified live; needs `permissionDecision:"allow"` |
| P5 | **PARTIAL** | skip-by-tool-name list is 100% dead; does see `apply_patch` |
| P6 | **PARTIAL** | same granularity gap as P3 |
| P7a | PARITY | `additionalContext`, model-visible |
| P7b | decorative | as everywhere |

### pi
| Policy | Verdict | Note |
|---|---|---|
| P1, P2 | **PARTIAL** | behaviour at parity, wording drifts |
| P3, P6 | PARITY | byte-identical, same checkers |
| P4 | PARITY | OOM segment byte-identical; quoting style differs, both valid POSIX |
| P5 | **PARITY, best of all four** | covers `bash`, `edit` **and** `write` — the only harness that sees file edits |
| P7a | PARITY | chained `systemPrompt` |
| P7b | decorative | |

### agy
| Policy | Verdict | Note |
|---|---|---|
| P1, P2, P6 | PARITY | block reasons **byte-identical** (217 B / 59 B / 83 B / 622 B, `cmp`-verified) |
| P3 | **PARTIAL** | `--backgrounded` arm dead by construction |
| P4 | PARITY | ordering genuinely enforced |
| P5 | **MISSING** | doc says `n/a`; it is not |
| P7a | **MISSING** | feasible today via the shim |
| P7b | n/a | decorative everywhere anyway |

## Bypasses, all harnesses

| Bypass | Harness | Severity | Fixable |
|---|---|---|---|
| shadow `jq` in the shim dir | agy | **total** | yes, one line |
| `write_stdin` into a live shell | codex | **total** | yes, config |
| `manage_task` `send_input` | agy | high | no — no event to hook |
| nested/plain `pi` gets no P1/P2/P4 | pi | medium | partly; P4 needs mngr's env |
| `.pi/extensions/` is **trust-gated** | pi | medium | doc it, or seed trust per subdir |
| `sh -c` / `eval` | all | low | none — claude has it too, by design |
| `bash -lc` argv form | agy | low | logs a canary nobody reads |
| `rm` the shim / the extension | agy, pi | accepted | seatbelt, not boundary |

On pi's trust gate: `.pi/extensions/` loads only `if (projectTrusted)`, and trust returns `false`
for any non-interactive run without a stored decision. A nested `pi -p` started from a
**subdirectory** keys on a different cwd, finds no entry, and gets **zero** guards. The doc claims
*"any pi that runs here — managed or not — is held to the guards."*

## Not our problem: permissions

Harness tool-approval is mngr's, via `auto_allow_permissions`: claude adds a `PermissionRequest`
hook, codex sets `approval_policy = "never"`, agy appends `--dangerously-skip-permissions`,
opencode writes a wildcard allow block, and pi **validates the flag to reject `False`** because it
has no approval gate at all. dwt correctly contains none of this — `.claude/settings.json` has no
`permissions` key.

The one apparent exception is not one: `agent_rewrite_bash_command.py` emits
`permissionDecision: "allow"` **for codex only**, because codex rejects an `updatedInput`-only
rewrite. It is withheld on claude, where it *would* auto-approve, with a test pinning the absence.

Keep P3 distinct from this. P3 guards the **chat's latchkey card**, not harness approval — so it
still applies to an agent running with `auto_allow_permissions`. Two systems, one word.

## Fix order

1. **Tool-name gate** on `agent_block_pipe_tail_head.sh` + `agent_prevent_commit_rewrite.sh` — one
   line each; stops a live hard-block on codex file edits.
2. **Sanitised PATH** in `agy_shim/bash` — one line; closes a total bypass.
3. **`[features] unified_exec = false`** in `.mngr/settings.toml` — closes codex's total bypass.
   (NOT `tools.write_stdin`, which is not a valid key and is silently ignored.)
4. **Multi-call display guard** in `codex/session_parser.py` — stops work vanishing from the chat.
5. **agy P7a** via the shim — turns a MISSING invariant live.
6. **agy P5** via the shim + widen the read-only skip list — MISSING → PARTIAL, improves claude
   and codex too.
7. **pi wording** — mngr PR + re-vendor.
8. **pi `mngrOriginalCommand`** — split the rewrite and the recording into two `try` blocks, so a
   failed recording cannot leave a rewritten command visible to the guards and block every `tk
   start` and every permission request.
9. **Docs** — the P3/P5/P7 rows, the P4-ordering myth, the pi trust gate, `write_stdin`,
   `AGENTS.md:53`, and the stale `POLICY_HOOKS.md` pointers in `mngr_pi_lifecycle.ts`.
10. **pi `session_switch`** — a handler registered on an event that pi 0.84.1 neither declares nor
    emits, so it never runs. Not policy (`recordSessionFile`), needs a semantics check rather than
    a blind rename.

Items 1–4 are the ones that change behaviour users can hit today. 5–6 close real invariants. 7–10
are hygiene.

## Genuinely impossible

- **agy P5 on file edits.** No hook carries tool identity; `write_to_file` spawns no process.
- **agy P3 `--backgrounded`.** The guard reads `.tool_input.run_in_background`; the shim's payload
  cannot carry it, and `WaitMsBeforeAsync` never reaches bash's argv. (It does **not** let a
  command escape a blocker — guards run before `exec` — it only kills this one arm.)
- **codex P3/P6 per-tool-call.** No outer-call identity in the payload.
- **codex `write_stdin` as a guard.** No event is emitted; only config can close it.
- **pi P4 in a nested pi.** It needs mngr's environment, which a nested pi does not have.


## Reproduction record

Every finding and every proposed fix was re-run first-hand, against codex-cli **0.147.0** and
pi **0.84.1** installed on this box, plus the real guard scripts and the real parsers. Nothing
below is taken on the auditors' word.

| What | How | Result |
|---|---|---|
| codex payload shape | live `codex exec`, instrumented `PreToolUse` | `exec_command` → `tool_name="Bash"`; `apply_patch` → `tool_name="apply_patch"` with the **patch body** in `.command` |
| P1 misfire | the captured `apply_patch` payload → the real guard | `exit=2` — file edit hard-blocked |
| Fix 1 (tool-name gate) | same payload, gated guard | `apply_patch` → 0, `Bash` pipe → 2, agy shim → 2, claude `Edit` → 0 |
| agy jq-shadow bypass | drop a 2-line `jq` in the shim dir | `exit=2` → `exit=0`, all four guards defeated |
| Fix 2 (sanitised PATH) | same, with `PATH=/usr/bin:/bin` | shadowed `jq` → still `exit=2`; benign runs; OOM prefix still `900` |
| codex batching | live code-mode run, 3 exec calls in one program | 1 `custom_tool_call` (`call_h9qw…`) → **3** `PreToolUse` events, 3 unrelated `exec-<uuid>`, no outer id |
| display harm | real `codex/tool_labels.py` + `tool_output.py` | batched `tk start` + `sed` → `display=hidden`; the `sed` vanishes |
| Fix 4 (multi-call gate) | proposed predicate over the same inputs | single tk → `hidden`; batched → `None`; ordinary → `None` |
| `write_stdin` bypass | live run, tty shell + `write_stdin` | marker written, **1** hook event (`Bash`/`bash`) — no hook for `write_stdin` |
| Fix 3 (first attempt) | `tools.write_stdin = { enabled = false }` | **FAILED** — key not in `ToolsToml`; bypass still worked |
| Fix 3 (corrected) | `[features] unified_exec = false` | bypass closed, ordinary shell still works *and* still hooks |
| agy block wording | `cmp` of real stderr, claude path vs shim path | identical: 217 B / 59 B / 83 B / 622 B |
| pi wording drift | shared scripts vs `mngr_pi_lifecycle.ts` | all four messages differ |
| pi P5 coverage | SDK tool list vs `READONLY_TOOLS` | nudges `bash`, `edit`, `write` — the only harness that sees file edits |
| pi dead handler | `types.d.ts` + runtime grep | `session_switch` neither declared nor emitted; the other 8 are live |
| pi ordering | `mergePaths(primary, additional)` + call site | CLI `-e` first ⇒ **mngr rewrites before the guards**, deterministically |
| agy P7a turn key | `statusline.sh:143/146`, inode test | `rm -f` + `touch` ⇒ inode changes per turn; reminder script runs standalone and emits the text |

**Re-testing codex: you must reproduce mngr's trust setup, or you will measure nothing.** codex
refuses to run command hooks unless they are trusted, so a naive probe sees *zero* hook events and
looks exactly like "the policy is not wired" — I hit this twice before working it out. **This is a
probe artifact, not a gap:** production is covered three ways, all verified present —
`--dangerously-bypass-hook-trust --enable hooks` on the launch command (pinned by
`mngr_codex/plugin_test.py:829`), `merge_project_trust` seeding `[projects."<path>"] trust_level =
"trusted"`, and `auto_dismiss_dialogs = true` so mngr answers the TUI's trust prompt on startup.
To reproduce by hand, pass the flag and seed the project entry.
