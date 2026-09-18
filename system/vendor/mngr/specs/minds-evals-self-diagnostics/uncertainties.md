# Uncertainties noticed while specifying the self-diagnostic suite

Places where the code contradicted an issue or the template's docs when `spec.md` was written, and the assumption the spec made.
The first two are settled by the suite's implementation (a per-flow `start_path`, and a seeded class beside pre-existing and delivered); the third is a standing disagreement inside default-workspace-template.

## Issue #934 assumes a ui_flow can reach the todo fixture's query-string knobs

#934 describes the diagnostic suite's flows as "driven through the fixture's knobs (`?latency=`, `?arm_delete=1`, ...)", but no eval case can express that today.
`evidence_collection.EvidenceCollector._flow_target_url` builds one bare forwarded origin for the whole case, `flow_runner.opening_action` navigates to exactly that, and `expectations._UI_FLOW_KEYS` has no field for a path or query string.
Noticed while writing `specs/minds-evals-self-diagnostics/spec.md`, which assumes the issue is right about the intent and specifies a per-flow `start_path` field as the prerequisite that makes it true.

## Issue #935's "seeded apps read as pre-existing" wrinkle depends on when the seed runs

#935 states that the pre-turn-1 registry snapshot "has to classify seeded apps as delivered (or as a third class) ... or every probe and flow ignores them", as an unconditional consequence.
It is conditional on ordering: `MindsPersonaDriver._capture_preexisting_registrations` is the last act of `_prepare_workspace_once`, and `_place_step_files` runs after it, so a seed placed on the existing uploads path is already classified delivered rather than pre-existing.
Noticed while writing `specs/minds-evals-self-diagnostics/spec.md`, which builds the seeded commit before launch, so the workspace boots with the seed registered and the pre-turn-1 snapshot sees it, and therefore does need the third class the issue asks for.

## default-workspace-template's build-app "escape hatch" shape breaks its uv workspace

The build-app skill (`.agents/skills/build-app/SKILL.md`, "Escape hatch: wrap an existing server") tells an agent to wrap a third-party server as `system/apps/<name>/app.toml` plus `icon.svg`, with no `pyproject.toml`.
The template's root `pyproject.toml` globs `system/apps/*` as uv workspace members, and uv refuses a member directory without a `pyproject.toml` (reproduced with uv 0.11.23 on a minimal workspace: `Workspace member ... is missing a pyproject.toml`), so every `uv run` service would fail after the next restart.
Read at default-workspace-template `main@96935db5`; not reproduced inside a workspace.
Noticed while writing `specs/minds-evals-self-diagnostics/spec.md`, which assumes the uv rule wins and places its escape-hatch-shaped fixture under `system/fixtures/`, outside every workspace glob.

