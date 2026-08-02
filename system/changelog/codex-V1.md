Added OpenAI Codex CLI support to the workspace image.

- Bake codex 0.145.0 into the image (CODEX_VERSION).
- Ship a repo-committed codex private-instructions channel at .codex/AGENTS.md,
  provisioned as codex's global instructions without polluting the shared
  AGENTS.md every harness reads.
- Pull in the codex agent-type dependencies (imbue-mngr-codex).


- Restored the faithful AGENTS.md/CLAUDE.md split from the original `add-codex` design: CLAUDE.md is again the pure Claude delta (`@AGENTS.md` include + the TodoWrite/tk claudism, the `.claude/skills` symlink note, and the Memory section, refreshed to the `data/memories/` layout and the restic host-backup survival story). The codex-V1 redo had kept the `@AGENTS.md` header but left the entire generic body inlined below it, so Claude read the shared content twice. Verified line-by-line that everything removed from CLAUDE.md exists in AGENTS.md (verbatim or as the original deliberate genericizations).

- Create templates split into two orthogonal kinds, stacked at create time as
  `mngr create <name> -t <harness> -t <role>`. A **harness** template (`claude`, `codex`)
  sets only `type`; a **role** template (`chat`, `caretaker`, `automation`, `worker`) says
  what the agent is for and never sets `type`, so the same role runs under any harness.
  Adding a harness now costs one template instead of one per role -- previously `chat` and
  `chat_codex` were near-duplicates and the codex one had no output style at all.

- Roles express their system prompt harness-neutrally. `output_style` names the `name:`
  frontmatter of a file in `.agents/output-styles/` (moved there from
  `.claude/output-styles/`, which is now a symlink to it, mirroring `.claude/skills`);
  `append_system_prompt` replaces the hand-written `agent_args` flag pairs. Claude applies
  the style as its native `outputStyle` setting; codex, having no output-style concept, gets
  the same file's body as developer instructions. Codex chat agents therefore pick up the
  Engineering Subordinate style they previously lacked.

- `uv sync --all-packages` now runs for **every** agent, from `[commands.create]`, rather
  than being repeated in individual templates. Agents sharing the workspace work_dir were
  hitting missing-package failures the per-template converge did not cover.

- Removed the worktree agent mode: the "New agent" menu entry, the
  `POST /api/agents/create-worktree` endpoint, `CreateWorktreeRequest`, and the `worktree`
  create template. The `+` menu now offers "New chat" (claude) and "New Codex Agent" (codex),
  which are the same `chat` role on different harnesses. This does not affect
  `/home/user/worktrees/` -- worker sub-agents still run in their own git worktrees.

- Removed the `subskill-worker` template. Its only difference from `worker` was installing
  the generic `harden-worker` sub-skill, which `worker` now does for every worker; the
  crystallize / heal / update creation flows and update-system-interface use
  `--template worker`.

- Removed the `chat` and `worker` agent types. Both existed only to hang role config off
  `parent_type = "claude"`; that config now lives in the role templates, so every claude
  role resolves to the `claude` type itself.

- Codex tool-call labels rebuilt against the tool surface of a live agent on codex-cli
  0.146.0, whose signatures are now recorded verbatim in the module docstring.
  `apply_patch` is gated on the function name rather than on the patch header appearing
  anywhere in the program -- a shell command that merely mentioned `*** Add File:` used to
  render as `Tool: Edit` -- and its labels now name the operation, so a create reads
  "Creating hello.txt" and a delete "Deleting gone.txt" instead of both reading "Editing".
  Argument keys (`cmd`, `q`, `path`, `prompt`, `uri`, `server`) come from the real
  signatures, and the patch-header pattern stops at a newline so it works whether the body
  arrives raw or JSON-escaped.

- A `tools.<fn>` that is parsed but has no label now renders `Tool: <fn>` rather than
  collapsing to `Tool: Code`, which is reserved for a program with no parseable call at
  all. This is what makes a prompt-banned tool (`update_plan`, the goal trio) visible the
  moment it leaks, and a stale label table self-reporting.

- Labels added for `image_gen__imagegen`, `list_mcp_resources`,
  `list_mcp_resource_templates`, and `read_mcp_resource`; the hosted `web_search` path was
  removed, since it cannot occur under `code_mode_host`.

