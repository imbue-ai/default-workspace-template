# Glossary of this workspace's terms

The words this workspace's skills use for its own things. One term per thing,
in every file an agent reads. The skill authoring guide's glossary
(`docs/system/skill_authoring_guide.md`) covers the authoring terms (skill,
reference, lead, worker); words shown to the user follow
`.agents/shared/references/user-facing-language.md`.

| Term | Meaning | Not |
|---|---|---|
| creation | Something built for the user here: an app, a skill, data, or a customization of one. | |
| app | A creation the user opens as a window: a supervisord program with an `app.toml`, under `system/apps/`. | site, view |
| service | A supervisord program with no window, under `system/services/`. | daemon, background app |
| program | A supervisord `[program:...]` entry, when the entry itself is meant; an app or a service otherwise. | |
| built-in skill | A skill that ships with the template. | system skill |
| crystallized skill | A skill the harden pass made from work done in a chat. | |
| automation | A skill that runs on a schedule. | cron job |
| routine | Anything that runs on a schedule and exits, made with `manage-routines`. | |
| template | A published, bootable snapshot of creations that another workspace can adopt. | inspiration |
| harden pass | One background run of the harden worker over a change. Shortened to "pass". | run, flow |
| operation | What a harden pass does: `crystallize`, `update` or `heal`. | |
| gate | A point where a worker stops for its lead's answer. | checkpoint |
| go-live | Making a hardened change the one that is running. | deploy |
| update | Moving the workspace to a newer release of the template, with `update-self`. | sync |
| release | A tagged version of the template. | |
| critical app | The shell, the chat, the terminal, or any app whose `app.toml` sets `critical = true`. | |
| shell | The top-level workspace UI, `system/apps/system_interface/`. | |
