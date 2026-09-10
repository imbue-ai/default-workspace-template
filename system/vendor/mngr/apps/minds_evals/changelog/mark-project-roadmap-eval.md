Replace the single-turn `eval-config-project-roadmap.json` eval with a two-step version built on the stepped-task feature, exercising a non-technical head of product who wants a roadmap visualization from their Slack and Linear exports.

The two steps run in one continuous workspace: `build-and-harden` uploads the first export and asks for a running roadmap app, then hardens it; `updated-data` uploads a second, later export and asks to bring the roadmap up to date. Each step declares its own goal-driven prompts, `minds-app` expectations, and UI flows.

The exports ship in-repo under `configs/datasets/project-roadmap-small/` and are staged into each step's workspace via `steps[].files`, so the case no longer pins `dwt_branch` and runs against the default workspace template.