- Codex's turn-boundary markers now cross the wire as a declared `special` event type
  carrying a `kind`, instead of three bare top-level types the frontend had never been
  told about and silently dropped. `SpecialEventKind` (backend) and its TypeScript mirror
  are the one list of legal kinds, so an undeclared marker is a type error rather than an
  event that vanishes. Nothing renders them; they exist so `/events` reflects the true
  transcript and the activity latch has an authoritative signal.

- Session watching now goes through a harness registry instead of an inline branch. A
  harness is resolved once, at discovery, from mngr's agent type; `harnesses/registry.py`
  turns that into a watcher and an activity tracker, and `app_context` no longer names a
  harness at all. `AgentSessionWatcher` became a real interface both watchers implement
  (the claude one is now `ClaudeSessionWatcher`), replacing the type union, and its
  `build()` takes the whole agent record so a caller need not know that claude wants a
  config dir and codex does not. Adding a harness is one registry entry.

- Harness code now lives in one package per harness. `imbue/system_interface/harnesses/`
  holds the generic pieces (the watcher and activity-tracker interfaces, the registry, the
  shared label helpers, the event contract) with `harnesses/claude/` and `harnesses/codex/`
  beside them, each carrying the same file names -- `watcher`, `session_parser`,
  `activity`, `activity_state`, `tool_labels`. Claude additionally holds auth, fast mode,
  and model settings, because those exist only for claude; an empty slot in the codex
  package is the honest statement of that, rather than a branch in shared code. The
  activity ABC and its two implementations are now three files instead of one.

- The harness is a `HarnessType` enum rather than a bare string. mngr's `AgentDetails.type`
  is an open string that names non-harness agent types too (`main`, `wait`), so
  `harnesses/harness_type.py` holds the closed set plus the one `parse_harness` boundary
  that narrows it. Every field, registry key and parameter downstream is the enum, so
  `get_harness_spec` is total and its silent string fallback is gone -- an unknown harness
  is now resolved once, at discovery, instead of being re-checked at each lookup.
- `creation_type` no longer names a harness. It was `claude -> "chat"`, `codex -> "codex"`,
  which mixed a role with a harness; both `+` menu entries create the `chat` role and the
  harness travels beside it, so `creation_type` is always `"chat"` and the frontend union
  collapses to match. `HARNESS_CREATION_TYPES` is deleted, as is `HARNESS_AGENT_TYPES`
  (an identity map from each harness to itself).
- `AgentStateItem.activity_state` is typed `ActivityState | None` rather than `str | None`.
  The enum already existed; only the field and the two `.value` unwraps at its assignment
  sites were bare.
- The send endpoint's cold-start handling is tighter: one tri-state result instead of a
  separate still-starting flag, the timeout rationale left where the timeout is
  (`CodexAgent._TUI_READY_TIMEOUT_SECONDS`) rather than restated, and the delivery pool
  bounded at 8 workers so a burst of cold starts queues instead of spawning per send.
  No behavior change.
- The send endpoint is a plain synchronous send again. The worker pool, the 20s sync
  budget and the accepted-for-delivery 202 are gone. Known regression, taken
  deliberately: messaging a STOPPED agent blocks until the harness confirms, and a cold
  codex resume replays its whole rollout, so that request can outlast the ingress
  proxy's timeout and surface as "failed to send: null". The durable fix is a
  pending-message queue owned by the app rather than by each harness's TUI process --
  codex and antigravity both keep theirs in memory where nothing can see it or recover
  it, while pi and opencode expose durable ones. Tracked as follow-up work.
- Feature flags are set at process start and nowhere else. `enable_workspace_feature_flag.sh`
  is deleted: 293 lines whose every step -- rewriting supervisord's `environment=` line,
  reread/update/restart, waiting for the port, verifying the value reached the process,
  broadcasting a browser reload -- existed only to change a value the process reads once
  at exec. Set `FEATURE_FLAG_ENABLE_CODEX` at workspace creation instead; absent at
  startup means off for that container's life.
- The index shell's `no-store` header is dropped from this branch. It is unrelated to
  codex, and gabriel's default-workspace-template#349 lands the same header on the same
  function with tests and the refresh motion that actually needs it.
