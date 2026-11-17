The shared layer (`.agents/shared/`) links no file inside a skill. It may name a skill, by its name or its `SKILL.md`; what several skills need lives in the shared layer.

- **`worker-failure.md` and `dead-worker-recovery.md`** move from `launch-task/references/` to `.agents/shared/references/lead-proxy/`. Every lead follows them through `lead-proxy.md`, whichever skill it came from. Every skill and reference that named them names the new paths.

- **Four other pointers** from shared files into a skill's references are gone. `harden-contention.md` and `interactive-delivery.md` no longer point into the harden worker's references, `freeing-memory.md` names the `update-self` skill in place of its reference, and the visiting agent's primer names the `connect-external-service` skill in place of its latchkey reference.

- **`skill_checks.py`** gains `shared_links_into_skills`, which lists every such link.
